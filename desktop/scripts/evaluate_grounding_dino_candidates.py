"""Evaluate persisted TrafficSuperVision JPEG candidates with Grounding DINO Tiny.

This command is a bounded research evaluator.  It stages deterministic copies
of existing candidate JPEGs, launches an offline Transformers worker, and
publishes only unreviewed object proposals under ProgramData ModelEvaluations.
It has no code path to labels, incidents, model promotion, reports, or
experimental sidecars.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from uuid import UUID, uuid4


SCRIPTS_ROOT = Path(__file__).resolve().parent
DESKTOP_ROOT = SCRIPTS_ROOT.parent
BRIDGE_ROOT = DESKTOP_ROOT / "bridge"
for value in (SCRIPTS_ROOT, BRIDGE_ROOT):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

import evaluate_existing_candidates as candidate_evaluator  # noqa: E402
from app.auto_labeling import (  # noqa: E402
    AutoLabelingError,
    compile_labeling_command,
)


EVALUATION_SCHEMA = "trafficvisson.grounding-dino-candidate-evaluation.v1"
WORKER_SCHEMA = "trafficvisson.grounding-dino-worker.v1"
PACK_SCHEMA = "trafficvisson.grounding-dino-evaluation-pack.v1"
PACK_ID = "trafficvisson-idea-grounding-dino-tiny-evaluation"
PACK_VERSION = "0.1.0"
IMMUTABLE_REVISION = "a2bb814dd30d776dcf7e30523b00659f4f141c71"
MODEL_FILE_SHA256 = (
    "1a2412ef99bd74bcd3c2a246fa1e48581f8889a1300c9051974741314fc042f3"
)
RUN_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
MAX_PACK_BYTES = 2 * 1024**3
MAX_IMAGE_BYTES = 24 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
MAX_WORKER_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_CANDIDATES = 349
MAX_PREDICTIONS_PER_FRAME = 32
DEFAULT_MAX_VRAM_BYTES = 10 * 1024**3
DEFAULT_MAX_WALL_SECONDS = 900.0
DEFAULT_MAX_OUTPUT_BYTES = 512 * 1024 * 1024
SELECTION_SEED = "trafficvisson-grounding-dino-deterministic-smoke-v1"
SCORE_LABEL = "model_score_not_probability"
COMMAND_COMPILER = "app.auto_labeling.compile_labeling_command"
COMMAND_RECIPE_KEYS = frozenset(
    {
        "recipe_id",
        "compiler",
        "compiler_schema_version",
        "issue_type",
        "command_sha256",
        "prompt_sha256",
        "mapping_version",
        "prompt_set_version",
        "grounding_dino_positive_prompts",
        "query_contract_sha256",
        "raw_command_stored",
    }
)

QUERY_CONTRACT = [
    {
        "query_id": "pothole",
        "prompt": "a pothole in the road",
        "candidate_type": "pothole_candidate",
    },
    {
        "query_id": "road_surface_damage",
        "prompt": "a broken or cracked road surface",
        "candidate_type": "road_surface_damage_candidate",
    },
    {
        "query_id": "road_debris",
        "prompt": "a fallen object or debris on the road",
        "candidate_type": "road_debris_candidate",
    },
    {
        "query_id": "barrier_damage",
        "prompt": "a damaged median barrier or damaged guardrail",
        "candidate_type": "barrier_damage_candidate",
    },
    {
        "query_id": "traffic_sign",
        "prompt": "a traffic sign",
        "candidate_type": "traffic_sign_candidate",
    },
    {
        "query_id": "vegetation_obstruction",
        "prompt": "excessive roadside vegetation blocking the view",
        "candidate_type": "vegetation_obstruction_candidate",
    },
    {
        "query_id": "tunnel_entrance_sign",
        "prompt": "a sign at a tunnel entrance",
        "candidate_type": "tunnel_entrance_sign_candidate",
    },
]

DISCLAIMER_KO = (
    "Grounding DINO 결과는 사용자 검토를 위한 실험적 객체 후보입니다. "
    "model_score는 실제 결함 확률, 정확도, 확정 라벨 또는 유지보수 판정이 "
    "아닙니다. 특히 차선 마모, 색깔유도선 재도색, 삭초 필요 같은 상태 "
    "판정은 이 평가가 확정하지 않습니다. 자동 라벨, 사건, 보고서 확정 "
    "입력, 모델 승격, 실험 사이드카를 생성하지 않습니다."
)


class GroundingDinoEvaluationError(RuntimeError):
    """Safe deterministic evaluation failure."""


class ProtectedSourceMutationError(GroundingDinoEvaluationError):
    """Raised when an immutable source inventory changes."""


def _canonical_json_sha256(value: Any) -> str:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise GroundingDinoEvaluationError(
            "Evaluation provenance must be finite JSON"
        ) from error
    return hashlib.sha256(raw).hexdigest()


def _canonical_uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise GroundingDinoEvaluationError(f"{label} must be a canonical UUID")
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError, TypeError) as error:
        raise GroundingDinoEvaluationError(
            f"{label} must be a canonical UUID"
        ) from error
    if canonical != value:
        raise GroundingDinoEvaluationError(f"{label} must be a canonical UUID")
    return canonical


def _query_contract_for_issue(
    issue_type: str,
    prompts: Sequence[str],
) -> list[dict[str, str]]:
    if (
        not isinstance(issue_type, str)
        or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", issue_type)
        or isinstance(prompts, (str, bytes, bytearray))
        or not isinstance(prompts, Sequence)
        or not 1 <= len(prompts) <= 16
    ):
        raise GroundingDinoEvaluationError(
            "The compiled Grounding DINO prompt contract is invalid"
        )
    queries: list[dict[str, str]] = []
    for index, prompt in enumerate(prompts, start=1):
        if (
            not isinstance(prompt, str)
            or not 1 <= len(prompt) <= 256
            or prompt != prompt.strip()
            or any(ord(character) < 32 for character in prompt)
        ):
            raise GroundingDinoEvaluationError(
                "The compiled Grounding DINO prompt contract is invalid"
            )
        query_id = issue_type if len(prompts) == 1 else f"{issue_type}_{index}"
        queries.append(
            {
                "query_id": query_id,
                "prompt": prompt,
                "candidate_type": f"{issue_type}_candidate",
            }
        )
    return queries


def _compile_command_recipe(
    command: str,
    *,
    project_id: str,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    try:
        plan = compile_labeling_command(command)
    except AutoLabelingError as error:
        raise GroundingDinoEvaluationError(
            "The command must resolve to exactly one supported road issue type"
        ) from error
    prompts = plan.get("positive_prompts", {}).get("grounding_dino")
    queries = _query_contract_for_issue(plan["issue_type"], prompts)
    recipe_id = _canonical_json_sha256(
        {
            "project_id": project_id,
            "command": command.strip(),
            "issue_type": plan["issue_type"],
            "prompt_sha256": plan["prompt_sha256"],
            "mapping_version": plan["mapping_version"],
            "prompt_set_version": plan["prompt_set_version"],
        }
    )
    recipe = {
        "recipe_id": recipe_id,
        "compiler": COMMAND_COMPILER,
        "compiler_schema_version": plan["schema_version"],
        "issue_type": plan["issue_type"],
        "command_sha256": plan["command_sha256"],
        "prompt_sha256": plan["prompt_sha256"],
        "mapping_version": plan["mapping_version"],
        "prompt_set_version": plan["prompt_set_version"],
        "grounding_dino_positive_prompts": list(prompts),
        "query_contract_sha256": _canonical_json_sha256(queries),
        "raw_command_stored": False,
    }
    return recipe, queries


def _validate_command_recipe(value: object) -> list[dict[str, str]]:
    if not isinstance(value, Mapping) or set(value) != COMMAND_RECIPE_KEYS:
        raise GroundingDinoEvaluationError(
            "The command recipe provenance is invalid"
        )
    if (
        value.get("compiler") != COMMAND_COMPILER
        or value.get("raw_command_stored") is not False
        or any(
            not isinstance(value.get(key), str)
            or not SHA256_PATTERN.fullmatch(value[key])
            for key in (
                "recipe_id",
                "command_sha256",
                "prompt_sha256",
                "query_contract_sha256",
            )
        )
    ):
        raise GroundingDinoEvaluationError(
            "The command recipe provenance is invalid"
        )
    issue_type = value.get("issue_type")
    try:
        fixed_plan = compile_labeling_command(
            str(issue_type),
            explicit_issue_type=issue_type if isinstance(issue_type, str) else None,
        )
    except AutoLabelingError as error:
        raise GroundingDinoEvaluationError(
            "The command recipe issue type is invalid"
        ) from error
    prompts = fixed_plan["positive_prompts"]["grounding_dino"]
    queries = _query_contract_for_issue(fixed_plan["issue_type"], prompts)
    if (
        value.get("compiler_schema_version") != fixed_plan["schema_version"]
        or value.get("issue_type") != fixed_plan["issue_type"]
        or value.get("prompt_sha256") != fixed_plan["prompt_sha256"]
        or value.get("mapping_version") != fixed_plan["mapping_version"]
        or value.get("prompt_set_version") != fixed_plan["prompt_set_version"]
        or value.get("grounding_dino_positive_prompts") != prompts
        or value.get("query_contract_sha256")
        != _canonical_json_sha256(queries)
    ):
        raise GroundingDinoEvaluationError(
            "The command recipe fixed prompts do not match the compiler"
        )
    return queries


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _safe_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise GroundingDinoEvaluationError(f"{label} is unavailable") from error
    if path.is_symlink() or _is_reparse_point(path) or not resolved.is_dir():
        raise GroundingDinoEvaluationError(f"{label} is unsafe")
    return resolved


def _safe_file(path: Path, *, maximum_bytes: int) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = path.stat()
    except OSError as error:
        raise GroundingDinoEvaluationError("A required file is unavailable") from error
    if (
        path.is_symlink()
        or _is_reparse_point(path)
        or not resolved.is_file()
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= maximum_bytes
    ):
        raise GroundingDinoEvaluationError("A required file is unsafe")
    return resolved


def _sha256(path: Path, *, maximum_bytes: int = MAX_PACK_BYTES) -> str:
    before = path.stat()
    if before.st_size > maximum_bytes:
        raise GroundingDinoEvaluationError("A file exceeds its size bound")
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while block := stream.read(4 * 1024 * 1024):
                total += len(block)
                if total > maximum_bytes:
                    raise GroundingDinoEvaluationError(
                        "A file exceeds its size bound"
                    )
                digest.update(block)
        after = path.stat()
    except OSError as error:
        raise GroundingDinoEvaluationError("A file could not be read") from error
    if (
        total != before.st_size
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise ProtectedSourceMutationError("A file changed while it was hashed")
    return digest.hexdigest()


def _fingerprint(path: Path) -> tuple[int, int, str]:
    metadata = path.stat()
    return (
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        _sha256(path),
    )


def _strict_json(raw: bytes, label: str) -> dict[str, Any]:
    def pairs(values: Iterable[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise GroundingDinoEvaluationError(
                    f"{label} contains duplicate keys"
                )
            result[key] = value
        return result

    def reject(_: str) -> object:
        raise GroundingDinoEvaluationError(
            f"{label} contains a non-finite number"
        )

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=reject,
        )
    except GroundingDinoEvaluationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GroundingDinoEvaluationError(
            f"{label} is not valid UTF-8 JSON"
        ) from error
    if not isinstance(value, dict):
        raise GroundingDinoEvaluationError(f"{label} must be an object")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    if len(encoded) > MAX_WORKER_OUTPUT_BYTES:
        raise GroundingDinoEvaluationError("A JSON output exceeded its bound")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_pack(manifest_path: Path | str) -> dict[str, Any]:
    manifest = _safe_file(Path(manifest_path), maximum_bytes=512 * 1024)
    payload = _strict_json(manifest.read_bytes(), "Grounding DINO pack manifest")
    if (
        payload.get("schema_version") != PACK_SCHEMA
        or payload.get("pack_id") != PACK_ID
        or payload.get("pack_version") != PACK_VERSION
        or payload.get("review_only") is not True
        or payload.get("automatic_labeling_allowed") is not False
        or payload.get("incident_creation_allowed") is not False
        or payload.get("model_promotion_allowed") is not False
        or payload.get("sidecar_recording_allowed") is not False
        or payload.get("source", {}).get("resolved_revision")
        != IMMUTABLE_REVISION
        or payload.get("inference_contract", {}).get("queries")
        != QUERY_CONTRACT
        or payload.get("inference_contract", {}).get("score_semantics")
        != SCORE_LABEL
        or payload.get("inference_contract", {}).get("local_files_only")
        is not True
        or payload.get("inference_contract", {}).get("trust_remote_code")
        is not False
        or payload.get("inference_contract", {}).get("use_safetensors")
        is not True
    ):
        raise GroundingDinoEvaluationError(
            "The Grounding DINO evaluation pack identity is invalid"
        )
    root = _safe_directory(manifest.parent, "Grounding DINO pack directory")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise GroundingDinoEvaluationError("The pack inventory is invalid")
    inventory: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in artifacts:
        if not isinstance(row, Mapping) or set(row) != {
            "path",
            "size_bytes",
            "sha256",
        }:
            raise GroundingDinoEvaluationError("A pack artifact row is invalid")
        relative = row["path"]
        digest = row["sha256"]
        size = row["size_bytes"]
        if (
            not isinstance(relative, str)
            or relative in seen
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not isinstance(digest, str)
            or not SHA256_PATTERN.fullmatch(digest)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 1 <= size <= MAX_PACK_BYTES
        ):
            raise GroundingDinoEvaluationError("A pack artifact value is invalid")
        seen.add(relative)
        path = _safe_file(root / relative, maximum_bytes=MAX_PACK_BYTES)
        if path.stat().st_size != size or _sha256(path) != digest:
            raise GroundingDinoEvaluationError(
                "A pack artifact failed integrity checks"
            )
        inventory.append(
            {
                "path": relative,
                "size_bytes": size,
                "sha256": digest,
                "absolute_path": path,
            }
        )
    model = next(
        (
            row
            for row in inventory
            if row["path"] == "snapshot/model.safetensors"
        ),
        None,
    )
    if model is None or model["sha256"] != MODEL_FILE_SHA256:
        raise GroundingDinoEvaluationError(
            "The pinned Safetensors artifact is missing"
        )
    snapshot = _safe_directory(root / "snapshot", "Grounding DINO snapshot")
    return {
        "manifest_path": manifest,
        "manifest_sha256": _sha256(manifest, maximum_bytes=512 * 1024),
        "root": root,
        "snapshot": snapshot,
        "payload": payload,
        "artifacts": inventory,
    }


def _protected_paths(
    data_root: Path,
    selected: Sequence[Any],
) -> list[Path]:
    paths: set[Path] = set()
    for path in data_root.glob("*.sqlite*"):
        if path.is_file():
            paths.add(_safe_file(path, maximum_bytes=512 * 1024 * 1024))
    for directory_name in ("candidate-labels", "experimental-proposals"):
        directory = data_root / directory_name
        if not directory.is_dir() or directory.is_symlink():
            continue
        for path in directory.rglob("*"):
            if path.is_file():
                paths.add(_safe_file(path, maximum_bytes=64 * 1024 * 1024))
    records_root = data_root / "projects"
    if records_root.is_dir() and not records_root.is_symlink():
        for path in records_root.glob("*/analysis-job-records/*"):
            if path.is_file():
                paths.add(_safe_file(path, maximum_bytes=64 * 1024 * 1024))
    for candidate in selected:
        paths.add(_safe_file(candidate.image_path, maximum_bytes=MAX_IMAGE_BYTES))
    return sorted(paths, key=lambda value: str(value).casefold())


def _snapshot_protected(
    data_root: Path,
    selected: Sequence[Any],
) -> dict[Path, tuple[int, int, str]]:
    return {
        path: _fingerprint(path)
        for path in _protected_paths(data_root, selected)
    }


def _verify_protected(
    before: Mapping[Path, tuple[int, int, str]],
    data_root: Path,
    selected: Sequence[Any],
) -> None:
    paths = set(_protected_paths(data_root, selected))
    if set(before) != paths:
        raise ProtectedSourceMutationError(
            "The protected source inventory changed"
        )
    for path, expected in before.items():
        if _fingerprint(path) != expected:
            raise ProtectedSourceMutationError(
                "A protected source changed during evaluation"
            )


def _select_candidates(
    candidates: Sequence[Any],
    *,
    limit: int,
    seed: str = SELECTION_SEED,
) -> list[Any]:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_CANDIDATES
        or limit > len(candidates)
    ):
        raise GroundingDinoEvaluationError(
            "The deterministic selection limit is invalid"
        )

    def key(candidate: Any) -> tuple[str, str]:
        basis = (
            f"{seed}\0{candidate.project_id}\0{candidate.job_id}\0"
            f"{candidate.source_candidate_id}\0{candidate.image_sha256}"
        ).encode("utf-8")
        return hashlib.sha256(basis).hexdigest(), candidate.evaluation_candidate_id

    return sorted(candidates, key=key)[:limit]


def _select_completed_job_candidates(
    candidates: Sequence[Any],
    *,
    data_root: Path,
    project_id: str,
    job_id: str,
) -> tuple[list[Any], dict[str, Any]]:
    selected_project_id = _canonical_uuid(project_id, "Selected project id")
    selected_job_id = _canonical_uuid(job_id, "Selected analysis job id")
    if any(
        _canonical_uuid(candidate.project_id, "Candidate project id")
        != selected_project_id
        for candidate in candidates
    ):
        raise GroundingDinoEvaluationError(
            "Candidate discovery crossed the selected project boundary"
        )
    selected = [
        candidate
        for candidate in candidates
        if _canonical_uuid(candidate.job_id, "Candidate analysis job id")
        == selected_job_id
    ]
    if not selected:
        raise GroundingDinoEvaluationError(
            "The selected completed analysis job has no valid persisted candidates"
        )
    if len(selected) > MAX_CANDIDATES:
        raise GroundingDinoEvaluationError(
            "The selected completed analysis job exceeds the candidate bound"
        )
    expected_record_ref = (
        f"projects/{selected_project_id}/analysis-job-records/"
        f"{selected_job_id}.json"
    )
    expected_record_path = data_root.joinpath(
        *Path(expected_record_ref).parts
    ).resolve(strict=True)
    identities = {
        (
            candidate.project_id,
            candidate.job_id,
            candidate.source_file_name,
            str(Path(candidate.record_path).resolve(strict=True)),
            candidate.record_relative_path,
            candidate.record_sha256,
        )
        for candidate in selected
    }
    if len(identities) != 1:
        raise GroundingDinoEvaluationError(
            "The selected analysis job candidates do not share one source identity"
        )
    (
        candidate_project_id,
        candidate_job_id,
        source_file_name,
        record_path_text,
        record_ref,
        record_sha256,
    ) = next(iter(identities))
    record_path = _safe_file(Path(record_path_text), maximum_bytes=64 * 1024 * 1024)
    if (
        candidate_project_id != selected_project_id
        or candidate_job_id != selected_job_id
        or not isinstance(source_file_name, str)
        or not source_file_name
        or record_ref != expected_record_ref
        or record_path != expected_record_path
        or not isinstance(record_sha256, str)
        or not SHA256_PATTERN.fullmatch(record_sha256)
        or _sha256(record_path, maximum_bytes=64 * 1024 * 1024)
        != record_sha256
    ):
        raise GroundingDinoEvaluationError(
            "The selected analysis job identity failed integrity checks"
        )
    digest_path = _safe_file(
        record_path.with_suffix(".sha256"),
        maximum_bytes=4 * 1024,
    )
    try:
        persisted_digest = digest_path.read_text(
            encoding="ascii",
            errors="strict",
        ).strip()
    except (OSError, UnicodeError) as error:
        raise GroundingDinoEvaluationError(
            "The selected analysis job digest is unreadable"
        ) from error
    if persisted_digest != record_sha256:
        raise GroundingDinoEvaluationError(
            "The selected analysis job digest does not match its record"
        )
    selected.sort(
        key=lambda candidate: (
            float(candidate.representative_timestamp_seconds),
            candidate.source_candidate_id,
            candidate.evaluation_candidate_id,
        )
    )
    return selected, {
        "project_id": selected_project_id,
        "analysis_job_id": selected_job_id,
        "source_file_name": source_file_name,
        "source_record_ref": record_ref,
        "source_record_sha256": record_sha256,
    }


def _prepare_output(
    *,
    program_data: Path,
    run_name: str,
) -> Path:
    if not RUN_NAME_PATTERN.fullmatch(run_name):
        raise GroundingDinoEvaluationError("The evaluation run name is invalid")
    if not program_data.is_absolute():
        raise GroundingDinoEvaluationError("PROGRAMDATA must be absolute")
    parent = program_data / "TrafficSuperVision" / "ModelEvaluations"
    parent.mkdir(parents=True, exist_ok=True)
    parent = _safe_directory(parent, "ModelEvaluations directory")
    output = parent / run_name
    if output.exists() or output.is_symlink():
        raise GroundingDinoEvaluationError(
            "The evaluation output directory already exists"
        )
    output.mkdir(exist_ok=False)
    output = _safe_directory(output, "Grounding DINO evaluation directory")
    if output.parent != parent:
        raise GroundingDinoEvaluationError(
            "The evaluation output escaped ModelEvaluations"
        )
    return output


def _worker_environment() -> dict[str, str]:
    allowed: dict[str, str] = {}
    for name in (
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "LOCALAPPDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "CUDA_PATH",
        "CUDA_VISIBLE_DEVICES",
    ):
        value = os.environ.get(name)
        if value:
            allowed[name] = value
    allowed.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "DO_NOT_TRACK": "1",
            "NO_PROXY": "*",
            "no_proxy": "*",
            "TOKENIZERS_PARALLELISM": "false",
            "USE_TF": "0",
            "USE_FLAX": "0",
            "USE_TORCH": "1",
            "OMP_NUM_THREADS": "2",
        }
    )
    return allowed


def _default_worker_runner(
    request_path: Path,
    output_path: Path,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    creation_flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    )
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker-request",
                str(request_path),
                "--worker-output",
                str(output_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            check=False,
            timeout=timeout_seconds,
            cwd=str(request_path.parent),
            env=_worker_environment(),
            creationflags=creation_flags,
        )
    except subprocess.TimeoutExpired as error:
        raise GroundingDinoEvaluationError(
            "The Grounding DINO worker exceeded its wall-time bound"
        ) from error
    except OSError as error:
        raise GroundingDinoEvaluationError(
            "The Grounding DINO worker failed"
        ) from error
    if not output_path.is_file():
        raise GroundingDinoEvaluationError(
            "The Grounding DINO worker produced no diagnostic output"
        )
    result = _safe_file(output_path, maximum_bytes=MAX_WORKER_OUTPUT_BYTES)
    payload = _strict_json(result.read_bytes(), "Grounding DINO worker output")
    if completed.returncode != 0:
        error_type = payload.get("error_type")
        message = payload.get("message")
        if isinstance(error_type, str) and isinstance(message, str):
            raise GroundingDinoEvaluationError(
                f"Grounding DINO worker failed: {error_type}: {message}"
            )
        raise GroundingDinoEvaluationError(
            "The Grounding DINO worker failed without a valid diagnostic"
        )
    return payload


def _normalized_words(value: str) -> set[str]:
    ignored = {"a", "an", "the", "in", "on", "or", "at", "of"}
    return {
        word
        for word in re.findall(r"[a-z]+", value.casefold())
        if word not in ignored
    }


def _map_text_label(
    label: str,
    queries: Sequence[Mapping[str, str]] = QUERY_CONTRACT,
) -> Mapping[str, str]:
    words = _normalized_words(label)
    ranked: list[tuple[int, int, Mapping[str, str]]] = []
    for query in queries:
        query_words = _normalized_words(query["prompt"])
        overlap = len(words & query_words)
        ranked.append((overlap, len(query_words), query))
    overlap, _, query = max(ranked, key=lambda row: (row[0], -row[1]))
    if overlap <= 0:
        return {
            "query_id": "unmapped",
            "prompt": label[:256],
            "candidate_type": "unmapped_object_candidate",
        }
    return query


def _run_worker(request_path: Path, output_path: Path) -> int:
    try:
        request = _strict_json(
            _safe_file(request_path, maximum_bytes=2 * 1024 * 1024).read_bytes(),
            "Grounding DINO worker request",
        )
        command_recipe = request.get("command_recipe")
        active_queries = (
            _validate_command_recipe(command_recipe)
            if command_recipe is not None
            else QUERY_CONTRACT
        )
        if (
            request.get("schema_version") != WORKER_SCHEMA
            or request.get("queries") != active_queries
            or request.get("score_semantics") != SCORE_LABEL
        ):
            raise GroundingDinoEvaluationError("The worker request is invalid")
        pack = _load_pack(Path(request["pack_manifest"]))
        if pack["manifest_sha256"] != request.get("pack_manifest_sha256"):
            raise GroundingDinoEvaluationError(
                "The worker pack identity did not match"
            )
        candidates = request.get("candidates")
        if (
            not isinstance(candidates, list)
            or not 1 <= len(candidates) <= MAX_CANDIDATES
        ):
            raise GroundingDinoEvaluationError(
                "The worker candidate batch is invalid"
            )
        box_threshold = float(request["box_threshold"])
        text_threshold = float(request["text_threshold"])
        max_predictions = int(request["max_predictions_per_frame"])
        max_vram_bytes = int(request["max_vram_bytes"])
        if (
            not 0.01 <= box_threshold <= 0.99
            or not 0.01 <= text_threshold <= 0.99
            or not 1 <= max_predictions <= MAX_PREDICTIONS_PER_FRAME
            or not 1 * 1024**3 <= max_vram_bytes <= 12 * 1024**3
        ):
            raise GroundingDinoEvaluationError(
                "The worker resource contract is invalid"
            )

        torch = importlib.import_module("torch")
        transformers = importlib.import_module("transformers")
        image_module = importlib.import_module("PIL.Image")
        cuda_available = bool(torch.cuda.is_available())
        device = "cuda:0" if cuda_available else "cpu"
        # The official checkpoint is F32 and Grounding DINO's current
        # Transformers implementation mixes image features with F32 positional
        # tensors. Forcing the complete model to F16 produces a matmul dtype
        # mismatch on the RTX A2000, so retain F32 while still running on CUDA.
        dtype = torch.float32
        total_vram = None
        if cuda_available:
            properties = torch.cuda.get_device_properties(0)
            total_vram = int(properties.total_memory)
            fraction = min(1.0, max_vram_bytes / total_vram)
            torch.cuda.set_per_process_memory_fraction(fraction, 0)
            torch.cuda.reset_peak_memory_stats(0)
        snapshot = str(pack["snapshot"])
        processor = transformers.AutoProcessor.from_pretrained(
            snapshot,
            local_files_only=True,
            trust_remote_code=False,
        )
        model = (
            transformers.AutoModelForZeroShotObjectDetection.from_pretrained(
                snapshot,
                local_files_only=True,
                trust_remote_code=False,
                use_safetensors=True,
                torch_dtype=dtype,
            )
            .to(device)
            .eval()
        )
        prompts = [row["prompt"] for row in active_queries]
        prompt_text = ". ".join(prompts).casefold() + "."
        results: list[dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, Mapping) or set(candidate) != {
                "candidate_id",
                "image_path",
                "image_size_bytes",
                "image_sha256",
            }:
                raise GroundingDinoEvaluationError(
                    "A worker candidate is invalid"
                )
            image_path = _safe_file(
                Path(candidate["image_path"]),
                maximum_bytes=MAX_IMAGE_BYTES,
            )
            if (
                image_path.stat().st_size != candidate["image_size_bytes"]
                or _sha256(image_path, maximum_bytes=MAX_IMAGE_BYTES)
                != candidate["image_sha256"]
            ):
                raise GroundingDinoEvaluationError(
                    "A worker candidate failed integrity checks"
                )
            started = time.monotonic()
            with image_module.open(image_path) as opened:
                opened.load()
                if (
                    opened.width <= 0
                    or opened.height <= 0
                    or opened.width * opened.height > MAX_IMAGE_PIXELS
                ):
                    raise GroundingDinoEvaluationError(
                        "A worker image dimension is invalid"
                    )
                image = opened.convert("RGB")
            inputs = processor(
                images=image,
                text=prompt_text,
                return_tensors="pt",
            ).to(device)
            if "pixel_values" in inputs:
                inputs["pixel_values"] = inputs["pixel_values"].to(dtype=dtype)
            with torch.inference_mode():
                outputs = model(**inputs)
            processed = processor.post_process_grounded_object_detection(
                outputs,
                inputs.input_ids,
                threshold=box_threshold,
                text_threshold=text_threshold,
                target_sizes=[(image.height, image.width)],
            )[0]
            scores = processed["scores"].detach().float().cpu().tolist()
            boxes = processed["boxes"].detach().float().cpu().tolist()
            labels = processed.get("text_labels", processed.get("labels"))
            if not isinstance(labels, list):
                labels = list(labels)
            ranked = sorted(
                zip(scores, boxes, labels),
                key=lambda row: float(row[0]),
                reverse=True,
            )[:max_predictions]
            predictions: list[dict[str, Any]] = []
            for score, box, label in ranked:
                if (
                    isinstance(score, bool)
                    or not math.isfinite(float(score))
                    or not 0 <= float(score) <= 1
                    or not isinstance(box, (list, tuple))
                    or len(box) != 4
                    or not isinstance(label, str)
                    or not label.strip()
                ):
                    raise GroundingDinoEvaluationError(
                        "A worker prediction is invalid"
                    )
                x1, y1, x2, y2 = (float(value) for value in box)
                x1 = min(max(x1, 0.0), float(image.width))
                x2 = min(max(x2, 0.0), float(image.width))
                y1 = min(max(y1, 0.0), float(image.height))
                y2 = min(max(y2, 0.0), float(image.height))
                if x2 <= x1 or y2 <= y1:
                    continue
                mapped = _map_text_label(label, active_queries)
                predictions.append(
                    {
                        "query_id": mapped["query_id"],
                        "query_prompt": mapped["prompt"],
                        "candidate_type": mapped["candidate_type"],
                        "source_text_label": label.strip()[:256],
                        "model_score": round(float(score), 8),
                        "score_semantics": SCORE_LABEL,
                        "is_probability": False,
                        "box": {
                            "x": round(x1 / image.width, 8),
                            "y": round(y1 / image.height, 8),
                            "width": round((x2 - x1) / image.width, 8),
                            "height": round((y2 - y1) / image.height, 8),
                        },
                    }
                )
            results.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "duration_ms": int(
                        min(3_600_000, (time.monotonic() - started) * 1000)
                    ),
                    "status": "classified" if predictions else "no_detection",
                    "predictions": predictions,
                }
            )
        runtime = {
            "python": sys.version.split()[0],
            "torch": str(torch.__version__),
            "transformers": str(transformers.__version__),
            "device": device,
            "cuda_available": cuda_available,
            "cuda_version": torch.version.cuda,
            "cuda_device_name": (
                str(torch.cuda.get_device_name(0)) if cuda_available else None
            ),
            "cuda_total_memory_bytes": total_vram,
            "maximum_cuda_allocator_bytes": max_vram_bytes,
            "peak_cuda_memory_allocated_bytes": (
                int(torch.cuda.max_memory_allocated(0))
                if cuda_available
                else None
            ),
            "peak_cuda_memory_reserved_bytes": (
                int(torch.cuda.max_memory_reserved(0))
                if cuda_available
                else None
            ),
            "dtype": str(dtype),
            "dtype_policy": "official_f32_checkpoint_cuda_compatible",
            "cpu_fallback_used": not cuda_available,
            "local_files_only": True,
            "trust_remote_code": False,
            "use_safetensors": True,
        }
        worker_payload: dict[str, Any] = {
            "schema_version": WORKER_SCHEMA,
            "status": "completed",
            "pack_id": PACK_ID,
            "pack_version": PACK_VERSION,
            "pack_manifest_sha256": pack["manifest_sha256"],
            "score_semantics": SCORE_LABEL,
            "runtime": runtime,
            "results": results,
        }
        if command_recipe is not None:
            worker_payload["command_recipe"] = dict(command_recipe)
            worker_payload["query_contract_sha256"] = _canonical_json_sha256(
                active_queries
            )
        _atomic_json(output_path, worker_payload)
        return 0
    except Exception as error:
        try:
            _atomic_json(
                output_path,
                {
                    "schema_version": WORKER_SCHEMA,
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "message": str(error).replace("\r", " ").replace("\n", " ")[
                        :2_000
                    ],
                    "automatic_labels_created": 0,
                    "incidents_created": 0,
                    "model_promoted": False,
                    "sidecars_created": 0,
                },
            )
        except Exception:
            pass
        return 1


def _validate_worker_result(
    value: Mapping[str, Any],
    selected: Sequence[Any],
    *,
    pack: Mapping[str, Any],
    queries: Sequence[Mapping[str, str]] = QUERY_CONTRACT,
    command_recipe: Optional[Mapping[str, Any]] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if (
        isinstance(queries, (str, bytes, bytearray))
        or not isinstance(queries, Sequence)
        or not 1 <= len(queries) <= 16
        or any(
            not isinstance(row, Mapping)
            or set(row) != {"query_id", "prompt", "candidate_type"}
            or any(
                not isinstance(row.get(key), str)
                or not row[key]
                or row[key] != row[key].strip()
                for key in ("query_id", "prompt", "candidate_type")
            )
            for row in queries
        )
    ):
        raise GroundingDinoEvaluationError(
            "The Grounding DINO query contract is invalid"
        )
    query_by_id = {row["query_id"]: row for row in queries}
    if len(query_by_id) != len(queries):
        raise GroundingDinoEvaluationError(
            "The Grounding DINO query contract is invalid"
        )
    if command_recipe is None:
        command_provenance_invalid = (
            value.get("command_recipe") is not None
            or value.get("query_contract_sha256") is not None
        )
    else:
        expected_queries = _validate_command_recipe(command_recipe)
        command_provenance_invalid = (
            list(queries) != expected_queries
            or value.get("command_recipe") != command_recipe
            or value.get("query_contract_sha256")
            != _canonical_json_sha256(expected_queries)
        )
    if (
        value.get("schema_version") != WORKER_SCHEMA
        or value.get("status") != "completed"
        or value.get("pack_id") != PACK_ID
        or value.get("pack_version") != PACK_VERSION
        or value.get("pack_manifest_sha256") != pack["manifest_sha256"]
        or value.get("score_semantics") != SCORE_LABEL
        or not isinstance(value.get("runtime"), Mapping)
        or not isinstance(value.get("results"), list)
        or len(value["results"]) != len(selected)
        or command_provenance_invalid
    ):
        raise GroundingDinoEvaluationError(
            "The Grounding DINO worker envelope is invalid"
        )
    expected = {
        candidate.evaluation_candidate_id: candidate
        for candidate in selected
    }
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value["results"]:
        if not isinstance(raw, Mapping):
            raise GroundingDinoEvaluationError("A worker result is invalid")
        candidate_id = raw.get("candidate_id")
        predictions = raw.get("predictions")
        if (
            candidate_id not in expected
            or candidate_id in seen
            or raw.get("status") not in {"classified", "no_detection"}
            or not isinstance(predictions, list)
            or len(predictions) > MAX_PREDICTIONS_PER_FRAME
        ):
            raise GroundingDinoEvaluationError("A worker result value is invalid")
        seen.add(candidate_id)
        candidate = expected[candidate_id]
        safe_predictions: list[dict[str, Any]] = []
        for prediction in predictions:
            if not isinstance(prediction, Mapping):
                raise GroundingDinoEvaluationError(
                    "A Grounding DINO proposal is invalid"
                )
            score = prediction.get("model_score")
            box = prediction.get("box")
            query_id = prediction.get("query_id")
            query = query_by_id.get(query_id)
            if (
                prediction.get("score_semantics") != SCORE_LABEL
                or prediction.get("is_probability") is not False
                or query_id not in set(query_by_id) | {"unmapped"}
                or not isinstance(prediction.get("query_prompt"), str)
                or not isinstance(prediction.get("candidate_type"), str)
                or not isinstance(prediction.get("source_text_label"), str)
                or (
                    query is not None
                    and (
                        prediction.get("query_prompt") != query["prompt"]
                        or prediction.get("candidate_type")
                        != query["candidate_type"]
                    )
                )
                or isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(float(score))
                or not 0 <= float(score) <= 1
                or not isinstance(box, Mapping)
                or set(box) != {"x", "y", "width", "height"}
            ):
                raise GroundingDinoEvaluationError(
                    "A Grounding DINO proposal violates its contract"
                )
            safe_box: dict[str, float] = {}
            for key in ("x", "y", "width", "height"):
                number = box[key]
                if (
                    isinstance(number, bool)
                    or not isinstance(number, (int, float))
                    or not math.isfinite(float(number))
                ):
                    raise GroundingDinoEvaluationError(
                        "A Grounding DINO box is invalid"
                    )
                safe_box[key] = round(float(number), 8)
            if (
                not 0 <= safe_box["x"] <= 1
                or not 0 <= safe_box["y"] <= 1
                or not 0 < safe_box["width"] <= 1
                or not 0 < safe_box["height"] <= 1
                or safe_box["x"] + safe_box["width"] > 1.00000001
                or safe_box["y"] + safe_box["height"] > 1.00000001
            ):
                raise GroundingDinoEvaluationError(
                    "A Grounding DINO box escaped its image"
                )
            safe_predictions.append(
                {
                    **dict(prediction),
                    "model_score": round(float(score), 8),
                    "box": safe_box,
                }
            )
        if (raw["status"] == "classified") != bool(safe_predictions):
            raise GroundingDinoEvaluationError(
                "A Grounding DINO result status is inconsistent"
            )
        normalized.append(
            {
                "project_id": candidate.project_id,
                "analysis_job_id": candidate.job_id,
                "source_file_name": candidate.source_file_name,
                "source_candidate_id": candidate.source_candidate_id,
                "evaluation_candidate_id": candidate.evaluation_candidate_id,
                "timestamp_seconds": candidate.representative_timestamp_seconds,
                "source_record_ref": candidate.record_relative_path,
                "source_record_sha256": candidate.record_sha256,
                "source_image_ref": candidate.image_relative_path,
                "source_image_size_bytes": candidate.image_size_bytes,
                "source_image_sha256": candidate.image_sha256,
                "status": raw["status"],
                "duration_ms": int(raw.get("duration_ms", 0)),
                "predictions": safe_predictions,
                "review_required": bool(safe_predictions),
                "review_state": "unreviewed",
                "automatic_label_created": False,
                "incident_created": False,
                "model_promoted": False,
                "sidecar_created": False,
            }
        )
    if seen != set(expected):
        raise GroundingDinoEvaluationError(
            "The Grounding DINO worker omitted candidates"
        )
    normalized.sort(
        key=lambda row: next(
            index
            for index, candidate in enumerate(selected)
            if candidate.evaluation_candidate_id
            == row["evaluation_candidate_id"]
        )
    )
    return normalized, dict(value["runtime"])


def _copy_evidence(
    output: Path,
    staging: Path,
    results: Sequence[Mapping[str, Any]],
    *,
    maximum_evidence: int,
) -> tuple[list[dict[str, Any]], dict[str, Path]]:
    evidence_root = output / "evidence"
    evidence_root.mkdir(exist_ok=False)
    ranked = sorted(
        (row for row in results if row["predictions"]),
        key=lambda row: row["predictions"][0]["model_score"],
        reverse=True,
    )
    selection_kind = "grounding_dino_proposal"
    selected = ranked[:maximum_evidence]
    if not selected:
        selection_kind = "no_detection_quality_sample"
        selected = list(results[: min(10, maximum_evidence)])
    evidence: list[dict[str, Any]] = []
    paths: dict[str, Path] = {}
    for row in selected:
        source = _safe_file(
            staging / f"{row['evaluation_candidate_id']}.jpg",
            maximum_bytes=MAX_IMAGE_BYTES,
        )
        target = evidence_root / (
            f"{row['evaluation_candidate_id']}-{row['source_image_sha256'][:12]}.jpg"
        )
        shutil.copyfile(source, target)
        target = _safe_file(target, maximum_bytes=MAX_IMAGE_BYTES)
        digest = _sha256(target, maximum_bytes=MAX_IMAGE_BYTES)
        if digest != row["source_image_sha256"]:
            raise GroundingDinoEvaluationError(
                "An evidence frame copy failed integrity checks"
            )
        relative = target.relative_to(output).as_posix()
        row["evidence_ref"] = relative
        paths[row["evaluation_candidate_id"]] = target
        evidence.append(
            {
                "evaluation_candidate_id": row["evaluation_candidate_id"],
                "selection_kind": selection_kind,
                "relative_path": relative,
                "size_bytes": target.stat().st_size,
                "sha256": digest,
                "review_state": "unreviewed",
            }
        )
    for row in results:
        row.setdefault("evidence_ref", None)
    return evidence, paths


def _write_contact_sheets(
    output: Path,
    results: Sequence[Mapping[str, Any]],
    evidence_paths: Mapping[str, Path],
) -> list[str]:
    try:
        from PIL import Image, ImageDraw
    except Exception as error:
        raise GroundingDinoEvaluationError(
            "Pillow is required for contact sheets"
        ) from error
    rows = [
        row
        for row in results
        if row["evaluation_candidate_id"] in evidence_paths
    ]
    names: list[str] = []
    per_sheet = 12
    for start in range(0, len(rows), per_sheet):
        chunk = rows[start : start + per_sheet]
        tile_width = 480
        tile_height = 360
        columns = min(3, len(chunk))
        sheet_rows = math.ceil(len(chunk) / columns)
        sheet = Image.new(
            "RGB",
            (columns * tile_width, sheet_rows * tile_height),
            (18, 24, 32),
        )
        draw = ImageDraw.Draw(sheet)
        for index, row in enumerate(chunk):
            left = (index % columns) * tile_width
            top = (index // columns) * tile_height
            with Image.open(evidence_paths[row["evaluation_candidate_id"]]) as source:
                source.load()
                image = source.convert("RGB")
            image.thumbnail((tile_width - 20, tile_height - 85))
            image_left = left + (tile_width - image.width) // 2
            image_top = top + 8
            sheet.paste(image, (image_left, image_top))
            overlay = ImageDraw.Draw(sheet)
            for prediction in row["predictions"][:6]:
                box = prediction["box"]
                x1 = image_left + round(float(box["x"]) * image.width)
                y1 = image_top + round(float(box["y"]) * image.height)
                x2 = x1 + round(float(box["width"]) * image.width)
                y2 = y1 + round(float(box["height"]) * image.height)
                overlay.rectangle((x1, y1, x2, y2), outline=(255, 92, 76), width=3)
            label = (
                f"{row['evaluation_candidate_id']} "
                f"t={row['timestamp_seconds']:.2f}s"
            )
            if row["predictions"]:
                first = row["predictions"][0]
                label += (
                    f" {first['query_id']} model_score="
                    f"{first['model_score']:.3f}"
                )
            else:
                label += " no detection"
            draw.text(
                (left + 10, top + tile_height - 55),
                label[:78],
                fill=(238, 244, 250),
            )
            draw.text(
                (left + 10, top + tile_height - 32),
                "unreviewed; model_score is not probability",
                fill=(255, 196, 92),
            )
        name = f"contact-sheet-{len(names) + 1:03d}.jpg"
        sheet.save(output / name, format="JPEG", quality=88, optimize=True)
        names.append(name)
    return names


def _write_csv(
    path: Path,
    results: Sequence[Mapping[str, Any]],
    *,
    command_recipe: Optional[Mapping[str, Any]] = None,
) -> None:
    fields = [
        "project_id",
        "analysis_job_id",
        "source_file_name",
        "source_candidate_id",
        "evaluation_candidate_id",
        "timestamp_seconds",
        "status",
        "review_required",
        "rank",
        "query_id",
        "query_prompt",
        "candidate_type",
        "source_text_label",
        "model_score",
        "score_semantics",
        "is_probability",
        "box_json",
        "source_image_ref",
        "source_image_sha256",
        "evidence_ref",
        "automatic_label_created",
        "incident_created",
        "model_promoted",
        "sidecar_created",
        "recipe_id",
        "issue_type",
        "command_sha256",
        "prompt_sha256",
    ]
    recipe = dict(command_recipe or {})
    with path.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for result in results:
            rows = result["predictions"] or [None]
            for rank, prediction in enumerate(rows, start=1):
                writer.writerow(
                    {
                        "project_id": result["project_id"],
                        "analysis_job_id": result["analysis_job_id"],
                        "source_file_name": result["source_file_name"],
                        "source_candidate_id": result["source_candidate_id"],
                        "evaluation_candidate_id": result[
                            "evaluation_candidate_id"
                        ],
                        "timestamp_seconds": result["timestamp_seconds"],
                        "status": result["status"],
                        "review_required": result["review_required"],
                        "rank": rank if prediction else "",
                        "query_id": prediction["query_id"] if prediction else "",
                        "query_prompt": (
                            prediction["query_prompt"] if prediction else ""
                        ),
                        "candidate_type": (
                            prediction["candidate_type"] if prediction else ""
                        ),
                        "source_text_label": (
                            prediction["source_text_label"] if prediction else ""
                        ),
                        "model_score": (
                            prediction["model_score"] if prediction else ""
                        ),
                        "score_semantics": SCORE_LABEL,
                        "is_probability": False,
                        "box_json": (
                            json.dumps(
                                prediction["box"],
                                separators=(",", ":"),
                                sort_keys=True,
                            )
                            if prediction
                            else ""
                        ),
                        "source_image_ref": result["source_image_ref"],
                        "source_image_sha256": result["source_image_sha256"],
                        "evidence_ref": result["evidence_ref"] or "",
                        "automatic_label_created": False,
                        "incident_created": False,
                        "model_promoted": False,
                        "sidecar_created": False,
                        "recipe_id": recipe.get("recipe_id", ""),
                        "issue_type": recipe.get("issue_type", ""),
                        "command_sha256": recipe.get("command_sha256", ""),
                        "prompt_sha256": recipe.get("prompt_sha256", ""),
                    }
                )


def _markdown(envelope: Mapping[str, Any]) -> str:
    summary = envelope["summary"]
    runtime = envelope["runtime"]
    command_recipe = envelope.get("command_recipe")
    command_section = ""
    if isinstance(command_recipe, Mapping):
        prompts = "\n".join(
            f"  - `{prompt}`"
            for prompt in command_recipe["grounding_dino_positive_prompts"]
        )
        command_section = f"""
## 명령형 평가 Provenance

- recipe id: `{command_recipe["recipe_id"]}`
- issue type: `{command_recipe["issue_type"]}`
- command SHA-256: `{command_recipe["command_sha256"]}`
- prompt SHA-256: `{command_recipe["prompt_sha256"]}`
- raw command stored: `False`
- 고정 Grounding DINO prompts:
{prompts}
"""
    return f"""# TrafficSuperVision Grounding DINO candidate smoke

## 결론

{envelope["disclaimer_ko"]}

- 결정적 표본: {summary["candidate_count"]}개
- 제안 프레임: {summary["proposal_frame_count"]}개
- 전체 박스: {summary["prediction_count"]}개
- 사용 장치: `{runtime.get("device")}`
- CUDA 장치: `{runtime.get("cuda_device_name")}`
- peak CUDA allocated: `{runtime.get("peak_cuda_memory_allocated_bytes")}`
- 소요시간: {envelope["elapsed_seconds"]:.3f}초
- 자동 라벨/사건/승격/사이드카: `0 / 0 / 0 / 0`

## 해석 제한

박스는 객체 검토 후보일 뿐입니다. 포트홀, 도로 파손, 낙하물, 파손된
방호시설, 표지판, 식생 가림, 터널 입구 표지 가능성을 사람이 확인해야
합니다. 이 모델만으로 차선 마모, 색깔유도선 재도색, 삭초 필요 같은
상태 또는 조치를 확정하지 않습니다.

## Provenance

- 모델: `IDEA-Research/grounding-dino-tiny`
- 고정 revision: `{envelope["model"]["resolved_revision"]}`
- pack manifest SHA-256: `{envelope["model"]["manifest_sha256"]}`
- score semantics: `{SCORE_LABEL}`
- offline load: `local_files_only=True`
- remote code: `trust_remote_code=False`
- weights: `use_safetensors=True`
{command_section}

## 산출물

- `predictions.json`
- `candidate_scores.csv`
- `selection_manifest.json`
- `provenance.json`
- `evidence/`
- `contact-sheet-*.jpg`
- `manifest.json`
"""


def _output_manifest(
    output: Path,
    *,
    generated_at: str,
    selection: Mapping[str, Any],
    command_recipe: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    artifacts: list[dict[str, Any]] = []
    for current, directories, files in os.walk(output, followlinks=False):
        current_path = Path(current)
        directories[:] = [name for name in directories if name != ".staging"]
        for name in files:
            path = current_path / name
            if path.name == "manifest.json":
                continue
            artifacts.append(
                {
                    "path": path.relative_to(output).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
    artifacts.sort(key=lambda row: row["path"])
    result: dict[str, Any] = {
        "schema_version": EVALUATION_SCHEMA,
        "generated_at": generated_at,
        "purpose": "review_only_open_vocabulary_object_proposals",
        "score_semantics": SCORE_LABEL,
        "selection": dict(selection),
        "automatic_labels_created": 0,
        "incidents_created": 0,
        "model_promoted": False,
        "sidecars_created": 0,
        "artifacts": artifacts,
    }
    if command_recipe is not None:
        result["command_recipe"] = dict(command_recipe)
    return result


def run_evaluation(
    *,
    data_root: Path | str,
    pack_manifest: Path | str,
    run_name: str,
    limit: int = 30,
    project_id: Optional[str] = None,
    job_id: Optional[str] = None,
    command: Optional[str] = None,
    box_threshold: float = 0.35,
    text_threshold: float = 0.25,
    max_predictions_per_frame: int = 16,
    maximum_wall_seconds: float = DEFAULT_MAX_WALL_SECONDS,
    maximum_vram_bytes: int = DEFAULT_MAX_VRAM_BYTES,
    maximum_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    maximum_evidence: int = 30,
    program_data: Optional[Path | str] = None,
    worker_runner: Callable[..., Mapping[str, Any]] = _default_worker_runner,
    discovery_function: Callable[..., Any] = (
        candidate_evaluator.discover_candidates
    ),
    staging_function: Callable[..., list[dict[str, Any]]] = (
        candidate_evaluator._stage_candidates
    ),
) -> dict[str, Any]:
    for value, minimum, maximum, label in (
        (box_threshold, 0.01, 0.99, "box_threshold"),
        (text_threshold, 0.01, 0.99, "text_threshold"),
        (maximum_wall_seconds, 30.0, 3_600.0, "maximum_wall_seconds"),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not minimum <= float(value) <= maximum
        ):
            raise GroundingDinoEvaluationError(f"{label} is invalid")
    for value, minimum, maximum, label in (
        (limit, 1, MAX_CANDIDATES, "limit"),
        (
            max_predictions_per_frame,
            1,
            MAX_PREDICTIONS_PER_FRAME,
            "max_predictions_per_frame",
        ),
        (maximum_vram_bytes, 1 * 1024**3, 12 * 1024**3, "maximum_vram_bytes"),
        (
            maximum_output_bytes,
            16 * 1024 * 1024,
            2 * 1024**3,
            "maximum_output_bytes",
        ),
        (maximum_evidence, 1, MAX_CANDIDATES, "maximum_evidence"),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not minimum <= value <= maximum
        ):
            raise GroundingDinoEvaluationError(f"{label} is invalid")
    selected_project_id = (
        _canonical_uuid(project_id, "Selected project id")
        if project_id is not None
        else None
    )
    selected_job_id = (
        _canonical_uuid(job_id, "Selected analysis job id")
        if job_id is not None
        else None
    )
    if selected_job_id is not None and selected_project_id is None:
        raise GroundingDinoEvaluationError(
            "--job-id requires a canonical --project-id"
        )
    if command is not None and selected_job_id is None:
        raise GroundingDinoEvaluationError(
            "--command requires --project-id and --job-id"
        )
    command_recipe: Optional[dict[str, Any]] = None
    active_queries: list[dict[str, str]] = list(QUERY_CONTRACT)
    if command is not None:
        command_recipe, active_queries = _compile_command_recipe(
            command,
            project_id=selected_project_id,
        )
    root = _safe_directory(Path(data_root), "TrafficSuperVision data root")
    pack = _load_pack(pack_manifest)
    pack_before = {
        row["absolute_path"]: _fingerprint(row["absolute_path"])
        for row in pack["artifacts"]
    }
    pack_before[pack["manifest_path"]] = _fingerprint(pack["manifest_path"])
    discovery = discovery_function(root, project_id=selected_project_id)
    discovered_candidates = tuple(discovery.candidates)
    if selected_project_id is not None and any(
        _canonical_uuid(candidate.project_id, "Candidate project id")
        != selected_project_id
        for candidate in discovered_candidates
    ):
        raise GroundingDinoEvaluationError(
            "Candidate discovery crossed the selected project boundary"
        )
    job_identity: Optional[dict[str, Any]] = None
    if selected_job_id is not None:
        selected, job_identity = _select_completed_job_candidates(
            discovered_candidates,
            data_root=root,
            project_id=selected_project_id,
            job_id=selected_job_id,
        )
        selection: dict[str, Any] = {
            "method": "completed_analysis_job_all_candidates",
            "seed": None,
            "available_candidate_count": len(discovered_candidates),
            "selected_candidate_count": len(selected),
            "requested_limit_applied": False,
            **job_identity,
        }
        selection_seed: Optional[str] = None
    else:
        selected = _select_candidates(discovered_candidates, limit=limit)
        selection = {
            "method": "sha256_rank",
            "seed": SELECTION_SEED,
            "available_candidate_count": len(discovered_candidates),
            "selected_candidate_count": len(selected),
        }
        selection_seed = SELECTION_SEED
    protected_before = _snapshot_protected(root, selected)
    selected_program_data = (
        Path(program_data)
        if program_data is not None
        else Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    )
    output = _prepare_output(
        program_data=selected_program_data,
        run_name=run_name,
    )
    staging = output / ".staging"
    generated_at = _utc_now()
    started = time.monotonic()
    try:
        staged = staging_function(selected, staging)
        candidate_by_id = {
            candidate.evaluation_candidate_id: candidate
            for candidate in selected
        }
        request_rows: list[dict[str, Any]] = []
        for row in staged:
            candidate_id = row["candidate_id"]
            candidate = candidate_by_id[candidate_id]
            staged_path = _safe_file(
                staging / row["representative_image"]["relative_path"],
                maximum_bytes=MAX_IMAGE_BYTES,
            )
            if (
                staged_path.stat().st_size != candidate.image_size_bytes
                or _sha256(staged_path, maximum_bytes=MAX_IMAGE_BYTES)
                != candidate.image_sha256
            ):
                raise GroundingDinoEvaluationError(
                    "A staged candidate failed integrity checks"
                )
            request_rows.append(
                {
                    "candidate_id": candidate_id,
                    "image_path": str(staged_path),
                    "image_size_bytes": candidate.image_size_bytes,
                    "image_sha256": candidate.image_sha256,
                }
            )
        request = {
            "schema_version": WORKER_SCHEMA,
            "pack_manifest": str(pack["manifest_path"]),
            "pack_manifest_sha256": pack["manifest_sha256"],
            "queries": active_queries,
            "score_semantics": SCORE_LABEL,
            "box_threshold": round(float(box_threshold), 6),
            "text_threshold": round(float(text_threshold), 6),
            "max_predictions_per_frame": max_predictions_per_frame,
            "max_vram_bytes": maximum_vram_bytes,
            "candidates": request_rows,
        }
        if command_recipe is not None:
            request["command_recipe"] = command_recipe
        request_path = output / ".worker-request.json"
        worker_output = output / ".worker-output.json"
        _atomic_json(request_path, request)
        raw_worker = worker_runner(
            request_path,
            worker_output,
            timeout_seconds=float(maximum_wall_seconds),
        )
        results, runtime = _validate_worker_result(
            raw_worker,
            selected,
            pack=pack,
            queries=active_queries,
            command_recipe=command_recipe,
        )
        _verify_protected(protected_before, root, selected)
        for path, expected in pack_before.items():
            if _fingerprint(path) != expected:
                raise ProtectedSourceMutationError(
                    "The model pack changed during evaluation"
                )
        evidence, evidence_paths = _copy_evidence(
            output,
            staging,
            results,
            maximum_evidence=maximum_evidence,
        )
        contact_sheets = _write_contact_sheets(
            output,
            results,
            evidence_paths,
        )
        status_counts = Counter(row["status"] for row in results)
        proposal_frame_count = sum(bool(row["predictions"]) for row in results)
        prediction_count = sum(len(row["predictions"]) for row in results)
        completed_at = _utc_now()
        selection_rows = [
            {
                "selection_rank": index,
                "selection_method": selection["method"],
                "selection_seed": selection_seed,
                "project_id": candidate.project_id,
                "analysis_job_id": candidate.job_id,
                "source_candidate_id": candidate.source_candidate_id,
                "evaluation_candidate_id": candidate.evaluation_candidate_id,
                "source_file_name": candidate.source_file_name,
                "timestamp_seconds": candidate.representative_timestamp_seconds,
                "source_image_ref": candidate.image_relative_path,
                "source_image_size_bytes": candidate.image_size_bytes,
                "source_image_sha256": candidate.image_sha256,
            }
            for index, candidate in enumerate(selected, start=1)
        ]
        provenance = {
            "schema_version": EVALUATION_SCHEMA,
            "generated_at": generated_at,
            "completed_at": completed_at,
            "selection": selection,
            "selection_seed": selection_seed,
            "protected_file_count": len(protected_before),
            "protected_sources_verified_unchanged": True,
            "model_pack_files_verified_unchanged": True,
            "model": {
                "repo_id": pack["payload"]["source"]["repo_id"],
                "resolved_revision": IMMUTABLE_REVISION,
                "pack_id": PACK_ID,
                "pack_version": PACK_VERSION,
                "manifest_sha256": pack["manifest_sha256"],
                "snapshot_total_bytes": pack["payload"]["source"][
                    "snapshot_total_bytes"
                ],
                "snapshot_file_count": pack["payload"]["source"][
                    "snapshot_file_count"
                ],
                "snapshot_files": pack["payload"]["snapshot_files"],
            },
            "runtime": runtime,
        }
        if command_recipe is not None:
            provenance["command_recipe"] = command_recipe
        envelope = {
            "schema_version": EVALUATION_SCHEMA,
            "status": "completed",
            "generated_at": generated_at,
            "completed_at": completed_at,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "purpose": "review_only_open_vocabulary_object_proposals",
            "disclaimer_ko": DISCLAIMER_KO,
            "score_semantics": {
                "calibration": "model_score",
                "label": SCORE_LABEL,
                "is_probability": False,
                "is_accuracy": False,
                "is_label": False,
                "is_maintenance_decision": False,
            },
            "model": provenance["model"],
            "runtime": runtime,
            "selection": selection,
            "queries": active_queries,
            "excluded_state_claims": pack["payload"]["inference_contract"][
                "excluded_state_claims"
            ],
            "limits": {
                "maximum_frames": (
                    len(selected) if selected_job_id is not None else limit
                ),
                "maximum_wall_seconds": maximum_wall_seconds,
                "maximum_vram_bytes": maximum_vram_bytes,
                "maximum_output_bytes": maximum_output_bytes,
                "maximum_predictions_per_frame": max_predictions_per_frame,
                "maximum_evidence_frames": maximum_evidence,
                "box_threshold": box_threshold,
                "text_threshold": text_threshold,
            },
            "summary": {
                "candidate_count": len(results),
                "proposal_frame_count": proposal_frame_count,
                "prediction_count": prediction_count,
                "evidence_frame_count": len(evidence),
                "contact_sheet_count": len(contact_sheets),
                "status_counts": dict(sorted(status_counts.items())),
            },
            "side_effects": {
                "analysis_records_modified": 0,
                "label_records_modified": 0,
                "sqlite_databases_modified": 0,
                "automatic_labels_created": 0,
                "incidents_created": 0,
                "model_promoted": False,
                "sidecars_created": 0,
            },
            "results": results,
            "evidence": evidence,
            "contact_sheets": contact_sheets,
        }
        if command_recipe is not None:
            envelope["command_recipe"] = command_recipe
        _atomic_json(output / "predictions.json", envelope)
        _write_csv(
            output / "candidate_scores.csv",
            results,
            command_recipe=command_recipe,
        )
        selection_manifest: dict[str, Any] = {
            "schema_version": EVALUATION_SCHEMA,
            "selection": selection,
            "candidates": selection_rows,
        }
        if command_recipe is not None:
            selection_manifest["command_recipe"] = command_recipe
        _atomic_json(
            output / "selection_manifest.json",
            selection_manifest,
        )
        _atomic_json(output / "provenance.json", provenance)
        (output / "README.md").write_text(
            _markdown(envelope),
            encoding="utf-8",
        )
        _verify_protected(protected_before, root, selected)
        for path, expected in pack_before.items():
            if _fingerprint(path) != expected:
                raise ProtectedSourceMutationError(
                    "The model pack changed before publication"
                )
        for path in (request_path, worker_output):
            if path.exists():
                path.unlink()
        staging_resolved = _safe_directory(
            staging,
            "Grounding DINO staging directory",
        )
        if staging_resolved.parent != output:
            raise GroundingDinoEvaluationError(
                "The staging cleanup target escaped the evaluation"
            )
        shutil.rmtree(staging_resolved)
        total = sum(
            path.stat().st_size
            for path in output.rglob("*")
            if path.is_file()
        )
        if total > maximum_output_bytes:
            raise GroundingDinoEvaluationError(
                "The evaluation output exceeded its byte bound"
            )
        _atomic_json(
            output / "manifest.json",
            _output_manifest(
                output,
                generated_at=completed_at,
                selection=selection,
                command_recipe=command_recipe,
            ),
        )
        return envelope
    except Exception:
        raise


def _default_data_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise GroundingDinoEvaluationError("LOCALAPPDATA is unavailable")
    return Path(local_app_data) / "TrafficSuperVision" / "bridge-data"


def _default_pack_manifest() -> Path:
    program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    return (
        program_data
        / "TrafficSuperVision"
        / "ModelPacks"
        / f"{PACK_ID}-{PACK_VERSION}"
        / "trafficvisson-grounding-dino-pack.json"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate deterministic persisted TrafficSuperVision candidate JPEGs "
            "with the pinned review-only Grounding DINO Tiny pack."
        )
    )
    parser.add_argument("--data-root", default=str(_default_data_root()))
    parser.add_argument(
        "--pack-manifest",
        default=str(_default_pack_manifest()),
    )
    parser.add_argument("--run-name", required=False)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--project-id")
    parser.add_argument(
        "--job-id",
        help=(
            "Evaluate every persisted candidate from this completed analysis "
            "job; requires --project-id and does not apply --limit."
        ),
    )
    parser.add_argument(
        "--command",
        help=(
            "Compile one operator issue request to an allowlisted fixed "
            "Grounding DINO prompt; requires --project-id and --job-id."
        ),
    )
    parser.add_argument("--box-threshold", type=float, default=0.35)
    parser.add_argument("--text-threshold", type=float, default=0.25)
    parser.add_argument("--max-predictions-per-frame", type=int, default=16)
    parser.add_argument(
        "--maximum-wall-seconds",
        type=float,
        default=DEFAULT_MAX_WALL_SECONDS,
    )
    parser.add_argument(
        "--maximum-vram-bytes",
        type=int,
        default=DEFAULT_MAX_VRAM_BYTES,
    )
    parser.add_argument(
        "--maximum-output-bytes",
        type=int,
        default=DEFAULT_MAX_OUTPUT_BYTES,
    )
    parser.add_argument("--maximum-evidence", type=int, default=30)
    parser.add_argument("--worker-request")
    parser.add_argument("--worker-output")
    arguments = parser.parse_args(argv)
    if arguments.worker_request or arguments.worker_output:
        if not arguments.worker_request or not arguments.worker_output:
            return 1
        return _run_worker(
            Path(arguments.worker_request),
            Path(arguments.worker_output),
        )
    if not arguments.run_name:
        parser.error("--run-name is required for an evaluation")
    try:
        envelope = run_evaluation(
            data_root=Path(arguments.data_root),
            pack_manifest=Path(arguments.pack_manifest),
            run_name=arguments.run_name,
            limit=arguments.limit,
            project_id=arguments.project_id,
            job_id=arguments.job_id,
            command=arguments.command,
            box_threshold=arguments.box_threshold,
            text_threshold=arguments.text_threshold,
            max_predictions_per_frame=arguments.max_predictions_per_frame,
            maximum_wall_seconds=arguments.maximum_wall_seconds,
            maximum_vram_bytes=arguments.maximum_vram_bytes,
            maximum_output_bytes=arguments.maximum_output_bytes,
            maximum_evidence=arguments.maximum_evidence,
        )
    except (GroundingDinoEvaluationError, OSError) as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "message": str(error),
                    "automatic_labels_created": 0,
                    "incidents_created": 0,
                    "model_promoted": False,
                    "sidecars_created": 0,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    console_result: dict[str, Any] = {
        "status": envelope["status"],
        "candidate_count": envelope["summary"]["candidate_count"],
        "proposal_frame_count": envelope["summary"]["proposal_frame_count"],
        "prediction_count": envelope["summary"]["prediction_count"],
        "device": envelope["runtime"].get("device"),
        "score_semantics": SCORE_LABEL,
        "automatic_labels_created": 0,
        "incidents_created": 0,
        "model_promoted": False,
        "sidecars_created": 0,
        "output_directory": str(
            (
                Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
                / "TrafficSuperVision"
                / "ModelEvaluations"
                / arguments.run_name
            ).resolve()
        ),
    }
    if "command_recipe" in envelope:
        console_result["recipe_id"] = envelope["command_recipe"]["recipe_id"]
        console_result["issue_type"] = envelope["command_recipe"]["issue_type"]
        console_result["command_sha256"] = envelope["command_recipe"][
            "command_sha256"
        ]
    print(json.dumps(console_result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
