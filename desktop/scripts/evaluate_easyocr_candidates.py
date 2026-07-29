r"""Non-destructive Korean/English EasyOCR smoke over persisted candidates.

This operator tool reads completed TrafficSuperVision representative JPEG evidence
and writes an independent review package below a new output directory.  It
never writes analysis JSON, labels, SQLite, experimental sidecars, incidents,
annotations, reports, or model versions.

The full-frame OCR output is deliberately named an *unverified text
observation*.  EasyOCR recognition scores are not treated as calibrated
probabilities, facility identities, sign/tunnel classifications, geolocation
proof, or maintenance decisions.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import platform
import re
import stat
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from numbers import Real
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence


SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

import evaluate_existing_candidates as candidate_source  # noqa: E402


EVALUATION_SCHEMA = "trafficvisson.easyocr-candidate-evaluation.v1"
MANIFEST_SCHEMA = "trafficvisson.easyocr-artifact-manifest.v1"
SCORE_SEMANTICS = "ocr_score_not_verified_probability"
MAX_SAMPLE_COUNT = 60
DEFAULT_SAMPLE_COUNT = 12
MAX_IMAGE_BYTES = 24 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
MAX_OBSERVATIONS_PER_FRAME = 128
MAX_TEXT_CHARACTERS = 500
DEFAULT_MAX_WALL_SECONDS = 900.0
DEFAULT_MAX_OUTPUT_BYTES = 256 * 1024 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024 * 1024
MAX_MODEL_BYTES = 512 * 1024 * 1024
MAX_GUARDED_FILE_BYTES = 512 * 1024 * 1024
CONTACT_SHEET_PAGE_SIZE = 10
HANGUL_PATTERN = re.compile(r"[\u3131-\u318e\uac00-\ud7a3]")
DIGIT_PATTERN = re.compile(r"[0-9]")
SAFE_RUN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

REQUIRED_MODEL_FILES = (
    "craft_mlt_25k.pth",
    "korean_g2.pth",
)


class EasyOcrEvaluationError(RuntimeError):
    """A deterministic operator-facing OCR evaluation failure."""


class SourceMutationError(EasyOcrEvaluationError):
    """A protected analysis, label, database, or sidecar changed."""


class ResourceLimitError(EasyOcrEvaluationError):
    """A configured wall-time, image, observation, or storage limit was hit."""


@dataclass(frozen=True)
class GuardFingerprint:
    size_bytes: int
    modified_time_ns: int
    sha256: str


@dataclass(frozen=True)
class RuntimeBundle:
    easyocr_version: str
    torch_version: str
    numpy_module: Any
    torch_module: Any
    reader_factory: Callable[[Path], Any]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _safe_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise EasyOcrEvaluationError(f"{label} is unavailable") from error
    if path.is_symlink() or _is_reparse_point(path) or not resolved.is_dir():
        raise EasyOcrEvaluationError(f"{label} must be a real directory")
    return resolved


def _safe_regular_file(
    path: Path,
    *,
    maximum_bytes: int,
    label: str,
) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = path.stat()
    except OSError as error:
        raise EasyOcrEvaluationError(f"{label} is unavailable") from error
    if (
        path.is_symlink()
        or _is_reparse_point(path)
        or not resolved.is_file()
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= maximum_bytes
    ):
        raise EasyOcrEvaluationError(f"{label} is not a safe regular file")
    return resolved


def _sha256_file(
    path: Path,
    *,
    maximum_bytes: Optional[int] = None,
) -> str:
    try:
        before = path.stat()
    except OSError as error:
        raise EasyOcrEvaluationError("A protected file is unavailable") from error
    if maximum_bytes is not None and before.st_size > maximum_bytes:
        raise ResourceLimitError("A protected file exceeds the hashing limit")
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while True:
                block = stream.read(4 * 1024 * 1024)
                if not block:
                    break
                total += len(block)
                if maximum_bytes is not None and total > maximum_bytes:
                    raise ResourceLimitError(
                        "A protected file exceeds the hashing limit"
                    )
                digest.update(block)
        after = path.stat()
    except OSError as error:
        raise EasyOcrEvaluationError("A protected file could not be read") from error
    if (
        total != before.st_size
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise SourceMutationError("A protected file changed while it was hashed")
    return digest.hexdigest()


def _fingerprint(path: Path) -> GuardFingerprint:
    metadata = path.stat()
    return GuardFingerprint(
        size_bytes=int(metadata.st_size),
        modified_time_ns=int(metadata.st_mtime_ns),
        sha256=_sha256_file(path, maximum_bytes=MAX_GUARDED_FILE_BYTES),
    )


def _relative_ref(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as error:
        raise EasyOcrEvaluationError(
            "A protected source escaped the bridge data root"
        ) from error


def _extra_guarded_paths(data_root: Path) -> set[Path]:
    paths: set[Path] = set()
    sidecar_root = data_root / "experimental-proposals"
    if sidecar_root.exists():
        sidecar_root = _safe_directory(
            sidecar_root, "Experimental proposal root"
        )
        for candidate in sidecar_root.rglob("*"):
            if candidate.is_file():
                paths.add(
                    _safe_regular_file(
                        candidate,
                        maximum_bytes=MAX_GUARDED_FILE_BYTES,
                        label="Experimental proposal source",
                    )
                )
    for candidate in data_root.rglob("*"):
        if not candidate.is_file():
            continue
        name = candidate.name.casefold()
        if (
            candidate.suffix.casefold() in {".sqlite", ".sqlite3", ".db"}
            or name.endswith("-wal")
            or name.endswith("-shm")
        ):
            paths.add(
                _safe_regular_file(
                    candidate,
                    maximum_bytes=MAX_GUARDED_FILE_BYTES,
                    label="SQLite source",
                )
            )
    return paths


def _guarded_paths(
    data_root: Path,
    candidates: Sequence[Any],
) -> tuple[Path, ...]:
    # The shared candidate evaluator already includes every analysis-job JSON
    # and digest, candidate-label file, catalog.sqlite3, and selected image.
    paths = set(candidate_source._protected_paths(data_root, candidates))
    paths.update(_extra_guarded_paths(data_root))
    return tuple(sorted(paths, key=lambda value: str(value).casefold()))


def snapshot_guarded_sources(
    data_root: Path,
    candidates: Sequence[Any],
) -> dict[Path, GuardFingerprint]:
    return {path: _fingerprint(path) for path in _guarded_paths(data_root, candidates)}


def verify_guarded_sources(
    before: Mapping[Path, GuardFingerprint],
    data_root: Path,
    candidates: Sequence[Any],
) -> None:
    after_paths = set(_guarded_paths(data_root, candidates))
    if set(before) != after_paths:
        raise SourceMutationError("The protected source inventory changed")
    for path, expected in before.items():
        if not path.is_file() or path.is_symlink() or _is_reparse_point(path):
            raise SourceMutationError("A protected source disappeared")
        if _fingerprint(path) != expected:
            raise SourceMutationError("A protected source changed")


def _stable_job_sample(
    candidates: Sequence[Any],
    sample_count: int,
) -> tuple[Any, ...]:
    if (
        isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or not 1 <= sample_count <= MAX_SAMPLE_COUNT
    ):
        raise EasyOcrEvaluationError(
            f"Sample count must be between 1 and {MAX_SAMPLE_COUNT}"
        )
    by_job: dict[str, list[Any]] = defaultdict(list)
    for candidate in candidates:
        by_job[str(candidate.job_id)].append(candidate)
    if not by_job:
        raise EasyOcrEvaluationError("No completed analysis jobs were discovered")
    for rows in by_job.values():
        rows.sort(
            key=lambda value: (
                value.representative_timestamp_seconds,
                value.evaluation_candidate_id,
            )
        )
    job_ids = sorted(by_job)
    selected: list[Any] = []
    if len(job_ids) >= sample_count:
        if sample_count == 1:
            job_indexes = [len(job_ids) // 2]
        else:
            job_indexes = [
                round(index * (len(job_ids) - 1) / (sample_count - 1))
                for index in range(sample_count)
            ]
        if len(job_indexes) != len(set(job_indexes)):
            raise EasyOcrEvaluationError(
                "Deterministic job sampling produced duplicate indexes"
            )
        for job_index in job_indexes:
            job_id = job_ids[job_index]
            rows = by_job[job_id]
            digest = hashlib.sha256(job_id.encode("ascii")).digest()
            selected.append(rows[int.from_bytes(digest[:4], "big") % len(rows)])
    else:
        offsets = {job_id: 0 for job_id in job_ids}
        while len(selected) < sample_count:
            made_progress = False
            for job_id in job_ids:
                offset = offsets[job_id]
                rows = by_job[job_id]
                if offset >= len(rows):
                    continue
                selected.append(rows[offset])
                offsets[job_id] += 1
                made_progress = True
                if len(selected) == sample_count:
                    break
            if not made_progress:
                break
    if len(selected) != min(sample_count, len(candidates)):
        raise EasyOcrEvaluationError(
            "The requested deterministic OCR sample could not be selected"
        )
    identities = [value.evaluation_candidate_id for value in selected]
    if len(identities) != len(set(identities)):
        raise EasyOcrEvaluationError("The OCR sample contains duplicate candidates")
    return tuple(selected)


def _prepare_output_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise EasyOcrEvaluationError("Output directory must be absolute")
    if path.exists() or path.is_symlink():
        raise EasyOcrEvaluationError("Output directory already exists")
    parent = _safe_directory(path.parent, "Output parent")
    try:
        path.mkdir(exist_ok=False)
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise EasyOcrEvaluationError(
            "Output directory could not be created"
        ) from error
    if (
        path.is_symlink()
        or _is_reparse_point(path)
        or not resolved.is_dir()
        or resolved.parent != parent
    ):
        raise EasyOcrEvaluationError("Output directory is unsafe")
    return resolved


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise EasyOcrEvaluationError(
            "Evaluation output is not bounded JSON"
        ) from error


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json(path: Path, value: Any) -> None:
    _write_bytes_atomic(path, _canonical_json_bytes(value) + b"\n")


def _csv_text(value: Any) -> str:
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (dict, list, tuple)):
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    else:
        text = str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
        return "'" + text
    return text


def _write_observation_csv(
    path: Path,
    frames: Sequence[Mapping[str, Any]],
) -> None:
    columns = (
        "frame_id",
        "project_id",
        "job_id",
        "source_file_name",
        "source_candidate_id",
        "representative_timestamp_seconds",
        "observation_id",
        "raw_text",
        "normalized_text",
        "score_value",
        "score_semantics",
        "score_calibration",
        "is_probability",
        "is_verified",
        "review_only",
        "automatic_labeling_allowed",
        "contains_hangul",
        "contains_digit",
        "review_filter_match",
        "pixel_polygon",
        "normalized_polygon",
        "normalized_box",
        "facility_kind",
        "facility_name",
        "geolocation_verified",
        "frame_observation_count",
        "frame_filtered_observation_count",
        "runtime_ms",
        "source_image_sha256",
        "source_record_sha256",
    )
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(
                stream, fieldnames=columns, lineterminator="\r\n"
            )
            writer.writeheader()
            for frame in frames:
                observations = frame["raw_observations"] or [None]
                for observation in observations:
                    row = {
                        "frame_id": frame["frame_id"],
                        "project_id": frame["project_id"],
                        "job_id": frame["job_id"],
                        "source_file_name": frame["source_file_name"],
                        "source_candidate_id": frame["source_candidate_id"],
                        "representative_timestamp_seconds": frame[
                            "representative_timestamp_seconds"
                        ],
                        "observation_id": (
                            observation["observation_id"]
                            if observation is not None
                            else ""
                        ),
                        "raw_text": (
                            observation["raw_text"]
                            if observation is not None
                            else ""
                        ),
                        "normalized_text": (
                            observation["normalized_text"]
                            if observation is not None
                            else ""
                        ),
                        "score_value": (
                            observation["score_value"]
                            if observation is not None
                            else ""
                        ),
                        "score_semantics": (
                            observation["score_semantics"]
                            if observation is not None
                            else SCORE_SEMANTICS
                        ),
                        "score_calibration": (
                            observation["score_calibration"]
                            if observation is not None
                            else "easyocr_recognition_score"
                        ),
                        "is_probability": False,
                        "is_verified": False,
                        "review_only": True,
                        "automatic_labeling_allowed": False,
                        "contains_hangul": (
                            observation["contains_hangul"]
                            if observation is not None
                            else False
                        ),
                        "contains_digit": (
                            observation["contains_digit"]
                            if observation is not None
                            else False
                        ),
                        "review_filter_match": (
                            observation["review_filter_match"]
                            if observation is not None
                            else False
                        ),
                        "pixel_polygon": (
                            observation["pixel_polygon"]
                            if observation is not None
                            else ""
                        ),
                        "normalized_polygon": (
                            observation["normalized_polygon"]
                            if observation is not None
                            else ""
                        ),
                        "normalized_box": (
                            observation["normalized_box"]
                            if observation is not None
                            else ""
                        ),
                        "facility_kind": "",
                        "facility_name": "",
                        "geolocation_verified": False,
                        "frame_observation_count": frame[
                            "observation_count"
                        ],
                        "frame_filtered_observation_count": frame[
                            "filtered_observation_count"
                        ],
                        "runtime_ms": frame["runtime_ms"],
                        "source_image_sha256": frame[
                            "source_image_sha256"
                        ],
                        "source_record_sha256": frame[
                            "source_record_sha256"
                        ],
                    }
                    writer.writerow(
                        {key: _csv_text(value) for key, value in row.items()}
                    )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _directory_size(path: Path) -> int:
    total = 0
    for candidate in path.iterdir():
        if candidate.is_file():
            total += candidate.stat().st_size
    return total


def _enforce_output_limit(path: Path, maximum_bytes: int) -> None:
    if _directory_size(path) > maximum_bytes:
        raise ResourceLimitError("OCR evaluation output exceeded its byte limit")


def _runtime_bundle() -> RuntimeBundle:
    # Hide every CUDA device before importing torch/EasyOCR.  This evaluator is
    # intentionally CPU-only so it can coexist with detector experiments.
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    os.environ.setdefault("OMP_NUM_THREADS", "4")
    os.environ.setdefault("MKL_NUM_THREADS", "4")
    try:
        import easyocr
        import numpy
        import torch
    except ImportError as error:
        raise EasyOcrEvaluationError(
            "EasyOCR, NumPy, and PyTorch must be installed locally"
        ) from error

    def reader_factory(model_directory: Path):
        return easyocr.Reader(
            ["ko", "en"],
            gpu=False,
            model_storage_directory=str(model_directory),
            download_enabled=False,
            verbose=False,
        )

    return RuntimeBundle(
        easyocr_version=str(getattr(easyocr, "__version__", "unknown")),
        torch_version=str(getattr(torch, "__version__", "unknown")),
        numpy_module=numpy,
        torch_module=torch,
        reader_factory=reader_factory,
    )


def _configure_cpu_runtime(runtime: RuntimeBundle, cpu_threads: int) -> None:
    torch = runtime.torch_module
    try:
        torch.set_num_threads(cpu_threads)
    except (AttributeError, RuntimeError):
        pass
    try:
        torch.set_num_interop_threads(1)
    except (AttributeError, RuntimeError):
        pass
    try:
        torch.manual_seed(0)
    except (AttributeError, RuntimeError):
        pass
    try:
        torch.use_deterministic_algorithms(True, warn_only=True)
    except (AttributeError, RuntimeError, TypeError):
        pass
    try:
        import cv2

        cv2.setNumThreads(1)
    except (ImportError, AttributeError):
        pass


def _model_provenance(model_directory: Path) -> tuple[dict[str, Any], ...]:
    root = _safe_directory(model_directory, "EasyOCR model directory")
    rows = []
    for name in REQUIRED_MODEL_FILES:
        path = _safe_regular_file(
            root / name,
            maximum_bytes=MAX_MODEL_BYTES,
            label=f"EasyOCR model {name}",
        )
        rows.append(
            {
                "file_name": name,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256_file(path, maximum_bytes=MAX_MODEL_BYTES),
            }
        )
    return tuple(rows)


def _finite_score(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        raise EasyOcrEvaluationError(
            "EasyOCR returned an invalid recognition score"
        )
    return round(float(value), 8)


def _point(value: Any) -> tuple[float, float]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or isinstance(value[0], bool)
        or isinstance(value[1], bool)
        or not isinstance(value[0], Real)
        or not isinstance(value[1], Real)
        or not math.isfinite(float(value[0]))
        or not math.isfinite(float(value[1]))
    ):
        raise EasyOcrEvaluationError("EasyOCR returned an invalid text polygon")
    return round(float(value[0]), 3), round(float(value[1]), 3)


def _normalize_observation(
    value: Any,
    *,
    frame_id: str,
    observation_index: int,
    width: int,
    height: int,
) -> dict[str, Any]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 3
        or not isinstance(value[0], (list, tuple))
        or len(value[0]) != 4
        or not isinstance(value[1], str)
        or not value[1]
        or len(value[1]) > MAX_TEXT_CHARACTERS
        or "\x00" in value[1]
    ):
        raise EasyOcrEvaluationError("EasyOCR returned an invalid observation")
    polygon = [_point(point) for point in value[0]]
    normalized_polygon = [
        [
            round(min(1.0, max(0.0, x / width)), 8),
            round(min(1.0, max(0.0, y / height)), 8),
        ]
        for x, y in polygon
    ]
    xs = [point[0] for point in normalized_polygon]
    ys = [point[1] for point in normalized_polygon]
    left = min(xs)
    top = min(ys)
    right = max(xs)
    bottom = max(ys)
    normalized_text = " ".join(value[1].split())
    contains_hangul = bool(HANGUL_PATTERN.search(normalized_text))
    contains_digit = bool(DIGIT_PATTERN.search(normalized_text))
    return {
        "observation_id": f"{frame_id}-ocr-{observation_index:03d}",
        "raw_text": value[1],
        "normalized_text": normalized_text,
        "score_value": _finite_score(value[2]),
        "score_semantics": SCORE_SEMANTICS,
        "score_calibration": "easyocr_recognition_score",
        "is_probability": False,
        "is_verified": False,
        "review_only": True,
        "automatic_labeling_allowed": False,
        "contains_hangul": contains_hangul,
        "contains_digit": contains_digit,
        "review_filter_match": contains_hangul or contains_digit,
        "pixel_polygon": [[x, y] for x, y in polygon],
        "normalized_polygon": normalized_polygon,
        "normalized_box": {
            "x": round(left, 8),
            "y": round(top, 8),
            "width": round(max(0.0, right - left), 8),
            "height": round(max(0.0, bottom - top), 8),
        },
        "interpretation": {
            "kind": "unclassified_full_frame_text_observation",
            "facility_kind": None,
            "facility_name": None,
            "facility_identity_verified": False,
            "geolocation_verified": False,
        },
    }


def _load_rgb_array(
    path: Path,
    *,
    numpy_module: Any,
) -> tuple[Any, int, int]:
    try:
        from PIL import Image
    except ImportError as error:
        raise EasyOcrEvaluationError(
            "Pillow is required for OCR image loading"
        ) from error
    image_path = _safe_regular_file(
        path,
        maximum_bytes=MAX_IMAGE_BYTES,
        label="Representative image",
    )
    try:
        with Image.open(image_path) as image:
            width, height = image.size
            if (
                width <= 0
                or height <= 0
                or width * height > MAX_IMAGE_PIXELS
            ):
                raise ResourceLimitError(
                    "A representative image exceeds the pixel limit"
                )
            image.load()
            array = numpy_module.asarray(image.convert("RGB"))
    except ResourceLimitError:
        raise
    except Exception as error:
        raise EasyOcrEvaluationError(
            "A representative image could not be decoded"
        ) from error
    return array, width, height


def _working_set_bytes() -> Optional[int]:
    try:
        import psutil

        return int(psutil.Process(os.getpid()).memory_info().rss)
    except (ImportError, OSError, AttributeError):
        return None


def _contact_sheets(
    output_root: Path,
    candidates: Sequence[Any],
    frames: Sequence[Mapping[str, Any]],
) -> list[Path]:
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageOps
    except ImportError as error:
        raise EasyOcrEvaluationError(
            "Pillow is required for OCR contact sheets"
        ) from error
    candidate_by_id = {
        candidate.evaluation_candidate_id: candidate
        for candidate in candidates
    }
    font_path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "malgun.ttf"
    try:
        font = ImageFont.truetype(str(font_path), 18)
        small_font = ImageFont.truetype(str(font_path), 15)
    except OSError:
        font = ImageFont.load_default()
        small_font = font

    page_paths = []
    columns = 2
    card_width = 760
    image_height = 430
    caption_height = 118
    margin = 20
    rows_per_page = math.ceil(CONTACT_SHEET_PAGE_SIZE / columns)
    for page_offset in range(0, len(frames), CONTACT_SHEET_PAGE_SIZE):
        page = frames[page_offset : page_offset + CONTACT_SHEET_PAGE_SIZE]
        canvas = Image.new(
            "RGB",
            (
                columns * card_width + (columns + 1) * margin,
                rows_per_page * (image_height + caption_height)
                + (rows_per_page + 1) * margin,
            ),
            "white",
        )
        draw = ImageDraw.Draw(canvas)
        for page_index, frame in enumerate(page):
            candidate = candidate_by_id[frame["frame_id"]]
            with Image.open(candidate.image_path) as source:
                source.load()
                original_width, original_height = source.size
                image = ImageOps.contain(
                    source.convert("RGB"),
                    (card_width, image_height),
                    method=Image.Resampling.LANCZOS,
                )
            column = page_index % columns
            row = page_index // columns
            left = margin + column * (card_width + margin)
            top = margin + row * (image_height + caption_height + margin)
            image_left = left + (card_width - image.width) // 2
            image_top = top + (image_height - image.height) // 2
            canvas.paste(image, (image_left, image_top))
            scale_x = image.width / original_width
            scale_y = image.height / original_height
            for observation in frame["raw_observations"]:
                polygon = [
                    (
                        image_left + point[0] * scale_x,
                        image_top + point[1] * scale_y,
                    )
                    for point in observation["pixel_polygon"]
                ]
                color = (
                    (0, 175, 80)
                    if observation["review_filter_match"]
                    else (230, 65, 55)
                )
                draw.line([*polygon, polygon[0]], fill=color, width=3)
            filtered = [
                (
                    f"{observation['normalized_text'][:24]} "
                    f"({observation['score_value']:.3f})"
                )
                for observation in frame["raw_observations"]
                if observation["review_filter_match"]
            ][:3]
            title = (
                f"{page_offset + page_index + 1:02d}. "
                f"{frame['source_file_name'][:34]}  "
                f"t={frame['representative_timestamp_seconds']:.3f}s"
            )
            detail = (
                f"raw={frame['observation_count']} / "
                f"한글·숫자={frame['filtered_observation_count']}\n"
                + (" | ".join(filtered) if filtered else "필터 후보 없음")
            )
            draw.text(
                (left + 4, top + image_height + 5),
                title,
                fill="black",
                font=font,
            )
            draw.multiline_text(
                (left + 4, top + image_height + 34),
                detail,
                fill=(20, 20, 20),
                font=small_font,
                spacing=4,
            )
        page_number = page_offset // CONTACT_SHEET_PAGE_SIZE + 1
        path = output_root / f"contact-sheet-{page_number:03d}.jpg"
        canvas.save(path, format="JPEG", quality=90, optimize=True)
        page_paths.append(path)
    return page_paths


def _readme(
    *,
    generated_at: str,
    discovery: Any,
    selected: Sequence[Any],
    frames: Sequence[Mapping[str, Any]],
    model_files: Sequence[Mapping[str, Any]],
    runtime: Mapping[str, Any],
    resource_limits: Mapping[str, Any],
    contact_sheets: Sequence[Path],
) -> str:
    raw_count = sum(frame["observation_count"] for frame in frames)
    filtered_count = sum(
        frame["filtered_observation_count"] for frame in frames
    )
    frames_with_filtered = sum(
        frame["filtered_observation_count"] > 0 for frame in frames
    )
    filtered_texts = []
    for frame in frames:
        for observation in frame["raw_observations"]:
            if observation["review_filter_match"]:
                filtered_texts.append(
                    (
                        frame["source_file_name"],
                        frame["representative_timestamp_seconds"],
                        observation["normalized_text"],
                        observation["score_value"],
                    )
                )
    lines = [
        "# TrafficSuperVision 한국어 이정표·터널명 OCR 비파괴 스모크",
        "",
        f"생성 시각(UTC): `{generated_at}`",
        "",
        "## 결론 및 해석 제한",
        "",
        "이 결과는 전체 프레임 EasyOCR이 제안한 미검증 텍스트 관측이다. "
        "텍스트가 실제 이정표나 터널명 영역에서 나온 것인지, 시설 이름이 "
        "맞는지, 촬영 위치와 일치하는지는 아직 확인하지 않았다.",
        "",
        f"- 점수 의미: `{SCORE_SEMANTICS}`",
        "- EasyOCR 점수는 검증된 확률·정확도가 아님",
        "- 사용자 확정 라벨: `0건`",
        "- 자동 라벨: `0건`",
        "- 사건·annotation·유지보수 조치 생성: `0건`",
        "- experimental sidecar 기록: `0건`",
        "- 모델 승격: `아니오`",
        "- 시설 유형·시설명·지리 위치 확정: `0건`",
        "",
        "## 실행 범위",
        "",
        f"- 무결성 검증 completed job: `{discovery.completed_record_count}건`",
        f"- 발견한 대표 JPEG: `{len(discovery.candidates)}건`",
        f"- 결정적 분산 표본: `{len(selected)}장 / {len(set(item.job_id for item in selected))}개 job`",
        f"- raw OCR 관측: `{raw_count}건`",
        f"- 한글 또는 숫자 포함 검토 필터: `{filtered_count}건 / {frames_with_filtered}장`",
        f"- contact sheet: `{len(contact_sheets)}장`",
        "",
        "## 실행 환경",
        "",
        f"- EasyOCR: `{runtime['easyocr_version']}`",
        f"- PyTorch: `{runtime['torch_version']}`",
        "- Device: `cpu`",
        "- GPU 접근: `비활성`",
        "- 설정 VRAM 예산: `0 bytes`",
        f"- 전체 경과: `{runtime['total_runtime_seconds']}초`",
        f"- CPU threads: `{resource_limits['cpu_threads']}`",
        f"- 최대 실행 시간: `{resource_limits['max_wall_seconds']}초`",
        f"- 최대 출력: `{resource_limits['max_output_bytes']} bytes`",
        "",
        "## 로컬 모델 무결성",
        "",
        "| 파일 | 크기(bytes) | SHA-256 |",
        "| --- | ---: | --- |",
    ]
    for model in model_files:
        lines.append(
            f"| `{model['file_name']}` | {model['size_bytes']} | "
            f"`{model['sha256']}` |"
        )
    lines.extend(
        [
            "",
            "## 한글·숫자 검토 후보",
            "",
            "| 영상 | 시각(초) | OCR 원문 | 점수 |",
            "| --- | ---: | --- | ---: |",
        ]
    )
    if filtered_texts:
        for source_name, timestamp, text, score in filtered_texts[:100]:
            safe_text = text.replace("|", r"\|").replace("\n", " ")
            lines.append(
                f"| `{source_name}` | {timestamp:.3f} | {safe_text} | "
                f"{score:.6f} |"
            )
    else:
        lines.append("| - | - | 한글·숫자 후보 없음 | - |")
    lines.extend(
        [
            "",
            "위 표는 문자 포함 필터일 뿐 이정표·터널명 판정 결과가 아니다. "
            "차량 번호, 도로 노면 숫자, 광고, 대시보드 글자, 오인식도 "
            "포함될 수 있으므로 이미지 질문 카드에서 사용자가 확인해야 한다.",
            "",
            "## 산출물",
            "",
            "- `ocr_results.json`: 프레임별 raw 관측, 박스, 점수, 출처 해시",
            "- `ocr_observations.csv`: raw 관측과 한글·숫자 필터 열",
            "- `contact-sheet-*.jpg`: 전체 표본과 OCR 영역 시각화",
            "- `manifest.json`: 산출물 SHA-256 및 실행 provenance",
            "",
            "## 다음 단계",
            "",
            "연락시트를 육안 검토해 실제 표지판·터널 입구 텍스트인지 먼저 "
            "구분하고, 원본 전후 프레임과 GPS를 함께 본 뒤 질문형 라벨링에서 "
            "시설 유형과 이름을 사용자에게 확인해야 한다.",
            "",
        ]
    )
    return "\n".join(lines)


def _artifact_manifest(
    output_root: Path,
    *,
    generated_at: str,
    source_summary: Mapping[str, Any],
    model_files: Sequence[Mapping[str, Any]],
    runtime: Mapping[str, Any],
    resource_limits: Mapping[str, Any],
) -> dict[str, Any]:
    artifacts = []
    for path in sorted(
        (
            candidate
            for candidate in output_root.iterdir()
            if candidate.is_file() and candidate.name != "manifest.json"
        ),
        key=lambda candidate: candidate.name,
    ):
        artifacts.append(
            {
                "path": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    return {
        "schema_version": MANIFEST_SCHEMA,
        "generated_at": generated_at,
        "purpose": "review_only_full_frame_easyocr_smoke",
        "score_semantics": SCORE_SEMANTICS,
        "is_probability": False,
        "review_only": True,
        "automatic_labeling_allowed": False,
        "facility_identity_verified": False,
        "geolocation_verified": False,
        "sidecars_created": 0,
        "annotations_created": 0,
        "incidents_created": 0,
        "model_promoted": False,
        "source_integrity": dict(source_summary),
        "model_files": list(model_files),
        "runtime": dict(runtime),
        "resource_limits": dict(resource_limits),
        "artifacts": artifacts,
    }


def run_evaluation(
    *,
    data_root: Path | str,
    model_directory: Path | str,
    output_directory: Path | str,
    sample_count: int = DEFAULT_SAMPLE_COUNT,
    max_wall_seconds: float = DEFAULT_MAX_WALL_SECONDS,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    cpu_threads: int = 4,
    runtime_factory: Callable[[], RuntimeBundle] = _runtime_bundle,
) -> dict[str, Any]:
    """Run a bounded CPU-only OCR smoke and return the exported envelope."""

    if (
        isinstance(max_wall_seconds, bool)
        or not isinstance(max_wall_seconds, Real)
        or not math.isfinite(float(max_wall_seconds))
        or not 10 <= float(max_wall_seconds) <= 3_600
    ):
        raise EasyOcrEvaluationError(
            "max_wall_seconds must be between 10 and 3600"
        )
    if (
        isinstance(max_output_bytes, bool)
        or not isinstance(max_output_bytes, int)
        or not 1 * 1024 * 1024 <= max_output_bytes <= MAX_OUTPUT_BYTES
    ):
        raise EasyOcrEvaluationError(
            "max_output_bytes is outside the supported range"
        )
    if (
        isinstance(cpu_threads, bool)
        or not isinstance(cpu_threads, int)
        or not 1 <= cpu_threads <= 8
    ):
        raise EasyOcrEvaluationError("cpu_threads must be between 1 and 8")

    root = _safe_directory(Path(data_root), "Bridge data root")
    model_root = _safe_directory(
        Path(model_directory), "EasyOCR model directory"
    )
    output_path = Path(output_directory)
    discovery = candidate_source.discover_candidates(
        root, limit=candidate_source.MAX_CANDIDATES
    )
    selected = _stable_job_sample(discovery.candidates, sample_count)
    guarded_before = snapshot_guarded_sources(root, selected)
    model_files = _model_provenance(model_root)
    model_before = {
        model["file_name"]: _fingerprint(model_root / model["file_name"])
        for model in model_files
    }
    output_root = _prepare_output_directory(output_path)

    started = time.perf_counter()
    deadline = started + float(max_wall_seconds)
    generated_at = _utc_now()
    runtime = runtime_factory()
    _configure_cpu_runtime(runtime, cpu_threads)
    working_set_before = _working_set_bytes()
    reader_started = time.perf_counter()
    reader = runtime.reader_factory(model_root)
    reader_initialization_seconds = round(
        time.perf_counter() - reader_started, 6
    )
    frames: list[dict[str, Any]] = []
    try:
        for frame_index, candidate in enumerate(selected, start=1):
            if time.perf_counter() >= deadline:
                raise ResourceLimitError(
                    "The OCR evaluation reached its wall-time limit"
                )
            array, width, height = _load_rgb_array(
                candidate.image_path,
                numpy_module=runtime.numpy_module,
            )
            inference_started = time.perf_counter()
            raw = reader.readtext(
                array,
                detail=1,
                paragraph=False,
                decoder="greedy",
                batch_size=1,
                workers=0,
                min_size=10,
                text_threshold=0.7,
                low_text=0.4,
                link_threshold=0.4,
                canvas_size=2560,
                mag_ratio=1.0,
            )
            runtime_ms = round(
                (time.perf_counter() - inference_started) * 1000
            )
            if not isinstance(raw, (list, tuple)):
                raise EasyOcrEvaluationError(
                    "EasyOCR returned a non-list result"
                )
            if len(raw) > MAX_OBSERVATIONS_PER_FRAME:
                raise ResourceLimitError(
                    "A frame exceeded the OCR observation limit"
                )
            observations = [
                _normalize_observation(
                    value,
                    frame_id=candidate.evaluation_candidate_id,
                    observation_index=index,
                    width=width,
                    height=height,
                )
                for index, value in enumerate(raw, start=1)
            ]
            frames.append(
                {
                    "frame_id": candidate.evaluation_candidate_id,
                    "selection_index": frame_index,
                    "project_id": candidate.project_id,
                    "job_id": candidate.job_id,
                    "source_file_name": candidate.source_file_name,
                    "source_candidate_id": candidate.source_candidate_id,
                    "representative_timestamp_seconds": round(
                        candidate.representative_timestamp_seconds, 6
                    ),
                    "source_record_ref": candidate.record_relative_path,
                    "source_record_sha256": candidate.record_sha256,
                    "source_image_ref": candidate.image_relative_path,
                    "source_image_size_bytes": candidate.image_size_bytes,
                    "source_image_sha256": candidate.image_sha256,
                    "image_width": width,
                    "image_height": height,
                    "runtime_ms": runtime_ms,
                    "observation_count": len(observations),
                    "filtered_observation_count": sum(
                        observation["review_filter_match"]
                        for observation in observations
                    ),
                    "raw_observations": observations,
                    "facility_interpretation": {
                        "status": "unverified",
                        "facility_kind": None,
                        "facility_name": None,
                        "facility_identity_verified": False,
                        "geolocation_verified": False,
                    },
                    "review_only": True,
                    "automatic_labeling_allowed": False,
                }
            )
            if time.perf_counter() >= deadline:
                raise ResourceLimitError(
                    "The OCR evaluation reached its wall-time limit"
                )

        contact_sheets = _contact_sheets(output_root, selected, frames)
        _enforce_output_limit(output_root, max_output_bytes)
        total_runtime_seconds = round(time.perf_counter() - started, 6)
        working_set_after = _working_set_bytes()
        runtime_provenance = {
            "easyocr_version": runtime.easyocr_version,
            "torch_version": runtime.torch_version,
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "device": "cpu",
            "gpu_access_disabled": True,
            "configured_vram_budget_bytes": 0,
            "cuda_visible_devices": "",
            "reader_languages": ["ko", "en"],
            "download_enabled": False,
            "reader_initialization_seconds": (
                reader_initialization_seconds
            ),
            "total_runtime_seconds": total_runtime_seconds,
            "working_set_before_bytes": working_set_before,
            "working_set_after_bytes": working_set_after,
        }
        resource_limits = {
            "sample_count": sample_count,
            "maximum_sample_count": MAX_SAMPLE_COUNT,
            "maximum_image_bytes": MAX_IMAGE_BYTES,
            "maximum_image_pixels": MAX_IMAGE_PIXELS,
            "maximum_observations_per_frame": MAX_OBSERVATIONS_PER_FRAME,
            "maximum_text_characters": MAX_TEXT_CHARACTERS,
            "max_wall_seconds": float(max_wall_seconds),
            "max_output_bytes": max_output_bytes,
            "cpu_threads": cpu_threads,
            "configured_vram_budget_bytes": 0,
        }
        source_summary = {
            "bridge_data_files_guarded": len(guarded_before),
            "analysis_json_modified": 0,
            "label_files_modified": 0,
            "sqlite_files_modified": 0,
            "experimental_sidecars_modified": 0,
            "selected_image_files_modified": 0,
            "guard_inventory_sha256": hashlib.sha256(
                _canonical_json_bytes(
                    [
                        {
                            "path": _relative_ref(path, root),
                            "size_bytes": fingerprint.size_bytes,
                            "sha256": fingerprint.sha256,
                        }
                        for path, fingerprint in sorted(
                            guarded_before.items(),
                            key=lambda item: str(item[0]).casefold(),
                        )
                    ]
                )
            ).hexdigest(),
        }
        envelope = {
            "schema_version": EVALUATION_SCHEMA,
            "generated_at": generated_at,
            "purpose": "review_only_full_frame_easyocr_smoke",
            "score_semantics": {
                "label": SCORE_SEMANTICS,
                "is_probability": False,
                "is_accuracy": False,
                "is_facility_identity": False,
                "is_geolocation_verification": False,
                "is_maintenance_decision": False,
            },
            "review_only": True,
            "automatic_labeling_allowed": False,
            "sidecars_created": 0,
            "annotations_created": 0,
            "incidents_created": 0,
            "model_promoted": False,
            "discovery": {
                "completed_record_count": (
                    discovery.completed_record_count
                ),
                "candidate_count": len(discovery.candidates),
                "skipped_record_count": discovery.skipped_record_count,
                "skipped_candidate_count": (
                    discovery.skipped_candidate_count
                ),
                "selected_frame_count": len(selected),
                "selected_job_count": len(
                    {candidate.job_id for candidate in selected}
                ),
                "selection_method": (
                    "evenly_spaced_sorted_jobs_then_stable_candidate_hash"
                ),
            },
            "model": {
                "engine": "easyocr_full_frame",
                "languages": ["ko", "en"],
                "files": list(model_files),
            },
            "runtime": runtime_provenance,
            "resource_limits": resource_limits,
            "source_integrity": source_summary,
            "summary": {
                "frame_count": len(frames),
                "raw_observation_count": sum(
                    frame["observation_count"] for frame in frames
                ),
                "filtered_observation_count": sum(
                    frame["filtered_observation_count"]
                    for frame in frames
                ),
                "frames_with_filtered_observations": sum(
                    frame["filtered_observation_count"] > 0
                    for frame in frames
                ),
                "hangul_observation_count": sum(
                    observation["contains_hangul"]
                    for frame in frames
                    for observation in frame["raw_observations"]
                ),
                "digit_observation_count": sum(
                    observation["contains_digit"]
                    for frame in frames
                    for observation in frame["raw_observations"]
                ),
                "raw_text_frequency": dict(
                    Counter(
                        observation["normalized_text"]
                        for frame in frames
                        for observation in frame["raw_observations"]
                    )
                ),
            },
            "frames": frames,
        }
        _write_json(output_root / "ocr_results.json", envelope)
        _write_observation_csv(
            output_root / "ocr_observations.csv", frames
        )
        readme = _readme(
            generated_at=generated_at,
            discovery=discovery,
            selected=selected,
            frames=frames,
            model_files=model_files,
            runtime=runtime_provenance,
            resource_limits=resource_limits,
            contact_sheets=contact_sheets,
        )
        _write_bytes_atomic(
            output_root / "README.md", readme.encode("utf-8")
        )
        _enforce_output_limit(output_root, max_output_bytes)
        verify_guarded_sources(guarded_before, root, selected)
        for model in model_files:
            model_path = model_root / model["file_name"]
            if _fingerprint(model_path) != model_before[model["file_name"]]:
                raise SourceMutationError(
                    "An EasyOCR model file changed during evaluation"
                )
        manifest = _artifact_manifest(
            output_root,
            generated_at=generated_at,
            source_summary=source_summary,
            model_files=model_files,
            runtime=runtime_provenance,
            resource_limits=resource_limits,
        )
        _write_json(output_root / "manifest.json", manifest)
        _enforce_output_limit(output_root, max_output_bytes)
        return envelope
    finally:
        # Verification in finally ensures a model/runtime exception cannot hide
        # an unrelated source mutation.  It performs read-only hashing only.
        verify_guarded_sources(guarded_before, root, selected)
        for model in model_files:
            model_path = model_root / model["file_name"]
            if _fingerprint(model_path) != model_before[model["file_name"]]:
                raise SourceMutationError(
                    "An EasyOCR model file changed during evaluation"
                )


def _default_data_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise EasyOcrEvaluationError("LOCALAPPDATA is unavailable")
    return Path(local_app_data) / "TrafficSuperVision" / "bridge-data"


def _default_model_directory() -> Path:
    return Path.home() / ".EasyOCR" / "model"


def _default_output_directory() -> Path:
    program_data = Path(
        os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    )
    parent = (
        program_data / "TrafficSuperVision" / "ModelEvaluations"
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return parent / f"easyocr-korean-smoke-{timestamp}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run a CPU-only, non-destructive EasyOCR smoke over persisted "
            "TrafficSuperVision representative JPEGs."
        )
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=_default_data_root(),
    )
    parser.add_argument(
        "--model-directory",
        type=Path,
        default=_default_model_directory(),
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--sample-count",
        type=int,
        default=DEFAULT_SAMPLE_COUNT,
    )
    parser.add_argument(
        "--max-wall-seconds",
        type=float,
        default=DEFAULT_MAX_WALL_SECONDS,
    )
    parser.add_argument(
        "--max-output-bytes",
        type=int,
        default=DEFAULT_MAX_OUTPUT_BYTES,
    )
    parser.add_argument(
        "--cpu-threads",
        type=int,
        default=min(4, max(1, os.cpu_count() or 1)),
    )
    arguments = parser.parse_args(argv)
    output = (
        arguments.output_directory
        if arguments.output_directory is not None
        else _default_output_directory()
    )
    if not SAFE_RUN_NAME.fullmatch(output.name):
        parser.error(
            "output directory name must use safe ASCII letters, digits, "
            "dots, underscores, or hyphens"
        )
    try:
        envelope = run_evaluation(
            data_root=arguments.data_root,
            model_directory=arguments.model_directory,
            output_directory=output,
            sample_count=arguments.sample_count,
            max_wall_seconds=arguments.max_wall_seconds,
            max_output_bytes=arguments.max_output_bytes,
            cpu_threads=arguments.cpu_threads,
        )
    except EasyOcrEvaluationError as error:
        print(f"EasyOCR evaluation failed: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "output_directory": str(output.resolve()),
                "selected_frame_count": envelope["discovery"][
                    "selected_frame_count"
                ],
                "selected_job_count": envelope["discovery"][
                    "selected_job_count"
                ],
                "raw_observation_count": envelope["summary"][
                    "raw_observation_count"
                ],
                "filtered_observation_count": envelope["summary"][
                    "filtered_observation_count"
                ],
                "score_semantics": SCORE_SEMANTICS,
                "device": "cpu",
                "automatic_labels_created": 0,
                "sidecars_created": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
