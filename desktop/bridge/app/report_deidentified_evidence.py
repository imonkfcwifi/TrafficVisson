"""Report-only, human-reviewed deidentified evidence derivatives.

This module deliberately does not reuse the Provider visual-evidence crop
registry.  Provider crops are minimized model inputs; a report derivative is a
full-frame JPEG that can be inserted into the audited HWPX photo slot.  The
two artifacts have different purposes, retention rules, and approval
boundaries.

Automatic face/license-plate detection is only a redaction *proposal*.  Every
new derivative remains ``awaiting_deidentification_review`` until an explicit
local-user decision is persisted.  Only a hash-verified approved derivative
can be resolved by the exact HWPX writer.
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
import stat
import threading
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from uuid import UUID, uuid4

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

from .current_incident_report_binder import (
    deidentification_record_sha256,
)


REPORT_DEIDENTIFIED_EVIDENCE_SCHEMA = (
    "trafficvisson.report-deidentified-evidence.v1"
)
REPORT_DEIDENTIFIED_REVIEW_SCHEMA = (
    "trafficvisson.report-deidentified-evidence-review.v1"
)
REPORT_ARTIFACT_LOCATION_SCHEMA = (
    "trafficvisson.report-deidentified-evidence-artifact-location.v1"
)
REPORT_ARTIFACT_ROOT_BINDING_SCHEMA = (
    "trafficvisson.report-deidentified-evidence-artifact-root-binding.v1"
)
REPORT_PRIVACY_DETECTOR_SCHEMA = (
    "trafficvisson.report-privacy-detector.v1"
)

MAX_IMAGE_SOURCE_BYTES = 96 * 1024 * 1024
MAX_VIDEO_SOURCE_BYTES = 64 * 1024 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_SIDE = 4_096
MAX_OUTPUT_PIXELS = 16_000_000
MAX_RECORD_BYTES = 1024 * 1024
MAX_DERIVATIVES_PER_INCIDENT = 8

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_DERIVATIVE_ID = re.compile(r"^rde_[a-f0-9]{32}$")
_DEIDENTIFIED_REF = re.compile(
    r"^report-deid:([a-f0-9]{32}):([a-f0-9]{32})$"
)
_OPAQUE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_OUTPUT_FILE_NAME = "deidentified.jpg"
_ARTIFACT_LOCATION_SUFFIX = ".artifact-location.json"
_ARTIFACT_LOCATION_FILE = re.compile(
    r"^(rde_[a-f0-9]{32})\.artifact-location\.json$"
)
_ARTIFACT_ROOT_BINDING_FILE = (
    ".trafficvisson-report-deidentified-root.json"
)
_ARTIFACT_CATEGORY = "report-deidentified-evidence"

JobArtifactRootResolver = Callable[[UUID, UUID, UUID], Path]


class ReportDeidentifiedEvidenceError(RuntimeError):
    """Stable error code for the report evidence trust boundary."""

    def __init__(
        self,
        code: str,
        message: str = "Report deidentification failed",
    ) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{2,79}", code) is None:
            raise ValueError("report deidentification error code is invalid")
        super().__init__(message)
        self.code = code


class ReportPrivacyDetector(Protocol):
    def identity(self) -> Mapping[str, Any]:
        ...

    def detect(
        self,
        image: Image.Image,
    ) -> Sequence[Mapping[str, Any]]:
        ...


@dataclass(frozen=True)
class ReportEvidenceSource:
    project_id: UUID
    pipeline_job_id: UUID
    analysis_job_id: UUID
    incident_id: str
    candidate_id: str
    source_sha256: str
    original_ref: str
    path: Path
    original_sha256: str
    size_bytes: int
    media_type: str
    time_ms: int = 0


@dataclass(frozen=True)
class ResolvedReportDeidentifiedArtifact:
    path: Path
    media_type: str
    size_bytes: int
    sha256: str
    deidentified_ref: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _sha256_value(value: Any) -> str:
    return _sha256(_canonical_json(value))


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(
        attributes
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _secure_directory(
    path: Path,
    *,
    parent: Optional[Path] = None,
    create: bool = True,
) -> Path:
    try:
        if create:
            path.mkdir(mode=0o700, parents=parent is None, exist_ok=True)
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ReportDeidentifiedEvidenceError(
            "report_deidentification_storage_unavailable"
        ) from error
    if (
        not resolved.is_dir()
        or path.is_symlink()
        or _is_reparse_point(path)
        or (parent is not None and resolved.parent != parent)
    ):
        raise ReportDeidentifiedEvidenceError(
            "report_deidentification_storage_unsafe"
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
        raise ReportDeidentifiedEvidenceError(
            "report_deidentification_storage_unavailable"
        ) from error
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _write_json_record(path: Path, value: Mapping[str, Any]) -> str:
    raw = _canonical_json(value)
    digest = _sha256(raw)
    _write_atomic(path, raw)
    _write_atomic(
        path.with_suffix(path.suffix + ".sha256"),
        f"{digest}\n".encode("ascii"),
    )
    return digest


def _read_json_record(path: Path) -> dict[str, Any]:
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
        raise ReportDeidentifiedEvidenceError(
            "report_deidentification_record_unavailable"
        ) from error
    if (
        not 1 <= len(raw) <= MAX_RECORD_BYTES
        or _SHA256.fullmatch(expected or "") is None
        or _sha256(raw) != expected
    ):
        raise ReportDeidentifiedEvidenceError(
            "report_deidentification_record_invalid"
        )

    def unique_pairs(rows: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in rows:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw,
            object_pairs_hook=unique_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite JSON number")
            ),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise ReportDeidentifiedEvidenceError(
            "report_deidentification_record_invalid"
        ) from error
    if not isinstance(value, dict):
        raise ReportDeidentifiedEvidenceError(
            "report_deidentification_record_invalid"
        )
    return value


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
        raise ReportDeidentifiedEvidenceError(
            "report_deidentification_artifact_root_unsafe"
        ) from error
    return resolved


def _existing_child_directory(path: Path, *, parent: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ReportDeidentifiedEvidenceError(
            "report_deidentification_not_found"
        ) from error
    if (
        resolved.parent != parent
        or not resolved.is_dir()
        or path.is_symlink()
        or _is_reparse_point(path)
    ):
        raise ReportDeidentifiedEvidenceError(
            "report_deidentification_artifact_root_unsafe"
        )
    return resolved


def _canonical_uuid(value: Any, label: str) -> str:
    try:
        result = str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as error:
        raise ReportDeidentifiedEvidenceError(
            "report_deidentification_identity_invalid",
            f"{label} is invalid",
        ) from error
    if str(value) != result:
        raise ReportDeidentifiedEvidenceError(
            "report_deidentification_identity_invalid",
            f"{label} is not canonical",
        )
    return result


def _validate_source(source: ReportEvidenceSource) -> None:
    if not isinstance(source, ReportEvidenceSource):
        raise ReportDeidentifiedEvidenceError(
            "report_deidentification_source_invalid"
        )
    for value, label in (
        (source.project_id, "project_id"),
        (source.pipeline_job_id, "pipeline_job_id"),
        (source.analysis_job_id, "analysis_job_id"),
    ):
        if not isinstance(value, UUID):
            raise ReportDeidentifiedEvidenceError(
                "report_deidentification_identity_invalid",
                f"{label} is invalid",
            )
    try:
        incident_id = str(UUID(source.incident_id))
    except (TypeError, ValueError, AttributeError) as error:
        raise ReportDeidentifiedEvidenceError(
            "report_deidentification_identity_invalid"
        ) from error
    if (
        incident_id != source.incident_id
        or _CANDIDATE_ID.fullmatch(source.candidate_id or "") is None
        or _SHA256.fullmatch(source.source_sha256 or "") is None
        or _SHA256.fullmatch(source.original_sha256 or "") is None
        or _OPAQUE_REF.fullmatch(source.original_ref or "") is None
        or source.original_ref
        != f"analysis:{source.candidate_id}:representative"
        or not isinstance(source.path, Path)
        or not source.path.is_absolute()
        or isinstance(source.size_bytes, bool)
        or not isinstance(source.size_bytes, int)
        or source.size_bytes < 1
        or source.media_type not in {"image/jpeg", "video/mp4"}
        or isinstance(source.time_ms, bool)
        or not isinstance(source.time_ms, int)
        or source.time_ms < 0
    ):
        raise ReportDeidentifiedEvidenceError(
            "report_deidentification_source_invalid"
        )


def _verified_file_digest(
    path: Path,
    *,
    expected_sha256: str,
    expected_size: int,
    maximum_size: int,
    capture: bool,
) -> Optional[bytes]:
    try:
        before = path.lstat()
        if (
            path.is_symlink()
            or _is_reparse_point(path)
            or not stat.S_ISREG(before.st_mode)
            or before.st_size != expected_size
            or not 1 <= expected_size <= maximum_size
        ):
            raise OSError
        digest = hashlib.sha256()
        captured = bytearray() if capture else None
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                if captured is not None:
                    captured.extend(chunk)
            opened_after = os.fstat(stream.fileno())
        after = path.lstat()
    except OSError as error:
        raise ReportDeidentifiedEvidenceError(
            "report_deidentification_source_unavailable"
        ) from error
    identity_changed = (
        getattr(opened, "st_dev", None) != getattr(before, "st_dev", None)
        or getattr(opened, "st_ino", None)
        != getattr(before, "st_ino", None)
        or getattr(opened_after, "st_dev", None)
        != getattr(opened, "st_dev", None)
        or getattr(opened_after, "st_ino", None)
        != getattr(opened, "st_ino", None)
        or getattr(after, "st_dev", None)
        != getattr(before, "st_dev", None)
        or getattr(after, "st_ino", None)
        != getattr(before, "st_ino", None)
        or opened.st_size != expected_size
        or opened_after.st_size != expected_size
        or after.st_size != expected_size
        or before.st_mtime_ns != opened_after.st_mtime_ns
        or before.st_mtime_ns != after.st_mtime_ns
    )
    if identity_changed or digest.hexdigest() != expected_sha256:
        raise ReportDeidentifiedEvidenceError(
            "report_deidentification_source_changed"
        )
    return bytes(captured) if captured is not None else None


def _load_source_image(source: ReportEvidenceSource) -> Image.Image:
    _validate_source(source)
    if source.media_type == "image/jpeg":
        raw = _verified_file_digest(
            source.path,
            expected_sha256=source.original_sha256,
            expected_size=source.size_bytes,
            maximum_size=MAX_IMAGE_SOURCE_BYTES,
            capture=True,
        )
        assert raw is not None
        if not raw.startswith(b"\xff\xd8\xff"):
            raise ReportDeidentifiedEvidenceError(
                "report_deidentification_source_invalid"
            )
        try:
            with Image.open(io.BytesIO(raw)) as opened:
                if opened.format != "JPEG":
                    raise ReportDeidentifiedEvidenceError(
                        "report_deidentification_source_invalid"
                    )
                width, height = opened.size
                if (
                    width < 16
                    or height < 16
                    or width > 16_384
                    or height > 16_384
                    or width * height > 100_000_000
                ):
                    raise ReportDeidentifiedEvidenceError(
                        "report_deidentification_source_invalid"
                    )
                opened.load()
                return ImageOps.exif_transpose(opened).convert("RGB")
        except ReportDeidentifiedEvidenceError:
            raise
        except (OSError, ValueError, UnidentifiedImageError) as error:
            raise ReportDeidentifiedEvidenceError(
                "report_deidentification_source_invalid"
            ) from error

    _verified_file_digest(
        source.path,
        expected_sha256=source.original_sha256,
        expected_size=source.size_bytes,
        maximum_size=MAX_VIDEO_SOURCE_BYTES,
        capture=False,
    )
    try:
        with source.path.open("rb") as stream:
            header = stream.read(32)
        if len(header) < 12 or header[4:8] != b"ftyp":
            raise OSError
        import cv2

        capture = cv2.VideoCapture(str(source.path))
        try:
            if not capture.isOpened():
                raise OSError
            capture.set(cv2.CAP_PROP_POS_MSEC, float(source.time_ms))
            success, frame = capture.read()
        finally:
            capture.release()
        if not success or frame is None:
            raise OSError
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb).convert("RGB")
    except (
        ImportError,
        OSError,
        ValueError,
        AttributeError,
    ) as error:
        raise ReportDeidentifiedEvidenceError(
            "report_video_frame_extraction_unavailable"
        ) from error
    # The video may be large, but the second digest is intentional: OpenCV
    # accepts only a path, so a path swap during frame extraction must fail.
    _verified_file_digest(
        source.path,
        expected_sha256=source.original_sha256,
        expected_size=source.size_bytes,
        maximum_size=MAX_VIDEO_SOURCE_BYTES,
        capture=False,
    )
    return image


def _cascade_sha256(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ReportDeidentifiedEvidenceError(
            "report_privacy_detector_unavailable"
        ) from error
    if not raw or len(raw) > 8 * 1024 * 1024:
        raise ReportDeidentifiedEvidenceError(
            "report_privacy_detector_unavailable"
        )
    return _sha256(raw)


def _merge_regions(
    regions: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    normalized: list[dict[str, Any]] = []
    for raw in regions:
        if not isinstance(raw, Mapping):
            continue
        kind = raw.get("kind")
        values = tuple(raw.get(key) for key in ("x", "y", "width", "height"))
        if (
            kind not in {"face", "license_plate"}
            or any(isinstance(value, bool) or not isinstance(value, int) for value in values)
        ):
            continue
        x, y, width, height = values
        if x < 0 or y < 0 or width < 1 or height < 1:
            continue
        candidate = {
            "kind": kind,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
        }
        duplicate = False
        for existing in normalized:
            if existing["kind"] != kind:
                continue
            left = max(x, existing["x"])
            top = max(y, existing["y"])
            right = min(x + width, existing["x"] + existing["width"])
            bottom = min(y + height, existing["y"] + existing["height"])
            intersection = max(0, right - left) * max(0, bottom - top)
            minimum = min(
                width * height,
                existing["width"] * existing["height"],
            )
            if minimum and intersection / minimum >= 0.55:
                duplicate = True
                break
        if not duplicate:
            normalized.append(candidate)
    normalized.sort(
        key=lambda item: (
            item["kind"],
            item["y"],
            item["x"],
            item["width"],
            item["height"],
        )
    )
    return tuple(normalized)


class OpenCvReportPrivacyDetector:
    """Local OpenCV proposal detector with exact model provenance."""

    def __init__(
        self,
        *,
        injected_regions: Optional[
            Callable[[Image.Image], Sequence[Mapping[str, Any]]]
        ] = None,
    ) -> None:
        self._injected_regions = injected_regions
        self._loaded = False
        self._identity: Optional[dict[str, Any]] = None
        self._cv2 = None
        self._faces: tuple[Any, ...] = ()
        self._plates: tuple[Any, ...] = ()

    def _load(self) -> None:
        if self._loaded:
            return
        if self._injected_regions is not None:
            model_sha256 = _sha256(
                b"trafficvisson-test-report-privacy-detector-v1"
            )
            self._identity = {
                "schema_version": REPORT_PRIVACY_DETECTOR_SCHEMA,
                "detector_name": "test_injected_report_privacy_detector",
                "detector_version": "1",
                "model_id": "test-report-face-plate-regions",
                "model_sha256": model_sha256,
                "coverage_guaranteed": False,
                "human_review_required": True,
                "known_limitations": ["test_detector"],
            }
            self._loaded = True
            return
        try:
            import cv2

            cascade_root = Path(cv2.data.haarcascades).resolve(strict=True)
            face_paths = (
                cascade_root / "haarcascade_frontalface_default.xml",
                cascade_root / "haarcascade_profileface.xml",
            )
            plate_paths = tuple(
                path
                for path in (
                    cascade_root
                    / "haarcascade_license_plate_rus_16stages.xml",
                    cascade_root / "haarcascade_russian_plate_number.xml",
                )
                if path.is_file()
            )
            if (
                len(plate_paths) < 1
                or any(
                    not path.is_file()
                    or path.is_symlink()
                    or _is_reparse_point(path)
                    or path.parent != cascade_root
                    for path in (*face_paths, *plate_paths)
                )
            ):
                raise OSError
            faces = tuple(
                cv2.CascadeClassifier(str(path)) for path in face_paths
            )
            plates = tuple(
                cv2.CascadeClassifier(str(path)) for path in plate_paths
            )
            if any(item.empty() for item in (*faces, *plates)):
                raise OSError
            face_hashes = [_cascade_sha256(path) for path in face_paths]
            plate_hashes = [_cascade_sha256(path) for path in plate_paths]
            model_sha256 = _sha256_value(
                {
                    "face_model_sha256s": face_hashes,
                    "plate_model_sha256s": plate_hashes,
                }
            )
        except (ImportError, OSError, AttributeError) as error:
            raise ReportDeidentifiedEvidenceError(
                "report_privacy_detector_unavailable"
            ) from error
        self._cv2 = cv2
        self._faces = faces
        self._plates = plates
        self._identity = {
            "schema_version": REPORT_PRIVACY_DETECTOR_SCHEMA,
            "detector_name": "opencv_haar_report_privacy_proposal",
            "detector_version": str(cv2.__version__),
            "model_id": "opencv-haar-face-license-plate-cascade-set",
            "model_sha256": model_sha256,
            "coverage_guaranteed": False,
            "human_review_required": True,
            "known_limitations": [
                "plate_cascades_are_not_a_korean_plate_guarantee",
                "small_occluded_or_profile_faces_may_be_missed",
                "zero_detected_regions_is_not_proof_of_absence",
            ],
        }
        self._loaded = True

    def identity(self) -> Mapping[str, Any]:
        self._load()
        assert self._identity is not None
        return json.loads(_canonical_json(self._identity).decode("utf-8"))

    def detect(
        self,
        image: Image.Image,
    ) -> Sequence[Mapping[str, Any]]:
        self._load()
        if self._injected_regions is not None:
            return _merge_regions(self._injected_regions(image))
        if self._cv2 is None:
            raise ReportDeidentifiedEvidenceError(
                "report_privacy_detector_unavailable"
            )
        try:
            import numpy

            rgb = numpy.asarray(image.convert("RGB"))
            gray = self._cv2.cvtColor(rgb, self._cv2.COLOR_RGB2GRAY)
            gray = self._cv2.equalizeHist(gray)
            rows: list[dict[str, Any]] = []
            for cascade in self._faces:
                for x, y, width, height in cascade.detectMultiScale(
                    gray,
                    scaleFactor=1.1,
                    minNeighbors=4,
                    minSize=(18, 18),
                ):
                    rows.append(
                        {
                            "kind": "face",
                            "x": int(x),
                            "y": int(y),
                            "width": int(width),
                            "height": int(height),
                        }
                    )
            for cascade in self._plates:
                for x, y, width, height in cascade.detectMultiScale(
                    gray,
                    scaleFactor=1.08,
                    minNeighbors=3,
                    minSize=(24, 8),
                ):
                    rows.append(
                        {
                            "kind": "license_plate",
                            "x": int(x),
                            "y": int(y),
                            "width": int(width),
                            "height": int(height),
                        }
                    )
        except Exception as error:
            raise ReportDeidentifiedEvidenceError(
                "report_privacy_detection_failed"
            ) from error
        return _merge_regions(rows)


def _resize_for_report(image: Image.Image) -> Image.Image:
    width, height = image.size
    scale = min(
        1.0,
        MAX_OUTPUT_SIDE / max(width, height),
        math.sqrt(MAX_OUTPUT_PIXELS / (width * height)),
    )
    if scale < 1.0:
        return image.resize(
            (
                max(16, int(width * scale)),
                max(16, int(height * scale)),
            ),
            Image.Resampling.LANCZOS,
        )
    return image


def _scaled_regions(
    regions: Sequence[Mapping[str, Any]],
    *,
    source_size: tuple[int, int],
    output_size: tuple[int, int],
) -> tuple[dict[str, Any], ...]:
    source_width, source_height = source_size
    output_width, output_height = output_size
    scale_x = output_width / source_width
    scale_y = output_height / source_height
    result: list[dict[str, Any]] = []
    for row in regions:
        x = int(round(int(row["x"]) * scale_x))
        y = int(round(int(row["y"]) * scale_y))
        width = max(1, int(round(int(row["width"]) * scale_x)))
        height = max(1, int(round(int(row["height"]) * scale_y)))
        padding_x = max(2, int(round(width * 0.12)))
        padding_y = max(2, int(round(height * 0.18)))
        left = max(0, x - padding_x)
        top = max(0, y - padding_y)
        right = min(output_width, x + width + padding_x)
        bottom = min(output_height, y + height + padding_y)
        if right > left and bottom > top:
            result.append(
                {
                    "kind": row["kind"],
                    "x": left,
                    "y": top,
                    "width": right - left,
                    "height": bottom - top,
                }
            )
    return _merge_regions(result)


def _render_derivative(
    image: Image.Image,
    regions: Sequence[Mapping[str, Any]],
    *,
    original_sha256: str,
) -> tuple[bytes, tuple[dict[str, Any], ...], int, int]:
    source_size = image.size
    output = _resize_for_report(image.convert("RGB"))
    scaled = _scaled_regions(
        regions,
        source_size=source_size,
        output_size=output.size,
    )
    draw = ImageDraw.Draw(output)
    for row in scaled:
        left = int(row["x"])
        top = int(row["y"])
        right = left + int(row["width"])
        bottom = top + int(row["height"])
        # Opaque coverage is used instead of a reversible weak blur.  The
        # human review still remains mandatory because detector recall is not
        # guaranteed.
        draw.rectangle((left, top, right, bottom), fill=(18, 18, 18))
    for quality in (92, 88, 82, 75):
        encoded = io.BytesIO()
        output.save(
            encoded,
            format="JPEG",
            quality=quality,
            optimize=True,
            progressive=True,
            subsampling=0,
        )
        raw = encoded.getvalue()
        if (
            1 <= len(raw) <= MAX_OUTPUT_BYTES
            and raw.startswith(b"\xff\xd8\xff")
            and _sha256(raw) != original_sha256
        ):
            return raw, scaled, output.width, output.height
    raise ReportDeidentifiedEvidenceError(
        "report_deidentification_output_invalid"
    )


class ReportDeidentifiedEvidenceService:
    """Durable report derivative registry with explicit human approval.

    ``artifact_root_resolver`` must return the already-authorized per-job
    ``report-deidentified-evidence`` category root for the supplied
    project/pipeline/analysis scope.  ``prepare(job_artifact_root=...)`` has
    the same category-root meaning.  The service adds only
    ``incident_id/derivative_id`` below it and never exposes that path.
    """

    def __init__(
        self,
        data_root: Path,
        *,
        detector: Optional[ReportPrivacyDetector] = None,
        artifact_root_resolver: Optional[
            JobArtifactRootResolver
        ] = None,
    ) -> None:
        root = Path(data_root)
        if not root.is_absolute():
            raise ValueError(
                "report-deidentified-evidence root must be absolute"
            )
        self._root = _secure_directory(root)
        self._detector = detector or OpenCvReportPrivacyDetector()
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

    @staticmethod
    def _artifact_root_binding(
        *,
        category_root: Path,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
    ) -> dict[str, Any]:
        root_identity_sha256 = _sha256_value(
            {
                "category": _ARTIFACT_CATEGORY,
                "normalized_absolute_path": (
                    _normalized_absolute_path(category_root)
                ),
            }
        )
        body = {
            "schema_version": (
                REPORT_ARTIFACT_ROOT_BINDING_SCHEMA
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
            "record_sha256": _sha256_value(body),
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
        _canonical_uuid(project_id, "project_id")
        _canonical_uuid(pipeline_job_id, "pipeline_job_id")
        _canonical_uuid(analysis_job_id, "analysis_job_id")
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
            except ReportDeidentifiedEvidenceError:
                raise
            except Exception as error:
                raise ReportDeidentifiedEvidenceError(
                    "report_deidentification_artifact_root_unavailable"
                ) from error
        if (
            resolved_explicit is not None
            and resolved_callback is not None
            and resolved_explicit != resolved_callback
        ):
            raise ReportDeidentifiedEvidenceError(
                "report_deidentification_artifact_root_conflict"
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
            if _read_json_record(binding_path) != binding:
                raise ReportDeidentifiedEvidenceError(
                    "report_deidentification_artifact_root_conflict"
                )
        elif initialize_binding:
            _write_json_record(binding_path, binding)
        else:
            raise ReportDeidentifiedEvidenceError(
                "report_deidentification_artifact_root_unavailable"
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
            raise ReportDeidentifiedEvidenceError(
                "report_deidentification_artifact_root_conflict"
            )
        self._artifact_root_cache[key] = (resolved, identity)
        return resolved, identity

    @staticmethod
    def _artifact_location_path(
        incident_root: Path,
        derivative_id: str,
    ) -> Path:
        return incident_root / (
            f"{derivative_id}{_ARTIFACT_LOCATION_SUFFIX}"
        )

    def _write_artifact_location(
        self,
        *,
        incident_root: Path,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        incident_id: str,
        derivative_id: str,
        input_revision: str,
        manifest_sha256: str,
        category_root_identity_sha256: str,
    ) -> None:
        body = {
            "schema_version": REPORT_ARTIFACT_LOCATION_SCHEMA,
            "category": _ARTIFACT_CATEGORY,
            "project_id": str(project_id),
            "pipeline_job_id": str(pipeline_job_id),
            "analysis_job_id": str(analysis_job_id),
            "incident_id": incident_id,
            "derivative_id": derivative_id,
            "input_revision": input_revision,
            "relative_artifact_path": (
                f"{incident_id}/{derivative_id}"
            ),
            "category_root_identity_sha256": (
                category_root_identity_sha256
            ),
            "manifest_sha256": manifest_sha256,
        }
        record = {
            **body,
            "record_sha256": _sha256_value(body),
        }
        path = self._artifact_location_path(
            incident_root,
            derivative_id,
        )
        if path.exists():
            if _read_json_record(path) != record:
                raise ReportDeidentifiedEvidenceError(
                    "report_deidentification_artifact_location_conflict"
                )
            return
        _write_json_record(path, record)

    def _load_artifact_location(
        self,
        *,
        incident_root: Path,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        incident_id: str,
        derivative_id: str,
    ) -> tuple[Path, dict[str, Any]]:
        locator = _read_json_record(
            self._artifact_location_path(
                incident_root,
                derivative_id,
            )
        )
        expected_keys = {
            "schema_version",
            "category",
            "project_id",
            "pipeline_job_id",
            "analysis_job_id",
            "incident_id",
            "derivative_id",
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
        if (
            set(locator) != expected_keys
            or locator.get("schema_version")
            != REPORT_ARTIFACT_LOCATION_SCHEMA
            or locator.get("category") != _ARTIFACT_CATEGORY
            or locator.get("project_id") != str(project_id)
            or locator.get("pipeline_job_id")
            != str(pipeline_job_id)
            or locator.get("analysis_job_id")
            != str(analysis_job_id)
            or locator.get("incident_id") != incident_id
            or locator.get("derivative_id") != derivative_id
            or locator.get("relative_artifact_path")
            != f"{incident_id}/{derivative_id}"
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
            or _sha256_value(body) != record_sha256
        ):
            raise ReportDeidentifiedEvidenceError(
                "report_deidentification_artifact_location_invalid"
            )
        resolved = self._resolve_job_artifact_root(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=analysis_job_id,
            explicit_root=None,
            initialize_binding=False,
        )
        if resolved is None:
            raise ReportDeidentifiedEvidenceError(
                "report_deidentification_artifact_root_unavailable"
            )
        artifact_root, identity = resolved
        if (
            identity
            != locator["category_root_identity_sha256"]
        ):
            raise ReportDeidentifiedEvidenceError(
                "report_deidentification_artifact_root_changed"
            )
        external_incident = _existing_child_directory(
            artifact_root / incident_id,
            parent=artifact_root,
        )
        external_derivative = _existing_child_directory(
            external_incident / derivative_id,
            parent=external_incident,
        )
        return external_derivative, locator

    def _assert_scope_artifact_root_binding(
        self,
        *,
        scope_ledger_root: Path,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        category_root_identity_sha256: str,
    ) -> None:
        for incident_root in scope_ledger_root.iterdir():
            if not incident_root.is_dir():
                continue
            try:
                incident_id = str(UUID(incident_root.name))
            except (ValueError, TypeError, AttributeError) as error:
                raise ReportDeidentifiedEvidenceError(
                    "report_deidentification_artifact_location_invalid"
                ) from error
            if (
                incident_id != incident_root.name
                or incident_root.is_symlink()
                or _is_reparse_point(incident_root)
            ):
                raise ReportDeidentifiedEvidenceError(
                    "report_deidentification_artifact_location_invalid"
                )
            for path in incident_root.iterdir():
                match = _ARTIFACT_LOCATION_FILE.fullmatch(
                    path.name
                )
                if match is None:
                    continue
                locator = _read_json_record(path)
                if (
                    locator.get("schema_version")
                    != REPORT_ARTIFACT_LOCATION_SCHEMA
                    or locator.get("category")
                    != _ARTIFACT_CATEGORY
                    or locator.get("project_id")
                    != str(project_id)
                    or locator.get("pipeline_job_id")
                    != str(pipeline_job_id)
                    or locator.get("analysis_job_id")
                    != str(analysis_job_id)
                    or locator.get("incident_id") != incident_id
                    or locator.get("derivative_id")
                    != match.group(1)
                    or locator.get(
                        "category_root_identity_sha256"
                    )
                    != category_root_identity_sha256
                ):
                    raise ReportDeidentifiedEvidenceError(
                        "report_deidentification_artifact_root_changed"
                    )

    def _scope_root(
        self,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        *,
        create: bool,
    ) -> Path:
        project = _canonical_uuid(project_id, "project_id")
        pipeline = _canonical_uuid(pipeline_job_id, "pipeline_job_id")
        analysis = _canonical_uuid(analysis_job_id, "analysis_job_id")
        current = self._root
        for identity in (project, pipeline, analysis):
            current = _secure_directory(
                current / identity,
                parent=current,
                create=create,
            )
        return current

    def _incident_root(
        self,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        incident_id: str,
        *,
        create: bool,
    ) -> Path:
        canonical = _canonical_uuid(incident_id, "incident_id")
        scope = self._scope_root(
            project_id,
            pipeline_job_id,
            analysis_job_id,
            create=create,
        )
        return _secure_directory(
            scope / canonical,
            parent=scope,
            create=create,
        )

    @staticmethod
    def _derivative_ref(incident_id: str, derivative_id: str) -> str:
        if _DERIVATIVE_ID.fullmatch(derivative_id or "") is None:
            raise ReportDeidentifiedEvidenceError(
                "report_deidentification_identity_invalid"
            )
        return (
            f"report-deid:{UUID(incident_id).hex}:"
            f"{derivative_id.removeprefix('rde_')}"
        )

    def _locate(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        incident_id: str,
        derivative_id: str,
    ) -> Path:
        if _DERIVATIVE_ID.fullmatch(derivative_id or "") is None:
            raise ReportDeidentifiedEvidenceError(
                "report_deidentification_identity_invalid"
            )
        incident_root = self._incident_root(
            project_id,
            pipeline_job_id,
            analysis_job_id,
            incident_id,
            create=False,
        )
        root = incident_root / derivative_id
        location_path = self._artifact_location_path(
            incident_root,
            derivative_id,
        )
        if location_path.exists():
            resolved, locator = self._load_artifact_location(
                incident_root=incident_root,
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                analysis_job_id=analysis_job_id,
                incident_id=incident_id,
                derivative_id=derivative_id,
            )
            manifest, manifest_sha256 = self._load_manifest(
                resolved,
                expected_scope=(
                    project_id,
                    pipeline_job_id,
                    analysis_job_id,
                    incident_id,
                    derivative_id,
                ),
            )
            if (
                manifest_sha256 != locator["manifest_sha256"]
                or manifest.get("input_revision")
                != locator["input_revision"]
                or not isinstance(
                    manifest.get("artifact_storage"),
                    Mapping,
                )
                or manifest["artifact_storage"].get(
                    "category_root_identity_sha256"
                )
                != locator["category_root_identity_sha256"]
            ):
                raise ReportDeidentifiedEvidenceError(
                    "report_deidentification_artifact_location_invalid"
                )
            return resolved
        try:
            resolved = root.resolve(strict=True)
        except OSError as error:
            raise ReportDeidentifiedEvidenceError(
                "report_deidentification_not_found"
            ) from error
        if (
            resolved.parent != incident_root
            or not resolved.is_dir()
            or root.is_symlink()
            or _is_reparse_point(root)
        ):
            raise ReportDeidentifiedEvidenceError(
                "report_deidentification_identity_invalid"
            )
        return resolved

    def _verify_output(
        self,
        root: Path,
        manifest: Mapping[str, Any],
    ) -> Path:
        path = root / _OUTPUT_FILE_NAME
        try:
            before = path.lstat()
            raw = path.read_bytes()
            after = path.lstat()
        except OSError as error:
            raise ReportDeidentifiedEvidenceError(
                "report_deidentification_output_unavailable"
            ) from error
        if (
            path.resolve(strict=True).parent != root
            or path.is_symlink()
            or _is_reparse_point(path)
            or not stat.S_ISREG(before.st_mode)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or len(raw) != manifest.get("deidentified_size_bytes")
            or not 1 <= len(raw) <= MAX_OUTPUT_BYTES
            or _sha256(raw) != manifest.get("deidentified_sha256")
            or not raw.startswith(b"\xff\xd8\xff")
        ):
            raise ReportDeidentifiedEvidenceError(
                "report_deidentification_output_changed"
            )
        try:
            with Image.open(io.BytesIO(raw)) as image:
                if (
                    image.format != "JPEG"
                    or image.size
                    != (
                        manifest.get("width"),
                        manifest.get("height"),
                    )
                ):
                    raise OSError
                image.verify()
        except (OSError, ValueError, UnidentifiedImageError) as error:
            raise ReportDeidentifiedEvidenceError(
                "report_deidentification_output_invalid"
            ) from error
        return path.resolve(strict=True)

    def _load_manifest(
        self,
        root: Path,
        *,
        expected_scope: Optional[
            tuple[UUID, UUID, UUID, str, str]
        ] = None,
    ) -> tuple[dict[str, Any], str]:
        manifest = _read_json_record(root / "manifest.json")
        expected_keys = {
            "schema_version",
            "project_id",
            "pipeline_job_id",
            "analysis_job_id",
            "source_sha256",
            "incident_id",
            "candidate_id",
            "derivative_id",
            "deidentified_ref",
            "input_revision",
            "original_ref",
            "original_path",
            "original_sha256",
            "original_size_bytes",
            "original_media_type",
            "source_time_ms",
            "deidentified_file_name",
            "deidentified_sha256",
            "deidentified_size_bytes",
            "media_type",
            "width",
            "height",
            "detector",
            "detector_identity_sha256",
            "redaction_regions",
            "redaction_region_count",
            "face_detection_count",
            "plate_detection_count",
            "automatic_redaction_performed",
            "automatic_privacy_verification",
            "created_at",
        }
        artifact_storage = manifest.get("artifact_storage")
        if (
            set(manifest)
            not in (
                expected_keys,
                expected_keys | {"artifact_storage"},
            )
            or manifest.get("schema_version")
            != REPORT_DEIDENTIFIED_EVIDENCE_SCHEMA
            or manifest.get("derivative_id") != root.name
            or manifest.get("deidentified_ref")
            != self._derivative_ref(
                str(manifest.get("incident_id")),
                str(manifest.get("derivative_id")),
            )
            or manifest.get("deidentified_file_name")
            != _OUTPUT_FILE_NAME
            or manifest.get("media_type") != "image/jpeg"
            or manifest.get("automatic_privacy_verification") is not False
            or _SHA256.fullmatch(str(manifest.get("source_sha256") or ""))
            is None
            or _SHA256.fullmatch(str(manifest.get("original_sha256") or ""))
            is None
            or _SHA256.fullmatch(
                str(manifest.get("deidentified_sha256") or "")
            )
            is None
            or manifest.get("original_sha256")
            == manifest.get("deidentified_sha256")
            or _SHA256.fullmatch(str(manifest.get("input_revision") or ""))
            is None
            or _SHA256.fullmatch(
                str(manifest.get("detector_identity_sha256") or "")
            )
            is None
            or not isinstance(manifest.get("original_path"), str)
            or not Path(manifest["original_path"]).is_absolute()
            or not isinstance(manifest.get("redaction_regions"), list)
            or manifest.get("redaction_region_count")
            != len(manifest["redaction_regions"])
            or manifest.get("face_detection_count")
            != sum(
                row.get("kind") == "face"
                for row in manifest["redaction_regions"]
                if isinstance(row, Mapping)
            )
            or manifest.get("plate_detection_count")
            != sum(
                row.get("kind") == "license_plate"
                for row in manifest["redaction_regions"]
                if isinstance(row, Mapping)
            )
            or manifest.get("automatic_redaction_performed")
            is not bool(manifest["redaction_regions"])
            or not isinstance(manifest.get("detector"), Mapping)
            or _sha256_value(manifest["detector"])
            != manifest.get("detector_identity_sha256")
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
            raise ReportDeidentifiedEvidenceError(
                "report_deidentification_record_invalid"
            )
        if expected_scope is not None:
            project_id, pipeline_job_id, analysis_job_id, incident_id, derivative_id = (
                expected_scope
            )
            if (
                manifest.get("project_id") != str(project_id)
                or manifest.get("pipeline_job_id")
                != str(pipeline_job_id)
                or manifest.get("analysis_job_id")
                != str(analysis_job_id)
                or manifest.get("incident_id") != incident_id
                or manifest.get("derivative_id") != derivative_id
            ):
                raise ReportDeidentifiedEvidenceError(
                    "report_deidentification_scope_conflict"
                )
        manifest_sha256 = _sha256_value(manifest)
        self._verify_output(root, manifest)
        return manifest, manifest_sha256

    def _load_review(
        self,
        root: Path,
        manifest: Mapping[str, Any],
        manifest_sha256: str,
    ) -> Optional[dict[str, Any]]:
        path = root / "review.json"
        if not path.exists():
            return None
        review = _read_json_record(path)
        expected_keys = {
            "schema_version",
            "project_id",
            "pipeline_job_id",
            "analysis_job_id",
            "incident_id",
            "derivative_id",
            "deidentified_ref",
            "manifest_sha256",
            "original_sha256",
            "deidentified_sha256",
            "approved",
            "decision",
            "explicit_user_confirmation",
            "automatic_privacy_verification",
            "reviewer_actor",
            "reviewed_at",
            "review_revision",
            "deidentification_record_sha256",
            "record_sha256",
        }
        record_sha256 = review.get("record_sha256")
        body = {
            key: review[key]
            for key in sorted(review)
            if key != "record_sha256"
        }
        approved = review.get("approved")
        if (
            set(review) != expected_keys
            or review.get("schema_version")
            != REPORT_DEIDENTIFIED_REVIEW_SCHEMA
            or review.get("project_id") != manifest.get("project_id")
            or review.get("pipeline_job_id")
            != manifest.get("pipeline_job_id")
            or review.get("analysis_job_id")
            != manifest.get("analysis_job_id")
            or review.get("incident_id") != manifest.get("incident_id")
            or review.get("derivative_id")
            != manifest.get("derivative_id")
            or review.get("deidentified_ref")
            != manifest.get("deidentified_ref")
            or review.get("manifest_sha256") != manifest_sha256
            or review.get("original_sha256")
            != manifest.get("original_sha256")
            or review.get("deidentified_sha256")
            != manifest.get("deidentified_sha256")
            or not isinstance(approved, bool)
            or review.get("decision")
            != ("approved" if approved else "rejected")
            or review.get("explicit_user_confirmation") is not True
            or review.get("automatic_privacy_verification") is not False
            or review.get("reviewer_actor") != "local_user"
            or review.get("review_revision") != 1
            or _SHA256.fullmatch(str(record_sha256 or "")) is None
            or _sha256_value(body) != record_sha256
        ):
            raise ReportDeidentifiedEvidenceError(
                "report_deidentification_review_invalid"
            )
        expected_deidentification_sha = (
            deidentification_record_sha256(
                incident_id=str(manifest["incident_id"]),
                project_id=UUID(str(manifest["project_id"])),
                pipeline_job_id=UUID(
                    str(manifest["pipeline_job_id"])
                ),
                analysis_job_id=UUID(
                    str(manifest["analysis_job_id"])
                ),
                source_sha256=str(manifest["source_sha256"]),
                original_ref=str(manifest["original_ref"]),
                original_sha256=str(manifest["original_sha256"]),
                deidentified_ref=str(manifest["deidentified_ref"]),
                deidentified_sha256=str(
                    manifest["deidentified_sha256"]
                ),
            )
            if approved
            else None
        )
        if (
            review.get("deidentification_record_sha256")
            != expected_deidentification_sha
        ):
            raise ReportDeidentifiedEvidenceError(
                "report_deidentification_review_invalid"
            )
        return review

    def _verify_manifest_source(
        self,
        manifest: Mapping[str, Any],
    ) -> None:
        path = Path(str(manifest["original_path"]))
        _verified_file_digest(
            path,
            expected_sha256=str(manifest["original_sha256"]),
            expected_size=int(manifest["original_size_bytes"]),
            maximum_size=(
                MAX_IMAGE_SOURCE_BYTES
                if manifest["original_media_type"] == "image/jpeg"
                else MAX_VIDEO_SOURCE_BYTES
            ),
            capture=False,
        )

    def _public_snapshot(
        self,
        root: Path,
        manifest: Mapping[str, Any],
        manifest_sha256: str,
    ) -> Mapping[str, Any]:
        review = self._load_review(root, manifest, manifest_sha256)
        if review is None:
            state = "awaiting_deidentification_review"
            decision = None
            reviewer_actor = None
            reviewed_at = None
            review_revision = 0
            review_record_sha256 = None
            deidentification_sha256 = None
        elif review["approved"]:
            state = "verified_for_shared_report"
            decision = "approved"
            reviewer_actor = "local_user"
            reviewed_at = review["reviewed_at"]
            review_revision = 1
            review_record_sha256 = review["record_sha256"]
            deidentification_sha256 = review[
                "deidentification_record_sha256"
            ]
        else:
            state = "rejected_by_deidentification_review"
            decision = "rejected"
            reviewer_actor = "local_user"
            reviewed_at = review["reviewed_at"]
            review_revision = 1
            review_record_sha256 = review["record_sha256"]
            deidentification_sha256 = None
        detector = manifest["detector"]
        public_detector = {
            "detector_name": detector["detector_name"],
            "detector_version": detector["detector_version"],
            "model_id": detector["model_id"],
            "model_sha256": detector["model_sha256"],
            "coverage_guaranteed": False,
            "human_review_required": True,
        }
        snapshot = {
            "schema_version": REPORT_DEIDENTIFIED_EVIDENCE_SCHEMA,
            "project_id": manifest["project_id"],
            "pipeline_job_id": manifest["pipeline_job_id"],
            "analysis_job_id": manifest["analysis_job_id"],
            "source_sha256": manifest["source_sha256"],
            "incident_id": manifest["incident_id"],
            "candidate_id": manifest["candidate_id"],
            "derivative_id": manifest["derivative_id"],
            "deidentified_ref": manifest["deidentified_ref"],
            "state": state,
            "input_revision": manifest["input_revision"],
            "manifest_sha256": manifest_sha256,
            "original_sha256": manifest["original_sha256"],
            "deidentified_sha256": manifest[
                "deidentified_sha256"
            ],
            "media_type": "image/jpeg",
            "size_bytes": manifest["deidentified_size_bytes"],
            "width": manifest["width"],
            "height": manifest["height"],
            "detector": public_detector,
            "detector_identity_sha256": manifest[
                "detector_identity_sha256"
            ],
            "redaction_regions": [
                dict(row) for row in manifest["redaction_regions"]
            ],
            "redaction_region_count": manifest[
                "redaction_region_count"
            ],
            "face_detection_count": manifest[
                "face_detection_count"
            ],
            "plate_detection_count": manifest[
                "plate_detection_count"
            ],
            "automatic_redaction_performed": manifest[
                "automatic_redaction_performed"
            ],
            "automatic_privacy_verification": False,
            "human_verified": state == "verified_for_shared_report",
            "review_required": state
            == "awaiting_deidentification_review",
            "reviewer_decision": decision,
            "reviewer_actor": reviewer_actor,
            "reviewed_at": reviewed_at,
            "review_revision": review_revision,
            "review_record_sha256": review_record_sha256,
            "deidentification_record_sha256": (
                deidentification_sha256
            ),
            "path_exposed": False,
        }
        snapshot["preparation_revision"] = _sha256_value(snapshot)
        return snapshot

    def prepare(
        self,
        source: ReportEvidenceSource,
        *,
        job_artifact_root: Optional[Path] = None,
    ) -> Mapping[str, Any]:
        _validate_source(source)
        external_storage = self._resolve_job_artifact_root(
            project_id=source.project_id,
            pipeline_job_id=source.pipeline_job_id,
            analysis_job_id=source.analysis_job_id,
            explicit_root=job_artifact_root,
            initialize_binding=True,
        )
        incident_root = self._incident_root(
            source.project_id,
            source.pipeline_job_id,
            source.analysis_job_id,
            source.incident_id,
            create=True,
        )
        with self._lock:
            if external_storage is not None:
                self._assert_scope_artifact_root_binding(
                    scope_ledger_root=incident_root.parent,
                    project_id=source.project_id,
                    pipeline_job_id=source.pipeline_job_id,
                    analysis_job_id=source.analysis_job_id,
                    category_root_identity_sha256=(
                        external_storage[1]
                    ),
                )
            existing_ids = {
                child.name
                for child in incident_root.iterdir()
                if child.is_dir()
                and _DERIVATIVE_ID.fullmatch(child.name)
            }
            existing_ids.update(
                match.group(1)
                for child in incident_root.iterdir()
                if child.is_file()
                and (
                    match := _ARTIFACT_LOCATION_FILE.fullmatch(
                        child.name
                    )
                )
                is not None
            )
            for existing_id in sorted(existing_ids):
                existing_root = self._locate(
                    project_id=source.project_id,
                    pipeline_job_id=source.pipeline_job_id,
                    analysis_job_id=source.analysis_job_id,
                    incident_id=source.incident_id,
                    derivative_id=existing_id,
                )
                manifest, manifest_sha256 = self._load_manifest(
                    existing_root
                )
                if (
                    manifest["project_id"] == str(source.project_id)
                    and manifest["pipeline_job_id"]
                    == str(source.pipeline_job_id)
                    and manifest["analysis_job_id"]
                    == str(source.analysis_job_id)
                    and manifest["incident_id"] == source.incident_id
                    and manifest["candidate_id"] == source.candidate_id
                    and manifest["source_sha256"]
                    == source.source_sha256
                    and manifest["original_ref"] == source.original_ref
                    and manifest["original_sha256"]
                    == source.original_sha256
                    and manifest["original_size_bytes"]
                    == source.size_bytes
                    and manifest["original_media_type"]
                    == source.media_type
                    and manifest["source_time_ms"] == source.time_ms
                ):
                    self._verify_manifest_source(manifest)
                    return self._public_snapshot(
                        existing_root,
                        manifest,
                        manifest_sha256,
                    )
            if len(existing_ids) >= MAX_DERIVATIVES_PER_INCIDENT:
                raise ReportDeidentifiedEvidenceError(
                    "report_deidentification_revision_limit_reached"
                )

            image = _load_source_image(source)
            detector = dict(self._detector.identity())
            if (
                detector.get("schema_version")
                != REPORT_PRIVACY_DETECTOR_SCHEMA
                or detector.get("coverage_guaranteed") is not False
                or detector.get("human_review_required") is not True
                or _SHA256.fullmatch(
                    str(detector.get("model_sha256") or "")
                )
                is None
            ):
                raise ReportDeidentifiedEvidenceError(
                    "report_privacy_detector_invalid"
                )
            detector_identity_sha256 = _sha256_value(detector)
            raw_regions = self._detector.detect(image)
            regions = _merge_regions(raw_regions)
            derivative_raw, output_regions, width, height = (
                _render_derivative(
                    image,
                    regions,
                    original_sha256=source.original_sha256,
                )
            )
            input_binding = {
                "schema_version": (
                    REPORT_DEIDENTIFIED_EVIDENCE_SCHEMA
                ),
                "project_id": str(source.project_id),
                "pipeline_job_id": str(source.pipeline_job_id),
                "analysis_job_id": str(source.analysis_job_id),
                "source_sha256": source.source_sha256,
                "incident_id": source.incident_id,
                "candidate_id": source.candidate_id,
                "original_ref": source.original_ref,
                "original_sha256": source.original_sha256,
                "source_time_ms": source.time_ms,
                "detector_identity_sha256": (
                    detector_identity_sha256
                ),
            }
            if external_storage is not None:
                input_binding["artifact_storage"] = {
                    "mode": "external_category_root",
                    "category": _ARTIFACT_CATEGORY,
                    "category_root_identity_sha256": (
                        external_storage[1]
                    ),
                }
            input_revision = _sha256_value(input_binding)
            derivative_id = f"rde_{input_revision[:32]}"
            location_path = self._artifact_location_path(
                incident_root,
                derivative_id,
            )
            if location_path.exists():
                derivative_root, locator = (
                    self._load_artifact_location(
                        incident_root=incident_root,
                        project_id=source.project_id,
                        pipeline_job_id=source.pipeline_job_id,
                        analysis_job_id=source.analysis_job_id,
                        incident_id=source.incident_id,
                        derivative_id=derivative_id,
                    )
                )
                manifest, manifest_sha256 = self._load_manifest(
                    derivative_root,
                    expected_scope=(
                        source.project_id,
                        source.pipeline_job_id,
                        source.analysis_job_id,
                        source.incident_id,
                        derivative_id,
                    ),
                )
                if (
                    manifest_sha256
                    != locator["manifest_sha256"]
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
                    raise ReportDeidentifiedEvidenceError(
                        "report_deidentification_artifact_location_invalid"
                    )
                return self._public_snapshot(
                    derivative_root,
                    manifest,
                    manifest_sha256,
                )
            if external_storage is None:
                artifact_incident_root = incident_root
            else:
                artifact_incident_root = _secure_directory(
                    external_storage[0] / source.incident_id,
                    parent=external_storage[0],
                    create=True,
                )
            derivative_root = artifact_incident_root / derivative_id
            if derivative_root.exists():
                manifest, manifest_sha256 = self._load_manifest(
                    derivative_root,
                    expected_scope=(
                        source.project_id,
                        source.pipeline_job_id,
                        source.analysis_job_id,
                        source.incident_id,
                        derivative_id,
                    ),
                )
                if external_storage is not None:
                    self._write_artifact_location(
                        incident_root=incident_root,
                        project_id=source.project_id,
                        pipeline_job_id=source.pipeline_job_id,
                        analysis_job_id=source.analysis_job_id,
                        incident_id=source.incident_id,
                        derivative_id=derivative_id,
                        input_revision=input_revision,
                        manifest_sha256=manifest_sha256,
                        category_root_identity_sha256=(
                            external_storage[1]
                        ),
                    )
                return self._public_snapshot(
                    derivative_root,
                    manifest,
                    manifest_sha256,
                )
            temporary = artifact_incident_root / (
                f".{derivative_id}.{uuid4().hex}.tmp"
            )
            temporary.mkdir(mode=0o700)
            try:
                _write_atomic(
                    temporary / _OUTPUT_FILE_NAME,
                    derivative_raw,
                )
                deidentified_ref = self._derivative_ref(
                    source.incident_id,
                    derivative_id,
                )
                manifest = {
                    "schema_version": (
                        REPORT_DEIDENTIFIED_EVIDENCE_SCHEMA
                    ),
                    "project_id": str(source.project_id),
                    "pipeline_job_id": str(source.pipeline_job_id),
                    "analysis_job_id": str(source.analysis_job_id),
                    "source_sha256": source.source_sha256,
                    "incident_id": source.incident_id,
                    "candidate_id": source.candidate_id,
                    "derivative_id": derivative_id,
                    "deidentified_ref": deidentified_ref,
                    "input_revision": input_revision,
                    "original_ref": source.original_ref,
                    "original_path": str(source.path.resolve(strict=True)),
                    "original_sha256": source.original_sha256,
                    "original_size_bytes": source.size_bytes,
                    "original_media_type": source.media_type,
                    "source_time_ms": source.time_ms,
                    "deidentified_file_name": _OUTPUT_FILE_NAME,
                    "deidentified_sha256": _sha256(
                        derivative_raw
                    ),
                    "deidentified_size_bytes": len(derivative_raw),
                    "media_type": "image/jpeg",
                    "width": width,
                    "height": height,
                    "detector": detector,
                    "detector_identity_sha256": (
                        detector_identity_sha256
                    ),
                    "redaction_regions": [
                        dict(row) for row in output_regions
                    ],
                    "redaction_region_count": len(output_regions),
                    "face_detection_count": sum(
                        row["kind"] == "face"
                        for row in output_regions
                    ),
                    "plate_detection_count": sum(
                        row["kind"] == "license_plate"
                        for row in output_regions
                    ),
                    "automatic_redaction_performed": bool(
                        output_regions
                    ),
                    "automatic_privacy_verification": False,
                    "created_at": _utc_now(),
                }
                if external_storage is not None:
                    manifest["artifact_storage"] = {
                        "mode": "external_category_root",
                        "category": _ARTIFACT_CATEGORY,
                        "category_root_identity_sha256": (
                            external_storage[1]
                        ),
                    }
                _write_json_record(
                    temporary / "manifest.json",
                    manifest,
                )
                try:
                    os.replace(temporary, derivative_root)
                except OSError as error:
                    if derivative_root.exists():
                        # Another idempotent caller won the race.
                        pass
                    else:
                        raise ReportDeidentifiedEvidenceError(
                            "report_deidentification_storage_unavailable"
                        ) from error
                if external_storage is not None:
                    written_manifest = _read_json_record(
                        derivative_root / "manifest.json"
                    )
                    self._write_artifact_location(
                        incident_root=incident_root,
                        project_id=source.project_id,
                        pipeline_job_id=source.pipeline_job_id,
                        analysis_job_id=source.analysis_job_id,
                        incident_id=source.incident_id,
                        derivative_id=derivative_id,
                        input_revision=input_revision,
                        manifest_sha256=_sha256_value(
                            written_manifest
                        ),
                        category_root_identity_sha256=(
                            external_storage[1]
                        ),
                    )
            finally:
                if temporary.exists():
                    for child in temporary.iterdir():
                        try:
                            child.unlink()
                        except OSError:
                            pass
                    try:
                        temporary.rmdir()
                    except OSError:
                        pass
            manifest, manifest_sha256 = self._load_manifest(
                derivative_root,
                expected_scope=(
                    source.project_id,
                    source.pipeline_job_id,
                    source.analysis_job_id,
                    source.incident_id,
                    derivative_id,
                ),
            )
            return self._public_snapshot(
                derivative_root,
                manifest,
                manifest_sha256,
            )

    def get(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        incident_id: str,
        derivative_id: str,
    ) -> Mapping[str, Any]:
        root = self._locate(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=analysis_job_id,
            incident_id=incident_id,
            derivative_id=derivative_id,
        )
        manifest, manifest_sha256 = self._load_manifest(
            root,
            expected_scope=(
                project_id,
                pipeline_job_id,
                analysis_job_id,
                incident_id,
                derivative_id,
            ),
        )
        return self._public_snapshot(
            root,
            manifest,
            manifest_sha256,
        )

    def list(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
    ) -> tuple[Mapping[str, Any], ...]:
        try:
            scope = self._scope_root(
                project_id,
                pipeline_job_id,
                analysis_job_id,
                create=False,
            )
        except ReportDeidentifiedEvidenceError as error:
            if error.code == "report_deidentification_storage_unavailable":
                return ()
            raise
        result = []
        for incident_root in sorted(
            (child for child in scope.iterdir() if child.is_dir()),
            key=lambda path: path.name,
        ):
            try:
                incident_id = str(UUID(incident_root.name))
            except (ValueError, AttributeError):
                raise ReportDeidentifiedEvidenceError(
                    "report_deidentification_record_invalid"
                )
            if (
                incident_id != incident_root.name
                or incident_root.is_symlink()
                or _is_reparse_point(incident_root)
            ):
                raise ReportDeidentifiedEvidenceError(
                    "report_deidentification_record_invalid"
                )
            derivative_ids: set[str] = set()
            for child in incident_root.iterdir():
                if child.is_dir():
                    if (
                        _DERIVATIVE_ID.fullmatch(child.name or "")
                        is None
                    ):
                        raise ReportDeidentifiedEvidenceError(
                            "report_deidentification_record_invalid"
                        )
                    derivative_ids.add(child.name)
                    continue
                match = _ARTIFACT_LOCATION_FILE.fullmatch(
                    child.name
                )
                if match is not None:
                    derivative_ids.add(match.group(1))
            for derivative_id in sorted(derivative_ids):
                if (
                    _DERIVATIVE_ID.fullmatch(derivative_id or "")
                    is None
                ):
                    raise ReportDeidentifiedEvidenceError(
                        "report_deidentification_record_invalid"
                    )
                root = self._locate(
                    project_id=project_id,
                    pipeline_job_id=pipeline_job_id,
                    analysis_job_id=analysis_job_id,
                    incident_id=incident_id,
                    derivative_id=derivative_id,
                )
                manifest, manifest_sha256 = self._load_manifest(
                    root,
                    expected_scope=(
                        project_id,
                        pipeline_job_id,
                        analysis_job_id,
                        incident_id,
                        derivative_id,
                    ),
                )
                result.append(
                    self._public_snapshot(
                        root,
                        manifest,
                        manifest_sha256,
                    )
                )
        return tuple(
            sorted(
                result,
                key=lambda row: (
                    str(row["incident_id"]),
                    str(row["derivative_id"]),
                ),
            )
        )

    def record_review(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        incident_id: str,
        derivative_id: str,
        expected_manifest_sha256: str,
        approved: bool,
        explicit_user_confirmation: bool,
    ) -> Mapping[str, Any]:
        if (
            _SHA256.fullmatch(expected_manifest_sha256 or "") is None
            or not isinstance(approved, bool)
            or explicit_user_confirmation is not True
        ):
            raise ReportDeidentifiedEvidenceError(
                "report_deidentification_review_confirmation_required"
            )
        root = self._locate(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=analysis_job_id,
            incident_id=incident_id,
            derivative_id=derivative_id,
        )
        with self._lock:
            manifest, manifest_sha256 = self._load_manifest(
                root,
                expected_scope=(
                    project_id,
                    pipeline_job_id,
                    analysis_job_id,
                    incident_id,
                    derivative_id,
                ),
            )
            if manifest_sha256 != expected_manifest_sha256:
                raise ReportDeidentifiedEvidenceError(
                    "report_deidentification_preparation_changed"
                )
            self._verify_manifest_source(manifest)
            self._verify_output(root, manifest)
            review_path = root / "review.json"
            if review_path.exists():
                existing = self._load_review(
                    root,
                    manifest,
                    manifest_sha256,
                )
                if (
                    existing is not None
                    and existing.get("approved") is approved
                ):
                    return self._public_snapshot(
                        root,
                        manifest,
                        manifest_sha256,
                    )
                raise ReportDeidentifiedEvidenceError(
                    "report_deidentification_review_already_recorded"
                )
            verified_record_sha256 = (
                deidentification_record_sha256(
                    incident_id=incident_id,
                    project_id=project_id,
                    pipeline_job_id=pipeline_job_id,
                    analysis_job_id=analysis_job_id,
                    source_sha256=str(manifest["source_sha256"]),
                    original_ref=str(manifest["original_ref"]),
                    original_sha256=str(
                        manifest["original_sha256"]
                    ),
                    deidentified_ref=str(
                        manifest["deidentified_ref"]
                    ),
                    deidentified_sha256=str(
                        manifest["deidentified_sha256"]
                    ),
                )
                if approved
                else None
            )
            review_body = {
                "schema_version": (
                    REPORT_DEIDENTIFIED_REVIEW_SCHEMA
                ),
                "project_id": str(project_id),
                "pipeline_job_id": str(pipeline_job_id),
                "analysis_job_id": str(analysis_job_id),
                "incident_id": incident_id,
                "derivative_id": derivative_id,
                "deidentified_ref": manifest["deidentified_ref"],
                "manifest_sha256": manifest_sha256,
                "original_sha256": manifest["original_sha256"],
                "deidentified_sha256": manifest[
                    "deidentified_sha256"
                ],
                "approved": approved,
                "decision": "approved" if approved else "rejected",
                "explicit_user_confirmation": True,
                "automatic_privacy_verification": False,
                "reviewer_actor": "local_user",
                "reviewed_at": _utc_now(),
                "review_revision": 1,
                "deidentification_record_sha256": (
                    verified_record_sha256
                ),
            }
            review = {
                **review_body,
                "record_sha256": _sha256_value(review_body),
            }
            _write_json_record(review_path, review)
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
        analysis_job_id: UUID,
        incident_id: str,
        derivative_id: str,
    ) -> ResolvedReportDeidentifiedArtifact:
        root = self._locate(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=analysis_job_id,
            incident_id=incident_id,
            derivative_id=derivative_id,
        )
        manifest, _manifest_sha256 = self._load_manifest(
            root,
            expected_scope=(
                project_id,
                pipeline_job_id,
                analysis_job_id,
                incident_id,
                derivative_id,
            ),
        )
        path = self._verify_output(root, manifest)
        return ResolvedReportDeidentifiedArtifact(
            path=path,
            media_type="image/jpeg",
            size_bytes=int(manifest["deidentified_size_bytes"]),
            sha256=str(manifest["deidentified_sha256"]),
            deidentified_ref=str(manifest["deidentified_ref"]),
        )

    def resolve_verified_descriptor(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        source_sha256: str,
        incident_id: str,
        original_ref: str,
        original_sha256: str,
    ) -> Optional[Mapping[str, Any]]:
        try:
            rows = self.list(
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                analysis_job_id=analysis_job_id,
            )
        except ReportDeidentifiedEvidenceError as error:
            if error.code == "report_deidentification_storage_unavailable":
                return None
            raise
        matches = [
            row
            for row in rows
            if row["incident_id"] == incident_id
            and row["source_sha256"] == source_sha256
            and row["original_sha256"] == original_sha256
            and row["state"] == "verified_for_shared_report"
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise ReportDeidentifiedEvidenceError(
                "report_deidentification_provenance_conflict"
            )
        row = matches[0]
        root = self._locate(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=analysis_job_id,
            incident_id=incident_id,
            derivative_id=str(row["derivative_id"]),
        )
        manifest, manifest_sha256 = self._load_manifest(root)
        if (
            manifest["original_ref"] != original_ref
            or manifest["source_sha256"] != source_sha256
            or manifest["original_sha256"] != original_sha256
        ):
            raise ReportDeidentifiedEvidenceError(
                "report_deidentification_provenance_conflict"
            )
        review = self._load_review(
            root,
            manifest,
            manifest_sha256,
        )
        if review is None or review.get("approved") is not True:
            return None
        self._verify_manifest_source(manifest)
        self._verify_output(root, manifest)
        return {
            "deidentified_ref": manifest["deidentified_ref"],
            "deidentified_sha256": manifest[
                "deidentified_sha256"
            ],
            "deidentification_record_sha256": review[
                "deidentification_record_sha256"
            ],
            "deidentified_verified": True,
        }

    def resolve_verified_file(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        deidentified_ref: str,
        expected_sha256: str,
    ) -> ResolvedReportDeidentifiedArtifact:
        match = _DEIDENTIFIED_REF.fullmatch(deidentified_ref or "")
        if (
            match is None
            or _SHA256.fullmatch(expected_sha256 or "") is None
        ):
            raise ReportDeidentifiedEvidenceError(
                "report_deidentification_reference_invalid"
            )
        incident_id = str(UUID(hex=match.group(1)))
        derivative_id = f"rde_{match.group(2)}"
        root = self._locate(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=analysis_job_id,
            incident_id=incident_id,
            derivative_id=derivative_id,
        )
        manifest, manifest_sha256 = self._load_manifest(
            root,
            expected_scope=(
                project_id,
                pipeline_job_id,
                analysis_job_id,
                incident_id,
                derivative_id,
            ),
        )
        review = self._load_review(
            root,
            manifest,
            manifest_sha256,
        )
        if (
            manifest["deidentified_ref"] != deidentified_ref
            or manifest["deidentified_sha256"] != expected_sha256
            or review is None
            or review.get("approved") is not True
        ):
            raise ReportDeidentifiedEvidenceError(
                "report_deidentification_not_verified"
            )
        self._verify_manifest_source(manifest)
        path = self._verify_output(root, manifest)
        return ResolvedReportDeidentifiedArtifact(
            path=path,
            media_type="image/jpeg",
            size_bytes=int(manifest["deidentified_size_bytes"]),
            sha256=str(manifest["deidentified_sha256"]),
            deidentified_ref=str(manifest["deidentified_ref"]),
        )


__all__ = [
    "OpenCvReportPrivacyDetector",
    "REPORT_DEIDENTIFIED_EVIDENCE_SCHEMA",
    "REPORT_DEIDENTIFIED_REVIEW_SCHEMA",
    "ReportDeidentifiedEvidenceError",
    "ReportDeidentifiedEvidenceService",
    "ReportEvidenceSource",
    "ReportPrivacyDetector",
    "ResolvedReportDeidentifiedArtifact",
]
