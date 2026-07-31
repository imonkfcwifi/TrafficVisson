"""Verified local Grounding DINO evaluation-pack proposal adapter.

The installed IDEA Research checkpoint predates the packaged command runner
contract.  It is still useful as a strictly local, review-only object proposal
model, provided that the bridge verifies the complete pack against an
out-of-band manifest hash and never treats its boxes as labels or Incidents.

This adapter deliberately supports only the object concepts declared by the
pack itself.  It does not turn generic vegetation or sign boxes into a
maintenance conclusion; SAM2, the structured semantic proposal, and the user
review boundary keep their separate roles.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from uuid import UUID

from .analysis_jobs import ResolvedAnalysisArtifact
from .auto_labeling import validate_command_plan
from .command_proposal_generation import (
    MAX_GENERATION_CANDIDATES,
    MAX_MODEL_ARTIFACT_BYTES,
    RUNNER_TIMEOUT_SECONDS,
    CommandProposalGenerationError,
    CommandProposalGenerationResult,
    UnavailableCommandProposalGenerator,
    _canonical_json,
    _is_reparse_point,
    _read_bounded_json,
    _run_bounded_process,
    _sha256,
    _verify_model_artifacts,
)
from .contracts import ExperimentalProposalPrediction


EVALUATION_PACK_SCHEMA = (
    "trafficvisson.grounding-dino-evaluation-pack.v1"
)
WORKER_REQUEST_SCHEMA = (
    "trafficvisson.grounding-dino-pack-worker-request.v1"
)
WORKER_RESPONSE_SCHEMA = (
    "trafficvisson.grounding-dino-pack-worker-response.v1"
)
PACK_ID = "trafficvisson-idea-grounding-dino-tiny-evaluation"
PACK_VERSION = "0.1.0"
PACK_DIRECTORY = f"{PACK_ID}-{PACK_VERSION}"
PACK_MANIFEST_NAME = "trafficvisson-grounding-dino-pack.json"
SOURCE_REPO_ID = "IDEA-Research/grounding-dino-tiny"
SOURCE_REVISION = "a2bb814dd30d776dcf7e30523b00659f4f141c71"
MODEL_ARTIFACT = "snapshot/model.safetensors"
MODEL_ARTIFACT_SHA256 = (
    "1a2412ef99bd74bcd3c2a246fa1e48581f8889a1300c9051974741314fc042f3"
)
PINNED_EVALUATION_PACK_MANIFEST_SHA256 = (
    "09f90e0a0122558a4f7263efb3fdc3c0d341e501984aba1880308ce3265811ec"
)
PRODUCER = "trafficvisson-grounding-dino-pack-worker"
PRODUCER_VERSION = "0.1.0"
SCORE_SEMANTICS = "model_score_not_probability"
BOX_THRESHOLD = 0.35
TEXT_THRESHOLD = 0.25

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_SAFE_TEXT = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")

# The model pack has a fixed query contract.  Product issue types which imply
# a relationship or maintenance state are intentionally absent unless the
# pack has a direct object query for them.
_QUERY_TO_ISSUE_TYPE: Mapping[str, str] = {
    "road_debris": "road_debris",
    "barrier_damage": "median_barrier_damage",
    "traffic_sign": "guide_sign_identity",
    "tunnel_entrance_sign": "tunnel_identity",
}
SUPPORTED_ISSUE_TYPES = tuple(
    sorted(set(_QUERY_TO_ISSUE_TYPE.values()))
)


@dataclass(frozen=True)
class VerifiedGroundingDinoPack:
    manifest_path: Path
    root: Path
    snapshot: Path
    manifest_sha256: str
    manifest: Mapping[str, Any]
    queries_by_issue_type: Mapping[str, Mapping[str, str]]
    maximum_frames_per_run: int
    maximum_predictions_per_frame: int


GroundingDinoWorkerRunner = Callable[
    [Mapping[str, Any]], Mapping[str, Any]
]


def default_program_data_manifest(
    *, program_data: Optional[str] = None
) -> Path:
    base = (
        program_data
        if program_data is not None
        else os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    )
    return (
        Path(base)
        / "TrafficVisson"
        / "ModelPacks"
        / PACK_DIRECTORY
        / PACK_MANIFEST_NAME
    )


def _safe_pack_root(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise CommandProposalGenerationError(
            "Grounding DINO evaluation pack is unavailable"
        ) from error
    if (
        path.is_symlink()
        or _is_reparse_point(path)
        or not path.is_dir()
        or not stat.S_ISDIR(path.stat().st_mode)
    ):
        raise CommandProposalGenerationError(
            "Grounding DINO evaluation pack root is unsafe"
        )
    return resolved


def _safe_pack_inventory(
    root: Path,
    manifest_name: str,
) -> set[str]:
    inventory: set[str] = set()
    for current_root, directories, files in os.walk(
        root, followlinks=False
    ):
        current = Path(current_root)
        if current.is_symlink() or _is_reparse_point(current):
            raise CommandProposalGenerationError(
                "Grounding DINO evaluation pack contains a linked directory"
            )
        for name in directories:
            nested = current / name
            if nested.is_symlink() or _is_reparse_point(nested):
                raise CommandProposalGenerationError(
                    "Grounding DINO evaluation pack contains a linked directory"
                )
        for name in files:
            path = current / name
            if path.is_symlink() or _is_reparse_point(path):
                raise CommandProposalGenerationError(
                    "Grounding DINO evaluation pack contains a linked file"
                )
            relative = path.relative_to(root).as_posix()
            if relative != manifest_name:
                inventory.add(relative)
    return inventory


def load_verified_grounding_dino_pack(
    manifest_path: Path | str,
    *,
    trusted_manifest_sha256: str,
) -> VerifiedGroundingDinoPack:
    path = Path(manifest_path)
    if (
        not path.is_absolute()
        or path.name != PACK_MANIFEST_NAME
        or not isinstance(trusted_manifest_sha256, str)
        or _SHA256.fullmatch(trusted_manifest_sha256) is None
    ):
        raise CommandProposalGenerationError(
            "Grounding DINO evaluation pack identity is invalid"
        )
    root = _safe_pack_root(path.parent)
    manifest, raw_manifest = _read_bounded_json(
        path,
        root=root,
        label="Grounding DINO evaluation pack manifest",
    )
    manifest_sha256 = hashlib.sha256(raw_manifest).hexdigest()
    if manifest_sha256 != trusted_manifest_sha256:
        raise CommandProposalGenerationError(
            "Grounding DINO evaluation pack manifest is not trusted"
        )
    required_keys = {
        "artifacts",
        "automatic_labeling_allowed",
        "created_at",
        "device_policy",
        "incident_creation_allowed",
        "inference_contract",
        "installer_distribution",
        "license",
        "model_promotion_allowed",
        "pack_id",
        "pack_version",
        "purpose",
        "review_only",
        "runtime_provenance_at_install",
        "schema_version",
        "sidecar_recording_allowed",
        "snapshot_files",
        "source",
    }
    if set(manifest) != required_keys:
        raise CommandProposalGenerationError(
            "Grounding DINO evaluation pack fields are invalid"
        )
    source = manifest.get("source")
    licence = manifest.get("license")
    contract = manifest.get("inference_contract")
    device_policy = manifest.get("device_policy")
    if (
        manifest.get("schema_version") != EVALUATION_PACK_SCHEMA
        or manifest.get("pack_id") != PACK_ID
        or manifest.get("pack_version") != PACK_VERSION
        or manifest.get("purpose")
        != "local_open_vocabulary_detector_evaluation_only"
        or manifest.get("review_only") is not True
        or manifest.get("automatic_labeling_allowed") is not False
        or manifest.get("incident_creation_allowed") is not False
        or manifest.get("model_promotion_allowed") is not False
        or manifest.get("sidecar_recording_allowed") is not False
        or manifest.get("installer_distribution") != "excluded"
        or not isinstance(source, Mapping)
        or source.get("repo_id") != SOURCE_REPO_ID
        or source.get("resolved_revision") != SOURCE_REVISION
        or source.get("license_spdx") != "Apache-2.0"
        or not isinstance(licence, Mapping)
        or licence.get("weights_spdx") != "Apache-2.0"
        or licence.get("runtime_spdx") != "Apache-2.0"
        or licence.get("redistribution_enabled") is not False
        or not isinstance(contract, Mapping)
        or contract.get("local_files_only") is not True
        or contract.get("trust_remote_code") is not False
        or contract.get("use_safetensors") is not True
        or contract.get("model_class")
        != "AutoModelForZeroShotObjectDetection"
        or contract.get("processor_class") != "AutoProcessor"
        or contract.get("score_semantics") != SCORE_SEMANTICS
        or contract.get("snapshot_directory") != "snapshot"
        or not isinstance(device_policy, Mapping)
        or device_policy.get("cpu_fallback") is not True
    ):
        raise CommandProposalGenerationError(
            "Grounding DINO evaluation pack contract is invalid"
        )
    maximum_frames = device_policy.get("maximum_frames_per_run")
    maximum_predictions = device_policy.get(
        "maximum_predictions_per_frame"
    )
    maximum_cuda_bytes = device_policy.get(
        "maximum_cuda_allocator_bytes"
    )
    if (
        isinstance(maximum_frames, bool)
        or not isinstance(maximum_frames, int)
        or not 1 <= maximum_frames <= MAX_GENERATION_CANDIDATES
        or isinstance(maximum_predictions, bool)
        or not isinstance(maximum_predictions, int)
        or not 1 <= maximum_predictions <= 32
        or isinstance(maximum_cuda_bytes, bool)
        or not isinstance(maximum_cuda_bytes, int)
        or not 1 * 1024**3 <= maximum_cuda_bytes <= 12 * 1024**3
    ):
        raise CommandProposalGenerationError(
            "Grounding DINO evaluation pack resource policy is invalid"
        )

    queries = contract.get("queries")
    if not isinstance(queries, list) or not 1 <= len(queries) <= 32:
        raise CommandProposalGenerationError(
            "Grounding DINO evaluation query contract is invalid"
        )
    query_ids: set[str] = set()
    queries_by_issue: dict[str, Mapping[str, str]] = {}
    for row in queries:
        if (
            not isinstance(row, Mapping)
            or set(row) != {"query_id", "prompt", "candidate_type"}
        ):
            raise CommandProposalGenerationError(
                "Grounding DINO evaluation query is invalid"
            )
        query_id = row.get("query_id")
        prompt = row.get("prompt")
        candidate_type = row.get("candidate_type")
        if (
            not isinstance(query_id, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", query_id)
            or query_id in query_ids
            or not isinstance(prompt, str)
            or _SAFE_TEXT.fullmatch(prompt) is None
            or not isinstance(candidate_type, str)
            or not re.fullmatch(
                r"[a-z][a-z0-9_]{1,63}", candidate_type
            )
        ):
            raise CommandProposalGenerationError(
                "Grounding DINO evaluation query value is invalid"
            )
        query_ids.add(query_id)
        issue_type = _QUERY_TO_ISSUE_TYPE.get(query_id)
        if issue_type is not None:
            queries_by_issue[issue_type] = {
                "query_id": query_id,
                "prompt": prompt,
                "candidate_type": candidate_type,
                "issue_type": issue_type,
            }
    if set(queries_by_issue) != set(SUPPORTED_ISSUE_TYPES):
        raise CommandProposalGenerationError(
            "Grounding DINO evaluation query mapping is incomplete"
        )

    _verify_model_artifacts(root, manifest)
    declared = {
        str(row["path"])
        for row in manifest["artifacts"]
        if isinstance(row, Mapping) and isinstance(row.get("path"), str)
    }
    if _safe_pack_inventory(root, path.name) != declared:
        raise CommandProposalGenerationError(
            "Grounding DINO evaluation pack inventory is incomplete"
        )
    model_row = next(
        (
            row
            for row in manifest["artifacts"]
            if isinstance(row, Mapping)
            and row.get("path") == MODEL_ARTIFACT
        ),
        None,
    )
    if (
        not isinstance(model_row, Mapping)
        or model_row.get("sha256") != MODEL_ARTIFACT_SHA256
    ):
        raise CommandProposalGenerationError(
            "Grounding DINO evaluation model artifact is not pinned"
        )
    snapshot = _safe_pack_root(root / "snapshot")
    return VerifiedGroundingDinoPack(
        manifest_path=path.resolve(strict=True),
        root=root,
        snapshot=snapshot,
        manifest_sha256=manifest_sha256,
        manifest=manifest,
        queries_by_issue_type=queries_by_issue,
        maximum_frames_per_run=maximum_frames,
        maximum_predictions_per_frame=maximum_predictions,
    )


class VerifiedGroundingDinoEvaluationPackProposalGenerator:
    """Expose the pinned evaluation checkpoint as review-only box proposals."""

    def __init__(
        self,
        manifest_path: Path | str,
        *,
        trusted_manifest_sha256: str = (
            PINNED_EVALUATION_PACK_MANIFEST_SHA256
        ),
        worker_runner: Optional[GroundingDinoWorkerRunner] = None,
        timeout_seconds: float = RUNNER_TIMEOUT_SECONDS,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not 5 <= float(timeout_seconds) <= RUNNER_TIMEOUT_SECONDS
        ):
            raise CommandProposalGenerationError(
                "Grounding DINO worker timeout is invalid"
            )
        self._pack = load_verified_grounding_dino_pack(
            manifest_path,
            trusted_manifest_sha256=trusted_manifest_sha256,
        )
        self._trusted_manifest_sha256 = trusted_manifest_sha256
        self._worker_runner = worker_runner or self._run_worker
        self._timeout_seconds = float(timeout_seconds)

    @classmethod
    def from_program_data(
        cls,
        *,
        program_data: Optional[str] = None,
    ):
        manifest = default_program_data_manifest(
            program_data=program_data
        )
        if not manifest.is_file():
            return UnavailableCommandProposalGenerator()
        try:
            return cls(manifest)
        except CommandProposalGenerationError:
            return UnavailableCommandProposalGenerator(
                "설치된 Grounding DINO 평가용 Model Pack이 "
                "무결성 검증을 통과하지 못했습니다."
            )

    def status(self) -> Mapping[str, Any]:
        model_row = next(
            row
            for row in self._pack.manifest["artifacts"]
            if row["path"] == MODEL_ARTIFACT
        )
        policy = self._pack.manifest["device_policy"]
        return {
            "state": "ready",
            "error_code": None,
            "provider": "transformers_grounding_dino",
            "pack_id": PACK_ID,
            "pack_version": PACK_VERSION,
            "manifest_sha256": self._pack.manifest_sha256,
            "capabilities": ["natural_language_box_proposals"],
            "supported_issue_types": list(SUPPORTED_ISSUE_TYPES),
            "review_only": True,
            "bulk_frame_detection_provider": "local_model",
            "model_pack": {
                "provenance_verified": True,
                "provider": "transformers_grounding_dino",
                "pack_id": PACK_ID,
                "pack_version": PACK_VERSION,
                "manifest_sha256": self._pack.manifest_sha256,
                "model_name": "Grounding DINO Tiny",
                "model_version": SOURCE_REVISION,
                "model_artifact_sha256": model_row["sha256"],
                "source_repo_id": SOURCE_REPO_ID,
                "source_revision": SOURCE_REVISION,
                "weights_spdx": "Apache-2.0",
                "runtime_spdx": "Apache-2.0",
                "commercial_use_confirmed": True,
                "redistribution_allowed": False,
                "gpu_required": False,
                "cpu_fallback": True,
                "recommended_min_vram_mb": int(
                    policy["maximum_cuda_allocator_bytes"]
                    // (1024 * 1024)
                ),
                "cuda_min_version": None,
                "review_only": True,
            },
        }

    def generate(
        self,
        *,
        project_id: UUID,
        analysis_job_id: UUID,
        recipe_id: str,
        command_plan: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
        resolve_image: Callable[
            [UUID, UUID, str], ResolvedAnalysisArtifact
        ],
    ) -> CommandProposalGenerationResult:
        return self.generate_taxonomy(
            project_id=project_id,
            analysis_job_id=analysis_job_id,
            recipe_id=recipe_id,
            command_plans=[command_plan],
            candidates=candidates,
            resolve_image=resolve_image,
        )

    def generate_taxonomy(
        self,
        *,
        project_id: UUID,
        analysis_job_id: UUID,
        recipe_id: str,
        command_plans: Sequence[Mapping[str, Any]],
        candidates: Sequence[Mapping[str, Any]],
        resolve_image: Callable[
            [UUID, UUID, str], ResolvedAnalysisArtifact
        ],
    ) -> CommandProposalGenerationResult:
        if (
            not isinstance(project_id, UUID)
            or not isinstance(analysis_job_id, UUID)
            or not isinstance(recipe_id, str)
            or _SHA256.fullmatch(recipe_id) is None
            or not isinstance(command_plans, Sequence)
            or isinstance(command_plans, (str, bytes, bytearray))
            or not 1 <= len(command_plans) <= len(
                SUPPORTED_ISSUE_TYPES
            )
        ):
            raise CommandProposalGenerationError(
                "Grounding DINO taxonomy request is invalid"
            )
        selected_queries: list[dict[str, str]] = []
        selected_issue_types: set[str] = set()
        for raw_plan in command_plans:
            plan = validate_command_plan(raw_plan)
            issue_type = str(plan["issue_type"])
            if (
                issue_type in selected_issue_types
                or issue_type not in self._pack.queries_by_issue_type
            ):
                raise CommandProposalGenerationError(
                    "Grounding DINO taxonomy issue type is unsupported"
                )
            selected_issue_types.add(issue_type)
            selected_queries.append(
                dict(self._pack.queries_by_issue_type[issue_type])
            )
        if (
            not isinstance(candidates, Sequence)
            or isinstance(candidates, (str, bytes, bytearray))
            or not 1 <= len(candidates)
            <= self._pack.maximum_frames_per_run
            or not callable(resolve_image)
        ):
            raise CommandProposalGenerationError(
                "Grounding DINO candidate scope exceeds the verified pack limit"
            )
        candidate_rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in candidates:
            candidate_id = (
                candidate.get("candidate_id")
                if isinstance(candidate, Mapping)
                else None
            )
            if (
                not isinstance(candidate_id, str)
                or _CANDIDATE_ID.fullmatch(candidate_id) is None
                or candidate_id in seen
            ):
                raise CommandProposalGenerationError(
                    "Grounding DINO candidate identity is invalid"
                )
            seen.add(candidate_id)
            artifact = resolve_image(
                project_id, analysis_job_id, candidate_id
            )
            if (
                not isinstance(artifact, ResolvedAnalysisArtifact)
                or artifact.media_type != "image/jpeg"
                or not artifact.path.is_absolute()
                or not artifact.path.is_file()
                or artifact.path.is_symlink()
                or _is_reparse_point(artifact.path)
                or not isinstance(artifact.size_bytes, int)
                or not 1 <= artifact.size_bytes <= 24 * 1024 * 1024
                or not isinstance(artifact.sha256, str)
                or _SHA256.fullmatch(artifact.sha256) is None
            ):
                raise CommandProposalGenerationError(
                    "Grounding DINO candidate image is invalid"
                )
            candidate_rows.append(
                {
                    "candidate_id": candidate_id,
                    "image_path": str(artifact.path),
                    "image_size_bytes": artifact.size_bytes,
                    "image_sha256": artifact.sha256,
                }
            )
        request = {
            "schema_version": WORKER_REQUEST_SCHEMA,
            "project_id": str(project_id),
            "analysis_job_id": str(analysis_job_id),
            "recipe_id": recipe_id,
            "pack_manifest": str(self._pack.manifest_path),
            "pack_manifest_sha256": self._pack.manifest_sha256,
            "queries": selected_queries,
            "candidates": candidate_rows,
            "box_threshold": BOX_THRESHOLD,
            "text_threshold": TEXT_THRESHOLD,
            "max_predictions_per_frame": (
                self._pack.maximum_predictions_per_frame
            ),
            "maximum_cuda_allocator_bytes": self._pack.manifest[
                "device_policy"
            ]["maximum_cuda_allocator_bytes"],
        }
        raw_result = self._worker_runner(request)
        proposals = self._validate_worker_result(
            raw_result,
            expected_project_id=project_id,
            expected_analysis_job_id=analysis_job_id,
            expected_recipe_id=recipe_id,
            expected_candidate_ids=seen,
            expected_issue_types=selected_issue_types,
        )
        return CommandProposalGenerationResult(
            available=True,
            generated=True,
            message_ko=(
                f"검증된 로컬 Grounding DINO가 {len(candidate_rows)}개 "
                "프레임을 검사해 사용자 검토용 박스만 제안했습니다."
            ),
            evaluated_candidate_count=len(candidate_rows),
            model={
                "provider": "transformers_grounding_dino",
                "pack_id": PACK_ID,
                "pack_version": PACK_VERSION,
                "manifest_sha256": self._pack.manifest_sha256,
            },
            evaluation={
                "evaluation_id": f"pack-{recipe_id}",
                "producer": PRODUCER,
                "producer_version": PRODUCER_VERSION,
            },
            proposals=tuple(proposals),
        )

    def _run_worker(
        self, request: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        executable = Path(sys.executable).resolve(strict=True)
        if getattr(sys, "frozen", False):
            argv = [str(executable), "--grounding-dino-pack-worker"]
        else:
            worker = (
                Path(__file__)
                .with_name("grounding_dino_pack_worker.py")
                .resolve(strict=True)
            )
            argv = [str(executable), str(worker)]
        environment = {
            key: value
            for key, value in {
                "SystemRoot": os.environ.get("SystemRoot"),
                "WINDIR": os.environ.get("WINDIR"),
                "TEMP": os.environ.get("TEMP"),
                "TMP": os.environ.get("TMP"),
                "PATH": os.environ.get("PATH"),
                "CUDA_PATH": os.environ.get("CUDA_PATH"),
                "CUDA_VISIBLE_DEVICES": os.environ.get(
                    "CUDA_VISIBLE_DEVICES"
                ),
                "TRAFFICSUPERVISION_EXTERNAL_MODEL_RUNTIME_PACK": (
                    os.environ.get(
                        "TRAFFICSUPERVISION_EXTERNAL_MODEL_RUNTIME_PACK"
                    )
                ),
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1",
                "NO_PROXY": "*",
                "no_proxy": "*",
                "TOKENIZERS_PARALLELISM": "false",
                "OMP_NUM_THREADS": "2",
                "USE_TF": "0",
                "USE_FLAX": "0",
                "USE_TORCH": "1",
            }.items()
            if value
        }
        completed = _run_bounded_process(
            argv,
            input=_canonical_json(request),
            cwd=str(self._pack.root),
            env=environment,
            timeout=self._timeout_seconds,
        )
        if (
            completed.returncode != 0
            or not isinstance(completed.stdout, bytes)
            or not completed.stdout
        ):
            raise CommandProposalGenerationError(
                "Grounding DINO local worker failed"
            )
        try:
            result = json.loads(
                completed.stdout.decode("utf-8", errors="strict"),
                parse_constant=lambda _: (_ for _ in ()).throw(
                    CommandProposalGenerationError(
                        "Grounding DINO worker returned a non-finite value"
                    )
                ),
            )
        except CommandProposalGenerationError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CommandProposalGenerationError(
                "Grounding DINO worker response is invalid"
            ) from error
        if not isinstance(result, Mapping):
            raise CommandProposalGenerationError(
                "Grounding DINO worker response is invalid"
            )
        return result

    def _validate_worker_result(
        self,
        value: Mapping[str, Any],
        *,
        expected_project_id: UUID,
        expected_analysis_job_id: UUID,
        expected_recipe_id: str,
        expected_candidate_ids: set[str],
        expected_issue_types: set[str],
    ) -> list[dict[str, Any]]:
        if (
            not isinstance(value, Mapping)
            or set(value)
            != {
                "schema_version",
                "project_id",
                "analysis_job_id",
                "recipe_id",
                "pack_manifest_sha256",
                "score_semantics",
                "results",
            }
            or value.get("schema_version") != WORKER_RESPONSE_SCHEMA
            or value.get("project_id") != str(expected_project_id)
            or value.get("analysis_job_id")
            != str(expected_analysis_job_id)
            or value.get("recipe_id") != expected_recipe_id
            or value.get("pack_manifest_sha256")
            != self._pack.manifest_sha256
            or value.get("score_semantics") != SCORE_SEMANTICS
            or not isinstance(value.get("results"), list)
            or len(value["results"]) != len(expected_candidate_ids)
        ):
            raise CommandProposalGenerationError(
                "Grounding DINO worker crossed its requested scope"
            )
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in value["results"]:
            if (
                not isinstance(row, Mapping)
                or set(row)
                != {"candidate_id", "status", "predictions"}
            ):
                raise CommandProposalGenerationError(
                    "Grounding DINO worker result is invalid"
                )
            candidate_id = row.get("candidate_id")
            predictions = row.get("predictions")
            status = row.get("status")
            if (
                candidate_id not in expected_candidate_ids
                or candidate_id in seen
                or status not in {"classified", "no_suggestion"}
                or not isinstance(predictions, list)
                or len(predictions)
                > self._pack.maximum_predictions_per_frame
                or (status == "classified") != bool(predictions)
            ):
                raise CommandProposalGenerationError(
                    "Grounding DINO worker result scope is invalid"
                )
            seen.add(str(candidate_id))
            safe_predictions: list[dict[str, Any]] = []
            for prediction in predictions:
                try:
                    validated = (
                        ExperimentalProposalPrediction.model_validate(
                            prediction
                        )
                    )
                except Exception as error:
                    raise CommandProposalGenerationError(
                        "Grounding DINO worker prediction is invalid"
                    ) from error
                if (
                    validated.issue_type
                    not in expected_issue_types
                    or validated.calibration != "model_score"
                    or validated.model_score is None
                    or validated.box is None
                ):
                    raise CommandProposalGenerationError(
                        "Grounding DINO worker prediction changed roles"
                    )
                safe_predictions.append(
                    validated.model_dump(mode="json")
                )
            safe_predictions.sort(
                key=lambda item: (
                    -float(item["model_score"]),
                    str(item["issue_type"]),
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            )
            if safe_predictions:
                normalized.append(
                    {
                        "candidate_id": candidate_id,
                        "status": "classified",
                        "predictions": safe_predictions,
                    }
                )
        if seen != expected_candidate_ids:
            raise CommandProposalGenerationError(
                "Grounding DINO worker omitted a candidate"
            )
        normalized.sort(key=lambda row: str(row["candidate_id"]))
        return normalized
