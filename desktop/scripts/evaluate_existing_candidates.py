"""Evaluate persisted candidate JPEGs with an opt-in local vision model pack.

This is a developer/operator tool, not a labeling or model-promotion path.
It reads completed TrafficSuperVision analysis records and their representative
JPEGs, stages independent copies in a new evaluation directory, and invokes
the same isolated ``LocalVisionInferenceService`` used by the bridge.

Safety contract:

* analysis JSON, label files and SQLite databases are never opened for write;
* the output directory must not already exist;
* protected source files are hashed before and after inference;
* model output is exported only as an unreviewed proposal;
* ``zero_shot_relative`` and ``model_score`` outputs are never called
  calibrated probabilities, maintenance decisions, labels, or validation
  metrics.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import stat
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from uuid import UUID


DESKTOP_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_ROOT = DESKTOP_ROOT / "bridge"
if str(BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(BRIDGE_ROOT))

from app.vision_inference import (  # noqa: E402
    LocalVisionInferenceService,
    VisionInferenceError,
)


EVALUATION_SCHEMA = "trafficvisson.candidate-model-evaluation.v1"
PERSISTED_JOB_SCHEMA = "trafficvisson.analysis-job.v1"
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
CANDIDATE_ID_PATTERN = re.compile(r"^[a-f0-9]{20}$")
ARTIFACT_DIRECTORY_PATTERN = re.compile(r"^candidate-extraction-[A-Za-z0-9_-]+$")
MAX_RECORD_BYTES = 16 * 1024 * 1024
MAX_IMAGE_BYTES = 24 * 1024 * 1024
MAX_CANDIDATES = 512

SUPPORTED_EVALUATION_ENGINES = {
    "transformers_clip_zero_shot": "zero_shot_relative",
    "onnx_yolo_v8": "model_score",
}
SCORE_SEMANTICS_LABELS = {
    "zero_shot_relative": "zero_shot_relative_not_probability",
    "model_score": "model_score_not_probability",
}
EVALUATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

CLIP_DISCLAIMER_KO = (
    "이 결과는 사용자 검토를 위한 실험적 후보입니다. CLIP의 "
    "zero_shot_relative 값은 후보 프롬프트와 음성 프롬프트 전체에 대한 "
    "softmax 상대점수이며, 실제 결함 확률·정확도·확정 판정이 아닙니다. "
    "자동 라벨, 유지보수 사건 확정, 보고서 확정 입력, 모델 승격에 "
    "사용하면 안 됩니다."
)

ONNX_DISCLAIMER_KO = (
    "이 결과는 사용자 검토를 위한 실험적 후보입니다. ONNX detector의 "
    "model_score 값은 현재 TrafficSuperVision 한국 도로 고정 검증 세트에서 "
    "보정되지 않은 모델 출력 점수이며, 실제 결함 확률·정확도·확정 "
    "판정이 아닙니다. 표시 박스도 사용자가 확인하기 전에는 정답 영역이 "
    "아닙니다. 자동 라벨, 유지보수 사건 확정, 보고서 확정 입력, 모델 "
    "승격에 사용하면 안 됩니다."
)


class EvaluationError(RuntimeError):
    """A deterministic operator-facing evaluation failure."""


class SourceMutationError(EvaluationError):
    """Raised if any protected source file changes during evaluation."""


@dataclass(frozen=True)
class FileFingerprint:
    size_bytes: int
    modified_time_ns: int
    sha256: str


@dataclass(frozen=True)
class SourceCandidate:
    project_id: str
    job_id: str
    source_file_name: str
    source_candidate_id: str
    evaluation_candidate_id: str
    representative_timestamp_seconds: float
    scene_score: float
    sharpness_score: float
    triggers: tuple[str, ...]
    record_path: Path
    record_relative_path: str
    record_sha256: str
    image_path: Path
    image_relative_path: str
    image_size_bytes: int
    image_sha256: str


@dataclass(frozen=True)
class Discovery:
    candidates: tuple[SourceCandidate, ...]
    completed_record_count: int
    skipped_record_count: int
    skipped_candidate_count: int


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _regular_file(path: Path, *, maximum_bytes: int) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = path.stat()
    except OSError as error:
        raise EvaluationError("A required source file is unavailable") from error
    if (
        path.is_symlink()
        or _is_reparse_point(path)
        or not resolved.is_file()
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= maximum_bytes
    ):
        raise EvaluationError("A required source file is not a safe regular file")
    return resolved


def _regular_directory(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise EvaluationError("A required source directory is unavailable") from error
    if path.is_symlink() or _is_reparse_point(path) or not resolved.is_dir():
        raise EvaluationError("A required source directory is not a safe directory")
    return resolved


def _sha256_file(path: Path, *, maximum_bytes: Optional[int] = None) -> str:
    before = path.stat()
    if maximum_bytes is not None and before.st_size > maximum_bytes:
        raise EvaluationError("A source file exceeds the supported size")
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while block := stream.read(4 * 1024 * 1024):
            total += len(block)
            if maximum_bytes is not None and total > maximum_bytes:
                raise EvaluationError("A source file exceeds the supported size")
            digest.update(block)
    after = path.stat()
    if (
        total != before.st_size
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise SourceMutationError("A source file changed while it was read")
    return digest.hexdigest()


def _fingerprint(path: Path, *, maximum_bytes: Optional[int] = None) -> FileFingerprint:
    metadata = path.stat()
    return FileFingerprint(
        size_bytes=int(metadata.st_size),
        modified_time_ns=int(metadata.st_mtime_ns),
        sha256=_sha256_file(path, maximum_bytes=maximum_bytes),
    )


def _strict_json_object(raw: bytes, label: str) -> dict[str, Any]:
    def object_pairs(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise EvaluationError(f"{label} contains duplicate JSON keys")
            result[key] = value
        return result

    def reject_constant(_: str) -> object:
        raise EvaluationError(f"{label} contains a non-finite JSON number")

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except EvaluationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvaluationError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise EvaluationError(f"{label} must be a JSON object")
    return value


def _finite_number(value: Any, *, minimum: float = 0.0) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < minimum
    ):
        raise EvaluationError("A persisted candidate numeric value is invalid")
    return float(value)


def _canonical_uuid(value: Any, label: str) -> str:
    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise EvaluationError(f"{label} is invalid") from error
    if str(parsed) != str(value):
        raise EvaluationError(f"{label} is not canonical")
    return str(parsed)


def _relative_posix(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as error:
        raise EvaluationError("A source path escaped the data root") from error


def _candidate_evaluation_id(
    project_id: str,
    job_id: str,
    source_candidate_id: str,
    image_sha256: str,
) -> str:
    basis = (
        f"{project_id}\0{job_id}\0{source_candidate_id}\0{image_sha256}"
    ).encode("ascii")
    return hashlib.sha256(basis).hexdigest()[:20]


def _load_completed_record(
    record_path: Path,
    *,
    data_root: Path,
    project_directory: Path,
    expected_project_id: str,
) -> tuple[dict[str, Any], str]:
    record_path = _regular_file(record_path, maximum_bytes=MAX_RECORD_BYTES)
    digest_path = _regular_file(
        record_path.with_suffix(".sha256"),
        maximum_bytes=4 * 1024,
    )
    expected_digest = digest_path.read_text(
        encoding="ascii", errors="strict"
    ).strip()
    if not SHA256_PATTERN.fullmatch(expected_digest):
        raise EvaluationError("An analysis record digest is invalid")
    raw = record_path.read_bytes()
    actual_digest = hashlib.sha256(raw).hexdigest()
    if actual_digest != expected_digest:
        raise EvaluationError("An analysis record failed its SHA-256 check")
    value = _strict_json_object(raw, "Analysis record")
    if (
        value.get("schema_version") != PERSISTED_JOB_SCHEMA
        or value.get("status") != "completed"
    ):
        raise EvaluationError("An analysis record is not a completed v1 record")
    project_id = _canonical_uuid(value.get("project_id"), "Analysis project id")
    job_id = _canonical_uuid(value.get("job_id"), "Analysis job id")
    if (
        project_id != expected_project_id
        or record_path.name != f"{job_id}.json"
    ):
        raise EvaluationError("An analysis record identity does not match its path")
    result = value.get("result")
    if not isinstance(result, dict):
        raise EvaluationError("A completed analysis record has no result")
    candidates = result.get("candidates")
    if (
        not isinstance(candidates, list)
        or result.get("candidate_count") != len(candidates)
        or len(candidates) > MAX_CANDIDATES
    ):
        raise EvaluationError("An analysis record candidate summary is invalid")
    directory_name = value.get("artifact_directory_name")
    if (
        not isinstance(directory_name, str)
        or not ARTIFACT_DIRECTORY_PATTERN.fullmatch(directory_name)
    ):
        raise EvaluationError("An analysis artifact directory name is invalid")
    artifact_root = _regular_directory(
        project_directory / "candidate-extractions" / directory_name
    )
    expected_parent = (
        project_directory / "candidate-extractions"
    ).resolve(strict=True)
    if artifact_root.parent != expected_parent:
        raise EvaluationError("An analysis artifact directory escaped its project")
    value["_evaluation_artifact_root"] = artifact_root
    value["_evaluation_record_relative"] = _relative_posix(record_path, data_root)
    return value, actual_digest


def discover_candidates(
    data_root: Path | str,
    *,
    project_id: Optional[str] = None,
    limit: Optional[int] = None,
) -> Discovery:
    """Read and integrity-check persisted candidates without mutating them."""
    root = _regular_directory(Path(data_root))
    projects_root = _regular_directory(root / "projects")
    selected_project = (
        _canonical_uuid(project_id, "Selected project id")
        if project_id is not None
        else None
    )
    if limit is not None and (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_CANDIDATES
    ):
        raise EvaluationError("Candidate limit must be between 1 and 512")

    candidates: list[SourceCandidate] = []
    completed_record_count = 0
    skipped_record_count = 0
    skipped_candidate_count = 0

    for project_directory in sorted(
        (path for path in projects_root.iterdir() if path.is_dir()),
        key=lambda value: value.name,
    ):
        try:
            current_project_id = _canonical_uuid(
                project_directory.name, "Project directory id"
            )
        except EvaluationError:
            skipped_record_count += 1
            continue
        if selected_project is not None and current_project_id != selected_project:
            continue
        if project_directory.is_symlink() or _is_reparse_point(project_directory):
            skipped_record_count += 1
            continue
        records_root = project_directory / "analysis-job-records"
        try:
            records_root = _regular_directory(records_root)
        except EvaluationError:
            skipped_record_count += 1
            continue

        for record_path in sorted(records_root.glob("*.json"), key=lambda value: value.name):
            try:
                record, record_digest = _load_completed_record(
                    record_path,
                    data_root=root,
                    project_directory=project_directory.resolve(strict=True),
                    expected_project_id=current_project_id,
                )
                result = record["result"]
                artifact_root: Path = record["_evaluation_artifact_root"]
                source_file_name = record.get("file_name")
                if not isinstance(source_file_name, str) or not source_file_name:
                    raise EvaluationError("An analysis source filename is invalid")
                job_id = str(record["job_id"])
                seen_source_ids: set[str] = set()
                record_candidates: list[SourceCandidate] = []
                for candidate in result["candidates"]:
                    if not isinstance(candidate, dict):
                        raise EvaluationError("A persisted candidate is invalid")
                    source_candidate_id = candidate.get("candidate_id")
                    if (
                        not isinstance(source_candidate_id, str)
                        or not CANDIDATE_ID_PATTERN.fullmatch(source_candidate_id)
                        or source_candidate_id in seen_source_ids
                    ):
                        raise EvaluationError("A persisted candidate id is invalid")
                    seen_source_ids.add(source_candidate_id)
                    image = candidate.get("representative_image")
                    if not isinstance(image, dict):
                        raise EvaluationError("A candidate representative image is missing")
                    relative_path = image.get("relative_path")
                    size_bytes = image.get("size_bytes")
                    image_sha256 = image.get("sha256")
                    if (
                        not isinstance(relative_path, str)
                        or Path(relative_path).is_absolute()
                        or len(Path(relative_path).parts) != 1
                        or ".." in Path(relative_path).parts
                        or isinstance(size_bytes, bool)
                        or not isinstance(size_bytes, int)
                        or not 1 <= size_bytes <= MAX_IMAGE_BYTES
                        or not isinstance(image_sha256, str)
                        or not SHA256_PATTERN.fullmatch(image_sha256)
                    ):
                        raise EvaluationError("Candidate image metadata is invalid")
                    image_path = _regular_file(
                        artifact_root / relative_path,
                        maximum_bytes=MAX_IMAGE_BYTES,
                    )
                    if image_path.parent != artifact_root:
                        raise EvaluationError("A candidate image escaped its artifact set")
                    if image_path.stat().st_size != size_bytes:
                        raise EvaluationError("A candidate image size does not match")
                    if (
                        _sha256_file(image_path, maximum_bytes=MAX_IMAGE_BYTES)
                        != image_sha256
                    ):
                        raise EvaluationError("A candidate image SHA-256 does not match")
                    triggers = candidate.get("triggers")
                    if (
                        not isinstance(triggers, list)
                        or any(
                            not isinstance(value, str) or not value
                            for value in triggers
                        )
                    ):
                        raise EvaluationError("A candidate trigger list is invalid")
                    evaluation_candidate_id = _candidate_evaluation_id(
                        current_project_id,
                        job_id,
                        source_candidate_id,
                        image_sha256,
                    )
                    normalized_candidate = SourceCandidate(
                        project_id=current_project_id,
                        job_id=job_id,
                        source_file_name=source_file_name,
                        source_candidate_id=source_candidate_id,
                        evaluation_candidate_id=evaluation_candidate_id,
                        representative_timestamp_seconds=_finite_number(
                            candidate.get("representative_timestamp_seconds")
                        ),
                        scene_score=_finite_number(candidate.get("scene_score")),
                        sharpness_score=_finite_number(
                            candidate.get("sharpness_score")
                        ),
                        triggers=tuple(triggers),
                        record_path=record_path.resolve(strict=True),
                        record_relative_path=record[
                            "_evaluation_record_relative"
                        ],
                        record_sha256=record_digest,
                        image_path=image_path,
                        image_relative_path=_relative_posix(image_path, root),
                        image_size_bytes=size_bytes,
                        image_sha256=image_sha256,
                    )
                    if (
                        limit is None
                        or len(candidates) + len(record_candidates) < limit
                    ):
                        record_candidates.append(normalized_candidate)
                    else:
                        skipped_candidate_count += 1
                candidates.extend(record_candidates)
                completed_record_count += 1
            except (EvaluationError, OSError, UnicodeError, json.JSONDecodeError):
                skipped_record_count += 1
                continue

    if selected_project is not None and not any(
        candidate.project_id == selected_project for candidate in candidates
    ):
        raise EvaluationError("The selected project has no valid persisted candidates")
    if not candidates:
        raise EvaluationError("No valid persisted candidates were discovered")
    evaluation_ids = [item.evaluation_candidate_id for item in candidates]
    if len(evaluation_ids) != len(set(evaluation_ids)):
        raise EvaluationError("Stable evaluation candidate ids collided")
    return Discovery(
        candidates=tuple(candidates),
        completed_record_count=completed_record_count,
        skipped_record_count=skipped_record_count,
        skipped_candidate_count=skipped_candidate_count,
    )


def _protected_paths(data_root: Path, candidates: Sequence[SourceCandidate]) -> list[Path]:
    paths: set[Path] = set()
    catalog = data_root / "catalog.sqlite3"
    if catalog.is_file():
        paths.add(_regular_file(catalog, maximum_bytes=256 * 1024 * 1024))
    label_root = data_root / "candidate-labels"
    if label_root.is_dir() and not label_root.is_symlink():
        for path in label_root.rglob("*"):
            if path.is_file():
                paths.add(_regular_file(path, maximum_bytes=MAX_RECORD_BYTES))
    projects_root = data_root / "projects"
    if projects_root.is_dir() and not projects_root.is_symlink():
        for records_root in projects_root.glob("*/analysis-job-records"):
            if not records_root.is_dir() or records_root.is_symlink():
                continue
            for path in records_root.iterdir():
                if path.is_file():
                    paths.add(_regular_file(path, maximum_bytes=MAX_RECORD_BYTES))
    for candidate in candidates:
        paths.add(candidate.record_path)
        digest_path = candidate.record_path.with_suffix(".sha256")
        paths.add(_regular_file(digest_path, maximum_bytes=4 * 1024))
        paths.add(candidate.image_path)
    return sorted(paths, key=lambda value: str(value).casefold())


def snapshot_protected_sources(
    data_root: Path,
    candidates: Sequence[SourceCandidate],
) -> dict[Path, FileFingerprint]:
    return {
        path: _fingerprint(path)
        for path in _protected_paths(data_root, candidates)
    }


def verify_protected_sources(
    before: Mapping[Path, FileFingerprint],
    data_root: Path,
    candidates: Sequence[SourceCandidate],
) -> None:
    after_paths = set(_protected_paths(data_root, candidates))
    if set(before) != after_paths:
        raise SourceMutationError("The protected source file inventory changed")
    for path, expected in before.items():
        if not path.is_file() or path.is_symlink() or _is_reparse_point(path):
            raise SourceMutationError("A protected source file disappeared")
        actual = _fingerprint(path)
        if actual != expected:
            raise SourceMutationError("A protected source file changed")


def _prepare_output_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise EvaluationError("The evaluation output directory must be absolute")
    if path.exists() or path.is_symlink():
        raise EvaluationError("The evaluation output directory already exists")
    try:
        path.mkdir(parents=True, exist_ok=False)
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise EvaluationError("The evaluation output directory could not be created") from error
    if path.is_symlink() or _is_reparse_point(path) or not resolved.is_dir():
        raise EvaluationError("The evaluation output directory is unsafe")
    return resolved


def _stage_candidates(
    candidates: Sequence[SourceCandidate],
    staging_root: Path,
) -> list[dict[str, Any]]:
    staging_root.mkdir(exist_ok=False)
    if staging_root.is_symlink() or _is_reparse_point(staging_root):
        raise EvaluationError("The staging directory is unsafe")
    rows: list[dict[str, Any]] = []
    for item in candidates:
        suffix = item.image_path.suffix.casefold()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise EvaluationError("A candidate image extension is unsupported")
        target = staging_root / f"{item.evaluation_candidate_id}{suffix}"
        shutil.copyfile(item.image_path, target)
        copied = _regular_file(target, maximum_bytes=MAX_IMAGE_BYTES)
        copied_digest = _sha256_file(copied, maximum_bytes=MAX_IMAGE_BYTES)
        if (
            copied.stat().st_size != item.image_size_bytes
            or copied_digest != item.image_sha256
        ):
            raise EvaluationError("A staged candidate copy failed integrity checks")
        rows.append(
            {
                "candidate_id": item.evaluation_candidate_id,
                "representative_image": {
                    "relative_path": copied.name,
                    "size_bytes": item.image_size_bytes,
                    "sha256": item.image_sha256,
                },
            }
        )
    return rows


def _inference_factory(
    model_manifest: Path,
    timeout_seconds: float,
) -> LocalVisionInferenceService:
    return LocalVisionInferenceService(
        model_pack_path=model_manifest,
        timeout_seconds=timeout_seconds,
    )


def _score_contract(engine: Any) -> tuple[str, str]:
    if not isinstance(engine, str) or engine not in SUPPORTED_EVALUATION_ENGINES:
        raise EvaluationError(
            "The configured pack engine is not supported by this evaluator"
        )
    calibration = SUPPORTED_EVALUATION_ENGINES[engine]
    disclaimer = (
        CLIP_DISCLAIMER_KO
        if calibration == "zero_shot_relative"
        else ONNX_DISCLAIMER_KO
    )
    return calibration, disclaimer


def _normalized_result(
    candidate: SourceCandidate,
    inference: Mapping[str, Any],
    *,
    expected_calibration: str,
) -> dict[str, Any]:
    predictions = inference.get("predictions")
    if not isinstance(predictions, list):
        raise EvaluationError("Inference output predictions are invalid")
    safe_predictions: list[dict[str, Any]] = []
    for rank, prediction in enumerate(predictions, start=1):
        if not isinstance(prediction, Mapping):
            raise EvaluationError("An inference prediction is invalid")
        score = prediction.get("confidence")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
            or not 0 <= float(score) <= 1
        ):
            raise EvaluationError("An inference relative score is invalid")
        calibration = prediction.get("calibration")
        if calibration != expected_calibration:
            raise EvaluationError(
                "A model proposal calibration does not match its pack engine"
            )
        rounded_score = round(float(score), 8)
        safe_predictions.append(
            {
                "rank": rank,
                "issue_type": prediction.get("issue_type"),
                "score": rounded_score,
                "relative_score": (
                    rounded_score
                    if calibration == "zero_shot_relative"
                    else None
                ),
                "model_score": (
                    rounded_score if calibration == "model_score" else None
                ),
                "source_prompt": prediction.get("source_label"),
                "source_label": prediction.get("source_label"),
                "calibration": calibration,
                "box": prediction.get("box"),
            }
        )
    return {
        "project_id": candidate.project_id,
        "job_id": candidate.job_id,
        "source_file_name": candidate.source_file_name,
        "source_candidate_id": candidate.source_candidate_id,
        "evaluation_candidate_id": candidate.evaluation_candidate_id,
        "representative_timestamp_seconds": (
            candidate.representative_timestamp_seconds
        ),
        "scene_score": candidate.scene_score,
        "sharpness_score": candidate.sharpness_score,
        "triggers": list(candidate.triggers),
        "source_record_ref": candidate.record_relative_path,
        "source_record_sha256": candidate.record_sha256,
        "source_image_ref": candidate.image_relative_path,
        "source_image_size_bytes": candidate.image_size_bytes,
        "source_image_sha256": candidate.image_sha256,
        "inference_status": inference.get("status"),
        "inference_mode": inference.get("mode"),
        "duration_ms": inference.get("duration_ms"),
        "warning_code": inference.get("warning_code"),
        "predictions": safe_predictions,
        "review_required": bool(safe_predictions),
        "review_state": "unreviewed",
        "automatic_label_created": False,
        "incident_created": False,
        "model_promoted": False,
    }


def _write_json(path: Path, value: Mapping[str, Any] | Sequence[Any]) -> None:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    path.write_bytes(encoded + b"\n")


def _write_scores_csv(path: Path, results: Sequence[Mapping[str, Any]]) -> None:
    fields = [
        "project_id",
        "job_id",
        "source_file_name",
        "source_candidate_id",
        "evaluation_candidate_id",
        "representative_timestamp_seconds",
        "inference_status",
        "review_required",
        "review_state",
        "rank",
        "issue_type",
        "score",
        "relative_score",
        "model_score",
        "calibration",
        "source_prompt",
        "box_json",
        "source_image_ref",
        "source_image_sha256",
        "automatic_label_created",
        "incident_created",
        "model_promoted",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for result in results:
            predictions = result["predictions"]
            rows = predictions if predictions else [None]
            for prediction in rows:
                writer.writerow(
                    {
                        "project_id": result["project_id"],
                        "job_id": result["job_id"],
                        "source_file_name": result["source_file_name"],
                        "source_candidate_id": result["source_candidate_id"],
                        "evaluation_candidate_id": result[
                            "evaluation_candidate_id"
                        ],
                        "representative_timestamp_seconds": result[
                            "representative_timestamp_seconds"
                        ],
                        "inference_status": result["inference_status"],
                        "review_required": str(result["review_required"]).lower(),
                        "review_state": result["review_state"],
                        "rank": prediction["rank"] if prediction else "",
                        "issue_type": (
                            prediction["issue_type"] if prediction else ""
                        ),
                        "score": prediction["score"] if prediction else "",
                        "relative_score": (
                            prediction["relative_score"]
                            if prediction
                            and prediction["relative_score"] is not None
                            else ""
                        ),
                        "model_score": (
                            prediction["model_score"]
                            if prediction
                            and prediction["model_score"] is not None
                            else ""
                        ),
                        "calibration": (
                            prediction["calibration"] if prediction else ""
                        ),
                        "source_prompt": (
                            prediction["source_prompt"] if prediction else ""
                        ),
                        "box_json": (
                            json.dumps(
                                prediction["box"],
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            )
                            if prediction and prediction["box"] is not None
                            else ""
                        ),
                        "source_image_ref": result["source_image_ref"],
                        "source_image_sha256": result["source_image_sha256"],
                        "automatic_label_created": "false",
                        "incident_created": "false",
                        "model_promoted": "false",
                    }
                )


def _safe_evaluation_id(
    requested: Optional[str],
    *,
    output_name: str,
    manifest_sha256: str,
) -> str:
    if requested is not None:
        if (
            not isinstance(requested, str)
            or not EVALUATION_ID_PATTERN.fullmatch(requested)
        ):
            raise EvaluationError(
                "Sidecar evaluation id must use 1 to 128 ASCII letters, "
                "digits, dots, underscores, or hyphens"
            )
        return requested
    if EVALUATION_ID_PATTERN.fullmatch(output_name):
        return output_name
    digest = hashlib.sha256(
        f"{output_name}\0{manifest_sha256}".encode("utf-8")
    ).hexdigest()[:24]
    return f"candidate-evaluation-{digest}"


def build_sidecar_job_payloads(
    results: Sequence[Mapping[str, Any]],
    *,
    model_summary: Mapping[str, Any],
    evaluation_id: str,
) -> list[dict[str, Any]]:
    """Build job-scoped inputs accepted by ``ExperimentalProposalStore``."""
    provider = model_summary.get("engine")
    expected_calibration, _ = _score_contract(provider)
    model = {
        "provider": provider,
        "pack_id": model_summary.get("pack_id"),
        "pack_version": model_summary.get("pack_version"),
        "manifest_sha256": model_summary.get("manifest_sha256"),
    }
    evaluation = {
        "evaluation_id": evaluation_id,
        "producer": "trafficvisson-candidate-evaluator",
        "producer_version": "0.2.0",
    }
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for result in results:
        if result.get("inference_mode") != "local_model" or result.get(
            "inference_status"
        ) not in {"classified", "no_detection"}:
            raise EvaluationError(
                "Failed, timed-out, or fallback inference cannot be recorded "
                "as an experimental sidecar"
            )
        predictions = []
        for prediction in result["predictions"]:
            if prediction.get("calibration") != expected_calibration:
                raise EvaluationError(
                    "A sidecar prediction does not match the model provider"
                )
            predictions.append(
                {
                    "issue_type": prediction["issue_type"],
                    "relative_score": prediction["relative_score"],
                    "model_score": prediction["model_score"],
                    "box": prediction["box"],
                    "source_label": prediction["source_label"],
                    "calibration": prediction["calibration"],
                }
            )
        # A no-suggestion row must not be persisted. latest_for_job selects
        # the newest sidecar per candidate, so recording an empty ONNX row
        # after a classified CLIP row would incorrectly hide the earlier
        # proposal. The complete 349-row outcome remains in predictions.json.
        if not predictions:
            continue
        row = {
            "candidate_id": result["source_candidate_id"],
            "status": "classified",
            "predictions": predictions,
        }
        key = (str(result["project_id"]), str(result["job_id"]))
        grouped.setdefault(key, []).append(row)
    return [
        {
            "project_id": project_id,
            "analysis_job_id": job_id,
            "model": model,
            "dataset": None,
            "evaluation": evaluation,
            "proposals": sorted(
                proposals,
                key=lambda value: value["candidate_id"],
            ),
        }
        for (project_id, job_id), proposals in sorted(grouped.items())
    ]


def _record_experimental_sidecars(
    data_root: Path,
    payloads: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Persist opt-in review-only sidecars through the production store."""
    from app.analysis_jobs import AnalysisJobService
    from app.candidate_labels import CandidateResolutionError
    from app.experimental_proposals import ExperimentalProposalStore
    from app.media_intake import MediaIntakeService

    media = MediaIntakeService(minimum_interval_seconds=0)
    analysis = AnalysisJobService(media, data_root=data_root)

    def resolve_completed_candidate(
        project_id: UUID,
        analysis_job_id: UUID,
        candidate_id: str,
    ) -> Mapping[str, Any]:
        job = analysis.get(project_id, analysis_job_id)
        if job["status"] != "completed" or job.get("result") is None:
            raise CandidateResolutionError(
                "the analysis candidate is not from a completed job"
            )
        candidate = next(
            (
                item
                for item in job["result"]["candidates"]
                if item["candidate_id"] == candidate_id
            ),
            None,
        )
        if candidate is None:
            raise CandidateResolutionError(
                "the completed analysis candidate was not found"
            )
        return {
            "status": "completed",
            "project_id": str(project_id),
            "analysis_job_id": str(analysis_job_id),
            "candidate_id": candidate_id,
            "metadata": candidate,
        }

    try:
        store = ExperimentalProposalStore(data_root, resolve_completed_candidate)
        run_ids: list[str] = []
        proposal_count = 0
        for payload in payloads:
            rows = store.record_run(
                payload["project_id"],
                payload["analysis_job_id"],
                model=payload["model"],
                dataset=payload["dataset"],
                evaluation=payload["evaluation"],
                proposals=payload["proposals"],
            )
            if not rows:
                raise EvaluationError(
                    "The experimental sidecar store returned no proposals"
                )
            run_id = rows[0].get("run_id")
            if not isinstance(run_id, str):
                raise EvaluationError(
                    "The experimental sidecar store returned an invalid run id"
                )
            run_ids.append(run_id)
            proposal_count += len(rows)
        return {
            "requested": True,
            "recorded": True,
            "run_count": len(run_ids),
            "proposal_count": proposal_count,
            "run_ids": run_ids,
            "review_only": True,
            "automatic_labeling_allowed": False,
        }
    finally:
        analysis.shutdown()
        media.shutdown()


def _write_contact_sheets(
    output_root: Path,
    results: Sequence[Mapping[str, Any]],
    candidate_by_id: Mapping[str, SourceCandidate],
    *,
    maximum_items: int,
) -> list[Path]:
    proposals = [row for row in results if row["predictions"]]
    proposals.sort(
        key=lambda row: (
            -float(row["predictions"][0]["score"]),
            str(row["evaluation_candidate_id"]),
        )
    )
    proposals = proposals[:maximum_items]
    if not proposals:
        return []
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageOps
    except Exception as error:
        raise EvaluationError("Pillow is required to create contact sheets") from error

    columns = 3
    rows_per_page = 4
    card_width = 360
    image_height = 202
    caption_height = 58
    margin = 16
    page_size = columns * rows_per_page
    font = ImageFont.load_default()
    sheets: list[Path] = []
    for page_index, offset in enumerate(
        range(0, len(proposals), page_size), start=1
    ):
        page_rows = proposals[offset : offset + page_size]
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
        for index, row in enumerate(page_rows):
            candidate = candidate_by_id[row["evaluation_candidate_id"]]
            with Image.open(candidate.image_path) as source:
                source.load()
                image = ImageOps.contain(
                    source.convert("RGB"),
                    (card_width, image_height),
                    method=Image.Resampling.LANCZOS,
                )
            column = index % columns
            line = index // columns
            left = margin + column * (card_width + margin)
            top = margin + line * (image_height + caption_height + margin)
            image_left = left + (card_width - image.width) // 2
            image_top = top + (image_height - image.height) // 2
            canvas.paste(image, (image_left, image_top))
            prediction = row["predictions"][0]
            box = prediction.get("box")
            if isinstance(box, Mapping):
                x1 = image_left + float(box["x"]) * image.width
                y1 = image_top + float(box["y"]) * image.height
                x2 = x1 + float(box["width"]) * image.width
                y2 = y1 + float(box["height"]) * image.height
                draw.rectangle(
                    (x1, y1, x2, y2),
                    outline=(255, 55, 45),
                    width=4,
                )
            score_label = (
                "relative"
                if prediction["calibration"] == "zero_shot_relative"
                else "model_score"
            )
            caption = (
                f"{prediction['issue_type']}  "
                f"{score_label}={prediction['score']:.6f}\n"
                f"{row['source_file_name'][:32]}  "
                f"t={row['representative_timestamp_seconds']:.3f}s\n"
                f"id={row['evaluation_candidate_id']}"
            )
            draw.multiline_text(
                (left + 4, top + image_height + 4),
                caption,
                fill="black",
                font=font,
                spacing=2,
            )
        path = output_root / f"review-contact-sheet-{page_index:03d}.jpg"
        canvas.save(path, format="JPEG", quality=90, optimize=True)
        sheets.append(path)
    return sheets


def _markdown_report(
    *,
    generated_at: str,
    discovery: Discovery,
    summary: Mapping[str, Any],
    results: Sequence[Mapping[str, Any]],
    contact_sheets: Sequence[Path],
    contact_sheet_maximum: int,
    disclaimer_ko: str,
    calibration: str,
    operator_note: Optional[str],
    sidecar_summary: Mapping[str, Any],
) -> str:
    status_counts = Counter(str(row["inference_status"]) for row in results)
    issue_counts = Counter(
        str(row["predictions"][0]["issue_type"])
        for row in results
        if row["predictions"]
    )
    proposal_count = sum(bool(row["predictions"]) for row in results)
    prediction_count = sum(len(row["predictions"]) for row in results)
    total_duration = sum(
        int(row["duration_ms"])
        for row in results
        if isinstance(row.get("duration_ms"), int)
    )
    lines = [
        "# TrafficSuperVision 기존 후보 로컬 모델 비파괴 평가",
        "",
        f"생성 시각(UTC): `{generated_at}`",
        "",
        "## 해석 제한",
        "",
        disclaimer_ko,
        "",
        "- 사용자 확정 라벨: `0건`",
        "- 자동 생성 라벨: `0건`",
        "- 자동 생성 유지보수 사건: `0건`",
        "- 모델 자동 승격: `아니오`",
        (
            "- 검토 전용 sidecar 기록: "
            f"`{sidecar_summary.get('run_count', 0)}개 run / "
            f"{sidecar_summary.get('proposal_count', 0)}개 후보`"
            if sidecar_summary.get("recorded") is True
            else (
                "- 검토 전용 sidecar 기록: "
                "`분류 제안이 없어 생성하지 않음`"
                if sidecar_summary.get("requested") is True
                else "- 검토 전용 sidecar 기록: `요청하지 않음`"
            )
        ),
        "- 정밀도·재현율: 사용자 확정 정답이 없으므로 산출하지 않음",
        "",
        "## 실행 범위",
        "",
        f"- 무결성 검증 완료 분석 레코드: `{discovery.completed_record_count}건`",
        f"- 평가한 대표 JPEG: `{len(results)}건`",
        f"- 검토 제안이 생성된 JPEG: `{proposal_count}건`",
        f"- 내보낸 모델 제안 점수 행: `{prediction_count}건`",
        f"- 워커가 보고한 후보별 추론 시간 합계: `{total_duration}ms`",
        f"- 건너뛴 레코드: `{discovery.skipped_record_count}건`",
        f"- 제한 때문에 건너뛴 후보: `{discovery.skipped_candidate_count}건`",
        "",
        "## 모델",
        "",
        f"- Pack: `{summary.get('pack_id')}` `{summary.get('pack_version')}`",
        f"- Engine: `{summary.get('engine')}`",
        f"- Manifest SHA-256: `{summary.get('manifest_sha256')}`",
        f"- Calibration: `{calibration}`",
        "",
        "## 추론 상태",
        "",
        "| 상태 | 후보 수 |",
        "| --- | ---: |",
    ]
    for status, count in sorted(status_counts.items()):
        lines.append(f"| `{status}` | {count} |")
    lines.extend(
        [
            "",
            "## 최상위 검토 제안 분포",
            "",
            "| 유형 | 후보 수 |",
            "| --- | ---: |",
        ]
    )
    if issue_counts:
        for issue_type, count in sorted(issue_counts.items()):
            lines.append(f"| `{issue_type}` | {count} |")
    else:
        lines.append("| 제안 없음 | 0 |")
    lines.extend(
        [
            "",
            "이 분포는 모델 성능 지표가 아니다. 동일 영상에서 연속 추출된 "
            "장면이 포함될 수 있고, 정답 라벨이 없는 검토 대기열의 구성만 "
            "보여준다.",
            "",
            "## 산출물",
            "",
            "- `predictions.json`: 출처 해시와 전체 실험 제안",
            "- `candidate_scores.csv`: Excel 호환 UTF-8 CSV",
            (
                "- `sidecar_job_payloads.json`: "
                "`ExperimentalProposalStore.record_run`에 전달 가능한 "
                "job별 검토 전용 payload"
            ),
            "- `manifest.json`: 산출물 SHA-256",
        ]
    )
    if contact_sheets:
        lines.append(
            f"- `review-contact-sheet-*.jpg`: 상위 상대점수 순 검토 연락시트 "
            f"({min(proposal_count, contact_sheet_maximum)}건)"
        )
    else:
        lines.append("- 연락시트: 임계값을 넘은 검토 제안이 없어 생성하지 않음")
    if operator_note:
        lines.extend(
            [
                "",
                "## 사전 스모크 및 하드 네거티브 주의",
                "",
                operator_note,
                "",
                "위 사례는 자동 승인 대상이 아니라 사용자 질문 카드에서 "
                "`아니오` 또는 적절한 정답으로 확인해 hard negative로 "
                "보존해야 한다.",
            ]
        )
    lines.extend(
        [
            "",
            "## 다음 단계",
            "",
            "이 CSV와 연락시트는 질문형 라벨링 대기열을 구성하는 참고자료다. "
            "사용자가 각 이미지를 `예`, `아니오`, `판단 어려움`, `영역 수정` "
            "중 하나로 확인하기 전에는 학습 데이터나 보고서 근거로 사용할 수 "
            "없다.",
            "",
        ]
    )
    return "\n".join(lines)


def _output_manifest(
    output_root: Path,
    *,
    generated_at: str,
    model_summary: Mapping[str, Any],
    calibration: str,
) -> dict[str, Any]:
    rows = []
    for path in sorted(
        (
            value
            for value in output_root.iterdir()
            if value.is_file() and value.name != "manifest.json"
        ),
        key=lambda value: value.name,
    ):
        rows.append(
            {
                "path": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    return {
        "schema_version": EVALUATION_SCHEMA,
        "generated_at": generated_at,
        "purpose": "unreviewed_model_proposal_evaluation",
        "score_semantics": SCORE_SEMANTICS_LABELS[calibration],
        "automatic_labels_created": 0,
        "incidents_created": 0,
        "model_promoted": False,
        "model": {
            key: model_summary.get(key)
            for key in (
                "engine",
                "pack_id",
                "pack_version",
                "manifest_sha256",
                "runtime_spdx",
                "weights_spdx",
            )
        },
        "artifacts": rows,
    }


def run_evaluation(
    *,
    data_root: Path | str,
    model_manifest: Path | str,
    output_directory: Path | str,
    project_id: Optional[str] = None,
    limit: Optional[int] = None,
    timeout_seconds: float = 600.0,
    maximum_contact_sheet_items: int = MAX_CANDIDATES,
    operator_note: Optional[str] = None,
    record_sidecars: bool = False,
    sidecar_evaluation_id: Optional[str] = None,
    service_factory: Callable[
        [Path, float], Any
    ] = _inference_factory,
    sidecar_recorder: Callable[
        [Path, Sequence[Mapping[str, Any]]], Mapping[str, Any]
    ] = _record_experimental_sidecars,
) -> dict[str, Any]:
    """Run a non-mutating local-model proposal evaluation and return its envelope."""
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or not 5 <= float(timeout_seconds) <= 3_600
    ):
        raise EvaluationError("Inference timeout must be between 5 and 3600 seconds")
    if (
        isinstance(maximum_contact_sheet_items, bool)
        or not isinstance(maximum_contact_sheet_items, int)
        or not 0 <= maximum_contact_sheet_items <= MAX_CANDIDATES
    ):
        raise EvaluationError("Contact-sheet item limit must be between 0 and 512")
    if operator_note is not None and (
        not isinstance(operator_note, str)
        or not operator_note.strip()
        or len(operator_note) > 2_000
        or "\x00" in operator_note
    ):
        raise EvaluationError("Operator note must be 1 to 2000 safe characters")
    operator_note = operator_note.strip() if operator_note is not None else None
    if not isinstance(record_sidecars, bool):
        raise EvaluationError("record_sidecars must be boolean")
    if sidecar_evaluation_id is not None and not record_sidecars:
        raise EvaluationError(
            "A sidecar evaluation id requires --record-sidecars"
        )

    root = _regular_directory(Path(data_root))
    manifest = _regular_file(
        Path(model_manifest),
        maximum_bytes=128 * 1024,
    )
    discovery = discover_candidates(root, project_id=project_id, limit=limit)
    protected_before = snapshot_protected_sources(root, discovery.candidates)
    output_root = _prepare_output_directory(Path(output_directory))
    staging_root = output_root / ".staging"
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    started = time.monotonic()
    try:
        staged = _stage_candidates(discovery.candidates, staging_root)
        service = service_factory(manifest, float(timeout_seconds))
        status = service.status()
        if (
            not isinstance(status, Mapping)
            or status.get("enabled") is not True
        ):
            raise EvaluationError(
                "The configured model pack is not enabled"
            )
        expected_calibration, disclaimer_ko = _score_contract(
            status.get("engine")
        )
        inference_by_id, inference_summary = service.infer_candidates(
            staged,
            staging_root,
        )
        if set(inference_by_id) != {
            item.evaluation_candidate_id for item in discovery.candidates
        }:
            raise EvaluationError("Inference output candidate ids do not match")
        results = [
            _normalized_result(
                item,
                inference_by_id[item.evaluation_candidate_id],
                expected_calibration=expected_calibration,
            )
            for item in discovery.candidates
        ]
        verify_protected_sources(
            protected_before,
            root,
            discovery.candidates,
        )
        evaluation_id = _safe_evaluation_id(
            sidecar_evaluation_id,
            output_name=output_root.name,
            manifest_sha256=str(inference_summary.get("manifest_sha256")),
        )
        sidecar_payloads = build_sidecar_job_payloads(
            results,
            model_summary=inference_summary,
            evaluation_id=evaluation_id,
        )
        classified_proposal_count = sum(
            1 for result in results if result["predictions"]
        )
        sidecar_summary: Mapping[str, Any] = {
            "requested": False,
            "recorded": False,
            "run_count": 0,
            "proposal_count": 0,
            "run_ids": [],
            "review_only": True,
            "automatic_labeling_allowed": False,
            "evaluation_id": evaluation_id,
        }
        if record_sidecars:
            if sidecar_payloads:
                recorded = dict(sidecar_recorder(root, sidecar_payloads))
                sidecar_summary = {
                    **recorded,
                    "evaluation_id": evaluation_id,
                }
                if (
                    sidecar_summary.get("recorded") is not True
                    or sidecar_summary.get("proposal_count")
                    != classified_proposal_count
                    or sidecar_summary.get("run_count")
                    != len(sidecar_payloads)
                ):
                    raise EvaluationError(
                        "Experimental sidecar recording did not cover every "
                        "classified proposal"
                    )
            else:
                sidecar_summary = {
                    **sidecar_summary,
                    "requested": True,
                    "reason": "no_classified_proposals",
                }
            verify_protected_sources(
                protected_before,
                root,
                discovery.candidates,
            )

        envelope = {
            "schema_version": EVALUATION_SCHEMA,
            "generated_at": generated_at,
            "completed_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "purpose": "unreviewed_model_proposal_evaluation",
            "disclaimer_ko": disclaimer_ko,
            "operator_note": operator_note,
            "score_semantics": {
                "calibration": expected_calibration,
                "label": SCORE_SEMANTICS_LABELS[expected_calibration],
                "is_probability": False,
                "is_accuracy": False,
                "is_maintenance_decision": False,
            },
            "side_effects": {
                "analysis_records_modified": 0,
                "label_records_modified": 0,
                "sqlite_databases_modified": 0,
                "automatic_labels_created": 0,
                "incidents_created": 0,
                "model_promoted": False,
                "experimental_sidecar_runs_created": int(
                    sidecar_summary.get("run_count", 0)
                ),
                "experimental_sidecar_proposals_created": int(
                    sidecar_summary.get("proposal_count", 0)
                ),
            },
            "experimental_sidecars": dict(sidecar_summary),
            "source": {
                "completed_record_count": discovery.completed_record_count,
                "candidate_count": len(discovery.candidates),
                "skipped_record_count": discovery.skipped_record_count,
                "skipped_candidate_count": discovery.skipped_candidate_count,
                "protected_file_count": len(protected_before),
                "protected_sources_verified_unchanged": True,
            },
            "model": dict(inference_summary),
            "results": results,
        }
        predictions_path = output_root / "predictions.json"
        scores_path = output_root / "candidate_scores.csv"
        _write_json(predictions_path, envelope)
        _write_scores_csv(scores_path, results)
        _write_json(
            output_root / "sidecar_job_payloads.json",
            {
                "schema_version": EVALUATION_SCHEMA,
                "evaluation_id": evaluation_id,
                "recorded": bool(sidecar_summary.get("recorded")),
                "review_only": True,
                "automatic_labeling_allowed": False,
                "jobs": sidecar_payloads,
            },
        )
        candidate_by_id = {
            item.evaluation_candidate_id: item for item in discovery.candidates
        }
        contact_sheets = _write_contact_sheets(
            output_root,
            results,
            candidate_by_id,
            maximum_items=maximum_contact_sheet_items,
        )
        markdown = _markdown_report(
            generated_at=generated_at,
            discovery=discovery,
            summary=inference_summary,
            results=results,
            contact_sheets=contact_sheets,
            contact_sheet_maximum=maximum_contact_sheet_items,
            disclaimer_ko=disclaimer_ko,
            calibration=expected_calibration,
            operator_note=operator_note,
            sidecar_summary=sidecar_summary,
        )
        (output_root / "EVALUATION.md").write_text(
            markdown,
            encoding="utf-8",
        )
        verify_protected_sources(
            protected_before,
            root,
            discovery.candidates,
        )
        _write_json(
            output_root / "manifest.json",
            _output_manifest(
                output_root,
                generated_at=generated_at,
                model_summary=inference_summary,
                calibration=expected_calibration,
            ),
        )
        return envelope
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root)


def _default_data_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise EvaluationError("LOCALAPPDATA is unavailable")
    return Path(local_app_data) / "TrafficSuperVision" / "bridge-data"


def _default_model_manifest() -> Path:
    program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    return (
        Path(program_data)
        / "TrafficSuperVision"
        / "ModelPacks"
        / "trafficvisson-laion-clip-b32-road-baseline-0.1.0"
        / "trafficsupervision-model-pack.json"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate persisted candidate JPEGs as unreviewed local-model proposals. "
            "The command never creates labels, incidents, or model promotions."
        )
    )
    parser.add_argument("--data-root", default=str(_default_data_root()))
    parser.add_argument(
        "--model-manifest",
        default=str(_default_model_manifest()),
    )
    parser.add_argument("--output-directory", required=True)
    parser.add_argument("--project-id")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument(
        "--maximum-contact-sheet-items",
        type=int,
        default=MAX_CANDIDATES,
    )
    parser.add_argument(
        "--operator-note",
        help=(
            "Bounded research note copied into the evaluation report, for "
            "example a known false-positive or hard-negative observation."
        ),
    )
    parser.add_argument(
        "--record-sidecars",
        action="store_true",
        help=(
            "Opt in to writing immutable review-only experimental proposal "
            "sidecars under the selected bridge data root. This still creates "
            "no labels, incidents, report inputs, or model promotions."
        ),
    )
    parser.add_argument(
        "--sidecar-evaluation-id",
        help=(
            "Optional 1-128 character provenance id for --record-sidecars. "
            "Defaults to the output directory name when safe."
        ),
    )
    arguments = parser.parse_args(argv)
    try:
        envelope = run_evaluation(
            data_root=Path(arguments.data_root),
            model_manifest=Path(arguments.model_manifest),
            output_directory=Path(arguments.output_directory),
            project_id=arguments.project_id,
            limit=arguments.limit,
            timeout_seconds=arguments.timeout_seconds,
            maximum_contact_sheet_items=arguments.maximum_contact_sheet_items,
            operator_note=arguments.operator_note,
            record_sidecars=arguments.record_sidecars,
            sidecar_evaluation_id=arguments.sidecar_evaluation_id,
        )
    except (EvaluationError, VisionInferenceError, OSError) as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "message": str(error),
                    "automatic_labels_created": 0,
                    "incidents_created": 0,
                    "model_promoted": False,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    proposed = sum(bool(row["predictions"]) for row in envelope["results"])
    print(
        json.dumps(
            {
                "status": "completed",
                "candidate_count": len(envelope["results"]),
                "review_proposal_count": proposed,
                "score_semantics": envelope["score_semantics"]["label"],
                "automatic_labels_created": 0,
                "incidents_created": 0,
                "model_promoted": False,
                "experimental_sidecar_runs_created": envelope[
                    "side_effects"
                ]["experimental_sidecar_runs_created"],
                "experimental_sidecar_proposals_created": envelope[
                    "side_effects"
                ]["experimental_sidecar_proposals_created"],
                "output_directory": str(Path(arguments.output_directory).resolve()),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
