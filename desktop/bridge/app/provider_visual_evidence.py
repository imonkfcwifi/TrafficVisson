"""Durable, fail-closed visual evidence preparation for provider comparison.

This module owns the local boundary between raw candidate/reference images and
the verified image-attachment contract used by ``structured_vision_judgment``.
It performs three separate operations that must not be conflated:

1. minimize a current-video target crop and three to five sample images;
2. actually cover locally detected face/plate regions with opaque pixels;
3. wait for an explicit human privacy review before producing
   ``VerifiedVisualAttachment`` objects.

OpenCV's bundled Haar cascades are useful redaction proposals, but they do not
prove that every Korean vehicle plate or every face was found. Consequently an
automatic preparation is *never* stamped human-verified. Until an explicit
review is persisted, Codex image transport must stop with
``awaiting_deidentification_review``. Claude may still run through its separate
metadata-only path without reading these files.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import threading
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, Sequence, Tuple
from uuid import UUID, uuid4

from PIL import Image, ImageOps, UnidentifiedImageError

from .structured_vision_judgment import (
    DEIDENTIFICATION_RECORD_SCHEMA_VERSION,
    VerifiedVisualAttachment,
)


PREPARATION_SCHEMA_VERSION = (
    "trafficvisson.provider-visual-evidence-preparation.v1"
)
ARTIFACT_LOCATION_SCHEMA_VERSION = (
    "trafficvisson.provider-visual-evidence-artifact-location.v1"
)
ARTIFACT_ROOT_BINDING_SCHEMA_VERSION = (
    "trafficvisson.provider-visual-evidence-artifact-root-binding.v1"
)
PRIVACY_REVIEW_SCHEMA_VERSION = (
    "trafficvisson.provider-visual-evidence-privacy-review.v1"
)
REDACTION_ENGINE_SCHEMA_VERSION = (
    "trafficvisson.opencv-privacy-redaction-engine.v1"
)

MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 5 * 1024 * 1024
MAX_OUTPUT_SIDE = 1024
MAX_OUTPUT_PIXELS = 1_000_000
MIN_CROP_SIDE = 16
TARGET_CROP_PADDING_FRACTION = 0.18
MIN_TARGET_CONTEXT_WIDTH_PIXELS = 320
MIN_TARGET_CONTEXT_HEIGHT_PIXELS = 256
CANDIDATE_CONTEXT_POLICY_SCHEMA_VERSION = (
    "trafficvisson.candidate-context-window.v1"
)
MAX_PREPARATIONS_PER_CANDIDATE = 8
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_FILE_NAME = re.compile(r"^[0-9]{2}-(?:candidate|sample)\.png$")
_LOCATION_FILE = re.compile(
    r"^([a-f0-9]{24})\.artifact-location\.json$"
)
_ARTIFACT_LOCATION_SUFFIX = ".artifact-location.json"
_ARTIFACT_ROOT_BINDING_FILE = (
    ".trafficvisson-provider-visual-root.json"
)
_ARTIFACT_CATEGORY = "provider-visual-evidence"

JobArtifactRootResolver = Callable[[UUID, UUID, UUID], Path]


class ProviderVisualEvidenceError(RuntimeError):
    """Stable-code error raised at the local privacy boundary."""

    def __init__(self, code: str, message: str = "Visual evidence failed") -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{2,63}", code) is None:
            raise ValueError("visual evidence error code is invalid")
        super().__init__(message)
        self.code = code


class PrivacyRedactor(Protocol):
    def identity(self) -> Mapping[str, Any]:
        ...

    def redact(
        self,
        image: Image.Image,
    ) -> tuple[Image.Image, Mapping[str, Any]]:
        ...


@dataclass(frozen=True)
class ResolvedReferenceImage:
    body: bytes
    media_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class ResolvedProviderVisualEvidenceArtifact:
    path: Path
    media_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class _PreparedImage:
    order: int
    role: str
    source_ref: str
    source_sha256: str
    file_name: str
    output_sha256: str
    size_bytes: int
    width: int
    height: int
    crop_box: Optional[Mapping[str, float]]
    face_detection_count: int
    plate_detection_count: int
    redaction_boxes: Tuple[Mapping[str, Any], ...]

    def as_manifest_row(self) -> Dict[str, Any]:
        return {
            "order": self.order,
            "role": self.role,
            "source_ref": self.source_ref,
            "source_sha256": self.source_sha256,
            "file_name": self.file_name,
            "output_sha256": self.output_sha256,
            "media_type": "image/png",
            "size_bytes": self.size_bytes,
            "width": self.width,
            "height": self.height,
            "crop_box": (
                dict(self.crop_box)
                if self.crop_box is not None
                else None
            ),
            "face_detection_count": self.face_detection_count,
            "plate_detection_count": self.plate_detection_count,
            "redaction_boxes": [
                dict(box) for box in self.redaction_boxes
            ],
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(
        attributes
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _ensure_directory(path: Path, *, parent: Optional[Path] = None) -> Path:
    try:
        path.mkdir(mode=0o700, parents=parent is None, exist_ok=True)
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ProviderVisualEvidenceError(
            "visual_evidence_storage_unavailable"
        ) from error
    if (
        not resolved.is_dir()
        or path.is_symlink()
        or _is_reparse_point(path)
        or (parent is not None and resolved.parent != parent)
    ):
        raise ProviderVisualEvidenceError(
            "visual_evidence_storage_unsafe"
        )
    return resolved


def _write_atomic(path: Path, raw: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise ProviderVisualEvidenceError(
            "visual_evidence_storage_unavailable"
        ) from error
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _read_json_record(
    path: Path,
    *,
    maximum_bytes: int = 512 * 1024,
) -> dict[str, Any]:
    digest_path = path.with_suffix(path.suffix + ".sha256")
    try:
        if (
            not path.is_file()
            or not digest_path.is_file()
            or path.is_symlink()
            or digest_path.is_symlink()
            or _is_reparse_point(path)
            or _is_reparse_point(digest_path)
        ):
            raise OSError
        raw = path.read_bytes()
        expected = digest_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as error:
        raise ProviderVisualEvidenceError(
            "visual_evidence_record_unavailable"
        ) from error
    if (
        not 1 <= len(raw) <= maximum_bytes
        or _SHA256.fullmatch(expected) is None
        or _sha256(raw) != expected
    ):
        raise ProviderVisualEvidenceError(
            "visual_evidence_record_invalid"
        )

    def pairs(rows: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in rows:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite number")
            ),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise ProviderVisualEvidenceError(
            "visual_evidence_record_invalid"
        ) from error
    if not isinstance(value, dict):
        raise ProviderVisualEvidenceError(
            "visual_evidence_record_invalid"
        )
    return value


def _write_json_record(path: Path, value: Mapping[str, Any]) -> str:
    raw = _canonical_json(value)
    digest = _sha256(raw)
    _write_atomic(path, raw)
    _write_atomic(
        path.with_suffix(path.suffix + ".sha256"),
        f"{digest}\n".encode("ascii"),
    )
    return digest


def _normalized_absolute_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _authorized_category_root(path: Path) -> Path:
    try:
        requested = Path(path)
        if not requested.is_absolute():
            raise OSError
        resolved = requested.resolve(strict=True)
        if (
            _normalized_absolute_path(requested)
            != _normalized_absolute_path(resolved)
            or resolved.parent == resolved
            or not resolved.is_dir()
            or requested.is_symlink()
            or _is_reparse_point(requested)
        ):
            raise OSError
    except (OSError, TypeError, ValueError) as error:
        raise ProviderVisualEvidenceError(
            "visual_evidence_artifact_root_unsafe"
        ) from error
    return resolved


def _existing_child_directory(path: Path, *, parent: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ProviderVisualEvidenceError(
            "visual_evidence_preparation_not_found"
        ) from error
    if (
        resolved.parent != parent
        or not resolved.is_dir()
        or path.is_symlink()
        or _is_reparse_point(path)
    ):
        raise ProviderVisualEvidenceError(
            "visual_evidence_artifact_root_unsafe"
        )
    return resolved


def _bounded_source_image(
    body: bytes,
    *,
    expected_sha256: str,
    expected_size: int,
    expected_media_type: str,
) -> Image.Image:
    if (
        not isinstance(body, bytes)
        or not 1 <= len(body) <= MAX_SOURCE_BYTES
        or isinstance(expected_size, bool)
        or expected_size != len(body)
        or _SHA256.fullmatch(expected_sha256 or "") is None
        or _sha256(body) != expected_sha256
        or expected_media_type
        not in {
            "image/bmp",
            "image/jpeg",
            "image/png",
            "image/webp",
        }
    ):
        raise ProviderVisualEvidenceError(
            "visual_evidence_source_invalid"
        )
    try:
        with Image.open(io.BytesIO(body)) as source:
            actual_media = {
                "BMP": "image/bmp",
                "JPEG": "image/jpeg",
                "PNG": "image/png",
                "WEBP": "image/webp",
            }.get(str(source.format or "").upper())
            width, height = source.size
            if (
                actual_media != expected_media_type
                or width < MIN_CROP_SIDE
                or height < MIN_CROP_SIDE
                or width > 16_384
                or height > 16_384
                or width * height > 80_000_000
            ):
                raise ProviderVisualEvidenceError(
                    "visual_evidence_source_invalid"
                )
            source.load()
            return ImageOps.exif_transpose(source).convert("RGB")
    except ProviderVisualEvidenceError:
        raise
    except (OSError, ValueError, UnidentifiedImageError) as error:
        raise ProviderVisualEvidenceError(
            "visual_evidence_source_invalid"
        ) from error


def _normalized_box(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != {
        "x",
        "y",
        "width",
        "height",
    }:
        raise ProviderVisualEvidenceError(
            "visual_evidence_target_box_missing"
        )
    result: dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        number = value[key]
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(float(number))
        ):
            raise ProviderVisualEvidenceError(
                "visual_evidence_target_box_invalid"
            )
        result[key] = float(number)
    if (
        not 0 <= result["x"] < 1
        or not 0 <= result["y"] < 1
        or not 0 < result["width"] <= 1
        or not 0 < result["height"] <= 1
        or result["x"] + result["width"] > 1.00000001
        or result["y"] + result["height"] > 1.00000001
    ):
        raise ProviderVisualEvidenceError(
            "visual_evidence_target_box_invalid"
        )
    return result


def _candidate_context_policy() -> dict[str, Any]:
    return {
        "schema_version": CANDIDATE_CONTEXT_POLICY_SCHEMA_VERSION,
        "target_padding_fraction": TARGET_CROP_PADDING_FRACTION,
        "minimum_source_width_pixels": (
            MIN_TARGET_CONTEXT_WIDTH_PIXELS
        ),
        "minimum_source_height_pixels": (
            MIN_TARGET_CONTEXT_HEIGHT_PIXELS
        ),
        "small_source_axis_policy": "use_full_axis",
        "boundary_policy": "center_expand_clamp_recenter",
        "padded_target_inclusion_required": True,
    }


def _visual_evidence_policy(
    *,
    include_context_window: bool,
) -> dict[str, Any]:
    policy: dict[str, Any] = {
        "candidate_target_crop_padding_fraction": (
            TARGET_CROP_PADDING_FRACTION
        ),
        "max_output_side": MAX_OUTPUT_SIDE,
        "max_output_pixels": MAX_OUTPUT_PIXELS,
        "redaction_method": "opaque_black_fill",
        "automatic_privacy_verification_allowed": False,
    }
    if include_context_window:
        policy["candidate_context_window"] = (
            _candidate_context_policy()
        )
    return policy


def _context_axis_window(
    *,
    total_pixels: int,
    padded_start: float,
    padded_end: float,
    target_center: float,
    minimum_pixels: int,
) -> tuple[int, int]:
    padded_first = max(
        0,
        min(
            total_pixels - 1,
            int(math.floor(padded_start * total_pixels)),
        ),
    )
    padded_last = max(
        padded_first + 1,
        min(
            total_pixels,
            int(math.ceil(padded_end * total_pixels)),
        ),
    )
    desired = min(
        total_pixels,
        max(minimum_pixels, padded_last - padded_first),
    )
    first = int(
        math.floor(target_center * total_pixels - desired / 2)
    )
    first = max(0, min(total_pixels - desired, first))
    if first > padded_first:
        first = padded_first
    if first + desired < padded_last:
        first = padded_last - desired
    first = max(0, min(total_pixels - desired, first))
    return first, first + desired


def _crop_target(
    image: Image.Image,
    normalized: Mapping[str, float],
) -> tuple[Image.Image, dict[str, float]]:
    width, height = image.size
    padding_x = (
        normalized["width"] * TARGET_CROP_PADDING_FRACTION
    )
    padding_y = (
        normalized["height"] * TARGET_CROP_PADDING_FRACTION
    )
    x0 = max(0.0, normalized["x"] - padding_x)
    y0 = max(0.0, normalized["y"] - padding_y)
    x1 = min(
        1.0,
        normalized["x"] + normalized["width"] + padding_x,
    )
    y1 = min(
        1.0,
        normalized["y"] + normalized["height"] + padding_y,
    )
    left, right = _context_axis_window(
        total_pixels=width,
        padded_start=x0,
        padded_end=x1,
        target_center=normalized["x"] + normalized["width"] / 2,
        minimum_pixels=MIN_TARGET_CONTEXT_WIDTH_PIXELS,
    )
    top, bottom = _context_axis_window(
        total_pixels=height,
        padded_start=y0,
        padded_end=y1,
        target_center=normalized["y"] + normalized["height"] / 2,
        minimum_pixels=MIN_TARGET_CONTEXT_HEIGHT_PIXELS,
    )
    if right - left < MIN_CROP_SIDE or bottom - top < MIN_CROP_SIDE:
        raise ProviderVisualEvidenceError(
            "visual_evidence_target_crop_too_small"
        )
    actual = {
        "x": left / width,
        "y": top / height,
        "width": (right - left) / width,
        "height": (bottom - top) / height,
    }
    return image.crop((left, top, right, bottom)), actual


def _minimize(image: Image.Image) -> Image.Image:
    width, height = image.size
    scale = min(
        1.0,
        MAX_OUTPUT_SIDE / max(width, height),
        math.sqrt(MAX_OUTPUT_PIXELS / (width * height)),
    )
    if scale < 1:
        image = image.resize(
            (
                max(MIN_CROP_SIDE, int(width * scale)),
                max(MIN_CROP_SIDE, int(height * scale)),
            ),
            Image.Resampling.LANCZOS,
        )
    return image


def _encode_png(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(
        output,
        format="PNG",
        optimize=False,
        compress_level=6,
    )
    raw = output.getvalue()
    if not 1 <= len(raw) <= MAX_OUTPUT_BYTES:
        raise ProviderVisualEvidenceError(
            "visual_evidence_output_too_large"
        )
    return raw


def _cascade_sha256(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ProviderVisualEvidenceError(
            "privacy_redaction_runtime_unavailable"
        ) from error
    if not raw or len(raw) > 8 * 1024 * 1024:
        raise ProviderVisualEvidenceError(
            "privacy_redaction_runtime_unavailable"
        )
    return _sha256(raw)


def _merge_boxes(
    boxes: Sequence[tuple[int, int, int, int]],
) -> list[tuple[int, int, int, int]]:
    result: list[tuple[int, int, int, int]] = []
    for candidate in sorted(
        boxes,
        key=lambda item: (item[0], item[1], item[2], item[3]),
    ):
        x, y, width, height = candidate
        if width <= 0 or height <= 0:
            continue
        duplicate = False
        for existing in result:
            ex, ey, ew, eh = existing
            left = max(x, ex)
            top = max(y, ey)
            right = min(x + width, ex + ew)
            bottom = min(y + height, ey + eh)
            intersection = max(0, right - left) * max(0, bottom - top)
            minimum_area = min(width * height, ew * eh)
            if minimum_area and intersection / minimum_area >= 0.55:
                duplicate = True
                break
        if not duplicate:
            result.append(candidate)
    return result


class OpenCvPrivacyRedactor:
    """Opaque-redact regions proposed by local OpenCV cascades.

    The engine identity records the exact OpenCV version and cascade hashes.
    Detection coverage is not represented as a privacy guarantee; the caller
    must persist a human review before transport.
    """

    def __init__(
        self,
        *,
        detected_boxes: Optional[
            Callable[[Image.Image], Mapping[str, Sequence[Sequence[int]]]]
        ] = None,
    ) -> None:
        self._detected_boxes = detected_boxes
        self._cv2 = None
        self._face_cascades: tuple[Any, ...] = ()
        self._plate_cascades: tuple[Any, ...] = ()
        self._identity: Optional[dict[str, Any]] = None
        if detected_boxes is None:
            self._load_runtime()
        else:
            self._identity = {
                "schema_version": REDACTION_ENGINE_SCHEMA_VERSION,
                "engine": "test_injected_local_detector",
                "engine_version": "1",
                "face_cascade_sha256s": [],
                "plate_cascade_sha256s": [],
                "coverage_guaranteed": False,
                "human_review_required": True,
            }

    def _load_runtime(self) -> None:
        try:
            import cv2

            cascade_root = Path(cv2.data.haarcascades).resolve(strict=True)
        except (ImportError, OSError, AttributeError) as error:
            raise ProviderVisualEvidenceError(
                "privacy_redaction_runtime_unavailable"
            ) from error
        face_paths = (
            cascade_root / "haarcascade_frontalface_default.xml",
            cascade_root / "haarcascade_profileface.xml",
        )
        plate_paths = (
            cascade_root / "haarcascade_license_plate_rus_16stages.xml",
            cascade_root / "haarcascade_russian_plate_number.xml",
        )
        if any(
            not path.is_file()
            or path.is_symlink()
            or _is_reparse_point(path)
            or path.parent != cascade_root
            for path in (*face_paths, *plate_paths)
        ):
            raise ProviderVisualEvidenceError(
                "privacy_redaction_runtime_unavailable"
            )
        face_cascades = tuple(
            cv2.CascadeClassifier(str(path)) for path in face_paths
        )
        plate_cascades = tuple(
            cv2.CascadeClassifier(str(path)) for path in plate_paths
        )
        if any(
            classifier.empty()
            for classifier in (*face_cascades, *plate_cascades)
        ):
            raise ProviderVisualEvidenceError(
                "privacy_redaction_runtime_unavailable"
            )
        self._cv2 = cv2
        self._face_cascades = face_cascades
        self._plate_cascades = plate_cascades
        self._identity = {
            "schema_version": REDACTION_ENGINE_SCHEMA_VERSION,
            "engine": "opencv_haar_opaque_redaction_proposal",
            "engine_version": str(cv2.__version__),
            "face_cascade_sha256s": [
                _cascade_sha256(path) for path in face_paths
            ],
            "plate_cascade_sha256s": [
                _cascade_sha256(path) for path in plate_paths
            ],
            "coverage_guaranteed": False,
            "human_review_required": True,
            "known_limitations": [
                "bundled_plate_cascades_are_not_korean_plate_guarantees",
                "small_occluded_or_profile_faces_may_be_missed",
            ],
        }

    def identity(self) -> Mapping[str, Any]:
        if self._identity is None:
            raise ProviderVisualEvidenceError(
                "privacy_redaction_runtime_unavailable"
            )
        return json.loads(
            _canonical_json(self._identity).decode("utf-8")
        )

    def _detect(
        self,
        image: Image.Image,
    ) -> tuple[
        list[tuple[int, int, int, int]],
        list[tuple[int, int, int, int]],
    ]:
        if self._detected_boxes is not None:
            raw = self._detected_boxes(image)
            faces = [
                tuple(int(number) for number in box)
                for box in raw.get("faces", ())
                if len(box) == 4
            ]
            plates = [
                tuple(int(number) for number in box)
                for box in raw.get("plates", ())
                if len(box) == 4
            ]
            return _merge_boxes(faces), _merge_boxes(plates)
        if self._cv2 is None:
            raise ProviderVisualEvidenceError(
                "privacy_redaction_runtime_unavailable"
            )
        import numpy

        array = numpy.asarray(image.convert("RGB"))
        gray = self._cv2.cvtColor(array, self._cv2.COLOR_RGB2GRAY)
        gray = self._cv2.equalizeHist(gray)
        faces: list[tuple[int, int, int, int]] = []
        plates: list[tuple[int, int, int, int]] = []
        for cascade in self._face_cascades:
            detected = cascade.detectMultiScale(
                gray,
                scaleFactor=1.1,
                minNeighbors=4,
                minSize=(20, 20),
            )
            faces.extend(tuple(int(value) for value in row) for row in detected)
        for cascade in self._plate_cascades:
            detected = cascade.detectMultiScale(
                gray,
                scaleFactor=1.08,
                minNeighbors=3,
                minSize=(24, 8),
            )
            plates.extend(tuple(int(value) for value in row) for row in detected)
        return _merge_boxes(faces), _merge_boxes(plates)

    def redact(
        self,
        image: Image.Image,
    ) -> tuple[Image.Image, Mapping[str, Any]]:
        faces, plates = self._detect(image)
        output = image.convert("RGB").copy()
        width, height = output.size
        boxes: list[dict[str, Any]] = []
        for kind, detections in (("face", faces), ("plate", plates)):
            for x, y, box_width, box_height in detections:
                padding_x = max(2, int(box_width * 0.12))
                padding_y = max(2, int(box_height * 0.18))
                left = max(0, x - padding_x)
                top = max(0, y - padding_y)
                right = min(width, x + box_width + padding_x)
                bottom = min(height, y + box_height + padding_y)
                if right <= left or bottom <= top:
                    continue
                output.paste((0, 0, 0), (left, top, right, bottom))
                boxes.append(
                    {
                        "kind": kind,
                        "x": left,
                        "y": top,
                        "width": right - left,
                        "height": bottom - top,
                        "method": "opaque_black_fill",
                    }
                )
        return output, {
            "face_detection_count": sum(
                box["kind"] == "face" for box in boxes
            ),
            "plate_detection_count": sum(
                box["kind"] == "plate" for box in boxes
            ),
            "redaction_boxes": boxes,
            "automatic_coverage_guaranteed": False,
            "human_review_required": True,
        }


class ProviderVisualEvidencePreparationService:
    """Persist minimized/redacted artifacts and explicit privacy reviews.

    ``artifact_root_resolver`` must return the already-authorized per-job
    ``provider-visual-evidence`` category root for the supplied
    project/pipeline/analysis scope.  ``prepare(job_artifact_root=...)`` has
    the same category-root meaning.  The service adds only
    ``candidate_id/preparation_id`` below it and never exposes that path.
    """

    def __init__(
        self,
        root: Path,
        *,
        redactor: Optional[PrivacyRedactor] = None,
        artifact_root_resolver: Optional[
            JobArtifactRootResolver
        ] = None,
    ) -> None:
        requested = Path(root)
        requested.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._root = _ensure_directory(requested)
        self._redactor = redactor
        if (
            artifact_root_resolver is not None
            and not callable(artifact_root_resolver)
        ):
            raise TypeError("artifact_root_resolver must be callable")
        self._artifact_root_resolver = artifact_root_resolver
        self._artifact_root_cache: dict[
            tuple[UUID, UUID, UUID],
            tuple[Path, str],
        ] = {}
        self._lock = threading.RLock()

    @property
    def attachment_root(self) -> Path:
        return self._root

    def runtime_status(self) -> Mapping[str, Any]:
        try:
            identity = dict(self._redactor_instance().identity())
        except ProviderVisualEvidenceError as error:
            return {
                "state": "unavailable",
                "error_code": error.code,
                "human_review_required": True,
            }
        return {
            "state": "ready",
            "engine": identity.get("engine"),
            "engine_version": identity.get("engine_version"),
            "engine_identity_sha256": _sha256(
                _canonical_json(identity)
            ),
            "coverage_guaranteed": False,
            "human_review_required": True,
        }

    def _redactor_instance(self) -> PrivacyRedactor:
        with self._lock:
            if self._redactor is None:
                self._redactor = OpenCvPrivacyRedactor()
            return self._redactor

    def _candidate_root(
        self,
        project_id: UUID,
        pipeline_job_id: UUID,
        candidate_id: str,
    ) -> Path:
        if (
            not isinstance(project_id, UUID)
            or not isinstance(pipeline_job_id, UUID)
            or _CANDIDATE_ID.fullmatch(candidate_id or "") is None
        ):
            raise ProviderVisualEvidenceError(
                "visual_evidence_identity_invalid"
            )
        project = _ensure_directory(
            self._root / str(project_id),
            parent=self._root,
        )
        pipeline = _ensure_directory(
            project / str(pipeline_job_id),
            parent=project,
        )
        return _ensure_directory(
            pipeline / candidate_id,
            parent=pipeline,
        )

    @staticmethod
    def _artifact_root_binding(
        *,
        category_root: Path,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
    ) -> dict[str, Any]:
        root_identity_sha256 = _sha256(
            _canonical_json(
                {
                    "category": _ARTIFACT_CATEGORY,
                    "normalized_absolute_path": (
                        _normalized_absolute_path(category_root)
                    ),
                }
            )
        )
        body = {
            "schema_version": (
                ARTIFACT_ROOT_BINDING_SCHEMA_VERSION
            ),
            "category": _ARTIFACT_CATEGORY,
            "project_id": str(project_id),
            "pipeline_job_id": str(pipeline_job_id),
            "analysis_job_id": str(analysis_job_id),
            "category_root_identity_sha256": (
                root_identity_sha256
            ),
        }
        return {
            **body,
            "record_sha256": _sha256(_canonical_json(body)),
        }

    def _resolve_job_artifact_root(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        explicit_root: Optional[Path],
        initialize_binding: bool,
    ) -> Optional[tuple[Path, str]]:
        if (
            not isinstance(project_id, UUID)
            or not isinstance(pipeline_job_id, UUID)
            or not isinstance(analysis_job_id, UUID)
        ):
            raise ProviderVisualEvidenceError(
                "visual_evidence_identity_invalid"
            )
        key = (project_id, pipeline_job_id, analysis_job_id)
        resolved_explicit = (
            _authorized_category_root(Path(explicit_root))
            if explicit_root is not None
            else None
        )
        resolved_callback: Optional[Path] = None
        if self._artifact_root_resolver is not None:
            try:
                callback_value = self._artifact_root_resolver(
                    project_id,
                    pipeline_job_id,
                    analysis_job_id,
                )
                resolved_callback = _authorized_category_root(
                    Path(callback_value)
                )
            except ProviderVisualEvidenceError:
                raise
            except Exception as error:
                raise ProviderVisualEvidenceError(
                    "visual_evidence_artifact_root_unavailable"
                ) from error
        if (
            resolved_explicit is not None
            and resolved_callback is not None
            and resolved_explicit != resolved_callback
        ):
            raise ProviderVisualEvidenceError(
                "visual_evidence_artifact_root_conflict"
            )
        resolved = resolved_callback or resolved_explicit
        if resolved is None:
            cached = self._artifact_root_cache.get(key)
            if cached is None:
                return None
            resolved, _cached_identity = cached
            resolved = _authorized_category_root(resolved)

        binding = self._artifact_root_binding(
            category_root=resolved,
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=analysis_job_id,
        )
        binding_path = resolved / _ARTIFACT_ROOT_BINDING_FILE
        if binding_path.exists():
            persisted = _read_json_record(binding_path)
            if persisted != binding:
                raise ProviderVisualEvidenceError(
                    "visual_evidence_artifact_root_conflict"
                )
        elif initialize_binding:
            _write_json_record(binding_path, binding)
        else:
            raise ProviderVisualEvidenceError(
                "visual_evidence_artifact_root_unavailable"
            )
        identity = str(
            binding["category_root_identity_sha256"]
        )
        cached = self._artifact_root_cache.get(key)
        if (
            cached is not None
            and (
                cached[0] != resolved
                or cached[1] != identity
            )
        ):
            raise ProviderVisualEvidenceError(
                "visual_evidence_artifact_root_conflict"
            )
        self._artifact_root_cache[key] = (resolved, identity)
        return resolved, identity

    @staticmethod
    def _artifact_location_path(
        candidate_root: Path,
        preparation_id: str,
    ) -> Path:
        return candidate_root / (
            f"{preparation_id}{_ARTIFACT_LOCATION_SUFFIX}"
        )

    def _write_artifact_location(
        self,
        *,
        candidate_root: Path,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        candidate_id: str,
        preparation_id: str,
        input_revision: str,
        manifest_sha256: str,
        category_root_identity_sha256: str,
    ) -> None:
        body = {
            "schema_version": ARTIFACT_LOCATION_SCHEMA_VERSION,
            "category": _ARTIFACT_CATEGORY,
            "project_id": str(project_id),
            "pipeline_job_id": str(pipeline_job_id),
            "analysis_job_id": str(analysis_job_id),
            "candidate_id": candidate_id,
            "preparation_id": preparation_id,
            "input_revision": input_revision,
            "relative_artifact_path": (
                f"{candidate_id}/{preparation_id}"
            ),
            "category_root_identity_sha256": (
                category_root_identity_sha256
            ),
            "manifest_sha256": manifest_sha256,
        }
        record = {
            **body,
            "record_sha256": _sha256(_canonical_json(body)),
        }
        path = self._artifact_location_path(
            candidate_root,
            preparation_id,
        )
        if path.exists():
            if _read_json_record(path) != record:
                raise ProviderVisualEvidenceError(
                    "visual_evidence_artifact_location_conflict"
                )
            return
        _write_json_record(path, record)

    def _load_artifact_location(
        self,
        *,
        candidate_root: Path,
        project_id: UUID,
        pipeline_job_id: UUID,
        candidate_id: str,
        preparation_id: str,
    ) -> tuple[Path, dict[str, Any]]:
        path = self._artifact_location_path(
            candidate_root,
            preparation_id,
        )
        locator = _read_json_record(path)
        expected_keys = {
            "schema_version",
            "category",
            "project_id",
            "pipeline_job_id",
            "analysis_job_id",
            "candidate_id",
            "preparation_id",
            "input_revision",
            "relative_artifact_path",
            "category_root_identity_sha256",
            "manifest_sha256",
            "record_sha256",
        }
        record_sha256 = locator.get("record_sha256")
        body = {
            key: locator[key]
            for key in sorted(locator)
            if key != "record_sha256"
        }
        try:
            analysis_job_id = UUID(
                str(locator.get("analysis_job_id"))
            )
        except (ValueError, TypeError, AttributeError) as error:
            raise ProviderVisualEvidenceError(
                "visual_evidence_artifact_location_invalid"
            ) from error
        if (
            set(locator) != expected_keys
            or locator.get("schema_version")
            != ARTIFACT_LOCATION_SCHEMA_VERSION
            or locator.get("category") != _ARTIFACT_CATEGORY
            or locator.get("project_id") != str(project_id)
            or locator.get("pipeline_job_id")
            != str(pipeline_job_id)
            or locator.get("analysis_job_id")
            != str(analysis_job_id)
            or locator.get("candidate_id") != candidate_id
            or locator.get("preparation_id") != preparation_id
            or locator.get("relative_artifact_path")
            != f"{candidate_id}/{preparation_id}"
            or _SHA256.fullmatch(
                str(locator.get("input_revision") or "")
            )
            is None
            or _SHA256.fullmatch(
                str(
                    locator.get(
                        "category_root_identity_sha256"
                    )
                    or ""
                )
            )
            is None
            or _SHA256.fullmatch(
                str(locator.get("manifest_sha256") or "")
            )
            is None
            or _SHA256.fullmatch(str(record_sha256 or ""))
            is None
            or _sha256(_canonical_json(body)) != record_sha256
        ):
            raise ProviderVisualEvidenceError(
                "visual_evidence_artifact_location_invalid"
            )
        resolved = self._resolve_job_artifact_root(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=analysis_job_id,
            explicit_root=None,
            initialize_binding=False,
        )
        if resolved is None:
            raise ProviderVisualEvidenceError(
                "visual_evidence_artifact_root_unavailable"
            )
        artifact_root, identity = resolved
        if (
            identity
            != locator["category_root_identity_sha256"]
        ):
            raise ProviderVisualEvidenceError(
                "visual_evidence_artifact_root_changed"
            )
        external_candidate = _existing_child_directory(
            artifact_root / candidate_id,
            parent=artifact_root,
        )
        external_preparation = _existing_child_directory(
            external_candidate / preparation_id,
            parent=external_candidate,
        )
        return external_preparation, locator

    def _assert_pipeline_artifact_root_binding(
        self,
        *,
        pipeline_ledger_root: Path,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        category_root_identity_sha256: str,
    ) -> None:
        for candidate_root in pipeline_ledger_root.iterdir():
            if not candidate_root.is_dir():
                continue
            if (
                candidate_root.is_symlink()
                or _is_reparse_point(candidate_root)
                or _CANDIDATE_ID.fullmatch(candidate_root.name)
                is None
            ):
                raise ProviderVisualEvidenceError(
                    "visual_evidence_artifact_location_invalid"
                )
            for path in candidate_root.iterdir():
                match = _LOCATION_FILE.fullmatch(path.name)
                if match is None:
                    continue
                locator = _read_json_record(path)
                if (
                    locator.get("schema_version")
                    != ARTIFACT_LOCATION_SCHEMA_VERSION
                    or locator.get("category")
                    != _ARTIFACT_CATEGORY
                    or locator.get("project_id")
                    != str(project_id)
                    or locator.get("pipeline_job_id")
                    != str(pipeline_job_id)
                    or locator.get("analysis_job_id")
                    != str(analysis_job_id)
                    or locator.get("candidate_id")
                    != candidate_root.name
                    or locator.get("preparation_id")
                    != match.group(1)
                    or locator.get(
                        "category_root_identity_sha256"
                    )
                    != category_root_identity_sha256
                ):
                    raise ProviderVisualEvidenceError(
                        "visual_evidence_artifact_root_changed"
                    )

    def _source_body_from_path(
        self,
        path: Path,
        *,
        expected_sha256: str,
        expected_size: int,
    ) -> bytes:
        try:
            candidate = Path(path)
            if (
                not candidate.is_absolute()
                or candidate.is_symlink()
                or _is_reparse_point(candidate)
                or not candidate.is_file()
                or not stat.S_ISREG(candidate.lstat().st_mode)
                or candidate.stat().st_size != expected_size
                or expected_size > MAX_SOURCE_BYTES
            ):
                raise OSError
            before = candidate.stat()
            raw = candidate.read_bytes()
            after = candidate.stat()
        except (OSError, TypeError, ValueError) as error:
            raise ProviderVisualEvidenceError(
                "visual_evidence_source_unavailable"
            ) from error
        if (
            len(raw) != expected_size
            or _sha256(raw) != expected_sha256
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ino != after.st_ino
        ):
            raise ProviderVisualEvidenceError(
                "visual_evidence_source_changed"
            )
        return raw

    def prepare_from_body(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        candidate_id: str,
        candidate_body: bytes,
        candidate_media_type: str,
        candidate_size_bytes: int,
        candidate_sha256: str,
        candidate_source_ref: str,
        target_box: Mapping[str, Any],
        similar_cases: Sequence[Mapping[str, Any]],
        resolve_reference: Callable[[str, str], Any],
        job_artifact_root: Optional[Path] = None,
    ) -> Mapping[str, Any]:
        """Prepare a path-free, hash-bound candidate image.

        Provider candidate montages are assembled from immutable analysis
        artifacts in memory. Stage the body only inside this service's
        server-owned private root, reuse the normal validation pipeline, and
        remove the staging file before returning. No caller-controlled path
        or temporary locator is persisted in the preparation manifest.
        """

        if (
            not isinstance(candidate_body, bytes)
            or isinstance(candidate_size_bytes, bool)
            or not isinstance(candidate_size_bytes, int)
            or not 1 <= candidate_size_bytes <= MAX_SOURCE_BYTES
            or len(candidate_body) != candidate_size_bytes
            or not isinstance(candidate_sha256, str)
            or _SHA256.fullmatch(candidate_sha256) is None
            or _sha256(candidate_body) != candidate_sha256
        ):
            raise ProviderVisualEvidenceError(
                "visual_evidence_source_changed"
            )
        try:
            staging_root = _ensure_directory(
                self._root / ".candidate-body-staging",
                parent=self._root,
            )
        except (OSError, ValueError) as error:
            raise ProviderVisualEvidenceError(
                "visual_evidence_source_unavailable"
            ) from error
        staged_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix="candidate-",
                suffix=".bin",
                dir=staging_root,
                delete=False,
            ) as stream:
                staged_path = Path(stream.name).resolve(
                    strict=False
                )
                stream.write(candidate_body)
                stream.flush()
                os.fsync(stream.fileno())
            return self.prepare(
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                analysis_job_id=analysis_job_id,
                candidate_id=candidate_id,
                candidate_path=staged_path,
                candidate_media_type=candidate_media_type,
                candidate_size_bytes=candidate_size_bytes,
                candidate_sha256=candidate_sha256,
                candidate_source_ref=candidate_source_ref,
                target_box=target_box,
                similar_cases=similar_cases,
                resolve_reference=resolve_reference,
                job_artifact_root=job_artifact_root,
            )
        except OSError as error:
            raise ProviderVisualEvidenceError(
                "visual_evidence_source_unavailable"
            ) from error
        finally:
            if staged_path is not None:
                try:
                    staged_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def prepare(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        candidate_id: str,
        candidate_path: Path,
        candidate_media_type: str,
        candidate_size_bytes: int,
        candidate_sha256: str,
        candidate_source_ref: str,
        target_box: Mapping[str, Any],
        similar_cases: Sequence[Mapping[str, Any]],
        resolve_reference: Callable[[str, str], Any],
        job_artifact_root: Optional[Path] = None,
    ) -> Mapping[str, Any]:
        if (
            not isinstance(analysis_job_id, UUID)
            or not isinstance(candidate_source_ref, str)
            or not candidate_source_ref.startswith(
                f"deid:{candidate_id}:"
            )
            or not callable(resolve_reference)
            or isinstance(similar_cases, (str, bytes))
            or not isinstance(similar_cases, Sequence)
            or not 3 <= len(similar_cases) <= 5
        ):
            raise ProviderVisualEvidenceError(
                "visual_evidence_input_invalid"
            )
        normalized_target = _normalized_box(target_box)
        redactor = self._redactor_instance()
        engine_identity = dict(redactor.identity())
        engine_identity_sha256 = _sha256(
            _canonical_json(engine_identity)
        )
        candidate_raw = self._source_body_from_path(
            candidate_path,
            expected_sha256=candidate_sha256,
            expected_size=candidate_size_bytes,
        )
        try:
            candidate_image = _bounded_source_image(
                candidate_raw,
                expected_sha256=candidate_sha256,
                expected_size=candidate_size_bytes,
                expected_media_type=candidate_media_type,
            )
        except ProviderVisualEvidenceError as error:
            if error.code == "visual_evidence_source_invalid":
                raise ProviderVisualEvidenceError(
                    "candidate_visual_evidence_source_invalid"
                ) from error
            raise
        reference_rows: list[
            tuple[
                Mapping[str, Any],
                ResolvedReferenceImage,
                Image.Image,
            ]
        ] = []
        seen_refs: set[str] = set()
        for case in similar_cases:
            if not isinstance(case, Mapping):
                raise ProviderVisualEvidenceError(
                    "visual_evidence_reference_invalid"
                )
            source_id = case.get("source_id")
            case_id = case.get("case_id")
            source_ref = case.get("source_ref")
            photo_sha256 = case.get("photo_sha256")
            if (
                not isinstance(source_id, str)
                or _SAFE_ID.fullmatch(source_id) is None
                or not isinstance(case_id, str)
                or _SAFE_ID.fullmatch(case_id) is None
                or not isinstance(source_ref, str)
                or source_ref in seen_refs
                or _SHA256.fullmatch(str(photo_sha256 or "")) is None
            ):
                raise ProviderVisualEvidenceError(
                    "visual_evidence_reference_invalid"
                )
            seen_refs.add(source_ref)
            try:
                resolved = resolve_reference(source_id, case_id)
                image = ResolvedReferenceImage(
                    body=bytes(resolved.body),
                    media_type=str(resolved.media_type),
                    size_bytes=int(resolved.size_bytes),
                    sha256=str(resolved.sha256),
                )
            except Exception as error:
                raise ProviderVisualEvidenceError(
                    "visual_evidence_reference_unavailable"
                ) from error
            if image.sha256 != photo_sha256:
                raise ProviderVisualEvidenceError(
                    "visual_evidence_reference_changed"
                )
            # A malformed historical weak-reference image must not discard a
            # valid current-video candidate. Keep the original ranking while
            # selecting only image payloads that satisfy the bounded decoder.
            # Structural provenance failures above still fail closed.
            try:
                sample_image = _bounded_source_image(
                    image.body,
                    expected_sha256=image.sha256,
                    expected_size=image.size_bytes,
                    expected_media_type=image.media_type,
                )
            except ProviderVisualEvidenceError as error:
                if error.code == "visual_evidence_source_invalid":
                    continue
                raise
            reference_rows.append((case, image, sample_image))
        if len(reference_rows) < 3:
            raise ProviderVisualEvidenceError(
                "similar_case_visual_evidence_insufficient"
            )

        external_storage = self._resolve_job_artifact_root(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=analysis_job_id,
            explicit_root=job_artifact_root,
            initialize_binding=True,
        )
        input_binding = {
            "schema_version": PREPARATION_SCHEMA_VERSION,
            "project_id": str(project_id),
            "pipeline_job_id": str(pipeline_job_id),
            "analysis_job_id": str(analysis_job_id),
            "candidate_id": candidate_id,
            "candidate_sha256": candidate_sha256,
            "candidate_source_ref": candidate_source_ref,
            "target_box": normalized_target,
            "references": [
                {
                    "source_id": case["source_id"],
                    "case_id": case["case_id"],
                    "source_ref": case["source_ref"],
                    "source_sha256": image.sha256,
                    "reference_state": case.get("reference_state"),
                }
                for case, image, _sample_image in reference_rows
            ],
            "engine_identity_sha256": engine_identity_sha256,
            "policy": _visual_evidence_policy(
                include_context_window=True
            ),
        }
        if external_storage is not None:
            _category_root, category_root_identity = (
                external_storage
            )
            input_binding["artifact_storage"] = {
                "mode": "external_category_root",
                "category": _ARTIFACT_CATEGORY,
                "category_root_identity_sha256": (
                    category_root_identity
                ),
            }
        input_revision = _sha256(_canonical_json(input_binding))
        preparation_id = input_revision[:24]
        candidate_root = self._candidate_root(
            project_id,
            pipeline_job_id,
            candidate_id,
        )
        with self._lock:
            if external_storage is not None:
                self._assert_pipeline_artifact_root_binding(
                    pipeline_ledger_root=candidate_root.parent,
                    project_id=project_id,
                    pipeline_job_id=pipeline_job_id,
                    analysis_job_id=analysis_job_id,
                    category_root_identity_sha256=(
                        external_storage[1]
                    ),
                )
            location_path = self._artifact_location_path(
                candidate_root,
                preparation_id,
            )
            if location_path.exists():
                final_root, locator = (
                    self._load_artifact_location(
                        candidate_root=candidate_root,
                        project_id=project_id,
                        pipeline_job_id=pipeline_job_id,
                        candidate_id=candidate_id,
                        preparation_id=preparation_id,
                    )
                )
                manifest, manifest_sha256 = self._load_manifest(
                    final_root,
                    expected_input_revision=input_revision,
                )
                if (
                    manifest_sha256
                    != locator["manifest_sha256"]
                    or manifest.get("project_id")
                    != str(project_id)
                    or manifest.get("pipeline_job_id")
                    != str(pipeline_job_id)
                    or manifest.get("analysis_job_id")
                    != str(analysis_job_id)
                    or manifest.get("candidate_id")
                    != candidate_id
                    or not isinstance(
                        manifest.get("artifact_storage"),
                        Mapping,
                    )
                    or manifest["artifact_storage"].get(
                        "category_root_identity_sha256"
                    )
                    != locator[
                        "category_root_identity_sha256"
                    ]
                ):
                    raise ProviderVisualEvidenceError(
                        "visual_evidence_artifact_location_invalid"
                    )
                return self._public_snapshot(
                    final_root,
                    manifest,
                    manifest_sha256,
                )
            if external_storage is None:
                artifact_candidate_root = candidate_root
            else:
                category_root, _category_root_identity = (
                    external_storage
                )
                artifact_candidate_root = _ensure_directory(
                    category_root / candidate_id,
                    parent=category_root,
                )
            final_root = artifact_candidate_root / preparation_id
            if final_root.exists():
                manifest, manifest_sha256 = self._load_manifest(
                    final_root,
                    expected_input_revision=input_revision,
                )
                if external_storage is not None:
                    self._write_artifact_location(
                        candidate_root=candidate_root,
                        project_id=project_id,
                        pipeline_job_id=pipeline_job_id,
                        analysis_job_id=analysis_job_id,
                        candidate_id=candidate_id,
                        preparation_id=preparation_id,
                        input_revision=input_revision,
                        manifest_sha256=manifest_sha256,
                        category_root_identity_sha256=(
                            external_storage[1]
                        ),
                    )
                return self._public_snapshot(
                    final_root,
                    manifest,
                    manifest_sha256,
                )
            existing_ids = {
                child.name
                for child in candidate_root.iterdir()
                if child.is_dir()
                and not child.name.startswith(".")
                and re.fullmatch(r"[a-f0-9]{24}", child.name)
            }
            existing_ids.update(
                match.group(1)
                for child in candidate_root.iterdir()
                if child.is_file()
                and (
                    match := _LOCATION_FILE.fullmatch(child.name)
                )
                is not None
            )
            if (
                len(existing_ids)
                >= MAX_PREPARATIONS_PER_CANDIDATE
            ):
                raise ProviderVisualEvidenceError(
                    "visual_evidence_preparation_limit"
                )
            try:
                temporary = Path(
                    tempfile.mkdtemp(
                        prefix=f".{preparation_id}.",
                        dir=str(artifact_candidate_root),
                    )
                )
            except OSError as error:
                raise ProviderVisualEvidenceError(
                    "visual_evidence_storage_unavailable"
                ) from error
            try:
                prepared: list[_PreparedImage] = []
                candidate_crop, crop_box = _crop_target(
                    candidate_image,
                    normalized_target,
                )
                prepared.append(
                    self._prepare_one(
                        temporary,
                        order=0,
                        role="candidate_representative",
                        source_ref=candidate_source_ref,
                        source_sha256=candidate_sha256,
                        image=candidate_crop,
                        crop_box=crop_box,
                        redactor=redactor,
                    )
                )
                for order, (
                    case,
                    resolved,
                    sample_image,
                ) in enumerate(
                    reference_rows,
                    start=1,
                ):
                    prepared.append(
                        self._prepare_one(
                            temporary,
                            order=order,
                            role="similar_case",
                            source_ref=str(case["source_ref"]),
                            source_sha256=resolved.sha256,
                            image=sample_image,
                            crop_box=None,
                            redactor=redactor,
                        )
                    )
                manifest = {
                    **input_binding,
                    "preparation_id": preparation_id,
                    "input_revision": input_revision,
                    "state": "awaiting_deidentification_review",
                    "engine": engine_identity,
                    "engine_identity_sha256": engine_identity_sha256,
                    "attachments": [
                        item.as_manifest_row() for item in prepared
                    ],
                    "automatic_redaction_performed": any(
                        item.face_detection_count
                        or item.plate_detection_count
                        for item in prepared
                    ),
                    "automatic_privacy_verification": False,
                    "human_verified": False,
                    "review_required": True,
                    "created_at": _utc_now().isoformat(),
                }
                manifest_sha256 = _write_json_record(
                    temporary / "manifest.json",
                    manifest,
                )
                os.replace(temporary, final_root)
                if external_storage is not None:
                    self._write_artifact_location(
                        candidate_root=candidate_root,
                        project_id=project_id,
                        pipeline_job_id=pipeline_job_id,
                        analysis_job_id=analysis_job_id,
                        candidate_id=candidate_id,
                        preparation_id=preparation_id,
                        input_revision=input_revision,
                        manifest_sha256=manifest_sha256,
                        category_root_identity_sha256=(
                            external_storage[1]
                        ),
                    )
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        return self._public_snapshot(
            final_root,
            manifest,
            manifest_sha256,
        )

    def _prepare_one(
        self,
        root: Path,
        *,
        order: int,
        role: str,
        source_ref: str,
        source_sha256: str,
        image: Image.Image,
        crop_box: Optional[Mapping[str, float]],
        redactor: PrivacyRedactor,
    ) -> _PreparedImage:
        minimized = _minimize(image)
        redacted, summary = redactor.redact(minimized)
        raw = _encode_png(redacted)
        file_name = (
            f"{order:02d}-candidate.png"
            if role == "candidate_representative"
            else f"{order:02d}-sample.png"
        )
        _write_atomic(root / file_name, raw)
        return _PreparedImage(
            order=order,
            role=role,
            source_ref=source_ref,
            source_sha256=source_sha256,
            file_name=file_name,
            output_sha256=_sha256(raw),
            size_bytes=len(raw),
            width=redacted.width,
            height=redacted.height,
            crop_box=crop_box,
            face_detection_count=int(
                summary.get("face_detection_count", 0)
            ),
            plate_detection_count=int(
                summary.get("plate_detection_count", 0)
            ),
            redaction_boxes=tuple(
                dict(row)
                for row in summary.get("redaction_boxes", ())
                if isinstance(row, Mapping)
            ),
        )

    def _load_manifest(
        self,
        root: Path,
        *,
        expected_input_revision: Optional[str] = None,
    ) -> tuple[dict[str, Any], str]:
        if (
            not root.is_dir()
            or root.is_symlink()
            or _is_reparse_point(root)
        ):
            raise ProviderVisualEvidenceError(
                "visual_evidence_preparation_invalid"
            )
        manifest_path = root / "manifest.json"
        manifest = _read_json_record(manifest_path)
        manifest_sha256 = _sha256(_canonical_json(manifest))
        attachments = manifest.get("attachments")
        policy = manifest.get("policy")
        artifact_storage = manifest.get("artifact_storage")
        if (
            manifest.get("schema_version")
            != PREPARATION_SCHEMA_VERSION
            or manifest.get("preparation_id") != root.name
            or _SHA256.fullmatch(
                str(manifest.get("input_revision") or "")
            )
            is None
            or (
                expected_input_revision is not None
                and manifest.get("input_revision")
                != expected_input_revision
            )
            or manifest.get("state")
            != "awaiting_deidentification_review"
            or manifest.get("automatic_privacy_verification") is not False
            or manifest.get("human_verified") is not False
            or not isinstance(policy, Mapping)
            or dict(policy)
            not in (
                # Legacy immutable preparations remain readable, while every
                # new input revision carries the minimum-context contract.
                _visual_evidence_policy(
                    include_context_window=False
                ),
                _visual_evidence_policy(
                    include_context_window=True
                ),
            )
            or not isinstance(attachments, list)
            or not 4 <= len(attachments) <= 6
            or (
                artifact_storage is not None
                and (
                    not isinstance(artifact_storage, Mapping)
                    or set(artifact_storage)
                    != {
                        "mode",
                        "category",
                        "category_root_identity_sha256",
                    }
                    or artifact_storage.get("mode")
                    != "external_category_root"
                    or artifact_storage.get("category")
                    != _ARTIFACT_CATEGORY
                    or _SHA256.fullmatch(
                        str(
                            artifact_storage.get(
                                "category_root_identity_sha256"
                            )
                            or ""
                        )
                    )
                    is None
                )
            )
        ):
            raise ProviderVisualEvidenceError(
                "visual_evidence_preparation_invalid"
            )
        for index, row in enumerate(attachments):
            if (
                not isinstance(row, Mapping)
                or row.get("order") != index
                or row.get("role")
                != (
                    "candidate_representative"
                    if index == 0
                    else "similar_case"
                )
                or not isinstance(row.get("file_name"), str)
                or _FILE_NAME.fullmatch(row["file_name"]) is None
                or _SHA256.fullmatch(
                    str(row.get("output_sha256") or "")
                )
                is None
                or _SHA256.fullmatch(
                    str(row.get("source_sha256") or "")
                )
                is None
            ):
                raise ProviderVisualEvidenceError(
                    "visual_evidence_preparation_invalid"
                )
            self._verify_output_file(root, row)
        return manifest, manifest_sha256

    def _verify_output_file(
        self,
        root: Path,
        row: Mapping[str, Any],
    ) -> Path:
        path = root / str(row["file_name"])
        try:
            resolved = path.resolve(strict=True)
            before = path.lstat()
            raw = resolved.read_bytes()
            after = resolved.stat()
        except OSError as error:
            raise ProviderVisualEvidenceError(
                "visual_evidence_output_unavailable"
            ) from error
        if (
            resolved.parent != root
            or path.is_symlink()
            or _is_reparse_point(path)
            or not stat.S_ISREG(before.st_mode)
            or isinstance(row.get("size_bytes"), bool)
            or row.get("size_bytes") != len(raw)
            or not 1 <= len(raw) <= MAX_OUTPUT_BYTES
            or _sha256(raw) != row.get("output_sha256")
            or not raw.startswith(b"\x89PNG\r\n\x1a\n")
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise ProviderVisualEvidenceError(
                "visual_evidence_output_invalid"
            )
        # Reopen with Pillow so a magic-prefix-only payload never qualifies.
        _bounded_source_image(
            raw,
            expected_sha256=str(row["output_sha256"]),
            expected_size=len(raw),
            expected_media_type="image/png",
        )
        return resolved

    def _locate(
        self,
        project_id: UUID,
        pipeline_job_id: UUID,
        candidate_id: str,
        preparation_id: str,
    ) -> Path:
        if re.fullmatch(r"[a-f0-9]{24}", preparation_id or "") is None:
            raise ProviderVisualEvidenceError(
                "visual_evidence_identity_invalid"
            )
        candidate_root = self._candidate_root(
            project_id,
            pipeline_job_id,
            candidate_id,
        )
        root = candidate_root / preparation_id
        location_path = self._artifact_location_path(
            candidate_root,
            preparation_id,
        )
        if location_path.exists():
            resolved, locator = self._load_artifact_location(
                candidate_root=candidate_root,
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                candidate_id=candidate_id,
                preparation_id=preparation_id,
            )
            manifest, manifest_sha256 = self._load_manifest(
                resolved
            )
            if (
                manifest_sha256 != locator["manifest_sha256"]
                or manifest.get("input_revision")
                != locator["input_revision"]
                or manifest.get("project_id")
                != str(project_id)
                or manifest.get("pipeline_job_id")
                != str(pipeline_job_id)
                or manifest.get("analysis_job_id")
                != locator["analysis_job_id"]
                or manifest.get("candidate_id") != candidate_id
                or not isinstance(
                    manifest.get("artifact_storage"),
                    Mapping,
                )
                or manifest["artifact_storage"].get(
                    "category_root_identity_sha256"
                )
                != locator["category_root_identity_sha256"]
            ):
                raise ProviderVisualEvidenceError(
                    "visual_evidence_artifact_location_invalid"
                )
            return resolved
        try:
            resolved = root.resolve(strict=True)
        except OSError as error:
            raise ProviderVisualEvidenceError(
                "visual_evidence_preparation_not_found"
            ) from error
        if resolved.parent != candidate_root:
            raise ProviderVisualEvidenceError(
                "visual_evidence_identity_invalid"
            )
        return resolved

    def record_privacy_review(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        candidate_id: str,
        preparation_id: str,
        expected_manifest_sha256: str,
        approved: bool,
        explicit_user_confirmation: bool,
    ) -> Mapping[str, Any]:
        if (
            _SHA256.fullmatch(expected_manifest_sha256 or "") is None
            or not isinstance(approved, bool)
            or explicit_user_confirmation is not True
        ):
            raise ProviderVisualEvidenceError(
                "privacy_review_confirmation_required"
            )
        root = self._locate(
            project_id,
            pipeline_job_id,
            candidate_id,
            preparation_id,
        )
        with self._lock:
            manifest, manifest_sha256 = self._load_manifest(root)
            if manifest_sha256 != expected_manifest_sha256:
                raise ProviderVisualEvidenceError(
                    "privacy_review_preparation_changed"
                )
            review_path = root / "privacy-review.json"
            if review_path.exists():
                existing = _read_json_record(review_path)
                if (
                    existing.get("preparation_manifest_sha256")
                    == manifest_sha256
                    and existing.get("approved") is approved
                    and existing.get("explicit_user_confirmation") is True
                ):
                    return self._public_snapshot(
                        root,
                        manifest,
                        manifest_sha256,
                    )
                raise ProviderVisualEvidenceError(
                    "privacy_review_already_recorded"
                )
            verification_records = []
            if approved:
                context_policy = manifest["policy"].get(
                    "candidate_context_window"
                )
                for row in manifest["attachments"]:
                    record_body = {
                        "schema_version": (
                            DEIDENTIFICATION_RECORD_SCHEMA_VERSION
                        ),
                        "source_ref": row["source_ref"],
                        "source_sha256": row["source_sha256"],
                        "output_sha256": row["output_sha256"],
                        "media_type": "image/png",
                        "size_bytes": row["size_bytes"],
                        "artifact_scope": (
                            "minimized_target_crop"
                            if row["role"]
                            == "candidate_representative"
                            else "minimized_reference_image"
                        ),
                        "deidentification_status": (
                            "verified_for_provider_transmission"
                        ),
                        "face_redaction_status": (
                            "redacted_verified"
                            if row["face_detection_count"] > 0
                            else "not_present_verified"
                        ),
                        "license_plate_redaction_status": (
                            "redacted_verified"
                            if row["plate_detection_count"] > 0
                            else "not_present_verified"
                        ),
                        "verification_method": (
                            "deterministic_redaction_and_human_review"
                            if (
                                row["face_detection_count"] > 0
                                or row["plate_detection_count"] > 0
                            )
                            else "human_review"
                        ),
                    }
                    if context_policy is not None:
                        record_body["candidate_context_policy"] = (
                            dict(context_policy)
                            if row["role"]
                            == "candidate_representative"
                            else None
                        )
                    verification_records.append(
                        {
                            **record_body,
                            "record_sha256": _sha256(
                                _canonical_json(record_body)
                            ),
                        }
                    )
            review_body = {
                "schema_version": PRIVACY_REVIEW_SCHEMA_VERSION,
                "project_id": str(project_id),
                "pipeline_job_id": str(pipeline_job_id),
                "candidate_id": candidate_id,
                "preparation_id": preparation_id,
                "preparation_manifest_sha256": manifest_sha256,
                "approved": approved,
                "explicit_user_confirmation": True,
                "face_and_plate_redaction_adequate": approved,
                "automatic_privacy_verification": False,
                "review_origin": "explicit_local_user_action",
                "verification_records": verification_records,
                "reviewed_at": _utc_now().isoformat(),
            }
            review = {
                **review_body,
                "record_sha256": _sha256(
                    _canonical_json(review_body)
                ),
            }
            _write_json_record(review_path, review)
        return self._public_snapshot(
            root,
            manifest,
            manifest_sha256,
        )

    def _load_review(
        self,
        root: Path,
        manifest: Mapping[str, Any],
        manifest_sha256: str,
    ) -> Optional[dict[str, Any]]:
        review_path = root / "privacy-review.json"
        if not review_path.exists():
            return None
        review = _read_json_record(review_path)
        record_sha256 = review.get("record_sha256")
        body = {
            key: review[key]
            for key in sorted(review)
            if key != "record_sha256"
        }
        if (
            review.get("schema_version")
            != PRIVACY_REVIEW_SCHEMA_VERSION
            or review.get("preparation_id")
            != manifest.get("preparation_id")
            or review.get("preparation_manifest_sha256")
            != manifest_sha256
            or review.get("automatic_privacy_verification") is not False
            or review.get("explicit_user_confirmation") is not True
            or _SHA256.fullmatch(str(record_sha256 or "")) is None
            or _sha256(_canonical_json(body)) != record_sha256
        ):
            raise ProviderVisualEvidenceError(
                "privacy_review_record_invalid"
            )
        approved = review.get("approved")
        records = review.get("verification_records")
        if (
            not isinstance(approved, bool)
            or not isinstance(records, list)
            or (
                approved
                and (
                    review.get("face_and_plate_redaction_adequate")
                    is not True
                    or len(records) != len(manifest["attachments"])
                )
            )
            or (
                not approved
                and (
                    review.get("face_and_plate_redaction_adequate")
                    is not False
                    or records
                )
            )
        ):
            raise ProviderVisualEvidenceError(
                "privacy_review_record_invalid"
            )
        return review

    def _public_snapshot(
        self,
        root: Path,
        manifest: Mapping[str, Any],
        manifest_sha256: str,
    ) -> Mapping[str, Any]:
        review = self._load_review(root, manifest, manifest_sha256)
        if review is None:
            state = "awaiting_deidentification_review"
            review_record_sha256 = None
        elif review["approved"]:
            state = "verified_for_provider_transmission"
            review_record_sha256 = review["record_sha256"]
        else:
            state = "rejected_by_privacy_review"
            review_record_sha256 = review["record_sha256"]
        attachments = [
            {
                "order": row["order"],
                "role": row["role"],
                "source_ref": row["source_ref"],
                "source_sha256": row["source_sha256"],
                "output_sha256": row["output_sha256"],
                "media_type": row["media_type"],
                "size_bytes": row["size_bytes"],
                "width": row["width"],
                "height": row["height"],
                "face_detection_count": row[
                    "face_detection_count"
                ],
                "plate_detection_count": row[
                    "plate_detection_count"
                ],
            }
            for row in manifest["attachments"]
        ]
        snapshot = {
            "schema_version": PREPARATION_SCHEMA_VERSION,
            "project_id": manifest["project_id"],
            "pipeline_job_id": manifest["pipeline_job_id"],
            "analysis_job_id": manifest["analysis_job_id"],
            "candidate_id": manifest["candidate_id"],
            "preparation_id": manifest["preparation_id"],
            "input_revision": manifest["input_revision"],
            "state": state,
            "manifest_sha256": manifest_sha256,
            "engine_identity_sha256": manifest[
                "engine_identity_sha256"
            ],
            "attachment_count": len(attachments),
            "attachments": attachments,
            "automatic_redaction_performed": manifest[
                "automatic_redaction_performed"
            ],
            "automatic_privacy_verification": False,
            "human_verified": state
            == "verified_for_provider_transmission",
            "review_required": state
            == "awaiting_deidentification_review",
            "review_record_sha256": review_record_sha256,
            "path_exposed": False,
        }
        snapshot["preparation_revision"] = _sha256(
            _canonical_json(snapshot)
        )
        return snapshot

    def get(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        candidate_id: str,
        preparation_id: str,
    ) -> Mapping[str, Any]:
        root = self._locate(
            project_id,
            pipeline_job_id,
            candidate_id,
            preparation_id,
        )
        manifest, manifest_sha256 = self._load_manifest(root)
        return self._public_snapshot(
            root,
            manifest,
            manifest_sha256,
        )

    def resolve_review_artifact(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        candidate_id: str,
        preparation_id: str,
        order: int,
    ) -> ResolvedProviderVisualEvidenceArtifact:
        if (
            isinstance(order, bool)
            or not isinstance(order, int)
            or not 0 <= order <= 5
        ):
            raise ProviderVisualEvidenceError(
                "visual_evidence_artifact_not_found"
            )
        root = self._locate(
            project_id,
            pipeline_job_id,
            candidate_id,
            preparation_id,
        )
        manifest, _manifest_sha256 = self._load_manifest(root)
        attachments = manifest["attachments"]
        if order >= len(attachments):
            raise ProviderVisualEvidenceError(
                "visual_evidence_artifact_not_found"
            )
        row = attachments[order]
        path = self._verify_output_file(root, row)
        return ResolvedProviderVisualEvidenceArtifact(
            path=path,
            media_type="image/png",
            size_bytes=int(row["size_bytes"]),
            sha256=str(row["output_sha256"]),
        )

    def resolve_verified_attachments(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        candidate_id: str,
        preparation_id: str,
        expected_manifest_sha256: str,
    ) -> Tuple[VerifiedVisualAttachment, ...]:
        root = self._locate(
            project_id,
            pipeline_job_id,
            candidate_id,
            preparation_id,
        )
        manifest, manifest_sha256 = self._load_manifest(root)
        if manifest_sha256 != expected_manifest_sha256:
            raise ProviderVisualEvidenceError(
                "visual_evidence_preparation_changed"
            )
        review = self._load_review(root, manifest, manifest_sha256)
        if review is None or review.get("approved") is not True:
            raise ProviderVisualEvidenceError(
                "awaiting_deidentification_review"
            )
        records = review["verification_records"]
        result = []
        for row, verification_record in zip(
            manifest["attachments"],
            records,
        ):
            if (
                verification_record.get("source_ref")
                != row["source_ref"]
                or verification_record.get("source_sha256")
                != row["source_sha256"]
                or verification_record.get("output_sha256")
                != row["output_sha256"]
            ):
                raise ProviderVisualEvidenceError(
                    "privacy_review_record_invalid"
                )
            path = self._verify_output_file(root, row)
            result.append(
                VerifiedVisualAttachment(
                    path=path,
                    source_ref=str(row["source_ref"]),
                    sha256=str(row["output_sha256"]),
                    media_type="image/png",
                    size_bytes=int(row["size_bytes"]),
                    role=str(row["role"]),
                    order=int(row["order"]),
                    verification_record=dict(
                        verification_record
                    ),
                )
            )
        return tuple(result)


__all__ = [
    "OpenCvPrivacyRedactor",
    "PREPARATION_SCHEMA_VERSION",
    "PRIVACY_REVIEW_SCHEMA_VERSION",
    "ProviderVisualEvidenceError",
    "ProviderVisualEvidencePreparationService",
    "ResolvedProviderVisualEvidenceArtifact",
    "ResolvedReferenceImage",
]
