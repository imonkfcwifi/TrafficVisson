import hashlib
import json
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping
from uuid import UUID, uuid4, uuid5

import pytest
from fastapi import HTTPException

from app.approved_incident_authority import (
    SURVEY_INCIDENT_AUTHORITY_SCHEMA,
    canonical_sha256,
)
from app.analysis_jobs import (
    ResolvedAnalysisArtifact,
    ResolvedAnalysisCandidateImage,
    ResolvedAnalysisCandidateImageBatch,
)
from app.candidate_evidence_overlay import (
    CandidateEvidenceMediaCommandOutput,
    CandidateEvidenceOverlayLimits,
    CandidateEvidenceOverlayStore,
    ResolvedCandidateEvidenceBinding,
    ResolvedDenseCandidateFrame,
)
from app.candidate_evidence_registry import (
    CandidateEvidenceRegistry,
)
from app.candidate_labels import CandidateLabelStore
from app.contracts import PipelineJobResponse
from app.derived_outputs import DerivedOutputsResult
from app.dense_frame_index import (
    DenseFrameIndexLimits,
    build_dense_frame_capacity_plan,
    initialize_dense_staging_root,
)
from app.dense_candidate_discovery import (
    capture_promoted_model_job_binding,
)
from app.media_intake import CompletedMediaGrant, MediaIntakeService
from app.folder_pipeline_batches import build_pipeline_storage_plan
from app.pipeline_jobs import (
    BridgePipelineRuntime,
    PIPELINE_JOB_RERUN_SCHEMA,
    PIPELINE_RERUN_MEDIA_PROOF_SCHEMA,
    PIPELINE_STAGES,
    REPORT_INSPECTION_DATE_POLICY,
    PipelineJobConflict,
    PipelineJobRecord,
    PipelineJobService,
    PipelineProfileMissing,
    PipelineStageFailure,
    PipelineStageResult,
    _derived_outputs_capacity_upper_bound,
    _provider_visual_capacity_upper_bound,
    _report_inspection_date_from_source_filename,
    _validated_storage_revisions,
)
from app.project_catalog import ProjectCatalog
from app.project_model_lifecycle import (
    ResolvedActiveProjectModel,
    VISUAL_CENTROID_MODEL_FORMAT,
)
from app.project_artifact_roots import (
    ProjectArtifactRootRegistry,
    ProjectArtifactRootUnavailable,
)
from app.weak_case_visual_index import WeakCaseVisualIndexError
from test_analysis_jobs import _make_video
from test_project_catalog_v2 import _project, _write_completed_analysis


def _wait(service, project_id, job_id, statuses, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = service.get(project_id, job_id)
        if row["status"] in statuses:
            return row
        time.sleep(0.01)
    raise AssertionError(service.get(project_id, job_id))


def test_completed_media_grant_allows_only_verified_in_flight_analysis_binding(
    tmp_path,
):
    project_id = uuid4()
    pipeline_job_id = uuid4()
    analysis_job_id = uuid4()
    snapshot_id = uuid4()
    hash_job_id = uuid4()
    source_path = (tmp_path / "drive.mp4").resolve()
    source_body = b"pipeline-owned-video"
    source_path.write_bytes(source_body)
    source_stat = source_path.stat()
    source_sha256 = hashlib.sha256(source_body).hexdigest()
    grant = CompletedMediaGrant(
        project_id=project_id,
        snapshot_id=snapshot_id,
        hash_job_id=hash_job_id,
        file_name=source_path.name,
        media_path=source_path,
        size_bytes=source_stat.st_size,
        modified_time_ns=source_stat.st_mtime_ns,
        sha256_lower=source_sha256,
    )

    class GrantRuntime(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.resolved_analysis_ids = []

        def resolve_completed_media_grant(
            self,
            *,
            project_id,
            analysis_job_id,
            context,
            expected_source_sha256,
        ):
            assert project_id == grant.project_id
            assert context["job_id"] == str(pipeline_job_id)
            assert expected_source_sha256 == source_sha256
            self.resolved_analysis_ids.append(analysis_job_id)
            return grant

    runtime = GrantRuntime()
    service = PipelineJobService(
        runtime,
        data_root=(tmp_path / "bridge-data").resolve(),
    )
    stages = [
        {
            "name": stage_name,
            "status": "pending",
            "outcome": None,
            "attempts": 0,
            "started_at": None,
            "completed_at": None,
            "error_code": None,
            "checkpoint": {},
            "metrics": {},
        }
        for stage_name in PIPELINE_STAGES
    ]
    sha_stage = stages[PIPELINE_STAGES.index("sha256")]
    sha_stage.update(
        {
            "status": "success",
            "outcome": "success",
            "checkpoint": {
                "source_sha256": source_sha256,
                "hash_job_id": str(hash_job_id),
                "total_bytes": source_stat.st_size,
            },
        }
    )
    candidate_stage = stages[
        PIPELINE_STAGES.index("candidate_rescan_5_10fps")
    ]
    candidate_stage["status"] = "running"
    now = datetime.now(timezone.utc)
    job = PipelineJobRecord(
        job_id=pipeline_job_id,
        project_id=project_id,
        snapshot_id=snapshot_id,
        file_name=source_path.name,
        idempotency_key="pipeline-test-in-flight-analysis-binding-0001",
        input_fingerprint="a" * 64,
        profile_snapshot={"candidate_rescan_fps": 8},
        capability_snapshot={},
        status="running",
        current_stage="candidate_rescan_5_10fps",
        outcome_code=None,
        error_code=None,
        retry_count=0,
        resume_count=0,
        review_completion_id=None,
        stages=stages,
        provenance={"profile_sha256": "b" * 64},
        created_at=now,
        updated_at=now,
        recovered_after_restart=False,
        cancel_event=threading.Event(),
    )
    service._jobs[pipeline_job_id] = job

    assert (
        service.resolve_completed_media_grant(
            project_id,
            pipeline_job_id,
            analysis_job_id,
            source_sha256,
        )
        == grant
    )
    assert runtime.resolved_analysis_ids == [analysis_job_id]

    candidate_stage["status"] = "pending"
    with pytest.raises(PipelineJobConflict):
        service.resolve_completed_media_grant(
            project_id,
            pipeline_job_id,
            analysis_job_id,
            source_sha256,
        )

    candidate_stage.update(
        {
            "status": "success",
            "outcome": "success",
            "checkpoint": {"analysis_job_id": str(analysis_job_id)},
        }
    )
    assert (
        service.resolve_completed_media_grant(
            project_id,
            pipeline_job_id,
            analysis_job_id,
            source_sha256,
        )
        == grant
    )


class FakeRuntime:
    def __init__(self):
        self.calls = []
        self.contexts = []
        self.analysis_job_id = None
        self.model_ready = True
        self.zero_detections = False
        self.block_stage = None
        self.block_started = threading.Event()
        self.block_release = threading.Event()
        self.fail_once = None
        self.cancel_calls = 0
        self.review_valid = True
        self.report_profile_ready = True

    def capability_snapshot(self, profile):
        return {
            "local_models": "ready" if self.model_ready else "missing",
            "selected_report_provider": profile["report_provider"],
            "bulk_frame_provider": "local_models_only",
        }

    def validate_report_profile(
        self,
        *,
        project_id,
        template_id,
        template_sha256,
        semantic_map_id,
        provider,
        explicit_transmission_consent,
    ):
        assert isinstance(project_id, UUID)
        assert template_id
        assert len(template_sha256) == 64
        return {
            "template_semantic_map_id": semantic_map_id,
            "template_semantic_map_sha256": "9" * 64,
            "template_semantic_map_ready": self.report_profile_ready,
            "report_provider_ready": self.report_profile_ready,
            "report_provider_state": (
                "ready" if self.report_profile_ready else "offline"
            ),
            "report_provider_capabilities": (
                ["report_draft", "document_edit_plan"]
                if self.report_profile_ready
                else []
            ),
            "explicit_transmission_consent": (
                explicit_transmission_consent
            ),
            "provider": provider,
            "path_exposed": False,
        }

    def execute(self, stage, context, cancel):
        self.calls.append(stage)
        self.contexts.append((stage, context))
        if stage == self.block_stage:
            self.block_started.set()
            while not self.block_release.wait(0.01):
                if cancel():
                    return PipelineStageResult(
                        "cancelled", error_code="test_cancelled"
                    )
        if stage == "intake_validation":
            return PipelineStageResult(
                "success",
                checkpoint={
                    "intake_validation_id": "intake_validation-v1",
                    "duration_seconds": 60.0,
                },
                metrics={"completed": 1},
            )
        if stage == self.fail_once:
            self.fail_once = None
            raise PipelineStageFailure("bounded_test_failure")
        if stage == "metadata_gps_preflight":
            return PipelineStageResult(
                "blocked_gpx_required",
                checkpoint={
                    "timed_track_status": "missing",
                    "capture_start_coordinate_used_as_route": False,
                },
                metrics={"route_point_count": 0},
                error_code="gpx_required",
                non_blocking=True,
            )
        if stage == "candidate_rescan_5_10fps" and not self.model_ready:
            return PipelineStageResult(
                "model_unavailable",
                error_code="candidate_rescan_model_unavailable",
            )
        if (
            stage == "candidate_rescan_5_10fps"
            and self.analysis_job_id is not None
        ):
            return PipelineStageResult(
                "success",
                checkpoint={
                    "analysis_job_id": str(self.analysis_job_id),
                    "source_sha256": hashlib.sha256(
                        b"projection-gate-source"
                    ).hexdigest(),
                },
                metrics={"candidate_count": 2},
            )
        if stage == "detection_ocr" and self.zero_detections:
            return PipelineStageResult(
                "zero_detections",
                checkpoint={"detection_count": 0},
                metrics={"detection_count": 0},
            )
        if stage == "question_queue":
            checkpoint = {"question_queue_revision": "questions-v1"}
            if self.analysis_job_id is not None:
                checkpoint["analysis_job_id"] = str(
                    self.analysis_job_id
                )
            return PipelineStageResult(
                "success",
                checkpoint=checkpoint,
                metrics={"question_count": 2},
            )
        if stage == "derived_outputs":
            assert context["review_completion_id"] or self.zero_detections
            dataset_version_id = (
                "aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa"
            )
            dataset_hash = "1" * 64
            return PipelineStageResult(
                "success",
                checkpoint={
                    "derived_output_revision": "outputs-v1",
                    "dataset_version_id": dataset_version_id,
                    "dataset_hash": dataset_hash,
                    "dataset_version": {
                        "state": "prepared",
                        "dataset_version_id": dataset_version_id,
                        "dataset_hash": dataset_hash,
                    },
                },
                metrics={"output_count": 6},
            )
        return PipelineStageResult(
            "success",
            checkpoint={f"{stage}_id": f"{stage}-v1"},
            metrics={"completed": 1},
        )

    def cancel(self, _context):
        self.cancel_calls += 1

    def validate_review_completion(
        self, _context, review_completion_id
    ):
        return {
            "review_completion_id": review_completion_id,
            "all_required_questions_answered": self.review_valid,
            "answered_question_count": 2 if self.review_valid else 0,
            "server_answer_ledger_revision": "answers-v1",
        }


class RerunRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.source_sha256 = hashlib.sha256(
            b"server-owned-rerun-source"
        ).hexdigest()
        self.hash_job_id = uuid4()
        self.total_bytes = 4_096
        self.rerun_validation_calls = []
        self.reject_rerun_source = False

    def capability_snapshot(self, profile):
        detector_pack_id = profile["detector_pack_id"]
        ocr_pack_id = profile["ocr_pack_id"]
        sam2_pack_id = profile["sam2_pack_id"]
        return {
            "local_models": "ready",
            "selected_report_provider": profile["report_provider"],
            "bulk_frame_provider": "local_models_only",
            "requested_detector_pack_id": detector_pack_id,
            "requested_ocr_pack_id": ocr_pack_id,
            "requested_sam2_pack_id": sam2_pack_id,
            "installed_detector_pack_id": detector_pack_id,
            "installed_ocr_pack_id": ocr_pack_id,
            "installed_sam2_pack_id": sam2_pack_id,
            "detector_model_manifest_sha256": hashlib.sha256(
                f"detector:{detector_pack_id}".encode()
            ).hexdigest(),
            "ocr_model_manifest_sha256": hashlib.sha256(
                f"ocr:{ocr_pack_id}".encode()
            ).hexdigest(),
            "sam2_model_manifest_sha256": hashlib.sha256(
                f"sam2:{sam2_pack_id}".encode()
            ).hexdigest(),
        }

    def execute(self, stage, context, cancel):
        if stage != "sha256":
            return super().execute(stage, context, cancel)
        self.calls.append(stage)
        self.contexts.append((stage, context))
        return PipelineStageResult(
            "success",
            checkpoint={
                "hash_job_id": str(self.hash_job_id),
                "source_sha256": self.source_sha256,
                "total_bytes": self.total_bytes,
            },
            metrics={"bytes_hashed": self.total_bytes},
        )

    def validate_server_owned_rerun_source(
        self,
        *,
        project_id,
        snapshot_id,
        file_name,
        sha_checkpoint,
    ):
        self.rerun_validation_calls.append(
            {
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "file_name": file_name,
                "sha_checkpoint": dict(sha_checkpoint),
            }
        )
        if self.reject_rerun_source:
            raise PipelineStageFailure(
                "private_media_grant_revalidation_failed"
            )
        assert sha_checkpoint["hash_job_id"] == str(self.hash_job_id)
        assert sha_checkpoint["source_sha256"] == self.source_sha256
        assert sha_checkpoint["total_bytes"] == self.total_bytes
        return {
            "schema_version": PIPELINE_RERUN_MEDIA_PROOF_SCHEMA,
            "project_id": str(project_id),
            "snapshot_id": str(snapshot_id),
            "hash_job_id": str(self.hash_job_id),
            "file_name": file_name,
            "source_sha256": self.source_sha256,
            "size_bytes": self.total_bytes,
            "path_exposed": False,
        }


def _pipeline_resolved_model(
    root: Path,
    project_id: UUID,
    *,
    marker: str,
    state_revision: int,
) -> ResolvedActiveProjectModel:
    training_analysis_job_id = uuid5(
        project_id,
        f"training-analysis-{marker}",
    )
    validation_analysis_job_id = uuid5(
        project_id,
        f"validation-analysis-{marker}",
    )
    source_lineage = {
        "training_analysis_job_ids": [
            str(training_analysis_job_id)
        ],
        "fixed_validation_analysis_job_ids": [
            str(validation_analysis_job_id)
        ],
    }
    source_lineage_sha256 = hashlib.sha256(
        json.dumps(
            source_lineage,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return ResolvedActiveProjectModel(
        project_id=str(project_id),
        state_revision=state_revision,
        model_version_id=str(
            uuid5(project_id, f"model-version-{marker}")
        ),
        model_version=f"project-model-{marker}@1.0",
        model_format=VISUAL_CENTROID_MODEL_FORMAT,
        dataset_version_id=str(
            uuid5(project_id, f"dataset-version-{marker}")
        ),
        dataset_hash=hashlib.sha256(
            f"dataset:{marker}".encode("utf-8")
        ).hexdigest(),
        fixed_validation_sha256=hashlib.sha256(
            f"validation:{marker}".encode("utf-8")
        ).hexdigest(),
        primary_artifact_path=root / f"model-{marker}.json",
        primary_artifact_sha256=hashlib.sha256(
            f"artifact:{marker}".encode("utf-8")
        ).hexdigest(),
        artifact_manifest_path=(
            root / f"artifact-manifest-{marker}.json"
        ),
        artifact_manifest_sha256=hashlib.sha256(
            f"manifest:{marker}".encode("utf-8")
        ).hexdigest(),
        training_analysis_job_ids=(
            str(training_analysis_job_id),
        ),
        fixed_validation_analysis_job_ids=(
            str(validation_analysis_job_id),
        ),
        source_analysis_lineage_sha256=source_lineage_sha256,
        dataset_provenance_graph_sha256=hashlib.sha256(
            f"dataset-provenance:{marker}".encode("utf-8")
        ).hexdigest(),
    )


class PromotedModelRerunRuntime(RerunRuntime):
    def __init__(self, model: ResolvedActiveProjectModel):
        super().__init__()
        self.model = model

    def capability_snapshot(self, profile):
        snapshot = super().capability_snapshot(profile)
        project_id = UUID(str(profile["project_id"]))
        snapshot["promoted_project_model_binding"] = (
            capture_promoted_model_job_binding(
                project_id,
                lambda _project_id: self.model,
            )
        )
        return snapshot


def _structured_delegation_profile(service, project_id):
    return service.configure_profile(
        project_id,
        question_policy="learning_assist",
        review_mode="precision",
        label_confirmation_authority=(
            "structured_llm_training_assist"
        ),
        default_template_id="sample-4q-v1",
        default_template_sha256="8" * 64,
        detector_pack_id="road-detector-v1",
        ocr_pack_id="korean-ocr-v1",
        sam2_pack_id="sam2-v1",
        report_provider="codex",
        report_transmission_consent=True,
        template_semantic_map_id="sample-q4-semantic-v1",
        output_location_id="project-default",
        candidate_rescan_fps=8,
    )


class StructuredDelegationRuntime(FakeRuntime):
    def __init__(
        self,
        project_id,
        *,
        partial_human_review=False,
        corrupt_review_proof=False,
    ):
        super().__init__()
        self.analysis_id = uuid4()
        self.review_validation_calls = []
        self.corrupt_review_proof = corrupt_review_proof
        candidate_ids = ["a" * 20, "b" * 20]
        human_ids = (
            [candidate_ids[-1]]
            if partial_human_review
            else []
        )
        delegated_ids = [
            candidate_id
            for candidate_id in candidate_ids
            if candidate_id not in human_ids
        ]
        delegated_proofs = [
            {
                "candidate_id": candidate_id,
                "record_id": str(uuid4()),
                "record_hash": hashlib.sha256(
                    f"record:{candidate_id}".encode("utf-8")
                ).hexdigest(),
                "answer_revision": 1,
                "assist_provenance_sha256": hashlib.sha256(
                    f"assist:{candidate_id}".encode("utf-8")
                ).hexdigest(),
            }
            for candidate_id in delegated_ids
        ]
        authority = {
            "schema_version": (
                "trafficvisson.structured-assist-authority.v1"
            ),
            "project_id": str(project_id),
            "authority": "project_profile_delegation",
        }
        identity = {
            "schema_version": (
                "trafficvisson.pipeline-structured-label-delegation.v1"
            ),
            "project_id": str(project_id),
            "analysis_job_id": str(self.analysis_id),
            "candidate_ids": candidate_ids,
            "delegation_authority": authority,
            "delegated_proofs": delegated_proofs,
            "human_question_reasons": {
                candidate_id: "mandatory_human_review"
                for candidate_id in human_ids
            },
        }
        delegation_revision = hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        delegation_state = (
            "partial_human_review_required"
            if human_ids
            else "completed"
        )
        delegation = {
            **identity,
            "delegation_revision": delegation_revision,
            "delegated_candidate_ids": delegated_ids,
            "human_question_candidate_ids": human_ids,
            "mandatory_human_candidate_ids": human_ids,
            "structured_training_label_count": len(
                delegated_ids
            ),
            "training_annotation_count": len(delegated_ids),
            "incident_count": 0,
            "report_incident_count": 0,
            "fixed_validation_membership_count": 0,
            "training_only": True,
            "automatic_training_started": False,
            "authority": "structured_llm_training_assist",
            "state": delegation_state,
        }
        self.question_checkpoint = {
            "analysis_job_id": str(self.analysis_id),
            "source_sha256": "6" * 64,
            "question_count": len(candidate_ids),
            "question_candidate_ids": candidate_ids,
            "human_question_count": len(human_ids),
            "human_question_candidate_ids": human_ids,
            "structured_training_label_count": len(
                delegated_ids
            ),
            "structured_training_label_candidate_ids": (
                delegated_ids
            ),
            "mandatory_human_candidate_ids": human_ids,
            "label_confirmation_authority": (
                "structured_llm_training_assist"
            ),
            "structured_label_delegation_state": (
                delegation_state
            ),
            "structured_label_delegation_revision": (
                delegation_revision
            ),
            "structured_label_delegation": delegation,
            "question_queue_revision": "7" * 64,
            "review_only": True,
            "automatic_labeling_allowed": bool(delegated_ids),
            "automatic_annotation_allowed": bool(delegated_ids),
            "automatic_incident_allowed": False,
            "automatic_training_membership_allowed": bool(
                delegated_ids
            ),
            "automatic_fixed_validation_membership_allowed": False,
            "automatic_report_truth_allowed": False,
            "annotation_count": len(delegated_ids),
            "incident_count": 0,
            "training_membership_count": len(delegated_ids),
            "report_incident_count": 0,
        }
        proof_fragments = [
            "\0".join(
                (
                    proof["candidate_id"],
                    proof["record_id"].lower(),
                    proof["record_hash"],
                    str(proof["answer_revision"]),
                )
            )
            for proof in delegated_proofs
        ]
        self.expected_completion_id = (
            "review:"
            + hashlib.sha256(
                "\n".join(sorted(proof_fragments)).encode("utf-8")
            ).hexdigest()
        )

    def execute(self, stage, context, cancel):
        if stage != "question_queue":
            return super().execute(stage, context, cancel)
        self.calls.append(stage)
        self.contexts.append((stage, context))
        return PipelineStageResult(
            "success",
            checkpoint=self.question_checkpoint,
            metrics={
                "question_count": self.question_checkpoint[
                    "question_count"
                ],
                "human_question_count": self.question_checkpoint[
                    "human_question_count"
                ],
                "structured_training_label_count": (
                    self.question_checkpoint[
                        "structured_training_label_count"
                    ]
                ),
                "annotation_count": self.question_checkpoint[
                    "annotation_count"
                ],
                "incident_count": 0,
                "training_membership_count": (
                    self.question_checkpoint[
                        "training_membership_count"
                    ]
                ),
                "report_incident_count": 0,
            },
        )

    def validate_review_completion(
        self, context, review_completion_id
    ):
        self.review_validation_calls.append(
            (context, review_completion_id)
        )
        assert context["checkpoints"]["question_queue"] == (
            self.question_checkpoint
        )
        assert review_completion_id == self.expected_completion_id
        ledger_revision = review_completion_id.split(":", 1)[1]
        return {
            "review_completion_id": review_completion_id,
            "all_required_questions_answered": True,
            "answered_question_count": self.question_checkpoint[
                "question_count"
            ],
            "server_answer_ledger_revision": (
                "0" * 64
                if self.corrupt_review_proof
                else ledger_revision
            ),
            "analysis_job_id": str(self.analysis_id),
            "question_queue_revision": self.question_checkpoint[
                "question_queue_revision"
            ],
        }


def _configure(service, project_id):
    return service.configure_profile(
        project_id,
        question_policy="learning_assist",
        review_mode="precision",
        default_template_id="sample-4q-v1",
        default_template_sha256=hashlib.sha256(b"template").hexdigest(),
        detector_pack_id="road-detector-v1",
        ocr_pack_id="korean-ocr-v1",
        sam2_pack_id="sam2-v1",
        report_provider="codex",
        output_location_id="project-default",
        candidate_rescan_fps=8,
    )


def _start(service, project_id, *, suffix="same"):
    return service.start(
        project_id,
        snapshot_id=uuid4(),
        file_name="drive.mp4",
        rights_confirmed=True,
        idempotency_key=f"pipeline-test-{suffix}-0001",
    )


def test_restart_fails_closed_when_pinned_custom_artifact_root_is_tampered(
    tmp_path,
    monkeypatch,
):
    fixed_disk_usage = lambda _path: SimpleNamespace(
        free=100 * 1024**3
    )
    monkeypatch.setattr(
        "app.pipeline_jobs.shutil.disk_usage",
        fixed_disk_usage,
    )
    data_root = (tmp_path / "bridge-data").resolve()
    selected = (tmp_path / "large-output").resolve()
    selected.mkdir()
    project_id = uuid4()
    registry = ProjectArtifactRootRegistry(
        data_root=data_root,
        disk_usage=fixed_disk_usage,
    )
    location = registry.register(
        project_id, selected_directory=selected
    )
    first = PipelineJobService(
        FakeRuntime(),
        data_root=data_root,
        artifact_root_registry=registry,
    )
    first.configure_profile(
        project_id,
        question_policy="learning_assist",
        review_mode="precision",
        default_template_id="sample-4q-v1",
        default_template_sha256=hashlib.sha256(
            b"template"
        ).hexdigest(),
        detector_pack_id="road-detector-v1",
        ocr_pack_id="korean-ocr-v1",
        sam2_pack_id="sam2-v1",
        report_provider="codex",
        output_location_id=location["output_location_id"],
        candidate_rescan_fps=8,
    )
    started, _ = _start(first, project_id, suffix="custom-root")
    waiting = _wait(
        first,
        project_id,
        started["job_id"],
        {"awaiting_review"},
    )
    assert waiting["status"] == "awaiting_review"
    first.shutdown()

    resolved = registry.resolve(
        project_id,
        output_location_id=location["output_location_id"],
    )
    marker = (
        resolved.root / ".trafficsupervision-root.json"
    )
    marker.unlink()
    marker.with_suffix(".json.sha256").unlink()

    restarted = PipelineJobService(
        FakeRuntime(),
        data_root=data_root,
        artifact_root_registry=ProjectArtifactRootRegistry(
            data_root=data_root,
            disk_usage=fixed_disk_usage,
        ),
    )
    failed = restarted.get(project_id, started["job_id"])
    assert failed["status"] == "failed"
    assert failed["error_code"] == (
        "artifact_root_revalidation_failed"
    )
    assert failed["recovered_after_restart"] is True
    restarted.shutdown()


def test_restart_reconstructs_awaiting_job_same_volume_reservation(
    tmp_path,
    monkeypatch,
):
    gib = 1024**3

    def fixed_disk_usage(_path):
        return SimpleNamespace(
            total=100 * gib,
            used=30 * gib,
            free=70 * gib,
        )

    from app.folder_pipeline_batches import (
        build_pipeline_storage_plan,
    )
    monkeypatch.setattr(
        "app.pipeline_jobs.shutil.disk_usage",
        fixed_disk_usage,
    )

    data_root = (tmp_path / "bridge-data").resolve()
    selected = (tmp_path / "large-output").resolve()
    selected.mkdir()
    project_id = uuid4()
    first_registry = ProjectArtifactRootRegistry(
        data_root=data_root,
        disk_usage=fixed_disk_usage,
    )
    location = first_registry.register(
        project_id,
        selected_directory=selected,
    )
    first = PipelineJobService(
        FakeRuntime(),
        data_root=data_root,
        artifact_root_registry=first_registry,
    )
    first.configure_profile(
        project_id,
        question_policy="learning_assist",
        review_mode="precision",
        default_template_id="sample-4q-v1",
        default_template_sha256=hashlib.sha256(
            b"template"
        ).hexdigest(),
        detector_pack_id="road-detector-v1",
        ocr_pack_id="korean-ocr-v1",
        sam2_pack_id="sam2-v1",
        report_provider="codex",
        output_location_id=location["output_location_id"],
        candidate_rescan_fps=8,
    )
    storage_plan = build_pipeline_storage_plan(
        duration_seconds=60,
        candidate_rescan_fps=8,
    )
    started, replayed = first.start(
        project_id,
        snapshot_id=uuid4(),
        file_name="drive.mp4",
        rights_confirmed=True,
        idempotency_key=(
            "pipeline-test-reservation-restart-0001"
        ),
        storage_plan=storage_plan,
    )
    assert replayed is False
    waiting = _wait(
        first,
        project_id,
        started["job_id"],
        {"awaiting_review"},
    )
    binding = waiting["provenance"]["artifact_root"]
    first.shutdown()

    restarted_registry = ProjectArtifactRootRegistry(
        data_root=data_root,
        disk_usage=fixed_disk_usage,
    )
    restarted = PipelineJobService(
        FakeRuntime(),
        data_root=data_root,
        artifact_root_registry=restarted_registry,
    )
    recovered = restarted.get(project_id, started["job_id"])
    assert recovered["status"] == "awaiting_review"
    assert recovered["provenance"]["capacity_reservation"][
        "reserved_bytes"
    ] == storage_plan["estimated_artifact_bytes"]

    with pytest.raises(ProjectArtifactRootUnavailable):
        restarted_registry.adjust_capacity_reservation(
            project_id,
            binding,
            reservation_id=f"folder-batch:{uuid4()}",
            reserved_bytes=66 * gib,
            safety_reserve_bytes=storage_plan[
                "safety_reserve_bytes"
            ],
        )
    restarted.shutdown()


@pytest.mark.parametrize("drop_duration", (False, True))
def test_restart_migrates_or_blocks_pre_v2_bound_direct_job(
    tmp_path,
    drop_duration,
    monkeypatch,
):
    gib = 1024**3

    def fixed_disk_usage(_path):
        return SimpleNamespace(
            total=100 * gib,
            used=10 * gib,
            free=90 * gib,
        )

    monkeypatch.setattr(
        "app.pipeline_jobs.shutil.disk_usage",
        fixed_disk_usage,
    )
    data_root = (tmp_path / "bridge-data").resolve()
    selected = (tmp_path / "large-output").resolve()
    selected.mkdir()
    project_id = uuid4()
    first_registry = ProjectArtifactRootRegistry(
        data_root=data_root,
        disk_usage=fixed_disk_usage,
    )
    location = first_registry.register(
        project_id,
        selected_directory=selected,
    )
    first = PipelineJobService(
        FakeRuntime(),
        data_root=data_root,
        artifact_root_registry=first_registry,
    )
    first.configure_profile(
        project_id,
        question_policy="learning_assist",
        review_mode="precision",
        default_template_id="sample-4q-v1",
        default_template_sha256=hashlib.sha256(
            b"template"
        ).hexdigest(),
        detector_pack_id="road-detector-v1",
        ocr_pack_id="korean-ocr-v1",
        sam2_pack_id="sam2-v1",
        report_provider="codex",
        output_location_id=location["output_location_id"],
        candidate_rescan_fps=8,
    )
    started, replayed = first.start(
        project_id,
        snapshot_id=uuid4(),
        file_name="drive.mp4",
        rights_confirmed=True,
        idempotency_key="pipeline-test-pre-v2-recovery-0001",
    )
    assert replayed is False
    _wait(
        first,
        project_id,
        started["job_id"],
        {"awaiting_review"},
    )
    with first._lock:
        durable = first._jobs[UUID(str(started["job_id"]))]
        durable.provenance.pop("storage_plan", None)
        durable.provenance.pop("storage_plan_source", None)
        durable.provenance.pop("storage_plan_revisions", None)
        durable.provenance.pop("capacity_reservation", None)
        if drop_duration:
            durable.stages[0]["checkpoint"].pop(
                "duration_seconds",
                None,
            )
        first._persist_job_locked(durable)
    first.shutdown()

    restarted_registry = ProjectArtifactRootRegistry(
        data_root=data_root,
        disk_usage=fixed_disk_usage,
    )
    restarted = PipelineJobService(
        FakeRuntime(),
        data_root=data_root,
        artifact_root_registry=restarted_registry,
    )
    recovered = restarted.get(project_id, started["job_id"])
    if drop_duration:
        assert recovered["status"] == "failed"
        assert recovered["current_stage"] == "intake_validation"
        assert recovered["error_code"] == (
            "storage_capacity_plan_invalid"
        )
        assert "storage_plan" not in recovered["provenance"]
    else:
        assert recovered["status"] == "awaiting_review"
        assert recovered["provenance"]["storage_plan_source"] == (
            "recovered_intake_duration_preflight"
        )
        assert recovered["provenance"]["storage_plan"][
            "schema_version"
        ] == "trafficvisson.pipeline-capacity-plan.v2"
        assert recovered["provenance"]["capacity_reservation"][
            "reserved_bytes"
        ] > 0
    restarted.shutdown()


def test_exact_stage_replan_is_immutable_and_hard_cap_fails_closed():
    from app.folder_pipeline_batches import (
        build_pipeline_storage_plan,
    )

    plan = build_pipeline_storage_plan(
        duration_seconds=60,
        candidate_rescan_fps=8,
    )
    stage = next(
        row
        for row in plan["stage_estimates"]
        if row["stage"] == "candidate_rescan_5_10fps"
    )
    context = {"storage_plan": plan}
    basis = {
        "basis_type": "actual_merged_candidate_windows",
        "candidate_count": 5,
    }
    required = int(stage["estimated_bytes"]) + 1

    with pytest.raises(PipelineStageFailure) as requested:
        BridgePipelineRuntime._storage_capacity_revision(
            context,
            stage="candidate_rescan_5_10fps",
            required_bytes=required,
            basis=basis,
        )
    assert requested.value.code == (
        "storage_capacity_replan_required"
    )
    revision = requested.value.observation
    assert revision["state"] == "same_volume_adjustment_required"
    assert revision["required_bytes"] == required

    accepted = BridgePipelineRuntime._storage_capacity_revision(
        {
            **context,
            "storage_plan_revisions": [revision],
        },
        stage="candidate_rescan_5_10fps",
        required_bytes=required,
        basis=basis,
    )
    assert accepted["state"] == "same_volume_adjustment_accepted"
    assert accepted["revision_sha256"] == revision["revision_sha256"]

    with pytest.raises(PipelineStageFailure) as blocked:
        BridgePipelineRuntime._storage_capacity_revision(
            context,
            stage="candidate_rescan_5_10fps",
            required_bytes=int(stage["hard_ceiling_bytes"]) + 1,
            basis=basis,
        )
    assert blocked.value.code == (
        "storage_capacity_replan_required"
    )
    assert blocked.value.observation["state"] == (
        "exceeds_absolute_engine_cap"
    )


def test_81_second_initial_plan_fits_35_gib_but_cap_overrun_blocks(
    tmp_path,
    monkeypatch,
):
    from app.folder_pipeline_batches import build_pipeline_storage_plan

    gib = 1024**3
    available = 35 * gib
    plan = build_pipeline_storage_plan(
        duration_seconds=81.1675,
        candidate_rescan_fps=8,
    )
    assert plan["safety_reserve_bytes"] == 4 * gib
    assert plan["required_free_bytes"] < available

    def fixed_disk_usage(_path):
        return SimpleNamespace(
            total=available + 10 * gib,
            used=10 * gib,
            free=available,
        )

    monkeypatch.setattr(
        "app.pipeline_jobs.shutil.disk_usage",
        fixed_disk_usage,
    )
    data_root = (tmp_path / "bridge-data").resolve()
    selected = (tmp_path / "project-output").resolve()
    selected.mkdir()
    project_id = uuid4()
    registry = ProjectArtifactRootRegistry(
        data_root=data_root,
        disk_usage=fixed_disk_usage,
    )
    location = registry.register(
        project_id,
        selected_directory=selected,
    )
    runtime = FakeRuntime()
    runtime.block_stage = "intake_validation"
    service = PipelineJobService(
        runtime,
        data_root=data_root,
        artifact_root_registry=registry,
    )
    service.configure_profile(
        project_id,
        question_policy="learning_assist",
        review_mode="precision",
        default_template_id="sample-4q-v1",
        default_template_sha256=hashlib.sha256(
            b"template"
        ).hexdigest(),
        detector_pack_id="road-detector-v1",
        ocr_pack_id="korean-ocr-v1",
        sam2_pack_id="sam2-v1",
        report_provider="codex",
        output_location_id=location["output_location_id"],
        candidate_rescan_fps=8,
    )
    started, replayed = service.start(
        project_id,
        snapshot_id=uuid4(),
        file_name="20250916_123653.mp4",
        rights_confirmed=True,
        idempotency_key="pipeline-test-81-second-capacity-0001",
        storage_plan=plan,
    )
    assert replayed is False
    assert runtime.block_started.wait(2)
    reservation = started["provenance"]["capacity_reservation"]
    assert reservation["reserved_bytes"] == plan[
        "estimated_artifact_bytes"
    ]
    assert reservation["safety_reserve_bytes"] == 4 * gib
    assert (
        reservation["available_free_bytes"]
        - reservation["other_reserved_bytes"]
        - reservation["reserved_bytes"]
        - reservation["safety_reserve_bytes"]
    ) == available - plan["required_free_bytes"]

    candidate_stage = next(
        row
        for row in plan["stage_estimates"]
        if row["stage"] == "candidate_rescan_5_10fps"
    )
    with pytest.raises(PipelineStageFailure) as blocked:
        BridgePipelineRuntime._storage_capacity_revision(
            {"storage_plan": plan},
            stage="candidate_rescan_5_10fps",
            required_bytes=(
                int(candidate_stage["hard_ceiling_bytes"]) + 1
            ),
            basis={
                "basis_type": "actual_merged_candidate_windows",
                "candidate_count": 512,
            },
        )
    assert blocked.value.code == "storage_capacity_replan_required"
    assert blocked.value.observation["state"] == (
        "exceeds_absolute_engine_cap"
    )

    runtime.block_release.set()
    service.shutdown()


def test_four_legitimate_capacity_revisions_survive_validation():
    from app.folder_pipeline_batches import build_pipeline_storage_plan

    plan = build_pipeline_storage_plan(
        duration_seconds=60,
        candidate_rescan_fps=8,
    )
    revisions = []
    for stage_name in (
        "dense_index_2fps",
        "candidate_rescan_5_10fps",
        "vision_proposal",
        "derived_outputs",
    ):
        stage = next(
            row
            for row in plan["stage_estimates"]
            if row["stage"] == stage_name
        )
        with pytest.raises(PipelineStageFailure) as requested:
            BridgePipelineRuntime._storage_capacity_revision(
                {"storage_plan": plan},
                stage=stage_name,
                required_bytes=int(stage["estimated_bytes"]) + 1,
                basis={
                    "basis_type": "test_exact_writer_bound",
                    "stage": stage_name,
                },
            )
        revisions.append(dict(requested.value.observation))

    validated = _validated_storage_revisions(
        revisions,
        plan=plan,
    )
    assert [item["stage"] for item in validated] == [
        "dense_index_2fps",
        "candidate_rescan_5_10fps",
        "vision_proposal",
        "derived_outputs",
    ]


def test_cumulative_replans_add_only_accepted_positive_deltas(
    tmp_path,
    monkeypatch,
):
    from app.folder_pipeline_batches import build_pipeline_storage_plan

    plan = build_pipeline_storage_plan(
        duration_seconds=2.8095,
        candidate_rescan_fps=8,
    )
    initial_required = int(plan["required_free_bytes"])
    replan_stages = (
        "dense_index_2fps",
        "candidate_rescan_5_10fps",
        "vision_proposal",
        "derived_outputs",
    )
    monkeypatch.setattr(
        "app.pipeline_jobs.shutil.disk_usage",
        lambda _path: SimpleNamespace(
            free=initial_required + len(replan_stages)
        ),
    )
    service = PipelineJobService(
        FakeRuntime(),
        data_root=(tmp_path / "bridge-data").resolve(),
    )
    now = datetime.now(timezone.utc)
    job = PipelineJobRecord(
        job_id=uuid4(),
        project_id=uuid4(),
        snapshot_id=uuid4(),
        file_name="drive.mp4",
        idempotency_key="pipeline-test-cumulative-replans-0001",
        input_fingerprint="a" * 64,
        profile_snapshot={"candidate_rescan_fps": 8},
        capability_snapshot={},
        status="queued",
        current_stage="dense_index_2fps",
        outcome_code=None,
        error_code=None,
        retry_count=0,
        resume_count=0,
        review_completion_id=None,
        stages=[
            {
                "name": stage_name,
                "status": "pending",
                "outcome": None,
                "attempts": 0,
                "started_at": None,
                "completed_at": None,
                "error_code": None,
                "checkpoint": {},
                "metrics": {},
            }
            for stage_name in PIPELINE_STAGES
        ],
        provenance={"storage_plan": plan},
        created_at=now,
        updated_at=now,
        recovered_after_restart=False,
        cancel_event=threading.Event(),
    )
    revisions = []
    invariant_totals = []
    for stage_name in replan_stages:
        stage_plan = next(
            row
            for row in plan["stage_estimates"]
            if row["stage"] == stage_name
        )
        with pytest.raises(PipelineStageFailure) as proposed:
            BridgePipelineRuntime._storage_capacity_revision(
                {
                    "storage_plan": plan,
                    "storage_plan_revisions": revisions,
                },
                stage=stage_name,
                required_bytes=int(stage_plan["estimated_bytes"]) + 1,
                basis={
                    "basis_type": "test_attainable_writer_bound",
                    "stage": stage_name,
                },
            )
        revisions.append(dict(proposed.value.observation))
        job.provenance["storage_plan_revisions"] = revisions
        observation = service._job_storage_budget_snapshot(
            job,
            stage_name,
        )
        assert observation is not None
        effective_total = sum(
            next(
                (
                    int(revision["required_bytes"])
                    for revision in revisions
                    if revision["stage"] == row["stage"]
                ),
                int(row["estimated_bytes"]),
            )
            for row in plan["stage_estimates"]
        )
        invariant_total = (
            effective_total
            + int(observation["safety_reserve_bytes"])
        )
        invariant_totals.append(invariant_total)
        assert invariant_total == initial_required + len(revisions)

    assert invariant_totals == [
        initial_required + 1,
        initial_required + 2,
        initial_required + 3,
        initial_required + 4,
    ]
    assert observation["safety_reserve_bytes"] == 4 * 1024**3
    service.shutdown()


def test_exact_replan_capacity_matches_bounded_writer_maxima():
    mib = 1024**2
    gib = 1024**3
    limits = CandidateEvidenceOverlayLimits()
    dense_plan = build_dense_frame_capacity_plan(
        source_sha256=hashlib.sha256(b"dense-writer").hexdigest(),
        duration_seconds=82 * 60,
        limits=DenseFrameIndexLimits(sampling_fps=2),
    )

    assert dense_plan.planned_output_bytes_upper_bound == 16 * gib
    assert (
        dense_plan.planned_output_bytes_upper_bound
        == dense_plan.hard_ceiling_bytes
    )
    assert limits.output_capacity_upper_bound(1) == 88 * mib
    assert limits.output_capacity_upper_bound(512) == 8 * gib
    assert _provider_visual_capacity_upper_bound(1) == 30 * mib
    assert _provider_visual_capacity_upper_bound(512) == (
        512 * 6 * 5 * mib
    )
    assert _derived_outputs_capacity_upper_bound(1) == (
        512 * mib + 256 * mib + 2 * gib + 16 * mib
    )
    assert _derived_outputs_capacity_upper_bound(512) == (
        512 * mib + 8 * gib + 512 * 16 * mib
    )

    from app.folder_pipeline_batches import (
        build_pipeline_storage_plan,
    )

    plan = build_pipeline_storage_plan(
        duration_seconds=60,
        candidate_rescan_fps=8,
    )
    stages = {
        row["stage"]: row for row in plan["stage_estimates"]
    }
    assert stages["dense_index_2fps"]["hard_ceiling_bytes"] == (
        dense_plan.hard_ceiling_bytes
    )
    assert stages["candidate_rescan_5_10fps"][
        "hard_ceiling_bytes"
    ] == 64 * mib + 8 * gib + 8 * gib
    assert stages["vision_proposal"]["hard_ceiling_bytes"] == (
        64 * mib + _provider_visual_capacity_upper_bound(512)
    )
    assert stages["derived_outputs"]["hard_ceiling_bytes"] == (
        _derived_outputs_capacity_upper_bound(512)
    )


def test_app_managed_staging_selects_largest_eligible_fixed_volume(
    tmp_path,
):
    from app.main import _select_app_managed_dense_staging_root

    gib = 1024**3
    roots = {
        name: (tmp_path / name).resolve()
        for name in ("C", "E", "L", "N", "R")
    }
    for root in roots.values():
        root.mkdir()
    drive_types = {
        roots["C"]: 3,
        roots["E"]: 3,
        roots["L"]: 3,
        roots["N"]: 4,
        roots["R"]: 2,
    }
    free_bytes = {
        roots["C"]: 20 * gib,
        roots["E"]: 33 * gib,
        roots["L"]: 10 * gib,
        roots["N"]: 100 * gib,
        roots["R"]: 100 * gib,
    }

    selected = _select_app_managed_dense_staging_root(
        list(roots.values()),
        drive_type=lambda root: drive_types[root],
        disk_usage=lambda root: SimpleNamespace(
            free=free_bytes[root]
        ),
    )

    assert selected == roots["E"] / "TrafficSuperVisionDenseStage"
    assert not selected.exists()


def test_app_managed_staging_skips_unowned_target_and_ties_stably(
    tmp_path,
):
    from app.main import _select_app_managed_dense_staging_root

    gib = 1024**3
    roots = [
        (tmp_path / name).resolve()
        for name in ("E", "F", "Y", "Z")
    ]
    for root in roots:
        root.mkdir()
    empty_unowned = (
        roots[2] / "TrafficSuperVisionDenseStage"
    )
    empty_unowned.mkdir()
    unsafe = roots[3] / "TrafficSuperVisionDenseStage"
    unsafe.mkdir()
    sentinel = unsafe / "foreign.txt"
    sentinel.write_text(
        "must remain untouched",
        encoding="utf-8",
    )
    free = {
        roots[0]: 33 * gib,
        roots[1]: 33 * gib,
        roots[2]: 45 * gib,
        roots[3]: 40 * gib,
    }

    selected = _select_app_managed_dense_staging_root(
        roots,
        drive_type=lambda _root: 3,
        disk_usage=lambda root: SimpleNamespace(free=free[root]),
    )

    assert selected == roots[0] / "TrafficSuperVisionDenseStage"
    assert sentinel.read_text(
        encoding="utf-8"
    ) == "must remain untouched"
    assert list(unsafe.iterdir()) == [sentinel]
    assert list(empty_unowned.iterdir()) == []


def test_explicit_staging_configuration_precedes_app_managed_selection(
    monkeypatch,
):
    from app.main import _resolve_dense_staging_configuration

    def forbidden_selector():
        raise AssertionError("app-managed selection must not run")

    monkeypatch.setattr(
        "app.main._app_managed_dense_staging_root",
        forbidden_selector,
    )
    root, source = _resolve_dense_staging_configuration(
        r"E:\UserSelectedDenseStage",
        production_runtime=True,
    )

    assert root == Path(r"E:\UserSelectedDenseStage")
    assert source == "configured"


def test_dense_staging_readiness_never_exposes_selected_path(
    tmp_path,
    monkeypatch,
):
    runtime = object.__new__(BridgePipelineRuntime)
    runtime._dense_staging_root = tmp_path.resolve()
    runtime._dense_staging_source = "app_managed_fixed_volume"
    runtime._dense_staging_recovered_children = 0
    runtime._dense_staging_error_code = None
    monkeypatch.setattr(
        "app.pipeline_jobs.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=33 * 1024**3),
    )

    status = runtime.dense_staging_status()

    assert status["state"] == "ready"
    assert status["source"] == "app_managed_fixed_volume"
    assert status["path_exposed"] is False
    assert "path" not in status
    assert str(tmp_path) not in json.dumps(status)


def test_intake_preflight_counts_same_volume_atomic_publish_peak(
    tmp_path,
    monkeypatch,
):
    staging = initialize_dense_staging_root(
        tmp_path / "dense-stage"
    )
    runtime = object.__new__(BridgePipelineRuntime)
    runtime._data_root = tmp_path.resolve()
    runtime._artifact_roots = None
    runtime._dense_staging_root = staging
    runtime._dense_staging_source = "configured"
    runtime._dense_staging_recovered_children = 0
    runtime._dense_staging_error_code = None
    duration_seconds = 60.0
    capacity = build_dense_frame_capacity_plan(
        source_sha256="0" * 64,
        duration_seconds=duration_seconds,
        limits=DenseFrameIndexLimits(sampling_fps=2),
    )
    one_copy_free = (
        capacity.planned_output_bytes_upper_bound
        + DenseFrameIndexLimits().min_free_disk_bytes
    )
    monkeypatch.setattr(
        "app.pipeline_jobs."
        "dense_frame_index_requires_short_staging",
        lambda _path: True,
    )
    monkeypatch.setattr(
        "app.pipeline_jobs.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=one_copy_free),
    )

    with pytest.raises(PipelineStageFailure) as blocked:
        runtime.preflight_dense_after_intake(
            {
                "job_id": str(uuid4()),
                "project_id": str(uuid4()),
            },
            duration_seconds=duration_seconds,
        )

    assert blocked.value.code == "dense_staging_storage_blocked"
    observation = blocked.value.observation
    assert observation["same_volume_atomic_publish"] is True
    assert observation["required_free_bytes"] == (
        2 * capacity.planned_output_bytes_upper_bound
        + DenseFrameIndexLimits().min_free_disk_bytes
    )
    assert observation["available_free_bytes"] == one_copy_free
    assert observation["path_exposed"] is False


def test_intake_preflight_uses_hash_bound_job_scoped_writer_ceiling(
    tmp_path,
    monkeypatch,
):
    from app.folder_pipeline_batches import build_pipeline_storage_plan

    duration_seconds = 330.03
    plan = build_pipeline_storage_plan(
        duration_seconds=duration_seconds,
        candidate_rescan_fps=8,
    )
    dense_stage = next(
        row
        for row in plan["stage_estimates"]
        if row["stage"] == "dense_index_2fps"
    )
    writer_capacity = int(dense_stage["estimated_bytes"])
    required_free = (
        2 * writer_capacity
        + DenseFrameIndexLimits().min_free_disk_bytes
    )
    staging = initialize_dense_staging_root(
        tmp_path / "dense-stage"
    )
    runtime = object.__new__(BridgePipelineRuntime)
    runtime._data_root = tmp_path.resolve()
    runtime._artifact_roots = None
    runtime._dense_staging_root = staging
    runtime._dense_staging_source = "configured"
    runtime._dense_staging_recovered_children = 0
    runtime._dense_staging_error_code = None
    monkeypatch.setattr(
        "app.pipeline_jobs."
        "dense_frame_index_requires_short_staging",
        lambda _path: True,
    )
    monkeypatch.setattr(
        "app.pipeline_jobs.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=required_free),
    )
    job_id = uuid4()

    observation = runtime.preflight_dense_after_intake(
        {
            "job_id": str(job_id),
            "project_id": str(uuid4()),
            "storage_plan": plan,
        },
        duration_seconds=duration_seconds,
    )

    assert observation["state"] == "ready"
    assert observation["writer_capacity_bytes"] == writer_capacity
    assert observation["writer_capacity_bytes"] < 16 * 1024**3
    assert observation["required_free_bytes"] == required_free
    binding = observation["writer_capacity_binding"]
    assert binding["job_id"] == str(job_id)
    assert binding["planned_frame_count"] == 661
    assert binding["average_frame_bytes_ceiling"] == 256 * 1024
    assert binding["fixed_allowance_bytes"] == 64 * 1024**2
    assert binding["writer_capacity_bytes"] == writer_capacity
    binding_body = {
        key: value
        for key, value in binding.items()
        if key != "binding_sha256"
    }
    assert binding["binding_sha256"] == hashlib.sha256(
        json.dumps(
            binding_body,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    assert binding["path_exposed"] is False


def test_direct_video_builds_capacity_plan_after_intake_before_sha(
    tmp_path,
    monkeypatch,
):
    gib = 1024**3

    class IntakePlanningRuntime(FakeRuntime):
        supports_intake_storage_planning = True

        def execute(self, stage, context, cancel):
            if stage == "intake_validation":
                self.calls.append(stage)
                self.contexts.append((stage, context))
                return PipelineStageResult(
                    "success",
                    checkpoint={
                        "duration_seconds": 60.0,
                        "intake_validation_id": "intake-v1",
                    },
                    metrics={"completed": 1},
                )
            return super().execute(stage, context, cancel)

    def fixed_disk_usage(_path):
        return SimpleNamespace(
            total=200 * gib,
            used=20 * gib,
            free=180 * gib,
        )

    monkeypatch.setattr(
        "app.pipeline_jobs.shutil.disk_usage",
        fixed_disk_usage,
    )
    data_root = (tmp_path / "bridge-data").resolve()
    selected = (tmp_path / "large-output").resolve()
    selected.mkdir()
    project_id = uuid4()
    registry = ProjectArtifactRootRegistry(
        data_root=data_root,
        disk_usage=fixed_disk_usage,
    )
    location = registry.register(
        project_id,
        selected_directory=selected,
    )
    runtime = IntakePlanningRuntime()
    service = PipelineJobService(
        runtime,
        data_root=data_root,
        artifact_root_registry=registry,
    )
    service.configure_profile(
        project_id,
        question_policy="learning_assist",
        review_mode="precision",
        default_template_id="sample-4q-v1",
        default_template_sha256=hashlib.sha256(
            b"template"
        ).hexdigest(),
        detector_pack_id="road-detector-v1",
        ocr_pack_id="korean-ocr-v1",
        sam2_pack_id="sam2-v1",
        report_provider="codex",
        output_location_id=location["output_location_id"],
        candidate_rescan_fps=8,
    )
    started, replayed = service.start(
        project_id,
        snapshot_id=uuid4(),
        file_name="drive.mp4",
        rights_confirmed=True,
        idempotency_key="pipeline-test-direct-capacity-0001",
    )
    assert replayed is False
    waiting = _wait(
        service,
        project_id,
        started["job_id"],
        {"awaiting_review"},
    )
    assert waiting["provenance"]["storage_plan_source"] == (
        "server_intake_duration_preflight"
    )
    assert waiting["provenance"]["storage_plan"][
        "schema_version"
    ] == "trafficvisson.pipeline-capacity-plan.v2"
    sha_context = next(
        context
        for stage, context in runtime.contexts
        if stage == "sha256"
    )
    assert sha_context["storage_plan"]["dense_frame_count"] == 120
    assert waiting["provenance"]["capacity_reservation"][
        "reserved_bytes"
    ] == waiting["provenance"]["storage_plan"][
        "estimated_artifact_bytes"
    ]
    service.shutdown()


def test_direct_video_staging_blocks_at_intake_before_sha_or_artifacts(
    tmp_path,
    monkeypatch,
):
    gib = 1024**3

    class BlockingStagingRuntime(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.staging_preflights = []

        def preflight_dense_after_intake(
            self,
            context,
            *,
            duration_seconds,
        ):
            self.staging_preflights.append(
                (context, duration_seconds)
            )
            raise PipelineStageFailure(
                "dense_staging_storage_blocked",
                observation={
                    "schema_version": (
                        "trafficvisson.dense-staging-observation.v1"
                    ),
                    "state": "insufficient_or_unavailable",
                    "source": "configured",
                    "required": True,
                    "required_free_bytes": 8 * gib,
                    "available_free_bytes": 1,
                    "writer_capacity_bytes": 8 * gib,
                    "error_code": "dense_staging_storage_blocked",
                    "path_exposed": False,
                },
            )

    def fixed_disk_usage(_path):
        return SimpleNamespace(
            total=200 * gib,
            used=20 * gib,
            free=180 * gib,
        )

    monkeypatch.setattr(
        "app.pipeline_jobs.shutil.disk_usage",
        fixed_disk_usage,
    )
    data_root = (tmp_path / "bridge-data").resolve()
    selected = (tmp_path / "long-output").resolve()
    selected.mkdir()
    project_id = uuid4()
    registry = ProjectArtifactRootRegistry(
        data_root=data_root,
        disk_usage=fixed_disk_usage,
    )
    location = registry.register(
        project_id,
        selected_directory=selected,
    )
    runtime = BlockingStagingRuntime()
    service = PipelineJobService(
        runtime,
        data_root=data_root,
        artifact_root_registry=registry,
    )
    service.configure_profile(
        project_id,
        question_policy="learning_assist",
        review_mode="precision",
        default_template_id="sample-4q-v1",
        default_template_sha256=hashlib.sha256(
            b"template"
        ).hexdigest(),
        detector_pack_id="road-detector-v1",
        ocr_pack_id="korean-ocr-v1",
        sam2_pack_id="sam2-v1",
        report_provider="codex",
        output_location_id=location["output_location_id"],
        candidate_rescan_fps=8,
    )
    started, replayed = service.start(
        project_id,
        snapshot_id=uuid4(),
        file_name="drive.mp4",
        rights_confirmed=True,
        idempotency_key="pipeline-test-staging-before-sha-0001",
    )
    assert replayed is False
    blocked = _wait(
        service,
        project_id,
        started["job_id"],
        {"blocked"},
    )

    assert runtime.calls == ["intake_validation"]
    assert len(runtime.staging_preflights) == 1
    preflight_context, duration = runtime.staging_preflights[0]
    assert duration == 60.0
    assert preflight_context["storage_plan"]["schema_version"] == (
        "trafficvisson.pipeline-capacity-plan.v2"
    )
    assert blocked["current_stage"] == "intake_validation"
    assert blocked["outcome_code"] == "partial"
    assert blocked["error_code"] == "dense_staging_storage_blocked"
    assert blocked["stages"][0]["outcome"] == "partial"
    assert blocked["provenance"]["capacity_reservation"][
        "reserved_bytes"
    ] > 0
    assert not (
        selected / "pipeline-jobs" / str(started["job_id"])
    ).exists()
    service.shutdown()


def test_bound_direct_video_without_duration_fails_before_sha(
    tmp_path,
):
    gib = 1024**3

    class MissingDurationRuntime(FakeRuntime):
        def execute(self, stage, context, cancel):
            if stage == "intake_validation":
                self.calls.append(stage)
                self.contexts.append((stage, context))
                return PipelineStageResult(
                    "success",
                    checkpoint={
                        "intake_validation_id": "intake-v1",
                    },
                    metrics={"completed": 1},
                )
            return super().execute(stage, context, cancel)

    def fixed_disk_usage(_path):
        return SimpleNamespace(
            total=30 * gib,
            used=10 * gib,
            free=20 * gib,
        )

    data_root = (tmp_path / "bridge-data").resolve()
    selected = (tmp_path / "large-output").resolve()
    selected.mkdir()
    project_id = uuid4()
    registry = ProjectArtifactRootRegistry(
        data_root=data_root,
        disk_usage=fixed_disk_usage,
    )
    location = registry.register(
        project_id,
        selected_directory=selected,
    )
    runtime = MissingDurationRuntime()
    service = PipelineJobService(
        runtime,
        data_root=data_root,
        artifact_root_registry=registry,
    )
    service.configure_profile(
        project_id,
        question_policy="learning_assist",
        review_mode="precision",
        default_template_id="sample-4q-v1",
        default_template_sha256=hashlib.sha256(
            b"template"
        ).hexdigest(),
        detector_pack_id="road-detector-v1",
        ocr_pack_id="korean-ocr-v1",
        sam2_pack_id="sam2-v1",
        report_provider="codex",
        output_location_id=location["output_location_id"],
        candidate_rescan_fps=8,
    )
    started, replayed = service.start(
        project_id,
        snapshot_id=uuid4(),
        file_name="drive.mp4",
        rights_confirmed=True,
        idempotency_key="pipeline-test-missing-duration-0001",
    )
    assert replayed is False
    failed = _wait(
        service,
        project_id,
        started["job_id"],
        {"failed"},
    )
    assert failed["current_stage"] == "intake_validation"
    assert failed["error_code"] == "storage_capacity_plan_invalid"
    assert "sha256" not in runtime.calls
    assert "storage_plan" not in failed["provenance"]
    service.shutdown()


def test_job_scoped_dense_capacity_needs_no_global_writer_replan(
    tmp_path,
    monkeypatch,
):
    from app.folder_pipeline_batches import build_pipeline_storage_plan

    gib = 1024**3
    duration_seconds = 82 * 60
    plan = build_pipeline_storage_plan(
        duration_seconds=duration_seconds,
        candidate_rescan_fps=8,
    )
    free_bytes = int(plan["required_free_bytes"])
    dense_stage = next(
        row
        for row in plan["stage_estimates"]
        if row["stage"] == "dense_index_2fps"
    )
    dense_writer_capacity = int(dense_stage["estimated_bytes"])

    class DenseCapacityRuntime(FakeRuntime):
        supports_intake_storage_planning = True

        def __init__(self):
            super().__init__()
            self.dense_attempts = 0
            self.write_attempts = 0

        def execute(self, stage, context, cancel):
            if stage == "intake_validation":
                self.calls.append(stage)
                self.contexts.append((stage, context))
                return PipelineStageResult(
                    "success",
                    checkpoint={
                        "duration_seconds": duration_seconds,
                    },
                )
            if stage == "dense_index_2fps":
                self.calls.append(stage)
                self.contexts.append((stage, context))
                self.dense_attempts += 1
                BridgePipelineRuntime._storage_capacity_revision(
                    context,
                    stage=stage,
                    required_bytes=dense_writer_capacity,
                    basis={
                        "basis_type": (
                            "job_scoped_dense_writer_bound"
                        ),
                        "writer_capacity_bytes": (
                            dense_writer_capacity
                        ),
                    },
                )
                self.write_attempts += 1
                return PipelineStageResult(
                    "success",
                    checkpoint={"dense_revision_applied": True},
                    metrics={"frame_count": 1},
                )
            return super().execute(stage, context, cancel)

    def fixed_disk_usage(_path):
        return SimpleNamespace(
            total=free_bytes + 10 * gib,
            used=10 * gib,
            free=free_bytes,
        )

    monkeypatch.setattr(
        "app.pipeline_jobs.shutil.disk_usage",
        fixed_disk_usage,
    )
    data_root = (tmp_path / "bridge-data").resolve()
    selected = (tmp_path / "large-output").resolve()
    selected.mkdir()
    project_id = uuid4()
    registry = ProjectArtifactRootRegistry(
        data_root=data_root,
        disk_usage=fixed_disk_usage,
    )
    location = registry.register(
        project_id,
        selected_directory=selected,
    )
    runtime = DenseCapacityRuntime()
    service = PipelineJobService(
        runtime,
        data_root=data_root,
        artifact_root_registry=registry,
    )
    service.configure_profile(
        project_id,
        question_policy="learning_assist",
        review_mode="precision",
        default_template_id="sample-4q-v1",
        default_template_sha256=hashlib.sha256(
            b"template"
        ).hexdigest(),
        detector_pack_id="road-detector-v1",
        ocr_pack_id="korean-ocr-v1",
        sam2_pack_id="sam2-v1",
        report_provider="codex",
        output_location_id=location["output_location_id"],
        candidate_rescan_fps=8,
    )
    started, _ = service.start(
        project_id,
        snapshot_id=uuid4(),
        file_name="drive.mp4",
        rights_confirmed=True,
        idempotency_key="pipeline-test-dense-insufficient-0001",
    )
    waiting = _wait(
        service,
        project_id,
        started["job_id"],
        {"awaiting_review"},
    )

    assert waiting["current_stage"] == "question_queue"
    assert waiting["error_code"] is None
    assert runtime.dense_attempts == 1
    assert runtime.write_attempts == 1
    assert waiting["provenance"].get(
        "storage_plan_revisions", []
    ) == []
    assert waiting["provenance"]["capacity_reservation"][
        "reserved_bytes"
    ] == int(plan["estimated_artifact_bytes"])
    assert waiting["provenance"]["capacity_reservation"][
        "safety_reserve_bytes"
    ] == 4 * gib
    service.shutdown()


@pytest.mark.parametrize(
    ("file_name", "expected"),
    (
        ("20250916_073358.mp4", "2025-09-16"),
        ("20240229-drive.mov", "2024-02-29"),
        ("20250229-drive.mov", ""),
        ("202509160-drive.mp4", ""),
        ("drive-20250916.mp4", ""),
    ),
)
def test_report_inspection_date_uses_only_valid_source_prefix(
    file_name,
    expected,
):
    assert _report_inspection_date_from_source_filename(
        file_name,
        policy=REPORT_INSPECTION_DATE_POLICY,
    ) == expected
    assert _report_inspection_date_from_source_filename(
        file_name,
        policy="unsupported-policy",
    ) == ""


def test_profile_is_required_and_snapshot_has_all_north_star_fields(tmp_path):
    runtime = FakeRuntime()
    service = PipelineJobService(runtime, data_root=tmp_path.resolve())
    project_id = uuid4()
    with pytest.raises(PipelineProfileMissing):
        _start(service, project_id)
    profile = _configure(service, project_id)
    assert profile["revision"] == 1
    assert profile["review_mode"] == "precision"
    assert profile["question_policy"] == "learning_assist"
    assert profile["default_template_id"] == "sample-4q-v1"
    assert len(profile["default_template_sha256"]) == 64
    assert profile["detector_pack_id"] == "road-detector-v1"
    assert profile["ocr_pack_id"] == "korean-ocr-v1"
    assert profile["sam2_pack_id"] == "sam2-v1"
    assert profile["report_provider"] == "codex"
    assert profile["schema_version"] == (
        "trafficvisson.pipeline-profile.v4"
    )
    assert profile["label_confirmation_authority"] == "human_review"
    assert profile["report_transmission_consent"] is False
    assert profile["template_semantic_map_id"] == "unconfigured"
    assert profile["template_semantic_map_ready"] is False
    assert (
        profile["report_provider_ready_at_configuration"]
        is False
    )
    assert profile["report_route_name"] == "unconfigured"
    assert profile["report_inspector_name"] == "unconfigured"
    assert profile["report_evidence_mode"] == "internal_original"
    assert profile["report_inspection_date_policy"] == (
        "source_filename_yyyymmdd_or_block"
    )
    assert profile["output_location_id"] == "project-default"
    assert profile["bulk_frame_provider_policy"] == "local_models_only"
    service.shutdown()


def test_v4_label_authority_is_the_only_pipeline_auto_delegation_gate(
    tmp_path,
):
    project_id = uuid4()
    analysis_job_id = uuid4()
    safe_id = "a" * 20
    debris_id = "b" * 20
    calls = []

    def delegate(
        requested_project_id,
        requested_analysis_job_id,
        candidate_ids,
    ):
        calls.append(
            (
                requested_project_id,
                requested_analysis_job_id,
                list(candidate_ids),
            )
        )
        return {
            "delegation_revision": "d" * 64,
            "delegated_candidate_ids": [safe_id],
            "human_question_candidate_ids": [debris_id],
            "mandatory_human_candidate_ids": [debris_id],
            "structured_training_label_count": 1,
            "training_annotation_count": 1,
            "incident_count": 0,
            "report_incident_count": 0,
            "fixed_validation_membership_count": 0,
            "training_only": True,
            "automatic_training_started": False,
        }

    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        structured_label_delegator=delegate,
    )
    delegated = runtime._apply_structured_training_label_delegation(
        {
            "project_id": str(project_id),
            "profile": {
                "schema_version": "trafficvisson.pipeline-profile.v4",
                "label_confirmation_authority": (
                    "structured_llm_training_assist"
                ),
            },
        },
        analysis_job_id=str(analysis_job_id),
        candidate_ids=[safe_id, debris_id],
    )
    assert delegated["state"] == (
        "partial_human_review_required"
    )
    assert delegated["delegated_candidate_ids"] == [safe_id]
    assert delegated["mandatory_human_candidate_ids"] == [
        debris_id
    ]
    assert calls == [
        (project_id, analysis_job_id, [safe_id, debris_id])
    ]

    legacy = runtime._apply_structured_training_label_delegation(
        {
            "project_id": str(project_id),
            "profile": {
                "schema_version": "trafficvisson.pipeline-profile.v3",
            },
        },
        analysis_job_id=str(analysis_job_id),
        candidate_ids=[safe_id, debris_id],
    )
    assert legacy["authority"] == "human_review"
    assert legacy["delegated_candidate_ids"] == []
    assert calls == [
        (project_id, analysis_job_id, [safe_id, debris_id])
    ]


def test_restart_loads_verified_legacy_v1_profile_with_safe_report_defaults(
    tmp_path,
):
    root = tmp_path.resolve()
    service = PipelineJobService(FakeRuntime(), data_root=root)
    project_id = uuid4()
    current = _configure(service, project_id)
    service.shutdown()

    legacy = dict(current)
    legacy["schema_version"] = "trafficvisson.pipeline-profile.v1"
    for field in (
        "report_transmission_consent",
        "template_semantic_map_id",
        "template_semantic_map_sha256",
        "template_semantic_map_ready",
        "report_provider_ready_at_configuration",
        "report_route_name",
        "report_inspector_name",
        "report_evidence_mode",
        "report_inspection_date_policy",
        "label_confirmation_authority",
    ):
        legacy.pop(field)
    payload = json.dumps(
        legacy,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    profile_path = (
        root
        / "projects"
        / str(project_id)
        / "pipeline"
        / "profile.json"
    )
    profile_path.write_bytes(payload)
    profile_path.with_suffix(".json.sha256").write_text(
        hashlib.sha256(payload).hexdigest() + "\n",
        encoding="ascii",
    )

    restarted = PipelineJobService(FakeRuntime(), data_root=root)
    restored = restarted.get_profile(project_id)
    assert restored["schema_version"] == (
        "trafficvisson.pipeline-profile.v4"
    )
    assert restored["report_transmission_consent"] is False
    assert restored["label_confirmation_authority"] == "human_review"
    restarted.shutdown()


def test_restart_loads_verified_v2_profile_without_factual_context(
    tmp_path,
):
    root = tmp_path.resolve()
    service = PipelineJobService(FakeRuntime(), data_root=root)
    project_id = uuid4()
    current = _configure(service, project_id)
    service.shutdown()

    v2 = dict(current)
    v2["schema_version"] = "trafficvisson.pipeline-profile.v2"
    for field in (
        "report_route_name",
        "report_inspector_name",
        "report_evidence_mode",
        "report_inspection_date_policy",
        "label_confirmation_authority",
    ):
        v2.pop(field)
    payload = json.dumps(
        v2,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    profile_path = (
        root
        / "projects"
        / str(project_id)
        / "pipeline"
        / "profile.json"
    )
    profile_path.write_bytes(payload)
    profile_path.with_suffix(".json.sha256").write_text(
        hashlib.sha256(payload).hexdigest() + "\n",
        encoding="ascii",
    )

    restarted = PipelineJobService(FakeRuntime(), data_root=root)
    restored = restarted.get_profile(project_id)
    assert restored["schema_version"] == (
        "trafficvisson.pipeline-profile.v4"
    )
    assert restored["report_route_name"] == "unconfigured"
    assert restored["label_confirmation_authority"] == "human_review"
    restarted.shutdown()


def test_restart_migrates_v3_profile_to_human_review_v4(tmp_path):
    root = tmp_path.resolve()
    service = PipelineJobService(FakeRuntime(), data_root=root)
    project_id = uuid4()
    current = service.configure_profile(
        project_id,
        question_policy="learning_assist",
        review_mode="precision",
        label_confirmation_authority=(
            "structured_llm_training_assist"
        ),
        default_template_id="sample-4q-v1",
        default_template_sha256="8" * 64,
        detector_pack_id="road-detector-v1",
        ocr_pack_id="korean-ocr-v1",
        sam2_pack_id="sam2-v1",
        report_provider="codex",
        report_transmission_consent=True,
        template_semantic_map_id="sample-q4-semantic-v1",
        output_location_id="project-default",
        candidate_rescan_fps=8,
        report_route_name="서울북부고속도로",
    )
    service.shutdown()

    legacy = dict(current)
    legacy["schema_version"] = "trafficvisson.pipeline-profile.v3"
    legacy.pop("label_confirmation_authority")
    payload = json.dumps(
        legacy,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    profile_path = (
        root
        / "projects"
        / str(project_id)
        / "pipeline"
        / "profile.json"
    )
    profile_path.write_bytes(payload)
    profile_path.with_suffix(".json.sha256").write_text(
        hashlib.sha256(payload).hexdigest() + "\n",
        encoding="ascii",
    )

    restarted = PipelineJobService(FakeRuntime(), data_root=root)
    restored = restarted.get_profile(project_id)
    assert restored["schema_version"] == (
        "trafficvisson.pipeline-profile.v4"
    )
    assert restored["label_confirmation_authority"] == "human_review"
    assert restored["report_route_name"] == "서울북부고속도로"
    restarted.shutdown()


def test_v4_factual_report_context_and_label_authority_are_snapshotted_and_hashed(
    tmp_path,
):
    service = PipelineJobService(
        FakeRuntime(), data_root=tmp_path.resolve()
    )
    project_id = uuid4()
    profile = service.configure_profile(
        project_id,
        question_policy="learning_assist",
        review_mode="precision",
        label_confirmation_authority=(
            "structured_llm_training_assist"
        ),
        default_template_id="sample-4q-v1",
        default_template_sha256="8" * 64,
        detector_pack_id="road-detector-v1",
        ocr_pack_id="korean-ocr-v1",
        sam2_pack_id="sam2-v1",
        report_provider="codex",
        report_transmission_consent=True,
        template_semantic_map_id="sample-q4-semantic-v1",
        output_location_id="project-default",
        candidate_rescan_fps=8,
        report_route_name="  서울북부고속도로 (구리-포천)  ",
        report_inspector_name="  홍길동  ",
        report_evidence_mode="shared_deidentified",
    )
    assert profile["report_route_name"] == (
        "서울북부고속도로 (구리-포천)"
    )
    assert profile["label_confirmation_authority"] == (
        "structured_llm_training_assist"
    )
    assert profile["report_inspector_name"] == "홍길동"
    assert profile["report_evidence_mode"] == "shared_deidentified"

    started, _ = _start(service, project_id, suffix="report-context")
    assert started["profile_snapshot"] == profile
    expected_hash = hashlib.sha256(
        json.dumps(
            profile,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert started["provenance"]["profile_sha256"] == expected_hash

    with pytest.raises(ValueError, match="report_route_name"):
        service.configure_profile(
            project_id,
            question_policy="learning_assist",
            review_mode="precision",
            default_template_id="sample-4q-v1",
            default_template_sha256="8" * 64,
            detector_pack_id="road-detector-v1",
            ocr_pack_id="korean-ocr-v1",
            sam2_pack_id="sam2-v1",
            report_provider="codex",
            output_location_id="project-default",
            candidate_rescan_fps=8,
            report_route_name="잘못된\n노선",
        )
    service.shutdown()


def test_structured_llm_profile_requires_provider_and_explicit_consent(
    tmp_path,
):
    runtime = FakeRuntime()
    service = PipelineJobService(
        runtime, data_root=tmp_path.resolve()
    )
    project_id = uuid4()
    common = {
        "question_policy": "learning_assist",
        "review_mode": "precision",
        "label_confirmation_authority": (
            "structured_llm_training_assist"
        ),
        "default_template_id": "sample-4q-v1",
        "default_template_sha256": "8" * 64,
        "detector_pack_id": "road-detector-v1",
        "ocr_pack_id": "korean-ocr-v1",
        "sam2_pack_id": "sam2-v1",
        "output_location_id": "project-default",
        "candidate_rescan_fps": 8,
        "template_semantic_map_id": "sample-q4-semantic-v1",
    }

    for unsupported_provider in ("none", "local"):
        with pytest.raises(
            ValueError,
            match="requires codex or claude",
        ):
            service.configure_profile(
                project_id,
                report_provider=unsupported_provider,
                report_transmission_consent=True,
                **common,
            )
    with pytest.raises(
        ValueError,
        match="requires explicit report transmission consent",
    ):
        service.configure_profile(
            project_id,
            report_provider="codex",
            report_transmission_consent=False,
            **common,
        )
    with pytest.raises(PipelineProfileMissing):
        service.get_profile(project_id)

    saved = service.configure_profile(
        project_id,
        report_provider="codex",
        report_transmission_consent=True,
        **common,
    )
    assert saved["label_confirmation_authority"] == (
        "structured_llm_training_assist"
    )
    assert saved["report_provider"] == "codex"
    assert saved["report_transmission_consent"] is True
    assert saved["report_provider_ready_at_configuration"] is True
    service.shutdown()


def test_loaded_structured_llm_profile_rejects_stale_provider_or_consent(
    tmp_path,
):
    service = PipelineJobService(
        FakeRuntime(), data_root=tmp_path.resolve()
    )
    project_id = uuid4()
    saved = service.configure_profile(
        project_id,
        question_policy="learning_assist",
        review_mode="precision",
        label_confirmation_authority=(
            "structured_llm_training_assist"
        ),
        default_template_id="sample-4q-v1",
        default_template_sha256="8" * 64,
        detector_pack_id="road-detector-v1",
        ocr_pack_id="korean-ocr-v1",
        sam2_pack_id="sam2-v1",
        report_provider="codex",
        report_transmission_consent=True,
        template_semantic_map_id="sample-q4-semantic-v1",
        output_location_id="project-default",
        candidate_rescan_fps=8,
    )

    stale_provider = {**saved, "report_provider": "none"}
    with pytest.raises(
        ValueError,
        match="structured LLM authority is not explicitly ready",
    ):
        service._validate_loaded_profile(
            stale_provider,
            project_id,
        )

    stale_consent = {
        **saved,
        "report_transmission_consent": False,
    }
    with pytest.raises(
        ValueError,
        match="structured LLM authority is not explicitly ready",
    ):
        service._validate_loaded_profile(
            stale_consent,
            project_id,
        )
    service.shutdown()


def test_report_automation_profile_requires_explicit_ready_gates(
    tmp_path,
):
    runtime = FakeRuntime()
    service = PipelineJobService(
        runtime, data_root=tmp_path.resolve()
    )
    project_id = uuid4()
    ready = service.configure_profile(
        project_id,
        question_policy="learning_assist",
        review_mode="precision",
        default_template_id="sample-4q-v1",
        default_template_sha256="8" * 64,
        detector_pack_id="road-detector-v1",
        ocr_pack_id="korean-ocr-v1",
        sam2_pack_id="sam2-v1",
        report_provider="codex",
        report_transmission_consent=True,
        template_semantic_map_id="sample-q4-semantic-v1",
        output_location_id="project-default",
        candidate_rescan_fps=8,
    )
    assert ready["report_transmission_consent"] is True
    assert ready["template_semantic_map_ready"] is True
    assert ready["template_semantic_map_sha256"] == "9" * 64
    assert (
        ready["report_provider_ready_at_configuration"] is True
    )

    runtime.report_profile_ready = False
    with pytest.raises(
        PipelineJobConflict,
        match="provider and sample map",
    ):
        service.configure_profile(
            project_id,
            question_policy="learning_assist",
            review_mode="precision",
            default_template_id="sample-4q-v1",
            default_template_sha256="8" * 64,
            detector_pack_id="road-detector-v1",
            ocr_pack_id="korean-ocr-v1",
            sam2_pack_id="sam2-v1",
            report_provider="codex",
            report_transmission_consent=True,
            template_semantic_map_id=(
                "sample-q4-semantic-v1"
            ),
            output_location_id="project-default",
            candidate_rescan_fps=8,
        )
    assert service.get_profile(project_id) == ready
    service.shutdown()


def test_pipeline_reaches_review_then_requires_explicit_completion(tmp_path):
    runtime = FakeRuntime()
    service = PipelineJobService(runtime, data_root=tmp_path.resolve())
    project_id = uuid4()
    profile = _configure(service, project_id)
    snapshot_id = uuid4()
    started, replayed = service.start(
        project_id,
        snapshot_id=snapshot_id,
        file_name="drive.mp4",
        rights_confirmed=True,
        idempotency_key="pipeline-test-review-0001",
    )
    assert replayed is False
    waiting = _wait(
        service, project_id, started["job_id"], {"awaiting_review"}
    )
    assert waiting["current_stage"] == "question_queue"
    assert "derived_outputs" not in runtime.calls
    assert waiting["profile_snapshot"] == profile
    assert waiting["provenance"]["route_status"] == "blocked_gpx_required"
    gps = next(
        stage
        for stage in waiting["stages"]
        if stage["name"] == "metadata_gps_preflight"
    )
    assert gps["status"] == "blocked"
    assert gps["checkpoint"]["capture_start_coordinate_used_as_route"] is False
    resumed = service.complete_review(
        project_id,
        started["job_id"],
        review_completion_id="review-complete-v1",
    )
    assert resumed["status"] == "queued"
    completed = _wait(
        service, project_id, started["job_id"], {"completed"}
    )
    assert completed["outcome_code"] == "success"
    assert completed["progress_percent"] == 100
    assert completed["review_completion_id"] == "review-complete-v1"
    derived_stage = completed["stages"][-1]
    assert derived_stage["name"] == "derived_outputs"
    assert derived_stage["checkpoint"]["dataset_version_id"] == (
        "aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa"
    )
    assert derived_stage["checkpoint"]["dataset_hash"] == "1" * 64
    assert runtime.calls == list(PIPELINE_STAGES)
    replayed_completion = service.complete_review(
        project_id,
        started["job_id"],
        review_completion_id="review-complete-v1",
    )
    assert replayed_completion["job_id"] == completed["job_id"]
    assert replayed_completion["record_sha256"] == completed[
        "record_sha256"
    ]
    assert runtime.calls == list(PIPELINE_STAGES)
    with pytest.raises(PipelineJobConflict):
        service.complete_review(
            project_id,
            started["job_id"],
            review_completion_id="review-complete-v2",
        )
    derived_context = next(
        context
        for stage, context in runtime.contexts
        if stage == "derived_outputs"
    )
    assert (
        derived_context["input_fingerprint"]
        == completed["input_fingerprint"]
    )
    assert (
        derived_context["profile_sha256"]
        == completed["provenance"]["profile_sha256"]
    )
    assert derived_context["review_completion"] == (
        completed["provenance"]["review_completion"]
    )
    service.shutdown()


def test_promoted_model_survey_requires_and_hash_binds_final_missed_scene_qa(
    tmp_path,
):
    class PromotedSurveyRuntime(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.analysis_job_id = uuid4()
            self.source_sha256 = hashlib.sha256(
                b"promoted-survey-source"
            ).hexdigest()
            self.dense_index_sha256 = hashlib.sha256(
                b"promoted-survey-dense-index"
            ).hexdigest()
            self.discovery_revision = hashlib.sha256(
                b"promoted-survey-discovery"
            ).hexdigest()

        def execute(self, stage, context, cancel):
            if stage == "candidate_rescan_5_10fps":
                self.calls.append(stage)
                self.contexts.append((stage, context))
                discovery = {
                    "source_sha256": self.source_sha256,
                    "dense_index_sha256": self.dense_index_sha256,
                    "sampling_fps": 2,
                    "scanned_frame_count": 120,
                    "candidate_count": 2,
                    "candidates": [],
                    "provider_states": [
                        {
                            "provider": "promoted_project_model",
                            "state": "ready",
                            "model_version_id": "model-promoted-v1",
                            "dataset_version_id": "dataset-v1",
                            "dataset_hash": "c" * 64,
                            "fixed_validation_sha256": "d" * 64,
                            "primary_artifact_sha256": "a" * 64,
                            "artifact_manifest_sha256": "e" * 64,
                            "survey_analysis_job_id": str(
                                self.analysis_job_id
                            ),
                            "independent_survey_verified": True,
                            "source_analysis_lineage_sha256": (
                                "f" * 64
                            ),
                            "dataset_provenance_graph_sha256": (
                                "6" * 64
                            ),
                        }
                    ],
                    "discovery_revision": self.discovery_revision,
                    "review_only": True,
                }
                return PipelineStageResult(
                    "success",
                    checkpoint={
                        "analysis_job_id": str(self.analysis_job_id),
                        "source_sha256": self.source_sha256,
                        "dense_index_sha256": self.dense_index_sha256,
                        "dense_candidate_discovery": discovery,
                        "dense_candidate_discovery_revision": (
                            self.discovery_revision
                        ),
                    },
                    metrics={"candidate_count": 2},
                )
            if stage == "question_queue":
                self.calls.append(stage)
                self.contexts.append((stage, context))
                return PipelineStageResult(
                    "success",
                    checkpoint={
                        "analysis_job_id": str(self.analysis_job_id),
                        "source_sha256": self.source_sha256,
                        "question_queue_revision": "b" * 64,
                        "question_count": 2,
                        "question_candidate_ids": [
                            "a" * 20,
                            "b" * 20,
                        ],
                    },
                    metrics={"question_count": 2},
                )
            return super().execute(stage, context, cancel)

    runtime = PromotedSurveyRuntime()
    service = PipelineJobService(runtime, data_root=tmp_path.resolve())
    project_id = uuid4()
    _configure(service, project_id)
    started, _ = _start(service, project_id, suffix="promoted-final-qa")
    _wait(service, project_id, started["job_id"], {"awaiting_review"})

    with pytest.raises(
        PipelineJobConflict,
        match="final promoted-model survey QA choice",
    ):
        service.complete_review(
            project_id,
            started["job_id"],
            review_completion_id="review-promoted-survey-v1",
        )
    with pytest.raises(
        PipelineJobConflict,
        match="No user-discovered missed candidate",
    ):
        service.complete_review(
            project_id,
            started["job_id"],
            review_completion_id="review-promoted-survey-v1",
            final_qa_choice="miss_candidates_reviewed",
        )

    resumed = service.complete_review(
        project_id,
        started["job_id"],
        review_completion_id="review-promoted-survey-v1",
        final_qa_choice="no_miss_complete",
    )
    assert resumed["status"] == "queued"
    completed = _wait(
        service, project_id, started["job_id"], {"completed"}
    )
    survey_qa = completed["provenance"]["survey_qa_completion"]
    assert survey_qa["final_qa_choice"] == "no_miss_complete"
    assert survey_qa["analysis_job_id"] == str(
        runtime.analysis_job_id
    )
    assert (
        survey_qa["dense_index_sha256"]
        == runtime.dense_index_sha256
    )
    assert survey_qa["model_version_id"] == "model-promoted-v1"
    assert survey_qa["user_discovered_candidate_count"] == 0
    assert len(survey_qa["survey_qa_revision"]) == 64
    assert (
        completed["provenance"]["review_completion"][
            "survey_qa_completion"
        ]["survey_qa_revision"]
        == survey_qa["survey_qa_revision"]
    )
    with pytest.raises(
        PipelineJobConflict,
        match="different final survey QA choice",
    ):
        service.complete_review(
            project_id,
            started["job_id"],
            review_completion_id="review-promoted-survey-v1",
            final_qa_choice="miss_candidates_reviewed",
        )
    service.shutdown()


def test_promoted_model_zero_candidates_waits_for_explicit_no_miss_qa(
    tmp_path,
):
    class PromotedZeroRuntime(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.analysis_job_id = uuid4()
            self.source_sha256 = hashlib.sha256(
                b"promoted-zero-source"
            ).hexdigest()
            self.dense_index_sha256 = hashlib.sha256(
                b"promoted-zero-dense-index"
            ).hexdigest()
            self.discovery_revision = hashlib.sha256(
                b"promoted-zero-discovery"
            ).hexdigest()

        def execute(self, stage, context, cancel):
            if stage == "candidate_rescan_5_10fps":
                self.calls.append(stage)
                self.contexts.append((stage, context))
                discovery = {
                    "source_sha256": self.source_sha256,
                    "dense_index_sha256": self.dense_index_sha256,
                    "sampling_fps": 2,
                    "scanned_frame_count": 120,
                    "candidate_count": 0,
                    "candidates": [],
                    "provider_states": [
                        {
                            "provider": "promoted_project_model",
                            "state": "ready",
                            "model_version_id": "model-promoted-zero-v1",
                            "dataset_version_id": "dataset-zero-v1",
                            "dataset_hash": "1" * 64,
                            "fixed_validation_sha256": "2" * 64,
                            "primary_artifact_sha256": "3" * 64,
                            "artifact_manifest_sha256": "4" * 64,
                            "survey_analysis_job_id": str(
                                self.analysis_job_id
                            ),
                            "independent_survey_verified": True,
                            "source_analysis_lineage_sha256": (
                                "5" * 64
                            ),
                            "dataset_provenance_graph_sha256": (
                                "6" * 64
                            ),
                        }
                    ],
                    "discovery_revision": self.discovery_revision,
                    "review_only": True,
                }
                return PipelineStageResult(
                    "zero_detections",
                    checkpoint={
                        "analysis_job_id": str(self.analysis_job_id),
                        "source_sha256": self.source_sha256,
                        "candidate_count": 0,
                        "dense_index_sha256": self.dense_index_sha256,
                        "dense_candidate_discovery": discovery,
                        "dense_candidate_discovery_revision": (
                            self.discovery_revision
                        ),
                        "review_only": True,
                        "annotation_count": 0,
                        "incident_count": 0,
                    },
                    metrics={
                        "candidate_count": 0,
                        "annotation_count": 0,
                        "incident_count": 0,
                    },
                )
            return super().execute(stage, context, cancel)

    runtime = PromotedZeroRuntime()
    service = PipelineJobService(runtime, data_root=tmp_path.resolve())
    project_id = uuid4()
    _configure(service, project_id)
    started, _ = _start(service, project_id, suffix="promoted-zero-qa")
    waiting = _wait(
        service, project_id, started["job_id"], {"awaiting_review"}
    )
    assert waiting["current_stage"] == "question_queue"
    assert waiting["outcome_code"] == "zero_detections"
    assert "derived_outputs" not in runtime.calls
    question = waiting["stages"][
        PIPELINE_STAGES.index("question_queue")
    ]
    assert question["status"] == "success"
    assert question["checkpoint"]["question_count"] == 0
    assert question["checkpoint"]["question_candidate_ids"] == []
    assert question["checkpoint"]["schema_version"] == (
        "trafficvisson.zero-candidate-promoted-survey-queue.v1"
    )

    empty_ledger = hashlib.sha256(b"").hexdigest()
    with pytest.raises(
        PipelineJobConflict,
        match="exact no-miss QA decision",
    ):
        service.complete_review(
            project_id,
            started["job_id"],
            review_completion_id=f"zero-detections:{empty_ledger}",
        )
    resumed = service.complete_review(
        project_id,
        started["job_id"],
        review_completion_id=f"zero-detections:{empty_ledger}",
        final_qa_choice="no_miss_complete",
    )
    assert resumed["status"] == "queued"
    completed = _wait(
        service, project_id, started["job_id"], {"completed"}
    )
    assert completed["outcome_code"] == "zero_detections"
    assert completed["provenance"]["survey_qa_completion"][
        "final_qa_choice"
    ] == "no_miss_complete"
    assert runtime.calls[-1] == "derived_outputs"
    service.shutdown()


def test_promoted_zero_survey_first_user_miss_requires_answered_review(
    tmp_path,
):
    class PromotedZeroWithAnswerRuntime(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.analysis_job_id = uuid4()
            self.source_sha256 = hashlib.sha256(
                b"promoted-zero-first-user-miss-source"
            ).hexdigest()
            self.dense_index_sha256 = hashlib.sha256(
                b"promoted-zero-first-user-miss-dense-index"
            ).hexdigest()
            self.discovery_revision = hashlib.sha256(
                b"promoted-zero-first-user-miss-discovery"
            ).hexdigest()
            self.answer_recorded = False

        def execute(self, stage, context, cancel):
            if stage == "candidate_rescan_5_10fps":
                self.calls.append(stage)
                self.contexts.append((stage, context))
                discovery = {
                    "source_sha256": self.source_sha256,
                    "dense_index_sha256": self.dense_index_sha256,
                    "sampling_fps": 2,
                    "scanned_frame_count": 120,
                    "candidate_count": 0,
                    "candidates": [],
                    "provider_states": [
                        {
                            "provider": "promoted_project_model",
                            "state": "ready",
                            "model_version_id": "model-promoted-miss-v1",
                            "dataset_version_id": "dataset-miss-v1",
                            "dataset_hash": "1" * 64,
                            "fixed_validation_sha256": "2" * 64,
                            "primary_artifact_sha256": "3" * 64,
                            "artifact_manifest_sha256": "4" * 64,
                            "survey_analysis_job_id": str(
                                self.analysis_job_id
                            ),
                            "independent_survey_verified": True,
                            "source_analysis_lineage_sha256": (
                                "5" * 64
                            ),
                            "dataset_provenance_graph_sha256": (
                                "6" * 64
                            ),
                        }
                    ],
                    "discovery_revision": self.discovery_revision,
                    "review_only": True,
                }
                return PipelineStageResult(
                    "zero_detections",
                    checkpoint={
                        "analysis_job_id": str(self.analysis_job_id),
                        "source_sha256": self.source_sha256,
                        "candidate_count": 0,
                        "dense_index_sha256": self.dense_index_sha256,
                        "dense_candidate_discovery": discovery,
                        "dense_candidate_discovery_revision": (
                            self.discovery_revision
                        ),
                        "review_only": True,
                        "annotation_count": 0,
                        "incident_count": 0,
                    },
                    metrics={
                        "candidate_count": 0,
                        "annotation_count": 0,
                        "incident_count": 0,
                    },
                )
            return super().execute(stage, context, cancel)

        def validate_review_completion(
            self, _context, review_completion_id
        ):
            return {
                "review_completion_id": review_completion_id,
                "all_required_questions_answered": (
                    self.answer_recorded
                ),
                "answered_question_count": (
                    1 if self.answer_recorded else 0
                ),
                "server_answer_ledger_revision": (
                    hashlib.sha256(
                        b"answered-user-discovered-miss"
                    ).hexdigest()
                ),
            }

    runtime = PromotedZeroWithAnswerRuntime()
    service = PipelineJobService(runtime, data_root=tmp_path.resolve())
    project_id = uuid4()
    _configure(service, project_id)
    started, _ = _start(
        service, project_id, suffix="promoted-zero-first-user-miss"
    )
    waiting = _wait(
        service, project_id, started["job_id"], {"awaiting_review"}
    )
    question_stage = waiting["stages"][
        PIPELINE_STAGES.index("question_queue")
    ]
    empty_queue = question_stage["checkpoint"]
    assert empty_queue["question_count"] == 0
    assert empty_queue["question_candidate_ids"] == []
    assert empty_queue["schema_version"] == (
        "trafficvisson.zero-candidate-promoted-survey-queue.v1"
    )

    candidate_id = "a" * 20
    question_id = uuid5(runtime.analysis_job_id, candidate_id)
    overlay_id = "ceo_" + ("b" * 32)
    event_id = "rve_" + ("c" * 32)
    truth_zeros = {
        "annotations_created": 0,
        "incidents_created": 0,
        "training_members_created": 0,
        "report_truth_records_created": 0,
    }
    registration, replayed = (
        service.register_user_discovered_candidate(
            project_id,
            started["job_id"],
            expected_question_queue_revision=empty_queue[
                "question_queue_revision"
            ],
            candidate_evidence={
                "schema_version": (
                    "trafficvisson.user-discovered-candidate.v1"
                ),
                "candidate_kind": "user_discovered",
                "candidate_id": candidate_id,
                "request_sha256": "6" * 64,
                "project_id": str(project_id),
                "analysis_job_id": str(runtime.analysis_job_id),
                "pipeline_job_id": str(started["job_id"]),
                "source_sha256": runtime.source_sha256,
                "overlay_id": overlay_id,
                "review_only": True,
                **truth_zeros,
            },
            overlay_checkpoint={
                "overlay_id": overlay_id,
                "manifest_sha256": "7" * 64,
                "candidate_count": 1,
                "analysis_job_id": str(runtime.analysis_job_id),
                "pipeline_job_id": str(started["job_id"]),
                "source_sha256": runtime.source_sha256,
                "review_only": True,
                **truth_zeros,
            },
            delta_review_event_set={
                "review_event_set_id": "res_" + ("d" * 32),
                "review_event_set_revision": "8" * 64,
                "manifest_sha256": "9" * 64,
                "overlay_id": overlay_id,
                "analysis_job_id": str(runtime.analysis_job_id),
                "pipeline_job_id": str(started["job_id"]),
                "source_sha256": runtime.source_sha256,
                "event_count": 1,
                "event_ids": [event_id],
                "review_only": True,
                **truth_zeros,
            },
            delta_event_binding={
                "event_id": event_id,
                "representative_candidate_id": candidate_id,
                "event_revision": "a" * 64,
            },
            question_id=question_id,
            question_catalog_registration={
                "project_id": str(project_id),
                "analysis_job_id": str(runtime.analysis_job_id),
                "source_sha256": runtime.source_sha256,
                "question_count": 1,
                "question_set_sha256": "b" * 64,
                "evidence_binding_sha256": "c" * 64,
            },
        )
    )

    assert replayed is False
    assert all(registration[name] == 0 for name in truth_zeros)
    after_miss = service.get(project_id, started["job_id"])
    assert after_miss["status"] == "awaiting_review"
    assert after_miss["outcome_code"] == "success"
    question = after_miss["stages"][
        PIPELINE_STAGES.index("question_queue")
    ]["checkpoint"]
    assert question["question_count"] == 1
    assert question["question_candidate_ids"] == [candidate_id]
    assert question["question_catalog_revision"] == "b" * 64
    assert (
        question["question_queue_revision"]
        != empty_queue["question_queue_revision"]
    )
    assert question["incident_count"] == 0
    assert question["report_incident_count"] == 0
    assert "derived_outputs" not in runtime.calls

    with pytest.raises(
        PipelineJobConflict,
        match="Required questions are not fully reviewed",
    ):
        service.complete_review(
            project_id,
            started["job_id"],
            review_completion_id="review-first-user-miss-v1",
            final_qa_choice="miss_candidates_reviewed",
        )

    runtime.answer_recorded = True
    with pytest.raises(
        PipelineJobConflict,
        match="require the reviewed choice",
    ):
        service.complete_review(
            project_id,
            started["job_id"],
            review_completion_id="review-first-user-miss-v1",
            final_qa_choice="no_miss_complete",
        )

    resumed = service.complete_review(
        project_id,
        started["job_id"],
        review_completion_id="review-first-user-miss-v1",
        final_qa_choice="miss_candidates_reviewed",
    )
    assert resumed["status"] == "queued"
    completed = _wait(
        service, project_id, started["job_id"], {"completed"}
    )
    assert completed["outcome_code"] == "success"
    survey_qa = completed["provenance"]["survey_qa_completion"]
    assert survey_qa["final_qa_choice"] == (
        "miss_candidates_reviewed"
    )
    assert survey_qa["user_discovered_candidate_count"] == 1
    assert survey_qa["question_queue_revision"] == (
        question["question_queue_revision"]
    )
    service.shutdown()


def test_fully_delegated_training_only_queue_auto_advances_with_exact_ledger(
    tmp_path,
):
    project_id = uuid4()
    runtime = StructuredDelegationRuntime(project_id)
    service = PipelineJobService(
        runtime, data_root=tmp_path.resolve()
    )
    _structured_delegation_profile(service, project_id)

    started, replayed = _start(
        service, project_id, suffix="delegated-auto-review"
    )
    assert replayed is False
    completed = _wait(
        service, project_id, started["job_id"], {"completed"}
    )

    assert completed["review_completion_id"] == (
        runtime.expected_completion_id
    )
    assert completed["provenance"]["review_completion_mode"] == (
        "structured_llm_training_only"
    )
    review = completed["provenance"]["review_completion"]
    assert review["completion_source"] == (
        "structured_llm_training_delegation"
    )
    assert review["training_only"] is True
    assert review["automatic_training_started"] is False
    assert review["automatic_incident_approval"] is False
    assert review["automatic_report_truth"] is False
    assert len(runtime.review_validation_calls) == 1
    assert runtime.calls == list(PIPELINE_STAGES)
    derived_context = next(
        context
        for stage, context in runtime.contexts
        if stage == "derived_outputs"
    )
    assert derived_context["review_completion_id"] == (
        runtime.expected_completion_id
    )
    assert derived_context["review_completion"] == review
    question_stage = next(
        stage
        for stage in completed["stages"]
        if stage["name"] == "question_queue"
    )
    assert question_stage["checkpoint"]["human_question_count"] == 0
    assert question_stage["checkpoint"]["incident_count"] == 0
    assert question_stage["checkpoint"]["report_incident_count"] == 0
    service.shutdown()


def test_partial_or_mandatory_delegation_still_waits_for_human_review(
    tmp_path,
):
    project_id = uuid4()
    runtime = StructuredDelegationRuntime(
        project_id,
        partial_human_review=True,
    )
    service = PipelineJobService(
        runtime, data_root=tmp_path.resolve()
    )
    _structured_delegation_profile(service, project_id)

    started, _ = _start(
        service, project_id, suffix="delegated-partial-review"
    )
    waiting = _wait(
        service,
        project_id,
        started["job_id"],
        {"awaiting_review"},
    )

    assert waiting["current_stage"] == "question_queue"
    assert waiting["review_completion_id"] is None
    assert runtime.review_validation_calls == []
    assert "derived_outputs" not in runtime.calls
    question_stage = next(
        stage
        for stage in waiting["stages"]
        if stage["name"] == "question_queue"
    )
    checkpoint = question_stage["checkpoint"]
    assert checkpoint["structured_label_delegation_state"] == (
        "partial_human_review_required"
    )
    assert checkpoint["human_question_count"] == 1
    assert checkpoint["mandatory_human_candidate_ids"] == [
        "b" * 20
    ]
    assert checkpoint["incident_count"] == 0
    assert checkpoint["report_incident_count"] == 0
    service.shutdown()


def test_fully_delegated_queue_fails_closed_on_wrong_answer_ledger(
    tmp_path,
):
    project_id = uuid4()
    runtime = StructuredDelegationRuntime(
        project_id,
        corrupt_review_proof=True,
    )
    service = PipelineJobService(
        runtime, data_root=tmp_path.resolve()
    )
    _structured_delegation_profile(service, project_id)

    started, _ = _start(
        service, project_id, suffix="delegated-proof-failure"
    )
    failed = _wait(
        service, project_id, started["job_id"], {"failed"}
    )

    assert failed["current_stage"] == "question_queue"
    assert failed["review_completion_id"] is None
    assert failed["error_code"] == (
        "structured_llm_auto_review_verification_failed"
    )
    assert "derived_outputs" not in runtime.calls
    assert len(runtime.review_validation_calls) == 1
    service.shutdown()


def test_pipeline_analysis_projection_stays_gated_until_review_completion(
    tmp_path,
):
    runtime = FakeRuntime()
    runtime.analysis_job_id = uuid4()
    runtime.block_stage = "detection_ocr"
    service = PipelineJobService(runtime, data_root=tmp_path.resolve())
    project_id = uuid4()
    _configure(service, project_id)
    started, _ = _start(service, project_id, suffix="projection-gate")

    assert runtime.block_started.wait(1)
    assert (
        service.is_analysis_projection_gated(
            project_id, runtime.analysis_job_id
        )
        is True
    )
    runtime.block_release.set()
    _wait(service, project_id, started["job_id"], {"awaiting_review"})
    assert (
        service.is_analysis_projection_gated(
            project_id, runtime.analysis_job_id
        )
        is True
    )

    service.complete_review(
        project_id,
        started["job_id"],
        review_completion_id="review-projection-gate-v1",
    )
    assert (
        service.is_analysis_projection_gated(
            project_id, runtime.analysis_job_id
        )
        is False
    )
    assert (
        service.is_analysis_review_closed(
            project_id, runtime.analysis_job_id
        )
        is True
    )
    _wait(service, project_id, started["job_id"], {"completed"})
    service.shutdown()


def test_start_is_idempotent_by_key_and_input_profile_fingerprint(tmp_path):
    runtime = FakeRuntime()
    runtime.block_stage = "intake_validation"
    service = PipelineJobService(runtime, data_root=tmp_path.resolve())
    project_id = uuid4()
    _configure(service, project_id)
    snapshot_id = uuid4()
    first, replayed = service.start(
        project_id,
        snapshot_id=snapshot_id,
        file_name="drive.mp4",
        rights_confirmed=True,
        idempotency_key="pipeline-test-idempotent-01",
    )
    assert replayed is False
    runtime.block_started.wait(1)
    same, replayed = service.start(
        project_id,
        snapshot_id=snapshot_id,
        file_name="drive.mp4",
        rights_confirmed=True,
        idempotency_key="pipeline-test-idempotent-01",
    )
    assert replayed is True
    assert same["job_id"] == first["job_id"]
    same_input, replayed = service.start(
        project_id,
        snapshot_id=snapshot_id,
        file_name="drive.mp4",
        rights_confirmed=True,
        idempotency_key="pipeline-test-another-key-01",
    )
    assert replayed is True
    assert same_input["job_id"] == first["job_id"]
    with pytest.raises(PipelineJobConflict):
        service.start(
            project_id,
            snapshot_id=uuid4(),
            file_name="other.mp4",
            rights_confirmed=True,
            idempotency_key="pipeline-test-idempotent-01",
        )
    runtime.block_release.set()
    service.shutdown()


def test_source_hash_and_profile_reuse_canonical_job_across_snapshots(
    tmp_path,
):
    source_sha256 = hashlib.sha256(
        b"same-road-video"
    ).hexdigest()

    class SourceHashRuntime(FakeRuntime):
        def execute(self, stage, context, cancel):
            if stage == "sha256":
                self.calls.append(stage)
                self.contexts.append((stage, context))
                return PipelineStageResult(
                    "success",
                    checkpoint={"source_sha256": source_sha256},
                    metrics={"completed": 1},
                )
            return super().execute(stage, context, cancel)

    runtime = SourceHashRuntime()
    root = tmp_path.resolve()
    service = PipelineJobService(runtime, data_root=root)
    project_id = uuid4()
    _configure(service, project_id)
    first, _ = service.start(
        project_id,
        snapshot_id=uuid4(),
        file_name="drive.mp4",
        rights_confirmed=True,
        idempotency_key="pipeline-source-first-0001",
    )
    canonical = _wait(
        service, project_id, first["job_id"], {"awaiting_review"}
    )
    calls_before_reselection = list(runtime.calls)

    second, replayed_at_start = service.start(
        project_id,
        snapshot_id=uuid4(),
        file_name="drive.mp4",
        rights_confirmed=True,
        idempotency_key="pipeline-source-second-0001",
    )
    assert replayed_at_start is False
    replayed = _wait(
        service, project_id, second["job_id"], {"awaiting_review"}
    )
    assert replayed["job_id"] == canonical["job_id"]
    assert service.list(project_id) == [canonical]
    assert runtime.calls[len(calls_before_reselection) :] == [
        "intake_validation",
        "sha256",
    ]
    assert (
        service.recovery_summary()["aliases_loaded"] == 0
    )
    service.shutdown()

    restarted = PipelineJobService(
        SourceHashRuntime(), data_root=root
    )
    restored = restarted.get(project_id, second["job_id"])
    assert restored["job_id"] == canonical["job_id"]
    assert restarted.recovery_summary()["aliases_loaded"] == 1
    restarted.shutdown()


def test_explicit_new_source_prefix_bypasses_hash_reuse(tmp_path):
    source_sha256 = hashlib.sha256(
        b"same-road-video"
    ).hexdigest()

    class SourceHashRuntime(FakeRuntime):
        def execute(self, stage, context, cancel):
            if stage == "sha256":
                self.calls.append(stage)
                self.contexts.append((stage, context))
                return PipelineStageResult(
                    "success",
                    checkpoint={"source_sha256": source_sha256},
                    metrics={"completed": 1},
                )
            return super().execute(stage, context, cancel)

    service = PipelineJobService(
        SourceHashRuntime(), data_root=tmp_path.resolve()
    )
    project_id = uuid4()
    _configure(service, project_id)
    first, _ = _start(service, project_id, suffix="source-normal")
    _wait(service, project_id, first["job_id"], {"awaiting_review"})
    explicit, replayed = service.start(
        project_id,
        snapshot_id=uuid4(),
        file_name="drive.mp4",
        rights_confirmed=True,
        idempotency_key="explicit-new:source-run-0001",
    )
    assert replayed is False
    explicit_waiting = _wait(
        service,
        project_id,
        explicit["job_id"],
        {"awaiting_review"},
    )
    assert explicit_waiting["job_id"] == explicit["job_id"]
    assert len(service.list(project_id)) == 2
    service.shutdown()


def test_promoted_model_change_requires_explicit_rerun_before_candidate_stage(
    tmp_path,
):
    data_root = (tmp_path / "bridge-data").resolve()
    project_id = uuid4()
    first_model = _pipeline_resolved_model(
        tmp_path,
        project_id,
        marker="a",
        state_revision=4,
    )
    second_model = _pipeline_resolved_model(
        tmp_path,
        project_id,
        marker="b",
        state_revision=5,
    )
    runtime = PromotedModelRerunRuntime(first_model)
    runtime.fail_once = "dense_index_2fps"
    service = PipelineJobService(runtime, data_root=data_root)
    _configure(service, project_id)
    snapshot_id = uuid4()
    started, replayed = service.start(
        project_id,
        snapshot_id=snapshot_id,
        file_name="drive.mp4",
        rights_confirmed=True,
        idempotency_key="pipeline-model-pin-resume-0001",
    )
    assert replayed is False
    failed = _wait(
        service,
        project_id,
        started["job_id"],
        {"failed"},
    )
    assert failed["capability_snapshot"][
        "promoted_project_model_binding"
    ]["model_version_id"] == first_model.model_version_id

    runtime.model = second_model
    with pytest.raises(
        PipelineJobConflict,
        match="promoted project model changed",
    ):
        service.resume(project_id, started["job_id"])
    with pytest.raises(
        PipelineJobConflict,
        match="idempotency key belongs to different input",
    ):
        service.start(
            project_id,
            snapshot_id=snapshot_id,
            file_name="drive.mp4",
            rights_confirmed=True,
            idempotency_key="pipeline-model-pin-resume-0001",
        )
    service.shutdown()


def test_explicit_rerun_captures_new_promoted_model_and_replays_after_restart(
    tmp_path,
):
    data_root = (tmp_path / "bridge-data").resolve()
    project_id = uuid4()
    first_model = _pipeline_resolved_model(
        tmp_path,
        project_id,
        marker="a",
        state_revision=4,
    )
    second_model = _pipeline_resolved_model(
        tmp_path,
        project_id,
        marker="b",
        state_revision=5,
    )
    runtime = PromotedModelRerunRuntime(first_model)
    service = PipelineJobService(runtime, data_root=data_root)
    profile = _configure(service, project_id)
    source, _ = _start(
        service,
        project_id,
        suffix="model-pin-rerun-source",
    )
    source = _wait(
        service,
        project_id,
        source["job_id"],
        {"awaiting_review"},
    )
    source_binding = source["capability_snapshot"][
        "promoted_project_model_binding"
    ]
    assert source_binding["model_version_id"] == (
        first_model.model_version_id
    )

    runtime.model = second_model
    rerun, replayed = service.rerun_with_current_profile(
        project_id,
        source["job_id"],
        expected_source_record_sha256=source["record_sha256"],
        expected_profile_revision=profile["revision"],
        rights_confirmed=True,
        user_confirmed=True,
        idempotency_key="pipeline-model-pin-rerun-0001",
    )
    assert replayed is False
    rerun = _wait(
        service,
        project_id,
        rerun["job_id"],
        {"awaiting_review"},
    )
    rerun_binding = rerun["capability_snapshot"][
        "promoted_project_model_binding"
    ]
    assert rerun_binding["model_version_id"] == (
        second_model.model_version_id
    )
    assert rerun_binding["binding_sha256"] != (
        source_binding["binding_sha256"]
    )
    assert rerun["input_fingerprint"] != source["input_fingerprint"]

    replay, replayed = service.rerun_with_current_profile(
        project_id,
        source["job_id"],
        expected_source_record_sha256=source["record_sha256"],
        expected_profile_revision=profile["revision"],
        rights_confirmed=True,
        user_confirmed=True,
        idempotency_key="pipeline-model-pin-rerun-0001",
    )
    assert replayed is True
    assert replay["job_id"] == rerun["job_id"]
    service.shutdown()

    third_model = _pipeline_resolved_model(
        tmp_path,
        project_id,
        marker="c",
        state_revision=6,
    )
    recovered_runtime = PromotedModelRerunRuntime(third_model)
    recovered = PipelineJobService(
        recovered_runtime,
        data_root=data_root,
    )
    restored = recovered.get(project_id, rerun["job_id"])
    assert restored["capability_snapshot"][
        "promoted_project_model_binding"
    ] == rerun_binding
    replay, replayed = recovered.rerun_with_current_profile(
        project_id,
        source["job_id"],
        expected_source_record_sha256=source["record_sha256"],
        expected_profile_revision=profile["revision"],
        rights_confirmed=True,
        user_confirmed=True,
        idempotency_key="pipeline-model-pin-rerun-0001",
    )
    assert replayed is True
    assert replay["job_id"] == rerun["job_id"]
    assert recovered_runtime.rerun_validation_calls == []
    recovered.shutdown()


def test_restart_rejects_resealed_promoted_model_binding_tamper(
    tmp_path,
):
    data_root = (tmp_path / "bridge-data").resolve()
    project_id = uuid4()
    first_model = _pipeline_resolved_model(
        tmp_path,
        project_id,
        marker="a",
        state_revision=4,
    )
    second_model = _pipeline_resolved_model(
        tmp_path,
        project_id,
        marker="b",
        state_revision=5,
    )
    runtime = PromotedModelRerunRuntime(first_model)
    service = PipelineJobService(runtime, data_root=data_root)
    _configure(service, project_id)
    started, _ = _start(
        service,
        project_id,
        suffix="model-pin-tamper",
    )
    started = _wait(
        service,
        project_id,
        started["job_id"],
        {"awaiting_review"},
    )
    service.shutdown()

    job_path = (
        data_root
        / "projects"
        / str(project_id)
        / "pipeline"
        / "jobs"
        / f"{started['job_id']}.json"
    )
    payload = json.loads(job_path.read_text(encoding="utf-8"))
    payload["capability_snapshot"][
        "promoted_project_model_binding"
    ] = capture_promoted_model_job_binding(
        project_id,
        lambda _project_id: second_model,
    )
    payload["provenance"]["capability_sha256"] = hashlib.sha256(
        json.dumps(
            payload["capability_snapshot"],
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    job_path.write_bytes(raw)
    job_path.with_suffix(".json.sha256").write_text(
        f"{hashlib.sha256(raw).hexdigest()}\n",
        encoding="ascii",
    )

    recovered = PipelineJobService(
        PromotedModelRerunRuntime(second_model),
        data_root=data_root,
    )
    assert recovered.list(project_id) == []
    assert recovered.recovery_summary()["records_rejected"] >= 1
    recovered.shutdown()


def test_restart_uses_start_time_storage_plan_for_promoted_model_fingerprint(
    tmp_path,
):
    data_root = (tmp_path / "bridge-data").resolve()
    project_id = uuid4()
    model = _pipeline_resolved_model(
        tmp_path,
        project_id,
        marker="a",
        state_revision=4,
    )
    service = PipelineJobService(
        PromotedModelRerunRuntime(model),
        data_root=data_root,
    )
    _configure(service, project_id)
    started, _ = _start(
        service,
        project_id,
        suffix="model-pin-derived-storage",
    )
    started = _wait(
        service,
        project_id,
        started["job_id"],
        {"awaiting_review"},
    )
    service.shutdown()

    job_path = (
        data_root
        / "projects"
        / str(project_id)
        / "pipeline"
        / "jobs"
        / f"{started['job_id']}.json"
    )
    payload = json.loads(job_path.read_text(encoding="utf-8"))
    assert payload["provenance"].pop("input_storage_plan") is None
    payload["provenance"]["storage_plan"] = (
        build_pipeline_storage_plan(
            duration_seconds=60.0,
            candidate_rescan_fps=8,
        )
    )
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    job_path.write_bytes(raw)
    job_path.with_suffix(".json.sha256").write_text(
        f"{hashlib.sha256(raw).hexdigest()}\n",
        encoding="ascii",
    )

    recovered = PipelineJobService(
        PromotedModelRerunRuntime(model),
        data_root=data_root,
    )
    restored = recovered.get(project_id, started["job_id"])
    assert restored["job_id"] == started["job_id"]
    assert recovered.recovery_summary()["records_rejected"] == 0
    recovered.shutdown()


def test_server_owned_rerun_rebinds_current_profile_without_mutating_source(
    tmp_path,
):
    runtime = RerunRuntime()
    data_root = (tmp_path / "bridge-data").resolve()
    service = PipelineJobService(runtime, data_root=data_root)
    project_id = uuid4()
    source_profile = _configure(service, project_id)
    source, replayed = _start(
        service,
        project_id,
        suffix="rerun-source",
    )
    assert replayed is False
    source = _wait(
        service,
        project_id,
        source["job_id"],
        {"awaiting_review"},
    )
    source_path = (
        data_root
        / "projects"
        / str(project_id)
        / "pipeline"
        / "jobs"
        / f"{source['job_id']}.json"
    )
    source_bytes = source_path.read_bytes()
    source_file_sha256 = hashlib.sha256(source_bytes).hexdigest()

    current_profile = service.configure_profile(
        project_id,
        question_policy="full_review",
        review_mode="quick",
        default_template_id="sample-4q-v1",
        default_template_sha256=hashlib.sha256(b"template").hexdigest(),
        detector_pack_id="road-detector-v2",
        ocr_pack_id="korean-ocr-v2",
        sam2_pack_id="sam2-v2",
        report_provider="codex",
        output_location_id="project-default",
        candidate_rescan_fps=9,
    )
    assert current_profile["revision"] == source_profile["revision"] + 1

    rerun, replayed = service.rerun_with_current_profile(
        project_id,
        source["job_id"],
        expected_source_record_sha256=source["record_sha256"],
        expected_profile_revision=current_profile["revision"],
        rights_confirmed=True,
        user_confirmed=True,
        idempotency_key="pipeline-rerun-current-profile-0001",
    )
    assert replayed is False
    assert rerun["job_id"] != source["job_id"]
    assert rerun["snapshot_id"] == source["snapshot_id"]
    assert rerun["file_name"] == source["file_name"]
    rerun = _wait(
        service,
        project_id,
        rerun["job_id"],
        {"awaiting_review"},
    )

    assert rerun["profile_snapshot"]["revision"] == current_profile["revision"]
    assert rerun["profile_snapshot"]["detector_pack_id"] == "road-detector-v2"
    assert rerun["profile_snapshot"]["ocr_pack_id"] == "korean-ocr-v2"
    assert rerun["profile_snapshot"]["sam2_pack_id"] == "sam2-v2"
    assert rerun["capability_snapshot"]["requested_detector_pack_id"] == (
        "road-detector-v2"
    )
    assert rerun["capability_snapshot"]["detector_model_manifest_sha256"] == (
        hashlib.sha256(b"detector:road-detector-v2").hexdigest()
    )
    assert rerun["capability_snapshot"]["ocr_model_manifest_sha256"] == (
        hashlib.sha256(b"ocr:korean-ocr-v2").hexdigest()
    )
    assert rerun["capability_snapshot"]["sam2_model_manifest_sha256"] == (
        hashlib.sha256(b"sam2:sam2-v2").hexdigest()
    )
    assert rerun["stages"][0]["status"] == "success"
    assert rerun["stages"][0]["attempts"] == 0
    assert rerun["stages"][0]["metrics"]["server_owned_rerun_reused"] is True
    assert rerun["stages"][1]["status"] == "success"
    assert rerun["stages"][1]["attempts"] == 0
    target_calls = [
        stage
        for stage, context in runtime.contexts
        if context["job_id"] == str(rerun["job_id"])
    ]
    assert target_calls
    assert target_calls[0] == "metadata_gps_preflight"
    assert "intake_validation" not in target_calls
    assert "sha256" not in target_calls

    lineage = rerun["provenance"]["rerun"]
    assert lineage["schema_version"] == PIPELINE_JOB_RERUN_SCHEMA
    assert lineage["policy"] == "server_owned_current_profile_rerun"
    assert lineage["source_job_id"] == str(source["job_id"])
    assert lineage["source_record_sha256"] == source["record_sha256"]
    assert lineage["source_snapshot_id"] == str(source["snapshot_id"])
    assert lineage["source_sha256"] == runtime.source_sha256
    assert lineage["source_profile_revision"] == source_profile["revision"]
    assert lineage["target_profile_revision"] == current_profile["revision"]
    assert lineage["target_job_id"] == str(rerun["job_id"])
    assert lineage["media_proof"]["path_exposed"] is False
    assert set(runtime.rerun_validation_calls[0]) == {
        "project_id",
        "snapshot_id",
        "file_name",
        "sha_checkpoint",
    }

    source_after = service.get(project_id, source["job_id"])
    assert source_after["record_sha256"] == source["record_sha256"]
    assert source_after["profile_snapshot"] == source["profile_snapshot"]
    assert source_path.read_bytes() == source_bytes
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == (
        source_file_sha256
    )
    replay, replayed = service.rerun_with_current_profile(
        project_id,
        source["job_id"],
        expected_source_record_sha256=source["record_sha256"],
        expected_profile_revision=current_profile["revision"],
        rights_confirmed=True,
        user_confirmed=True,
        idempotency_key="pipeline-rerun-current-profile-0001",
    )
    assert replayed is True
    assert replay["job_id"] == rerun["job_id"]
    assert len(runtime.rerun_validation_calls) == 1

    service.shutdown()
    recovered_runtime = RerunRuntime()
    recovered = PipelineJobService(
        recovered_runtime,
        data_root=data_root,
    )
    replay, replayed = recovered.rerun_with_current_profile(
        project_id,
        source["job_id"],
        expected_source_record_sha256=source["record_sha256"],
        expected_profile_revision=current_profile["revision"],
        rights_confirmed=True,
        user_confirmed=True,
        idempotency_key="pipeline-rerun-current-profile-0001",
    )
    assert replayed is True
    assert replay["job_id"] == rerun["job_id"]
    assert recovered_runtime.rerun_validation_calls == []
    assert recovered.get(project_id, source["job_id"])["record_sha256"] == (
        source["record_sha256"]
    )
    recovered.shutdown()


def test_server_owned_rerun_rejects_stale_or_unverified_source(
    tmp_path,
):
    runtime = RerunRuntime()
    data_root = (tmp_path / "bridge-data").resolve()
    service = PipelineJobService(runtime, data_root=data_root)
    project_id = uuid4()
    source_profile = _configure(service, project_id)
    source, _ = _start(
        service,
        project_id,
        suffix="rerun-reject-source",
    )
    source = _wait(
        service,
        project_id,
        source["job_id"],
        {"awaiting_review"},
    )
    source_path = (
        data_root
        / "projects"
        / str(project_id)
        / "pipeline"
        / "jobs"
        / f"{source['job_id']}.json"
    )
    source_bytes = source_path.read_bytes()

    with pytest.raises(PipelineJobConflict, match="changed after"):
        service.rerun_with_current_profile(
            project_id,
            source["job_id"],
            expected_source_record_sha256="0" * 64,
            expected_profile_revision=source_profile["revision"],
            rights_confirmed=True,
            user_confirmed=True,
            idempotency_key="pipeline-rerun-stale-source-0001",
        )

    current_profile = service.configure_profile(
        project_id,
        question_policy="learning_assist",
        review_mode="precision",
        default_template_id="sample-4q-v1",
        default_template_sha256=hashlib.sha256(b"template").hexdigest(),
        detector_pack_id="road-detector-v2",
        ocr_pack_id="korean-ocr-v2",
        sam2_pack_id="sam2-v2",
        report_provider="codex",
        output_location_id="project-default",
        candidate_rescan_fps=8,
    )
    with pytest.raises(PipelineJobConflict, match="profile changed"):
        service.rerun_with_current_profile(
            project_id,
            source["job_id"],
            expected_source_record_sha256=source["record_sha256"],
            expected_profile_revision=source_profile["revision"],
            rights_confirmed=True,
            user_confirmed=True,
            idempotency_key="pipeline-rerun-stale-profile-0001",
        )

    runtime.reject_rerun_source = True
    with pytest.raises(PipelineJobConflict, match="could not be revalidated"):
        service.rerun_with_current_profile(
            project_id,
            source["job_id"],
            expected_source_record_sha256=source["record_sha256"],
            expected_profile_revision=current_profile["revision"],
            rights_confirmed=True,
            user_confirmed=True,
            idempotency_key="pipeline-rerun-invalid-grant-0001",
        )
    assert len(service.list(project_id)) == 1
    assert source_path.read_bytes() == source_bytes
    assert service.get(project_id, source["job_id"])["record_sha256"] == (
        source["record_sha256"]
    )
    service.shutdown()


def test_one_stable_folder_can_queue_all_51_videos(tmp_path):
    runtime = FakeRuntime()
    runtime.block_stage = "intake_validation"
    service = PipelineJobService(runtime, data_root=tmp_path.resolve())
    project_id = uuid4()
    _configure(service, project_id)
    snapshot_id = uuid4()

    started_ids = []
    for index in range(51):
        started, replayed = service.start(
            project_id,
            snapshot_id=snapshot_id,
            file_name=f"drive-{index + 1:03d}.mp4",
            rights_confirmed=True,
            idempotency_key=f"pipeline-folder-{index + 1:03d}-0001",
        )
        assert replayed is False
        started_ids.append(started["job_id"])

    assert runtime.block_started.wait(1)
    rows = service.list(project_id, limit=128)
    assert len(rows) == 51
    assert {row["file_name"] for row in rows} == {
        f"drive-{index + 1:03d}.mp4" for index in range(51)
    }
    assert set(started_ids) == {row["job_id"] for row in rows}
    assert sum(row["status"] == "queued" for row in rows) >= 50

    runtime.block_release.set()
    service.shutdown()


def test_model_unavailable_is_blocked_and_resume_retries_exact_stage(tmp_path):
    runtime = FakeRuntime()
    runtime.model_ready = False
    service = PipelineJobService(runtime, data_root=tmp_path.resolve())
    project_id = uuid4()
    _configure(service, project_id)
    started, _ = _start(service, project_id, suffix="model")
    blocked = _wait(service, project_id, started["job_id"], {"blocked"})
    assert blocked["current_stage"] == "candidate_rescan_5_10fps"
    assert blocked["outcome_code"] == "model_unavailable"
    before = list(runtime.calls)
    runtime.model_ready = True
    resumed = service.resume(project_id, started["job_id"])
    assert resumed["retry_count"] == 1
    waiting = _wait(
        service, project_id, started["job_id"], {"awaiting_review"}
    )
    assert runtime.calls[: len(before)] == before
    assert runtime.calls[len(before)] == "candidate_rescan_5_10fps"
    assert "intake_validation" not in runtime.calls[len(before) :]
    assert waiting["resume_count"] == 1
    assert waiting["capability_snapshot"]["local_models"] == "ready"
    assert len(waiting["provenance"]["capability_attempts"]) == 1
    service.shutdown()


def test_partial_derived_outputs_stays_resumable_and_replays_bundle(tmp_path):
    class PartialRuntime(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.derived_calls = 0

        def execute(self, stage, context, cancel):
            if stage != "derived_outputs":
                return super().execute(stage, context, cancel)
            self.calls.append(stage)
            self.contexts.append((stage, context))
            self.derived_calls += 1
            return PipelineStageResult(
                "partial",
                checkpoint={
                    "state": "partial",
                    "export_bundle_id": "bundle-stable-v1",
                    "report": {
                        "state": "blocked",
                        "blocking_reasons": [
                            "report_transmission_consent_required"
                        ],
                        "com_invoked": False,
                    },
                    "absolute_paths_exposed": False,
                    "com_invoked": False,
                },
                metrics={
                    "artifact_count": 10,
                    "idempotent_replay": self.derived_calls > 1,
                },
                error_code="derived_outputs_partial",
            )

    runtime = PartialRuntime()
    service = PipelineJobService(runtime, data_root=tmp_path.resolve())
    project_id = uuid4()
    _configure(service, project_id)
    started, _ = _start(service, project_id, suffix="partial")
    _wait(service, project_id, started["job_id"], {"awaiting_review"})
    service.complete_review(
        project_id,
        started["job_id"],
        review_completion_id="review-complete-partial-v1",
    )
    blocked = _wait(
        service, project_id, started["job_id"], {"blocked"}
    )
    assert blocked["current_stage"] == "derived_outputs"
    assert blocked["outcome_code"] == "partial"
    assert blocked["error_code"] == "derived_outputs_partial"
    derived_stage = blocked["stages"][-1]
    assert derived_stage["status"] == "blocked"
    assert derived_stage["outcome"] == "partial"
    assert (
        derived_stage["checkpoint"]["export_bundle_id"]
        == "bundle-stable-v1"
    )
    PipelineJobResponse(**blocked)

    calls_before_resume = list(runtime.calls)
    resumed = service.resume(project_id, started["job_id"])
    assert (
        resumed["stages"][-1]["checkpoint"]["export_bundle_id"]
        == "bundle-stable-v1"
    )
    replayed = _wait(
        service, project_id, started["job_id"], {"blocked"}
    )
    assert runtime.calls[: len(calls_before_resume)] == calls_before_resume
    assert runtime.calls[len(calls_before_resume) :] == ["derived_outputs"]
    assert (
        replayed["stages"][-1]["checkpoint"]["export_bundle_id"]
        == "bundle-stable-v1"
    )
    assert replayed["stages"][-1]["metrics"]["idempotent_replay"] is True
    assert runtime.derived_calls == 2
    service.shutdown()


def test_privacy_review_block_is_valid_at_vision_and_resumes_after_review(
    tmp_path,
):
    class PrivacyRuntime(FakeRuntime):
        def __init__(self, *, privacy_approved=False):
            super().__init__()
            self.privacy_approved = privacy_approved

        def execute(self, stage, context, cancel):
            if stage != "vision_proposal":
                return super().execute(stage, context, cancel)
            self.calls.append(stage)
            self.contexts.append((stage, context))
            if not self.privacy_approved:
                return PipelineStageResult(
                    "partial",
                    checkpoint={
                        "structured_llm_semantics_status": (
                            "awaiting_deidentification_review"
                        ),
                        "provider_invocation_count": 0,
                        "privacy_review_required_count": 2,
                        "provider_visual_evidence_by_candidate": {
                            "a" * 20: {
                                "state": (
                                    "awaiting_deidentification_review"
                                ),
                                "path_exposed": False,
                                "human_verified": False,
                            }
                        },
                    },
                    metrics={
                        "provider_invocation_count": 0,
                        "privacy_review_required_count": 2,
                    },
                    error_code="awaiting_deidentification_review",
                )
            return PipelineStageResult(
                "success",
                checkpoint={
                    "structured_llm_semantics_status": "completed",
                    "provider_invocation_count": 2,
                    "privacy_review_required_count": 0,
                },
                metrics={
                    "provider_invocation_count": 2,
                    "privacy_review_required_count": 0,
                },
            )

    root = tmp_path.resolve()
    runtime = PrivacyRuntime()
    service = PipelineJobService(runtime, data_root=root)
    project_id = uuid4()
    _configure(service, project_id)
    started, _ = _start(
        service,
        project_id,
        suffix="privacy-review",
    )
    blocked = _wait(
        service,
        project_id,
        started["job_id"],
        {"blocked"},
    )
    assert blocked["current_stage"] == "vision_proposal"
    assert blocked["outcome_code"] == "partial"
    assert blocked["error_code"] == (
        "awaiting_deidentification_review"
    )
    assert blocked["stages"][9]["status"] == "blocked"
    assert blocked["stages"][10]["status"] == "pending"
    assert blocked["stages"][9]["metrics"][
        "provider_invocation_count"
    ] == 0
    PipelineJobResponse(**blocked)

    invalid = {
        **blocked,
        "current_stage": "temporal_merge",
    }
    with pytest.raises(
        ValueError,
        match="outside a resumable review boundary",
    ):
        PipelineJobResponse(**invalid)

    service.shutdown()
    restarted_runtime = PrivacyRuntime(privacy_approved=True)
    restarted = PipelineJobService(
        restarted_runtime,
        data_root=root,
    )
    restored = restarted.get(project_id, started["job_id"])
    PipelineJobResponse(**restored)
    assert restored["status"] == "blocked"
    assert restored["current_stage"] == "vision_proposal"

    restarted.resume(project_id, started["job_id"])
    awaiting = _wait(
        restarted,
        project_id,
        started["job_id"],
        {"awaiting_review"},
    )
    assert awaiting["current_stage"] == "question_queue"
    assert restarted_runtime.calls[0] == "vision_proposal"
    assert "question_queue" in restarted_runtime.calls
    restarted.shutdown()


def test_report_preview_blocked_resumes_with_new_attempt_identity(
    tmp_path,
):
    class RecoveringReportRuntime(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.derived_attempts = []

        def execute(self, stage, context, cancel):
            if stage != "derived_outputs":
                return super().execute(stage, context, cancel)
            self.calls.append(stage)
            self.contexts.append((stage, context))
            attempt = context["stage_attempts"][
                "derived_outputs"
            ]
            self.derived_attempts.append(attempt)
            if len(self.derived_attempts) == 1:
                return PipelineStageResult(
                    "report_preview_blocked",
                    checkpoint={
                        "state": "blocked",
                        "export_bundle_id": "bundle-report-blocked-v1",
                        "report": {
                            "state": "blocked",
                            "blocking_reasons": [
                                "report_provider_unavailable"
                            ],
                            "com_invoked": False,
                        },
                        "absolute_paths_exposed": False,
                        "com_invoked": False,
                    },
                    metrics={"artifact_count": 10},
                    error_code="report_provider_unavailable",
                )
            return PipelineStageResult(
                "success",
                checkpoint={
                    "state": "completed",
                    "export_bundle_id": "bundle-report-ready-v2",
                    "report": {
                        "state": "validated_preview",
                        "document_edit_plan_prepared": True,
                        "com_invoked": False,
                    },
                    "absolute_paths_exposed": False,
                    "com_invoked": False,
                },
                metrics={"artifact_count": 10},
            )

    root = tmp_path.resolve()
    runtime = RecoveringReportRuntime()
    service = PipelineJobService(runtime, data_root=root)
    project_id = uuid4()
    _configure(service, project_id)
    started, _ = _start(
        service, project_id, suffix="report-retry"
    )
    _wait(
        service, project_id, started["job_id"], {"awaiting_review"}
    )
    service.complete_review(
        project_id,
        started["job_id"],
        review_completion_id="review-report-retry-v1",
    )
    blocked = _wait(
        service, project_id, started["job_id"], {"blocked"}
    )
    assert blocked["outcome_code"] == "report_preview_blocked"
    assert blocked["error_code"] == "report_provider_unavailable"
    assert blocked["stages"][-1]["outcome"] == (
        "report_preview_blocked"
    )
    assert blocked["review_completion_id"] == (
        "review-report-retry-v1"
    )
    PipelineJobResponse(**blocked)

    service.shutdown()
    restarted_runtime = RecoveringReportRuntime()
    # The provider becomes ready after the bridge restart. Keep the first
    # blocked attempt in this fake's logical history so the resumed call
    # returns success.
    restarted_runtime.derived_attempts.append(1)
    restarted = PipelineJobService(
        restarted_runtime, data_root=root
    )
    restored = restarted.get(project_id, started["job_id"])
    assert restored["status"] == "blocked"
    assert restored["review_completion_id"] == (
        "review-report-retry-v1"
    )
    restarted.resume(project_id, started["job_id"])
    completed = _wait(
        restarted,
        project_id,
        started["job_id"],
        {"completed"},
    )
    assert completed["outcome_code"] == "success"
    assert completed["review_completion_id"] == (
        "review-report-retry-v1"
    )
    assert restarted_runtime.calls == ["derived_outputs"]
    assert restarted_runtime.derived_attempts[-1] == 2
    assert completed["stages"][-1]["checkpoint"][
        "export_bundle_id"
    ] == "bundle-report-ready-v2"
    restarted.shutdown()


def test_bridge_runtime_builds_exact_safe_derived_output_request(tmp_path):
    project_id = uuid4()
    pipeline_job_id = uuid4()
    analysis_job_id = uuid4()
    source_sha256 = hashlib.sha256(b"source").hexdigest()
    input_fingerprint = hashlib.sha256(b"input").hexdigest()
    ledger_sha256 = hashlib.sha256(b"answer-ledger").hexdigest()
    completion_id = f"review:{ledger_sha256}"
    question_revision = hashlib.sha256(b"questions").hexdigest()
    candidate_id = "a" * 20
    profile = {
        "schema_version": "trafficvisson.pipeline-profile.v3",
        "default_template_id": "sample-4q-v1",
        "default_template_sha256": hashlib.sha256(
            b"template"
        ).hexdigest(),
        "report_provider": "codex",
        "report_route_name": "서울북부고속도로 (구리-포천)",
        "report_inspector_name": "홍길동",
        "report_evidence_mode": "shared_deidentified",
        "report_inspection_date_policy": (
            "source_filename_yyyymmdd_or_block"
        ),
        "output_location_id": "project-default",
    }
    profile_sha256 = hashlib.sha256(
        json.dumps(
            profile,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    class CapturingDerivedOutputs:
        def __init__(self):
            self.requests = []
            self.dataset_version_id = str(uuid4())
            self.dataset_hash = "1" * 64

        def generate(self, request, *, cancelled):
            assert cancelled() is False
            self.requests.append(request)
            return DerivedOutputsResult(
                checkpoint={
                    "state": "blocked",
                    "export_bundle_id": "bundle-safe-v1",
                    "dataset_version": {
                        "state": "prepared",
                        "dataset_version_id": (
                            self.dataset_version_id
                        ),
                        "dataset_hash": self.dataset_hash,
                        "dataset_manifest_sha256": "2" * 64,
                        "approved_reference_members_sha256": (
                            "3" * 64
                        ),
                        "approved_reference_member_count": 0,
                        "training_gate_member_count": 1,
                        "approved_references_count_toward_training_gate": False,
                        "approved_references_count_toward_validation": False,
                        "automatic_training_started": False,
                    },
                    "report": {
                        "state": "blocked",
                        "blocking_reasons": [
                            "report_transmission_consent_required",
                            "template_semantic_map_unavailable",
                        ],
                        "provider_called": False,
                        "document_edit_plan_prepared": False,
                        "com_invoked": False,
                    },
                    "absolute_paths_exposed": False,
                    "com_invoked": False,
                },
                metrics={
                    "artifact_count": 10,
                    "incident_count": 1,
                },
                replayed=False,
            )

    derived = CapturingDerivedOutputs()
    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        derived_outputs=derived,
    )
    context = {
        "job_id": str(pipeline_job_id),
        "project_id": str(project_id),
        "snapshot_id": str(uuid4()),
        "file_name": "20250916_drive.mp4",
        "input_fingerprint": input_fingerprint,
        "profile_sha256": profile_sha256,
        "profile": profile,
        "capabilities": {},
        "checkpoints": {
            "sha256": {"source_sha256": source_sha256},
            "question_queue": {
                "analysis_job_id": str(analysis_job_id),
                "source_sha256": source_sha256,
                "question_count": 1,
                "question_candidate_ids": [candidate_id],
                "question_queue_revision": question_revision,
            },
        },
        "review_completion_id": completion_id,
        "review_completion": {
            "review_completion_id": completion_id,
            "all_required_questions_answered": True,
            "answered_question_count": 1,
            "server_answer_ledger_revision": ledger_sha256,
            "analysis_job_id": str(analysis_job_id),
            "question_queue_revision": question_revision,
        },
    }
    result = runtime.execute("derived_outputs", context, lambda: False)
    assert result.outcome == "report_preview_blocked"
    assert result.error_code == "report_preview_blocked"
    assert result.checkpoint["absolute_paths_exposed"] is False
    assert result.checkpoint["com_invoked"] is False
    assert result.checkpoint["dataset_version_id"] == (
        derived.dataset_version_id
    )
    assert result.checkpoint["dataset_hash"] == derived.dataset_hash
    assert result.checkpoint["dataset_version"][
        "dataset_version_id"
    ] == derived.dataset_version_id
    assert result.checkpoint["dataset_version"][
        "dataset_hash"
    ] == derived.dataset_hash
    assert result.metrics["idempotent_replay"] is False
    request = derived.requests[0]
    assert request.project_id == project_id
    assert request.pipeline_job_id == pipeline_job_id
    assert request.analysis_job_id == analysis_job_id
    assert request.question_candidate_ids == (candidate_id,)
    assert request.pipeline_input_fingerprint == input_fingerprint
    assert request.pipeline_profile_sha256 == profile_sha256
    assert request.review_completion_id == completion_id
    assert request.answer_ledger_sha256 == ledger_sha256
    assert request.report_transmission_consent is False
    assert request.template_semantic_map_ready is False
    assert request.report_provider_ready is False
    assert request.report_attempt == 0
    assert request.report_route_name == (
        "서울북부고속도로 (구리-포천)"
    )
    assert request.report_inspection_date == "2025-09-16"
    assert request.report_policy == {
        "evidence_mode": "shared_deidentified",
        "evaluation_ratings": ["clear_unassessed"] * 27,
    }
    assert request.verified_deidentified_evidence_by_incident_id == {}
    assert request.question_queue_revision == question_revision
    assert request.survey_incident_authority_sha256 is None
    assert request.survey_incident_authority_manifest is None


def test_bridge_runtime_grants_incident_only_to_verified_promoted_survey(
    tmp_path,
):
    project_id = uuid4()
    pipeline_job_id = uuid4()
    analysis_job_id = uuid4()
    source_sha256 = hashlib.sha256(b"survey-source").hexdigest()
    input_fingerprint = hashlib.sha256(b"survey-input").hexdigest()
    ledger_sha256 = hashlib.sha256(b"survey-ledger").hexdigest()
    completion_id = f"review:{ledger_sha256}"
    question_revision = hashlib.sha256(
        b"survey-questions"
    ).hexdigest()
    dense_index_sha256 = hashlib.sha256(
        b"survey-dense"
    ).hexdigest()
    discovery_revision = hashlib.sha256(
        b"survey-discovery"
    ).hexdigest()
    event_set_id = "res_" + ("a" * 32)
    event_set_revision = "b" * 64
    event_set_manifest_sha256 = "c" * 64
    catalog_revision = "d" * 64
    candidate_id = "e" * 20
    provider_state = {
        "provider": "promoted_project_model",
        "state": "ready",
        "model_version_id": "model-promoted-v1",
        "dataset_version_id": "dataset-promoted-v1",
        "dataset_hash": "1" * 64,
        "fixed_validation_sha256": "2" * 64,
        "primary_artifact_sha256": "3" * 64,
        "artifact_manifest_sha256": "4" * 64,
        "survey_analysis_job_id": str(analysis_job_id),
        "independent_survey_verified": True,
        "source_analysis_lineage_sha256": "5" * 64,
        "dataset_provenance_graph_sha256": "6" * 64,
    }
    promoted_inference_sha256 = hashlib.sha256(
        json.dumps(
            {
                "pipeline_job_id": str(pipeline_job_id),
                "analysis_job_id": str(analysis_job_id),
                "dense_candidate_discovery_revision": (
                    discovery_revision
                ),
                "provider_state": provider_state,
                "active_model_artifact_manifest_sha256": "4" * 64,
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    survey_qa_base = {
        "schema_version": (
            "trafficvisson.promoted-model-survey-qa-completion.v1"
        ),
        "project_id": str(project_id),
        "pipeline_job_id": str(pipeline_job_id),
        "analysis_job_id": str(analysis_job_id),
        "source_sha256": source_sha256,
        "dense_index_sha256": dense_index_sha256,
        "dense_candidate_discovery_revision": discovery_revision,
        "question_queue_revision": question_revision,
        "model_version_id": "model-promoted-v1",
        "dataset_version_id": "dataset-promoted-v1",
        "primary_artifact_sha256": "3" * 64,
        "artifact_manifest_sha256": "4" * 64,
        "dataset_hash": "1" * 64,
        "fixed_validation_sha256": "2" * 64,
        "source_analysis_lineage_sha256": "5" * 64,
        "dataset_provenance_graph_sha256": "6" * 64,
        "promoted_model_inference_sha256": (
            promoted_inference_sha256
        ),
        "sampling_fps": 2,
        "scanned_frame_count": 120,
        "final_qa_choice": "no_miss_complete",
        "review_completion_id": completion_id,
        "server_answer_ledger_revision": ledger_sha256,
        "review_event_set_collection_revision": None,
        "question_catalog_collection_revision": None,
        "user_discovered_registrations_sha256": hashlib.sha256(
            b"[]"
        ).hexdigest(),
        "user_discovered_candidate_count": 0,
        "actor": "local_user",
        "recorded_at": "2026-07-30T00:00:00+00:00",
    }
    survey_qa = {
        **survey_qa_base,
        "survey_qa_revision": hashlib.sha256(
            json.dumps(
                survey_qa_base,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    profile = {
        "default_template_id": "sample-4q-v1",
        "default_template_sha256": "6" * 64,
        "report_provider": "none",
        "output_location_id": "project-default",
    }
    profile_sha256 = hashlib.sha256(
        json.dumps(
            profile,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    class CapturingSurveyOutputs:
        def __init__(self):
            self.requests = []
            self.dataset_version_id = str(uuid4())

        def generate(self, request, *, cancelled):
            assert cancelled() is False
            self.requests.append(request)
            return DerivedOutputsResult(
                checkpoint={
                    "state": "completed",
                    "dataset_version": {
                        "state": "prepared",
                        "dataset_version_id": (
                            self.dataset_version_id
                        ),
                        "dataset_hash": "7" * 64,
                        "dataset_manifest_sha256": "8" * 64,
                        "approved_reference_members_sha256": "9" * 64,
                        "approved_reference_member_count": 0,
                        "training_gate_member_count": 1,
                        "approved_references_count_toward_training_gate": False,
                        "approved_references_count_toward_validation": False,
                        "automatic_training_started": False,
                    },
                    "report": {
                        "state": (
                            "not_applicable_no_approved_incidents"
                        ),
                        "blocking_reasons": [],
                        "provider_called": False,
                        "document_edit_plan_prepared": False,
                        "com_invoked": False,
                    },
                    "absolute_paths_exposed": False,
                    "com_invoked": False,
                },
                metrics={
                    "artifact_count": 10,
                    "annotation_count": 1,
                    "incident_count": 1,
                },
                replayed=False,
            )

    derived = CapturingSurveyOutputs()
    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        derived_outputs=derived,
    )
    context = {
        "job_id": str(pipeline_job_id),
        "project_id": str(project_id),
        "snapshot_id": str(uuid4()),
        "file_name": "20260730_survey.mp4",
        "input_fingerprint": input_fingerprint,
        "profile_sha256": profile_sha256,
        "profile": profile,
        "capabilities": {},
        "checkpoints": {
            "sha256": {"source_sha256": source_sha256},
            "candidate_rescan_5_10fps": {
                "analysis_job_id": str(analysis_job_id),
                "source_sha256": source_sha256,
                "dense_index_sha256": dense_index_sha256,
                "dense_candidate_discovery_revision": (
                    discovery_revision
                ),
                "dense_candidate_discovery": {
                    "source_sha256": source_sha256,
                    "dense_index_sha256": dense_index_sha256,
                    "sampling_fps": 2,
                    "scanned_frame_count": 120,
                    "discovery_revision": discovery_revision,
                    "provider_states": [provider_state],
                },
            },
            "question_queue": {
                "analysis_job_id": str(analysis_job_id),
                "source_sha256": source_sha256,
                "question_count": 1,
                "question_candidate_ids": [candidate_id],
                "question_queue_revision": question_revision,
                "review_event_set_state": "materialized",
                "review_event_set_id": event_set_id,
                "review_event_set_revision": event_set_revision,
                "review_event_set_manifest_sha256": (
                    event_set_manifest_sha256
                ),
                "question_catalog_state": "registered",
                "question_catalog_revision": catalog_revision,
                "user_discovered_candidate_registrations": [],
            },
        },
        "review_completion_id": completion_id,
        "review_completion": {
            "review_completion_id": completion_id,
            "all_required_questions_answered": True,
            "answered_question_count": 1,
            "server_answer_ledger_revision": ledger_sha256,
            "analysis_job_id": str(analysis_job_id),
            "question_queue_revision": question_revision,
            "review_event_set_id": event_set_id,
            "review_event_set_revision": event_set_revision,
            "review_event_set_manifest_sha256": (
                event_set_manifest_sha256
            ),
            "question_catalog_revision": catalog_revision,
            "survey_qa_completion": survey_qa,
        },
    }

    result = runtime.execute("derived_outputs", context, lambda: False)

    assert result.outcome == "success"
    request = derived.requests[0]
    assert isinstance(
        request.survey_incident_authority_sha256, str
    )
    assert len(request.survey_incident_authority_sha256) == 64
    expected_authority = {
        "schema_version": SURVEY_INCIDENT_AUTHORITY_SCHEMA,
        "project_id": str(project_id),
        "pipeline_job_id": str(pipeline_job_id),
        "analysis_job_id": str(analysis_job_id),
        "source_sha256": source_sha256,
        "question_queue_revision": question_revision,
        "review_completion_id": completion_id,
        "answer_ledger_sha256": ledger_sha256,
        "survey_qa_revision": survey_qa["survey_qa_revision"],
        "model_version_id": "model-promoted-v1",
        "model_promotion_state": "promoted",
        "dataset_version_id": "dataset-promoted-v1",
        "dataset_hash": "1" * 64,
        "dataset_provenance_graph_sha256": "6" * 64,
        "fixed_validation_sha256": "2" * 64,
        "primary_artifact_sha256": "3" * 64,
        "artifact_manifest_sha256": "4" * 64,
        "source_analysis_lineage_sha256": "5" * 64,
        "promoted_model_inference_sha256": (
            promoted_inference_sha256
        ),
        "dense_index_sha256": dense_index_sha256,
        "dense_candidate_discovery_revision": discovery_revision,
        "sampling_fps": 2,
        "scanned_frame_count": 120,
        "event_binding": {
            "state": "materialized",
            "review_event_set_id": event_set_id,
            "review_event_set_revision": event_set_revision,
            "review_event_set_manifest_sha256": (
                event_set_manifest_sha256
            ),
        },
        "question_catalog_binding": {
            "state": "registered",
            "question_catalog_revision": catalog_revision,
        },
        "final_qa_choice": "no_miss_complete",
        "user_discovered_registrations_sha256": (
            hashlib.sha256(b"[]").hexdigest()
        ),
        "user_discovered_candidate_count": 0,
        "actor": "local_user",
        "independent_survey_verified": True,
    }
    assert (
        request.survey_incident_authority_manifest
        == expected_authority
    )
    assert request.question_queue_revision == question_revision
    assert request.survey_incident_authority_sha256 == (
        canonical_sha256(expected_authority)
    )

    forged = json.loads(json.dumps(context))
    forged_qa = forged["review_completion"][
        "survey_qa_completion"
    ]
    forged_qa["fixed_validation_sha256"] = "f" * 64
    forged_base = dict(forged_qa)
    forged_base.pop("survey_qa_revision")
    forged_qa["survey_qa_revision"] = hashlib.sha256(
        json.dumps(
            forged_base,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    with pytest.raises(PipelineStageFailure) as error:
        runtime.execute("derived_outputs", forged, lambda: False)
    assert error.value.code == (
        "derived_outputs_survey_authority_invalid"
    )
    assert len(derived.requests) == 1


def test_bridge_runtime_exports_verified_zero_detection_completion(
    tmp_path,
):
    project_id = uuid4()
    pipeline_job_id = uuid4()
    analysis_job_id = uuid4()
    source_sha256 = hashlib.sha256(b"zero-source").hexdigest()
    input_fingerprint = hashlib.sha256(b"zero-input").hexdigest()
    profile = {
        "default_template_id": "sample-4q-v1",
        "default_template_sha256": hashlib.sha256(
            b"template"
        ).hexdigest(),
        "report_provider": "codex",
        "output_location_id": "project-default",
    }
    profile_sha256 = hashlib.sha256(
        json.dumps(
            profile,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    class CapturingZeroOutputs:
        def __init__(self):
            self.requests = []

        def generate(self, request, *, cancelled):
            assert cancelled() is False
            self.requests.append(request)
            return DerivedOutputsResult(
                checkpoint={
                    "state": "completed",
                    "completion_mode": "zero_detections",
                    "export_bundle_id": "bundle-zero-v1",
                    "report": {
                        "state": (
                            "not_applicable_no_approved_incidents"
                        ),
                        "blocking_reasons": [],
                        "provider_called": False,
                        "document_edit_plan_prepared": False,
                        "com_invoked": False,
                    },
                    "absolute_paths_exposed": False,
                    "com_invoked": False,
                },
                metrics={
                    "raw_detection_count": 0,
                    "reviewed_answer_count": 0,
                    "incident_count": 0,
                    "annotation_count": 0,
                    "route_point_count": 0,
                    "evidence_file_count": 0,
                    "artifact_count": 10,
                },
                replayed=False,
            )

    derived = CapturingZeroOutputs()
    artifact_roots = ProjectArtifactRootRegistry(
        data_root=tmp_path.resolve(),
    )
    artifact_roots.capture_profile_binding(
        project_id,
        profile_revision=1,
        output_location_id="project-default",
    )
    artifact_binding = artifact_roots.capture_job_binding(
        project_id,
        profile_revision=1,
        output_location_id="project-default",
    ).binding
    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        derived_outputs=derived,
        artifact_root_registry=artifact_roots,
    )
    context = {
        "job_id": str(pipeline_job_id),
        "project_id": str(project_id),
        "snapshot_id": str(uuid4()),
        "file_name": "empty-drive.mp4",
        "input_fingerprint": input_fingerprint,
        "profile_sha256": profile_sha256,
        "profile": profile,
        "capabilities": {},
        "artifact_root": artifact_binding,
        "checkpoints": {
            "sha256": {"source_sha256": source_sha256},
            "candidate_rescan_5_10fps": {
                "analysis_job_id": str(analysis_job_id),
                "source_sha256": source_sha256,
                "candidate_count": 0,
            },
        },
        "review_completion_id": None,
        "review_completion": {},
    }

    result = runtime.execute("derived_outputs", context, lambda: False)

    assert result.outcome == "zero_detections"
    assert result.error_code == "zero_detections"
    assert result.checkpoint["completion_mode"] == "zero_detections"
    assert result.metrics["raw_detection_count"] == 0
    assert result.metrics["incident_count"] == 0
    request = derived.requests[0]
    empty_ledger_sha256 = hashlib.sha256(b"").hexdigest()
    assert request.project_id == project_id
    assert request.pipeline_job_id == pipeline_job_id
    assert request.analysis_job_id == analysis_job_id
    assert request.completion_mode == "zero_detections"
    assert request.question_candidate_ids == ()
    assert request.answer_ledger_sha256 == empty_ledger_sha256
    assert isinstance(request.question_queue_revision, str)
    assert len(request.question_queue_revision) == 64
    assert request.survey_incident_authority_sha256 is None
    assert request.survey_incident_authority_manifest is None
    assert request.review_completion_id == (
        f"zero-detections:{empty_ledger_sha256}"
    )
    assert request.report_route_name == ""
    assert request.report_inspection_date == ""
    assert request.report_policy == {
        "evidence_mode": "internal_original",
        "evaluation_ratings": ["clear_unassessed"] * 27,
    }
    assert request.verified_deidentified_evidence_by_incident_id == {}


def test_bridge_runtime_exports_promoted_zero_survey_only_after_no_miss_qa(
    tmp_path,
):
    project_id = uuid4()
    pipeline_job_id = uuid4()
    analysis_job_id = uuid4()
    source_sha256 = hashlib.sha256(
        b"promoted-zero-survey-source"
    ).hexdigest()
    input_fingerprint = hashlib.sha256(
        b"promoted-zero-survey-input"
    ).hexdigest()
    empty_ledger_sha256 = hashlib.sha256(b"").hexdigest()
    completion_id = f"zero-detections:{empty_ledger_sha256}"
    question_revision = hashlib.sha256(
        b"promoted-zero-survey-questions"
    ).hexdigest()
    profile = {
        "default_template_id": "sample-4q-v1",
        "default_template_sha256": hashlib.sha256(
            b"template"
        ).hexdigest(),
        "report_provider": "codex",
        "output_location_id": "project-default",
    }
    profile_sha256 = hashlib.sha256(
        json.dumps(
            profile,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    class CapturingPromotedZeroOutputs:
        def __init__(self):
            self.requests = []

        def generate(self, request, *, cancelled):
            assert cancelled() is False
            self.requests.append(request)
            return DerivedOutputsResult(
                checkpoint={
                    "state": "completed",
                    "completion_mode": "zero_detections",
                    "export_bundle_id": "bundle-promoted-zero-v1",
                    "report": {
                        "state": (
                            "not_applicable_no_approved_incidents"
                        ),
                        "blocking_reasons": [],
                        "provider_called": False,
                        "document_edit_plan_prepared": False,
                        "com_invoked": False,
                    },
                    "absolute_paths_exposed": False,
                    "com_invoked": False,
                },
                metrics={
                    "raw_detection_count": 0,
                    "reviewed_answer_count": 0,
                    "incident_count": 0,
                    "annotation_count": 0,
                    "route_point_count": 0,
                    "evidence_file_count": 0,
                    "artifact_count": 10,
                },
                replayed=False,
            )

    derived = CapturingPromotedZeroOutputs()
    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        derived_outputs=derived,
    )
    question_checkpoint = {
        "schema_version": (
            "trafficvisson.zero-candidate-promoted-survey-queue.v1"
        ),
        "analysis_job_id": str(analysis_job_id),
        "source_sha256": source_sha256,
        "question_count": 0,
        "question_candidate_ids": [],
        "question_queue_revision": question_revision,
        "review_event_set_state": "empty_promoted_survey",
        "review_event_sets": [],
        "review_event_count": 0,
        "review_event_ids": [],
        "review_event_bindings": [],
        "question_catalog_state": "empty_promoted_survey",
        "question_catalog_registrations": [],
        "user_discovered_candidate_registrations": [],
        "review_only": True,
        "incident_count": 0,
        "report_incident_count": 0,
    }
    review_completion = {
        "review_completion_id": completion_id,
        "all_required_questions_answered": True,
        "answered_question_count": 0,
        "server_answer_ledger_revision": empty_ledger_sha256,
        "analysis_job_id": str(analysis_job_id),
        "question_queue_revision": question_revision,
        "completion_source": (
            "local_user_zero_candidate_survey_qa"
        ),
        "survey_qa_completion": {
            "final_qa_choice": "no_miss_complete",
            "question_queue_revision": question_revision,
        },
    }
    context = {
        "job_id": str(pipeline_job_id),
        "project_id": str(project_id),
        "snapshot_id": str(uuid4()),
        "file_name": "empty-promoted-survey.mp4",
        "input_fingerprint": input_fingerprint,
        "profile_sha256": profile_sha256,
        "profile": profile,
        "capabilities": {},
        "checkpoints": {
            "sha256": {"source_sha256": source_sha256},
            "question_queue": question_checkpoint,
        },
        "review_completion_id": completion_id,
        "review_completion": review_completion,
    }

    result = runtime.execute("derived_outputs", context, lambda: False)

    assert result.outcome == "zero_detections"
    assert result.error_code == "zero_detections"
    request = derived.requests[0]
    assert request.analysis_job_id == analysis_job_id
    assert request.question_candidate_ids == ()
    assert request.completion_mode == "zero_detections"
    assert request.answer_ledger_sha256 == empty_ledger_sha256
    assert request.review_completion_id == completion_id

    missing_qa_context = json.loads(json.dumps(context))
    missing_qa_context["review_completion"].pop(
        "survey_qa_completion"
    )
    with pytest.raises(PipelineStageFailure) as error:
        runtime.execute(
            "derived_outputs",
            missing_qa_context,
            lambda: False,
        )
    assert error.value.code == "derived_outputs_provenance_invalid"


def test_zero_detections_is_not_saved_as_success(tmp_path):
    runtime = FakeRuntime()
    runtime.zero_detections = True
    service = PipelineJobService(runtime, data_root=tmp_path.resolve())
    project_id = uuid4()
    _configure(service, project_id)
    started, _ = _start(service, project_id, suffix="zero")
    completed = _wait(
        service, project_id, started["job_id"], {"completed"}
    )
    assert completed["outcome_code"] == "zero_detections"
    assert "question_queue" not in runtime.calls
    assert runtime.calls[-1] == "derived_outputs"
    skipped = {
        stage["name"]: stage["status"] for stage in completed["stages"]
    }
    assert skipped["sam2_segmentation_tracking"] == "skipped"
    assert skipped["question_queue"] == "skipped"
    service.shutdown()


def test_renderer_cannot_forge_review_completion(tmp_path):
    runtime = FakeRuntime()
    runtime.review_valid = False
    service = PipelineJobService(runtime, data_root=tmp_path.resolve())
    project_id = uuid4()
    _configure(service, project_id)
    started, _ = _start(service, project_id, suffix="forged-review")
    _wait(service, project_id, started["job_id"], {"awaiting_review"})
    with pytest.raises(PipelineJobConflict, match="not fully reviewed"):
        service.complete_review(
            project_id,
            started["job_id"],
            review_completion_id="forged-review-v1",
        )
    row = service.get(project_id, started["job_id"])
    assert row["status"] == "awaiting_review"
    assert row["review_completion_id"] is None
    assert "review_completion" not in row["provenance"]
    service.shutdown()


def test_restart_registers_legacy_question_sidecar_once_and_persists_revision(
    tmp_path,
):
    analysis_job_id = uuid4()
    source_sha256 = hashlib.sha256(b"legacy-question-source").hexdigest()
    candidate_ids = ["a" * 20, "b" * 20]
    queue_revision = hashlib.sha256(
        b"legacy-question-queue"
    ).hexdigest()

    class LegacyQuestionRuntime(FakeRuntime):
        def execute(self, stage, context, cancel):
            if stage != "question_queue":
                return super().execute(stage, context, cancel)
            self.calls.append(stage)
            self.contexts.append((stage, context))
            return PipelineStageResult(
                "success",
                checkpoint={
                    "analysis_job_id": str(analysis_job_id),
                    "source_sha256": source_sha256,
                    "question_count": len(candidate_ids),
                    "question_candidate_ids": list(candidate_ids),
                    "question_queue_revision": queue_revision,
                    "review_only": True,
                    "annotation_count": 0,
                    "incident_count": 0,
                },
                metrics={
                    "question_count": len(candidate_ids),
                    "annotation_count": 0,
                    "incident_count": 0,
                },
            )

    class CatalogMigrationRuntime(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.registered_sets = []

        def reconcile_question_catalog(self, context):
            checkpoint = context["checkpoints"]["question_queue"]
            exact = {
                "analysis_job_id": checkpoint["analysis_job_id"],
                "source_sha256": checkpoint["source_sha256"],
                "question_candidate_ids": list(
                    checkpoint["question_candidate_ids"]
                ),
                "question_queue_revision": checkpoint[
                    "question_queue_revision"
                ],
            }
            self.registered_sets.append(exact)
            assert exact == {
                "analysis_job_id": str(analysis_job_id),
                "source_sha256": source_sha256,
                "question_candidate_ids": candidate_ids,
                "question_queue_revision": queue_revision,
            }
            return PipelineStageResult(
                "success",
                checkpoint={
                    **checkpoint,
                    "question_catalog_state": "registered",
                    "question_catalog_revision": "f" * 64,
                },
                metrics={
                    "question_count": len(candidate_ids),
                    "catalog_question_count": len(candidate_ids),
                    "annotation_count": 0,
                    "incident_count": 0,
                },
            )

    root = tmp_path.resolve()
    first = PipelineJobService(
        LegacyQuestionRuntime(), data_root=root
    )
    project_id = uuid4()
    _configure(first, project_id)
    started, _ = _start(
        first, project_id, suffix="legacy-question-catalog"
    )
    waiting = _wait(
        first, project_id, started["job_id"], {"awaiting_review"}
    )
    legacy_hash = waiting["record_sha256"]
    assert waiting["stages"][-2]["checkpoint"].get(
        "question_catalog_state"
    ) is None
    first.shutdown()

    migration = CatalogMigrationRuntime()
    recovered = PipelineJobService(migration, data_root=root)
    upgraded = recovered.get(project_id, started["job_id"])
    question_stage = upgraded["stages"][-2]
    assert question_stage["checkpoint"][
        "question_catalog_state"
    ] == "registered"
    assert question_stage["checkpoint"][
        "question_catalog_revision"
    ] == "f" * 64
    assert question_stage["metrics"]["catalog_question_count"] == 2
    assert question_stage["checkpoint"]["annotation_count"] == 0
    assert question_stage["checkpoint"]["incident_count"] == 0
    assert upgraded["record_sha256"] != legacy_hash
    assert migration.registered_sets == [
        {
            "analysis_job_id": str(analysis_job_id),
            "source_sha256": source_sha256,
            "question_candidate_ids": candidate_ids,
            "question_queue_revision": queue_revision,
        }
    ]
    assert recovered.recovery_summary()[
        "question_catalogs_registered"
    ] == 1
    job_path = (
        root
        / "projects"
        / str(project_id)
        / "pipeline"
        / "jobs"
        / f"{started['job_id']}.json"
    )
    raw = job_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == (
        job_path.with_suffix(".json.sha256")
        .read_text(encoding="ascii")
        .strip()
    )
    recovered.shutdown()

    replay_runtime = CatalogMigrationRuntime()
    replayed = PipelineJobService(replay_runtime, data_root=root)
    assert replay_runtime.registered_sets == []
    assert replayed.recovery_summary()[
        "question_catalogs_registered"
    ] == 0
    assert replayed.get(project_id, started["job_id"])[
        "record_sha256"
    ] == upgraded["record_sha256"]
    replayed.shutdown()


def test_restart_requeues_the_exact_running_stage(tmp_path):
    root = tmp_path.resolve()
    first_runtime = FakeRuntime()
    first_runtime.block_stage = "dense_index_2fps"
    first = PipelineJobService(first_runtime, data_root=root)
    project_id = uuid4()
    _configure(first, project_id)
    started, _ = _start(first, project_id, suffix="restart")
    assert first_runtime.block_started.wait(1)
    first.shutdown()

    second_runtime = FakeRuntime()
    second = PipelineJobService(second_runtime, data_root=root)
    waiting = _wait(
        second, project_id, started["job_id"], {"awaiting_review"}
    )
    assert waiting["recovered_after_restart"] is True
    assert second.recovery_summary()["jobs_requeued"] == 1
    assert second_runtime.calls[0] == "dense_index_2fps"
    assert "intake_validation" not in second_runtime.calls
    second.shutdown()


def test_restart_finalizes_durable_cancelling_without_requeue(
    tmp_path,
):
    class UninterruptibleRuntime(FakeRuntime):
        def execute(self, stage, context, cancel):
            self.calls.append(stage)
            self.contexts.append((stage, context))
            if stage == "intake_validation":
                self.block_started.set()
                self.block_release.wait(5)
            return PipelineStageResult(
                "success",
                checkpoint={f"{stage}_id": f"{stage}-v1"},
                metrics={"completed": 1},
            )

    live_root = (tmp_path / "live").resolve()
    recovery_root = (tmp_path / "recovery").resolve()
    runtime = UninterruptibleRuntime()
    first = PipelineJobService(runtime, data_root=live_root)
    project_id = uuid4()
    _configure(first, project_id)
    started, _ = _start(
        first, project_id, suffix="durable-cancelling"
    )
    assert runtime.block_started.wait(1)
    cancelling = first.cancel(project_id, started["job_id"])
    assert cancelling["status"] == "cancelling"
    shutil.copytree(live_root, recovery_root)

    recovered_runtime = FakeRuntime()
    recovered = PipelineJobService(
        recovered_runtime, data_root=recovery_root
    )
    cancelled = recovered.get(project_id, started["job_id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["outcome_code"] == "cancelled"
    assert cancelled["recovered_after_restart"] is True
    assert cancelled["stages"][0]["status"] == "cancelled"
    assert recovered.recovery_summary()["jobs_requeued"] == 0
    assert (
        recovered.recovery_summary()["cancellations_finalized"]
        == 1
    )
    assert recovered_runtime.calls == []

    recovered.shutdown()
    runtime.block_release.set()
    first.shutdown()


def test_dense_index_restores_exact_private_media_grant_after_restart(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "app.pipeline_jobs.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=64 * 1024**3),
    )
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    data_root = (tmp_path / "bridge data").resolve()
    data_root.mkdir()
    project_id = uuid4()
    pipeline_job_id = uuid4()

    first_media = MediaIntakeService(
        ffprobe_path=ffprobe,
        minimum_interval_seconds=0,
    )
    snapshot = first_media.register_local_file(
        project_id,
        str(source),
    )
    first_runtime = BridgePipelineRuntime(
        first_media,
        data_root=data_root,
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
    )
    hash_result = first_runtime.execute(
        "sha256",
        {
            "job_id": str(pipeline_job_id),
            "project_id": str(project_id),
            "snapshot_id": str(snapshot["snapshot_id"]),
            "file_name": source.name,
            "profile": {},
            "capabilities": {},
            "checkpoints": {},
        },
        lambda: False,
    )
    assert hash_result.outcome == "success"
    assert hash_result.checkpoint["total_bytes"] == source.stat().st_size
    first_media.shutdown()

    restarted_media = MediaIntakeService(
        ffprobe_path=ffprobe,
        minimum_interval_seconds=0,
    )
    restarted_runtime = BridgePipelineRuntime(
        restarted_media,
        data_root=data_root,
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
    )
    with pytest.raises(HTTPException) as unresolved:
        restarted_media.resolve_completed_media(
            project_id,
            snapshot["snapshot_id"],
            source.name,
            UUID(hash_result.checkpoint["hash_job_id"]),
            hash_result.checkpoint["source_sha256"],
        )
    assert unresolved.value.status_code == 404

    class FakeDenseIndexEngine:
        def __init__(
            self,
            *,
            ffmpeg_path,
            ffprobe_path,
            limits,
            staging_root,
            staging_owner_id,
        ):
            assert ffmpeg_path == ffmpeg
            assert ffprobe_path == ffprobe
            assert limits.sampling_fps == 2
            assert staging_root is not None
            assert staging_owner_id == str(pipeline_job_id)

        def build(
            self,
            media_path,
            *,
            source_sha256,
            project_root,
            output_directory,
            cancel,
        ):
            assert media_path == source
            assert source_sha256 == hash_result.checkpoint[
                "source_sha256"
            ]
            assert cancel() is False
            run_root = (
                Path(output_directory)
                / "dense-frame-index-restartgrant"
            )
            run_root.mkdir()
            (run_root / "frame-index.json").write_text(
                "{}",
                encoding="utf-8",
            )
            return SimpleNamespace(
                output_directory=str(run_root),
                index_relative_path="frame-index.json",
                frames=(object(),),
                provenance=SimpleNamespace(
                    indexed_duration_seconds=0.0,
                    truncated=False,
                ),
            )

    monkeypatch.setattr(
        "app.pipeline_jobs.DenseFrameIndexEngine",
        FakeDenseIndexEngine,
    )
    dense_result = restarted_runtime.execute(
        "dense_index_2fps",
        {
            "job_id": str(pipeline_job_id),
            "project_id": str(project_id),
            "snapshot_id": str(snapshot["snapshot_id"]),
            "file_name": source.name,
            "profile": {},
            "capabilities": {},
            "checkpoints": {
                "sha256": dict(hash_result.checkpoint),
            },
        },
        lambda: False,
    )
    assert dense_result.outcome == "success"
    assert dense_result.metrics["frame_count"] > 0
    assert (
        restarted_media.resolve_completed_media(
            project_id,
            snapshot["snapshot_id"],
            source.name,
            UUID(hash_result.checkpoint["hash_job_id"]),
            hash_result.checkpoint["source_sha256"],
        ).media_path
        == source
    )
    restarted_media.shutdown()


def test_bridge_runtime_rerun_source_proof_is_path_free_and_revalidated(
    tmp_path,
):
    source, _ffmpeg, ffprobe = _make_video(tmp_path)
    data_root = (tmp_path / "bridge data").resolve()
    data_root.mkdir()
    project_id = uuid4()
    pipeline_job_id = uuid4()
    media = MediaIntakeService(
        ffprobe_path=ffprobe,
        minimum_interval_seconds=0,
    )
    snapshot = media.register_local_file(project_id, str(source))
    runtime = BridgePipelineRuntime(media, data_root=data_root)
    hashed = runtime.execute(
        "sha256",
        {
            "job_id": str(pipeline_job_id),
            "project_id": str(project_id),
            "snapshot_id": str(snapshot["snapshot_id"]),
            "file_name": source.name,
            "profile": {},
            "capabilities": {},
            "checkpoints": {},
        },
        lambda: False,
    )
    assert hashed.outcome == "success"

    proof = runtime.validate_server_owned_rerun_source(
        project_id=project_id,
        snapshot_id=snapshot["snapshot_id"],
        file_name=source.name,
        sha_checkpoint=hashed.checkpoint,
    )
    assert proof == {
        "schema_version": PIPELINE_RERUN_MEDIA_PROOF_SCHEMA,
        "project_id": str(project_id),
        "snapshot_id": str(snapshot["snapshot_id"]),
        "hash_job_id": hashed.checkpoint["hash_job_id"],
        "file_name": source.name,
        "source_sha256": hashed.checkpoint["source_sha256"],
        "size_bytes": hashed.checkpoint["total_bytes"],
        "path_exposed": False,
    }
    assert "media_path" not in proof
    assert str(source) not in json.dumps(proof)

    source.write_bytes(b"changed after server-owned rerun proof")
    with pytest.raises(PipelineStageFailure) as changed:
        runtime.validate_server_owned_rerun_source(
            project_id=project_id,
            snapshot_id=snapshot["snapshot_id"],
            file_name=source.name,
            sha_checkpoint=hashed.checkpoint,
        )
    assert changed.value.code == "private_media_grant_revalidation_failed"
    media.shutdown()


def test_dense_plan_duration_mismatch_interrupts_before_engine_or_artifacts(
    tmp_path,
    monkeypatch,
):
    from app.folder_pipeline_batches import build_pipeline_storage_plan

    source, ffmpeg, ffprobe = _make_video(tmp_path)
    data_root = (tmp_path / "bridge-data").resolve()
    project_id = uuid4()
    pipeline_job_id = uuid4()
    media = MediaIntakeService(
        ffprobe_path=ffprobe,
        minimum_interval_seconds=0,
    )
    snapshot = media.register_local_file(project_id, str(source))
    runtime = BridgePipelineRuntime(
        media,
        data_root=data_root,
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
    )
    hash_result = runtime.execute(
        "sha256",
        {
            "job_id": str(pipeline_job_id),
            "project_id": str(project_id),
            "snapshot_id": str(snapshot["snapshot_id"]),
            "file_name": source.name,
            "profile": {},
            "capabilities": {},
            "checkpoints": {},
        },
        lambda: False,
    )
    engine_calls = 0

    class NoWriteDenseEngine:
        def __init__(self, **_kwargs):
            nonlocal engine_calls
            engine_calls += 1
            raise AssertionError("dense engine must not be constructed")

    monkeypatch.setattr(
        "app.pipeline_jobs.DenseFrameIndexEngine",
        NoWriteDenseEngine,
    )
    storage_plan = build_pipeline_storage_plan(
        duration_seconds=60,
        candidate_rescan_fps=8,
    )
    with pytest.raises(PipelineStageFailure) as interrupted:
        runtime.execute(
            "dense_index_2fps",
            {
                "job_id": str(pipeline_job_id),
                "project_id": str(project_id),
                "snapshot_id": str(snapshot["snapshot_id"]),
                "file_name": source.name,
                "profile": {},
                "capabilities": {},
                "storage_plan": storage_plan,
                "checkpoints": {
                    "intake_validation": {
                        "duration_seconds": 82 * 60,
                    },
                    "sha256": dict(hash_result.checkpoint),
                },
            },
            lambda: False,
        )

    assert interrupted.value.code == "storage_capacity_replan_invalid"
    assert engine_calls == 0
    assert not (
        data_root
        / "projects"
        / str(project_id)
        / "pipeline"
        / "dense"
        / str(pipeline_job_id)
    ).exists()
    media.shutdown()


def test_intake_capacity_block_resumes_same_job_from_private_path_free_snapshot(
    tmp_path,
    monkeypatch,
):
    from app.folder_pipeline_batches import build_pipeline_storage_plan

    source, ffmpeg, ffprobe = _make_video(tmp_path)
    (source.parent / "unrelated-drive.mp4").write_bytes(
        b"unrelated media must not alter selected-file recovery"
    )
    data_root = (tmp_path / "bridge-data").resolve()
    staging_root = initialize_dense_staging_root(
        tmp_path / "dense-staging"
    )
    project_id = uuid4()
    free_bytes = {"value": 1}
    hash_start_count = {"value": 0}

    monkeypatch.setattr(
        "app.pipeline_jobs.dense_frame_index_requires_short_staging",
        lambda _path: True,
    )
    monkeypatch.setattr(
        "app.pipeline_jobs.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=free_bytes["value"]),
    )

    class StopAfterHashRuntime(BridgePipelineRuntime):
        def execute(self, stage, context, cancel):
            if stage == "metadata_gps_preflight":
                return PipelineStageResult(
                    "model_unavailable",
                    checkpoint={
                        "test_stop_after_hash": True,
                        "annotation_count": 0,
                        "incident_count": 0,
                    },
                    error_code="test_stop_after_hash",
                )
            return super().execute(stage, context, cancel)

    first_media = MediaIntakeService(
        ffprobe_path=ffprobe,
        minimum_interval_seconds=0,
    )
    snapshot = first_media.register_local_file(
        project_id,
        str(source),
    )
    original_start_hash = first_media.start_hash_job

    def counted_start_hash(*args, **kwargs):
        hash_start_count["value"] += 1
        return original_start_hash(*args, **kwargs)

    first_media.start_hash_job = counted_start_hash
    first_runtime = StopAfterHashRuntime(
        first_media,
        data_root=data_root,
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        dense_staging_root=staging_root,
    )
    first_service = PipelineJobService(
        first_runtime,
        data_root=data_root,
    )
    _configure(first_service, project_id)
    storage_plan = build_pipeline_storage_plan(
        duration_seconds=2.0,
        candidate_rescan_fps=8,
    )
    started, replayed = first_service.start(
        project_id,
        snapshot_id=snapshot["snapshot_id"],
        file_name=source.name,
        rights_confirmed=True,
        idempotency_key="pipeline-intake-restart-private-0001",
        storage_plan=storage_plan,
    )
    assert replayed is False
    blocked = _wait(
        first_service,
        project_id,
        started["job_id"],
        {"blocked", "failed"},
    )
    assert blocked["status"] == "blocked", blocked
    assert blocked["current_stage"] == "intake_validation"
    assert blocked["error_code"] == "storage_blocked"
    assert hash_start_count["value"] == 0
    binding = blocked["provenance"]["input_restart_binding"]
    assert binding["rights_confirmed"] is True
    assert binding["path_exposed"] is False
    assert binding["job_id"] == str(started["job_id"])
    assert binding["snapshot_id"] == str(snapshot["snapshot_id"])
    assert binding["size_bytes"] == source.stat().st_size
    private_snapshot = (
        data_root
        / "projects"
        / str(project_id)
        / "pipeline"
        / "private-media-grants"
        / f"prehash-{started['job_id']}.json"
    )
    assert private_snapshot.is_file()

    def string_values(value):
        if isinstance(value, Mapping):
            for item in value.values():
                yield from string_values(item)
        elif isinstance(value, list):
            for item in value:
                yield from string_values(item)
        elif isinstance(value, str):
            yield value

    persisted_job = json.loads(
        (
            data_root
            / "projects"
            / str(project_id)
            / "pipeline"
            / "jobs"
            / f"{started['job_id']}.json"
        ).read_text(encoding="utf-8")
    )
    assert str(source) not in set(string_values(blocked))
    assert str(source) not in set(string_values(persisted_job))
    first_service.shutdown()
    first_media.shutdown()

    free_bytes["value"] = 10 * 1024**3
    second_media = MediaIntakeService(
        ffprobe_path=ffprobe,
        minimum_interval_seconds=0,
    )
    second_original_start_hash = second_media.start_hash_job

    def counted_restart_hash(*args, **kwargs):
        hash_start_count["value"] += 1
        return second_original_start_hash(*args, **kwargs)

    second_media.start_hash_job = counted_restart_hash
    second_runtime = StopAfterHashRuntime(
        second_media,
        data_root=data_root,
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        dense_staging_root=staging_root,
    )
    second_service = PipelineJobService(
        second_runtime,
        data_root=data_root,
    )
    resumed = second_service.resume(
        project_id,
        started["job_id"],
    )
    assert resumed["job_id"] == started["job_id"]
    after_restart = _wait(
        second_service,
        project_id,
        started["job_id"],
        {"blocked", "failed"},
    )
    assert after_restart["status"] == "blocked", after_restart
    assert after_restart["current_stage"] == "metadata_gps_preflight"
    intake_stage = next(
        row
        for row in after_restart["stages"]
        if row["name"] == "intake_validation"
    )
    sha_stage = next(
        row
        for row in after_restart["stages"]
        if row["name"] == "sha256"
    )
    assert intake_stage["status"] == "success"
    assert intake_stage["attempts"] == 1
    assert (
        intake_stage["checkpoint"]["server_snapshot_recovered"]
        is True
    )
    assert (
        intake_stage["checkpoint"][
            "input_restart_binding_sha256"
        ]
        == binding["binding_sha256"]
    )
    assert sha_stage["status"] == "success"
    assert sha_stage["checkpoint"]["total_bytes"] == source.stat().st_size
    assert hash_start_count["value"] == 1
    assert after_restart["job_id"] == started["job_id"]
    assert str(source) not in set(string_values(after_restart))
    second_service.shutdown()
    second_media.shutdown()


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    (
        ("source_missing", "media_snapshot_revalidation_failed"),
        ("source_changed", "media_snapshot_revalidation_failed"),
        (
            "private_record_tampered",
            "media_snapshot_restart_binding_invalid",
        ),
    ),
)
def test_private_restart_snapshot_fails_closed_on_missing_changed_or_tampered(
    tmp_path,
    mutation,
    expected_error,
):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    data_root = (tmp_path / "bridge-data").resolve()
    project_id = uuid4()
    job_id = uuid4()
    input_fingerprint = hashlib.sha256(
        b"restart-source-identity"
    ).hexdigest()
    first_media = MediaIntakeService(
        ffprobe_path=ffprobe,
        minimum_interval_seconds=0,
    )
    snapshot = first_media.register_local_file(
        project_id,
        str(source),
    )
    first_runtime = BridgePipelineRuntime(
        first_media,
        data_root=data_root,
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
    )
    context = {
        "job_id": str(job_id),
        "project_id": str(project_id),
        "snapshot_id": str(snapshot["snapshot_id"]),
        "file_name": source.name,
        "input_fingerprint": input_fingerprint,
        "rights_confirmed": True,
    }
    binding = first_runtime.capture_restartable_input(context)
    context["input_restart_binding"] = binding
    private_record = (
        data_root
        / "projects"
        / str(project_id)
        / "pipeline"
        / "private-media-grants"
        / f"prehash-{job_id}.json"
    )
    first_media.shutdown()

    if mutation == "source_missing":
        source.unlink()
    elif mutation == "source_changed":
        source.write_bytes(source.read_bytes() + b"changed")
    else:
        payload = json.loads(
            private_record.read_text(encoding="utf-8")
        )
        payload["modified_time_ns"] += 1
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        private_record.write_bytes(encoded)
        private_record.with_suffix(".json.sha256").write_text(
            hashlib.sha256(encoded).hexdigest() + "\n",
            encoding="ascii",
        )

    restarted_media = MediaIntakeService(
        ffprobe_path=ffprobe,
        minimum_interval_seconds=0,
    )
    restarted_runtime = BridgePipelineRuntime(
        restarted_media,
        data_root=data_root,
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
    )
    with pytest.raises(PipelineStageFailure) as rejected:
        restarted_runtime._restore_restartable_input(context)
    assert rejected.value.code == expected_error
    with pytest.raises(HTTPException) as unresolved:
        restarted_media.start_hash_job(
            project_id,
            snapshot["snapshot_id"],
            source.name,
        )
    assert unresolved.value.status_code == 404
    restarted_media.shutdown()


def test_restart_media_grant_fails_closed_without_size_or_after_source_change(
    tmp_path,
):
    source, _ffmpeg, ffprobe = _make_video(tmp_path)
    data_root = (tmp_path / "bridge data").resolve()
    data_root.mkdir()
    project_id = uuid4()
    pipeline_job_id = uuid4()

    first_media = MediaIntakeService(
        ffprobe_path=ffprobe,
        minimum_interval_seconds=0,
    )
    snapshot = first_media.register_local_file(
        project_id,
        str(source),
    )
    first_runtime = BridgePipelineRuntime(
        first_media,
        data_root=data_root,
    )
    hash_result = first_runtime.execute(
        "sha256",
        {
            "job_id": str(pipeline_job_id),
            "project_id": str(project_id),
            "snapshot_id": str(snapshot["snapshot_id"]),
            "file_name": source.name,
            "profile": {},
            "capabilities": {},
            "checkpoints": {},
        },
        lambda: False,
    )
    first_media.shutdown()

    missing_size_media = MediaIntakeService(
        minimum_interval_seconds=0
    )
    missing_size_runtime = BridgePipelineRuntime(
        missing_size_media,
        data_root=data_root,
    )
    without_size = dict(hash_result.checkpoint)
    without_size.pop("total_bytes")
    with pytest.raises(PipelineStageFailure) as missing_size:
        missing_size_runtime._resolve_pipeline_media_grant(
            project_id=project_id,
            snapshot_id=snapshot["snapshot_id"],
            file_name=source.name,
            sha_checkpoint=without_size,
            cancel=lambda: False,
        )
    assert missing_size.value.code == "hash_checkpoint_missing"
    missing_size_media.shutdown()

    source.write_bytes(b"changed after the completed SHA checkpoint")
    changed_media = MediaIntakeService(minimum_interval_seconds=0)
    changed_runtime = BridgePipelineRuntime(
        changed_media,
        data_root=data_root,
    )
    with pytest.raises(PipelineStageFailure) as changed:
        changed_runtime._resolve_pipeline_media_grant(
            project_id=project_id,
            snapshot_id=snapshot["snapshot_id"],
            file_name=source.name,
            sha_checkpoint=hash_result.checkpoint,
            cancel=lambda: False,
        )
    assert (
        changed.value.code
        == "private_media_grant_revalidation_failed"
    )
    changed_media.shutdown()


def test_bridge_restart_restores_private_media_grant_and_reuses_dense_artifact(
    tmp_path,
):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    data_root = (tmp_path / "bridge data").resolve()
    data_root.mkdir()
    project_id = uuid4()

    class ReadyDinoStatus:
        def status(self):
            return {
                "state": "ready",
                "error_code": None,
                "review_only": True,
                "bulk_frame_detection_provider": "local_model",
                "pack_id": "dino-review",
                "pack_version": "1.0",
                "manifest_sha256": hashlib.sha256(b"dino").hexdigest(),
            }

    class RestartRuntime:
        def __init__(self, bridge, *, block_candidate):
            self.bridge = bridge
            self.block_candidate = block_candidate
            self.candidate_started = threading.Event()
            self.calls = []

        def capability_snapshot(self, profile):
            return self.bridge.capability_snapshot(profile)

        def execute(self, stage, context, cancel):
            self.calls.append(stage)
            if stage in {
                "intake_validation",
                "sha256",
                "metadata_gps_preflight",
            }:
                return self.bridge.execute(stage, context, cancel)
            if stage == "dense_index_2fps":
                run_id = "dense-frame-index-restart-fixture"
                run_root = (
                    data_root
                    / "projects"
                    / str(project_id)
                    / "pipeline"
                    / "dense"
                    / str(context["job_id"])
                    / run_id
                )
                frames_root = run_root / "frames"
                frames_root.mkdir(parents=True)
                frame = frames_root / "frame-00000000.jpg"
                frame.write_bytes(b"\xff\xd8\xff\xd9")
                frame_sha256 = hashlib.sha256(frame.read_bytes()).hexdigest()
                frame_index = {
                    "schema_version": "trafficvisson.dense-frame-index.v1",
                    "frame_count": 1,
                    "frames": [
                        {
                            "frame_id": "restart-frame-00000001",
                            "frame_index": 0,
                            "timestamp_seconds": 0.0,
                            "relative_path": (
                                "frames/frame-00000000.jpg"
                            ),
                            "size_bytes": frame.stat().st_size,
                            "sha256": frame_sha256,
                            "media_type": "image/jpeg",
                            "evidence_status": "unreviewed_candidate",
                        }
                    ],
                    "provenance": {
                        "source_name": source.name,
                        "source_sha256": context["checkpoints"]["sha256"][
                            "source_sha256"
                        ],
                        "sampling_fps": 2,
                        "review_only": True,
                    },
                }
                (run_root / "frame-index.json").write_text(
                    json.dumps(
                        frame_index,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                return PipelineStageResult(
                    "success",
                    checkpoint={
                        "dense_run_id": run_id,
                        "source_sha256": context["checkpoints"]["sha256"][
                            "source_sha256"
                        ],
                        "sampling_fps": 2,
                        "frame_index_artifact": (
                            f"pipeline-dense:{context['job_id']}:frame-index"
                        ),
                    },
                    metrics={
                        "frame_count": 1,
                        "indexed_duration_seconds": 0.0,
                        "truncated": False,
                    },
                )
            if stage == "candidate_rescan_5_10fps":
                if self.block_candidate:
                    self.candidate_started.set()
                    while not cancel():
                        time.sleep(0.01)
                    return PipelineStageResult(
                        "cancelled",
                        error_code="test_restart_boundary",
                    )
                return self.bridge.execute(stage, context, cancel)
            if stage == "question_queue":
                return PipelineStageResult(
                    "success",
                    checkpoint={
                        "question_queue_revision": "restart-questions-v1",
                        "question_candidate_ids": ["restart-candidate-0001"],
                        "annotation_count": 0,
                        "incident_count": 0,
                    },
                    metrics={"question_count": 1},
                )
            return PipelineStageResult(
                "success",
                checkpoint={
                    "state": "degraded_review_only",
                    "annotation_count": 0,
                    "incident_count": 0,
                },
                metrics={"completed": 1},
            )

        def cancel(self, context):
            self.bridge.cancel(context)

        def validate_review_completion(
            self, _context, review_completion_id
        ):
            return {
                "review_completion_id": review_completion_id,
                "all_required_questions_answered": True,
                "answered_question_count": 1,
                "server_answer_ledger_revision": "restart-ledger-v1",
            }

    first_media = MediaIntakeService(
        ffprobe_path=ffprobe,
        minimum_interval_seconds=0,
    )
    snapshot = first_media.register_local_file(project_id, str(source))
    first_bridge = BridgePipelineRuntime(
        first_media,
        data_root=data_root,
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        vision_status=lambda: {
            "enabled": False,
            "warning_code": "model_pack_not_configured",
        },
        analysis_jobs=object(),
        command_proposal_generator=ReadyDinoStatus(),
        experimental_proposals=object(),
    )
    first_runtime = RestartRuntime(first_bridge, block_candidate=True)
    first = PipelineJobService(first_runtime, data_root=data_root)
    _configure(first, project_id)
    started, replayed = first.start(
        project_id,
        snapshot_id=snapshot["snapshot_id"],
        file_name=source.name,
        rights_confirmed=True,
        idempotency_key="pipeline-test-real-media-restart-0001",
    )
    assert replayed is False
    assert first_runtime.candidate_started.wait(10), first.get(
        project_id, started["job_id"]
    )
    before_restart = first.get(project_id, started["job_id"])
    assert before_restart["current_stage"] == "candidate_rescan_5_10fps"
    dense_stage = next(
        item
        for item in before_restart["stages"]
        if item["name"] == "dense_index_2fps"
    )
    assert dense_stage["status"] == "success"
    dense_run_id = dense_stage["checkpoint"]["dense_run_id"]
    dense_index = (
        data_root
        / "projects"
        / str(project_id)
        / "pipeline"
        / "dense"
        / str(started["job_id"])
        / dense_run_id
        / "frame-index.json"
    )
    assert dense_index.is_file()
    hash_stage = next(
        item
        for item in before_restart["stages"]
        if item["name"] == "sha256"
    )
    hash_job_id = hash_stage["checkpoint"]["hash_job_id"]
    private_grant = (
        data_root
        / "projects"
        / str(project_id)
        / "pipeline"
        / "private-media-grants"
        / f"{hash_job_id}.json"
    )
    assert private_grant.is_file()
    assert str(source) not in json.dumps(
        before_restart, ensure_ascii=False, default=str
    )
    first.shutdown()
    first_media.shutdown()

    second_media = MediaIntakeService(
        ffprobe_path=ffprobe,
        minimum_interval_seconds=0,
    )
    with pytest.raises(HTTPException) as unresolved:
        second_media.resolve_completed_media(
            project_id,
            snapshot["snapshot_id"],
            source.name,
            UUID(hash_stage["checkpoint"]["hash_job_id"]),
            hash_stage["checkpoint"]["source_sha256"],
        )
    assert unresolved.value.status_code == 404

    class RestartAnalysisJobs:
        def __init__(self, media):
            self.media = media
            self.row = None
            self.start_attempts = 0

        def list(self, requested_project_id, *, limit):
            assert requested_project_id == project_id
            assert limit == 128
            return [] if self.row is None else [self.row]

        def start(
            self,
            requested_project_id,
            snapshot_id,
            file_name,
            requested_hash_job_id,
            source_sha256,
        ):
            self.start_attempts += 1
            self.media.resolve_completed_media(
                requested_project_id,
                snapshot_id,
                file_name,
                requested_hash_job_id,
                source_sha256,
            )
            self.row = {
                "job_id": uuid4(),
                "project_id": requested_project_id,
                "snapshot_id": snapshot_id,
                "hash_job_id": requested_hash_job_id,
                "file_name": file_name,
                "source_sha256": source_sha256,
                "status": "completed",
                "result": {
                    "candidates": [
                        {
                            "candidate_id": "restart-candidate-0001",
                            "representative_timestamp_seconds": 0.5,
                        }
                    ],
                    "provenance": {
                        "effective_cadence_seconds": 15.0,
                    },
                },
            }
            return self.row

        def get(self, requested_project_id, requested_job_id):
            assert self.row is not None
            assert requested_project_id == project_id
            assert requested_job_id == self.row["job_id"]
            return self.row

        def cancel(self, _project_id, _job_id):
            return None

    restarted_analysis = RestartAnalysisJobs(second_media)
    second_bridge = BridgePipelineRuntime(
        second_media,
        data_root=data_root,
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        vision_status=lambda: {
            "enabled": False,
            "warning_code": "model_pack_not_configured",
        },
        analysis_jobs=restarted_analysis,
        command_proposal_generator=ReadyDinoStatus(),
        experimental_proposals=object(),
    )
    second_runtime = RestartRuntime(second_bridge, block_candidate=False)
    second = PipelineJobService(second_runtime, data_root=data_root)
    waiting = _wait(
        second,
        project_id,
        started["job_id"],
        {"awaiting_review", "failed"},
        timeout=10,
    )
    assert waiting["status"] == "awaiting_review", waiting
    assert waiting["job_id"] == started["job_id"]
    assert waiting["snapshot_id"] == snapshot["snapshot_id"]
    assert waiting["recovered_after_restart"] is True
    assert waiting["provenance"]["source"]["snapshot_id"] == str(
        snapshot["snapshot_id"]
    )
    assert restarted_analysis.start_attempts == 2
    assert second_runtime.calls[0] == "candidate_rescan_5_10fps"
    assert "dense_index_2fps" not in second_runtime.calls
    assert dense_index.is_file()
    final_dense_stage = next(
        item
        for item in waiting["stages"]
        if item["name"] == "dense_index_2fps"
    )
    assert final_dense_stage["attempts"] == 1
    question_stage = next(
        item
        for item in waiting["stages"]
        if item["name"] == "question_queue"
    )
    assert question_stage["checkpoint"]["annotation_count"] == 0
    assert question_stage["checkpoint"]["incident_count"] == 0
    assert str(source) not in json.dumps(
        waiting, ensure_ascii=False, default=str
    )
    second.shutdown()
    second_media.shutdown()


def test_legacy_private_grant_migration_is_full_hash_verified_and_path_private(
    tmp_path,
):
    source = (tmp_path / "legacy-drive.mp4").resolve()
    source.write_bytes(b"previously authorized legacy video")
    source_stat = source.stat()
    created_time_ns = int(
        getattr(
            source_stat,
            "st_birthtime_ns",
            source_stat.st_ctime_ns,
        )
    )
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    data_root = (tmp_path / "bridge data").resolve()
    data_root.mkdir()
    project_id = uuid4()
    snapshot_id = uuid4()
    hash_job_id = uuid4()

    media = MediaIntakeService(minimum_interval_seconds=0)
    runtime = BridgePipelineRuntime(media, data_root=data_root)
    migrated = runtime.migrate_legacy_completed_media_grant(
        project_id=project_id,
        snapshot_id=snapshot_id,
        hash_job_id=hash_job_id,
        file_name=source.name,
        authorized_media_path=source,
        size_bytes=source_stat.st_size,
        created_time_ns=created_time_ns,
        modified_time_ns=source_stat.st_mtime_ns,
        source_sha256=source_sha256,
    )
    assert migrated["status"] == "migrated"
    assert migrated["path_exposed"] is False
    assert str(source) not in json.dumps(migrated, ensure_ascii=False)
    grant_record = (
        data_root
        / "projects"
        / str(project_id)
        / "pipeline"
        / "private-media-grants"
        / f"{hash_job_id}.json"
    )
    assert grant_record.is_file()
    media.shutdown()

    restarted_media = MediaIntakeService(minimum_interval_seconds=0)
    restarted_runtime = BridgePipelineRuntime(
        restarted_media,
        data_root=data_root,
    )
    restored = restarted_runtime._restore_private_media_grant(
        project_id=project_id,
        snapshot_id=snapshot_id,
        hash_job_id=hash_job_id,
        file_name=source.name,
        source_sha256=source_sha256,
        total_bytes=source_stat.st_size,
        cancel=lambda: False,
    )
    assert restored.sha256_lower == source_sha256
    assert restored.media_path == source
    restarted_media.shutdown()

    source.write_bytes(b"tampered legacy video with same bytes!")
    rejected_media = MediaIntakeService(minimum_interval_seconds=0)
    rejected_runtime = BridgePipelineRuntime(
        rejected_media,
        data_root=data_root,
    )
    with pytest.raises(PipelineStageFailure) as rejected:
        rejected_runtime._restore_private_media_grant(
            project_id=project_id,
            snapshot_id=snapshot_id,
            hash_job_id=hash_job_id,
            file_name=source.name,
            source_sha256=source_sha256,
            total_bytes=source_stat.st_size,
            cancel=lambda: False,
        )
    assert (
        rejected.value.code
        == "private_media_grant_revalidation_failed"
    )
    rejected_media.shutdown()


def test_blocked_job_restart_restores_verified_record_sha256(tmp_path):
    root = tmp_path.resolve()
    first_runtime = FakeRuntime()
    first_runtime.model_ready = False
    first = PipelineJobService(first_runtime, data_root=root)
    project_id = uuid4()
    _configure(first, project_id)
    started, _ = _start(first, project_id, suffix="blocked-restart")
    blocked = _wait(
        first, project_id, started["job_id"], {"blocked"}
    )
    assert blocked["current_stage"] == "candidate_rescan_5_10fps"
    assert blocked["outcome_code"] == "model_unavailable"
    assert len(blocked["record_sha256"]) == 64
    expected_record_sha256 = blocked["record_sha256"]
    first.shutdown()

    second = PipelineJobService(FakeRuntime(), data_root=root)
    reloaded = second.get(project_id, started["job_id"])
    assert reloaded["status"] == "blocked"
    assert reloaded["current_stage"] == "candidate_rescan_5_10fps"
    assert reloaded["record_sha256"] == expected_record_sha256
    assert second.recovery_summary()["jobs_loaded"] == 1
    assert second.recovery_summary()["jobs_requeued"] == 0
    second.shutdown()


def test_restart_accepts_bounded_sam2_polygon_checkpoint_depth(tmp_path):
    class NestedSam2Runtime(FakeRuntime):
        def execute(self, stage, context, cancel):
            if stage == "sam2_segmentation_tracking":
                return PipelineStageResult(
                    "success",
                    checkpoint={
                        "proposals": [
                            {
                                "tracked_frames": [
                                    {
                                        "polygon": [
                                            [0.1, 0.2],
                                            [0.3, 0.4],
                                            [0.5, 0.6],
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    metrics={"mask_count": 1},
                )
            if stage == "temporal_merge":
                raise PipelineStageFailure("temporal_merge_failed")
            return super().execute(stage, context, cancel)

    root = tmp_path.resolve()
    first = PipelineJobService(NestedSam2Runtime(), data_root=root)
    project_id = uuid4()
    _configure(first, project_id)
    started, _ = _start(first, project_id, suffix="nested-sam2")
    failed = _wait(first, project_id, started["job_id"], {"failed"})
    assert failed["current_stage"] == "temporal_merge"
    assert failed["error_code"] == "temporal_merge_failed"
    first.shutdown()

    restarted = PipelineJobService(FakeRuntime(), data_root=root)
    restored = restarted.get(project_id, started["job_id"])
    assert restored["status"] == "failed"
    assert restored["current_stage"] == "temporal_merge"
    sam2 = next(
        stage
        for stage in restored["stages"]
        if stage["name"] == "sam2_segmentation_tracking"
    )
    assert sam2["checkpoint"]["proposals"][0][
        "tracked_frames"
    ][0]["polygon"][2] == [0.5, 0.6]
    assert restarted.recovery_summary()["jobs_loaded"] == 1
    assert restarted.recovery_summary()["records_rejected"] == 0
    restarted.shutdown()


def test_project_scope_and_cancel_are_enforced(tmp_path):
    runtime = FakeRuntime()
    runtime.block_stage = "intake_validation"
    service = PipelineJobService(runtime, data_root=tmp_path.resolve())
    project_id = uuid4()
    _configure(service, project_id)
    started, _ = _start(service, project_id, suffix="cancel")
    assert runtime.block_started.wait(1)
    with pytest.raises(Exception):
        service.get(uuid4(), started["job_id"])
    service.cancel(project_id, started["job_id"])
    runtime.block_release.set()
    cancelled = _wait(
        service, project_id, started["job_id"], {"cancelled"}
    )
    assert cancelled["outcome_code"] == "cancelled"
    assert runtime.cancel_calls == 1
    service.shutdown()


def test_bridge_runtime_matches_each_selected_local_pack_before_ready(tmp_path):
    class UnusedMediaIntake:
        pass

    status = {
        "enabled": True,
        "pack_id": "road-detector-v1",
        "capabilities": [
            "candidate_rescan_5_10fps",
            "detection",
            "ocr",
            "segmentation",
            "tracking",
        ],
        "warning_code": None,
    }
    runtime = BridgePipelineRuntime(
        UnusedMediaIntake(),
        data_root=tmp_path.resolve(),
        vision_status=lambda: status,
    )
    profile = {
        "detector_pack_id": "road-detector-v1",
        "ocr_pack_id": "different-ocr-v1",
        "sam2_pack_id": "different-sam-v1",
        "report_provider": "codex",
    }
    capabilities = runtime.capability_snapshot(profile)
    assert capabilities["candidate_rescan_5_10fps"] == "ready"
    assert capabilities["detector"] == "ready"
    assert capabilities["ocr"] == "missing"
    assert capabilities["sam2_segmentation_tracking"] == "missing"
    assert capabilities["bulk_frame_provider"] == "local_models_only"


def _embedded_local_inference(
    *,
    status="classified",
    mode="local_model",
    predictions=None,
    ocr_observations=None,
):
    return {
        "schema_version": "trafficvisson.vision-inference.v1",
        "mode": mode,
        "status": status,
        "provider": "onnx_yolo_v8",
        "pack_id": "road-ocr-v1",
        "pack_version": "1.0.0",
        "manifest_sha256": hashlib.sha256(
            b"road-ocr-v1"
        ).hexdigest(),
        "capabilities": ["defect_detection", "ocr"],
        "predictions": (
            []
            if predictions is None
            else predictions
        ),
        "ocr_observations": (
            []
            if ocr_observations is None
            else ocr_observations
        ),
        "duration_ms": 12,
        "warning_code": (
            "model_inference_failed"
            if status == "failed"
            else (
                "model_pack_not_configured"
                if status == "provider_unavailable"
                else None
            )
        ),
    }


def test_bridge_runtime_connects_separate_detector_and_ocr_to_question_queue(
    tmp_path,
):
    project_id = uuid4()
    snapshot_id = uuid4()
    analysis_job_id = uuid4()
    pipeline_job_id = uuid4()
    hash_job_id = uuid4()
    source_sha256 = hashlib.sha256(b"local-source").hexdigest()
    candidates = []
    for index, candidate_id in enumerate(("a" * 20, "b" * 20)):
        candidates.append(
            {
                "candidate_id": candidate_id,
                "representative_timestamp_seconds": 1.0 + index * 0.5,
                "ai_inference": _embedded_local_inference(
                    predictions=[
                        {
                            "issue_type": "sign_damage",
                            "confidence": 0.82 - index * 0.04,
                            "box": {
                                "x": 0.2,
                                "y": 0.2,
                                "width": 0.2,
                                "height": 0.2,
                            },
                            "source_label": "damaged sign",
                            "calibration": "model_score",
                        }
                    ],
                    ocr_observations=[
                        {
                            "text": "서울",
                            "confidence": 0.88 - index * 0.03,
                            "box": {
                                "x": 0.25,
                                "y": 0.25,
                                "width": 0.15,
                                "height": 0.1,
                            },
                            "kind": "sign_or_tunnel_text_unverified",
                        }
                    ],
                ),
            }
        )
    analysis_row = {
        "job_id": analysis_job_id,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "hash_job_id": hash_job_id,
        "file_name": "drive.mp4",
        "source_sha256": source_sha256,
        "status": "completed",
        "result": {
            "candidates": candidates,
            "provenance": {"effective_cadence_seconds": 15.0},
        },
    }
    run_directory = (tmp_path / "candidate-run").resolve()
    run_directory.mkdir()
    resolved_images = []
    for candidate in candidates:
        image_path = run_directory / (
            f"{candidate['candidate_id']}.jpg"
        )
        image_path.write_bytes(b"\xff\xd8\xfffixture")
        resolved_images.append(
            ResolvedAnalysisCandidateImage(
                candidate_id=candidate["candidate_id"],
                path=image_path,
                media_type="image/jpeg",
                size_bytes=image_path.stat().st_size,
                sha256=hashlib.sha256(
                    image_path.read_bytes()
                ).hexdigest(),
            )
        )

    class FakeAnalysisJobs:
        def list(self, requested_project_id, *, limit):
            assert requested_project_id == project_id
            assert limit == 128
            return [analysis_row]

        def get(self, requested_project_id, requested_job_id):
            assert requested_project_id == project_id
            assert requested_job_id == analysis_job_id
            return analysis_row

        def resolve_candidate_image_batch(
            self,
            requested_project_id,
            requested_job_id,
            candidate_ids,
        ):
            assert requested_project_id == project_id
            assert requested_job_id == analysis_job_id
            assert list(candidate_ids) == [
                candidate["candidate_id"]
                for candidate in candidates
            ]
            return ResolvedAnalysisCandidateImageBatch(
                run_directory=run_directory,
                images=tuple(resolved_images),
            )

    class FakeOcr:
        def infer_candidates(
            self, inference_candidates, _run_directory, *, cancel
        ):
            assert not cancel()
            return (
                {
                    candidate["candidate_id"]: {
                        "schema_version": (
                            "trafficvisson.vision-inference.v1"
                        ),
                        "mode": "local_model",
                        "status": "classified",
                        "provider": "easyocr_ocr_only",
                        "pack_id": "easyocr-v1",
                        "pack_version": "1.0.0",
                        "manifest_sha256": "e" * 64,
                        "capabilities": ["ocr"],
                        "predictions": [],
                        "ocr_observations": [
                            {
                                "text": "서울",
                                "confidence": 0.88 - index * 0.03,
                                "box": {
                                    "x": 0.25,
                                    "y": 0.25,
                                    "width": 0.15,
                                    "height": 0.1,
                                },
                                "kind": (
                                    "sign_or_tunnel_text_unverified"
                                ),
                            }
                        ],
                        "duration_ms": 12,
                        "warning_code": None,
                    }
                    for index, candidate in enumerate(
                        inference_candidates
                    )
                },
                {},
            )

    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        vision_status=lambda: {
            "enabled": True,
            "pack_id": "road-ocr-v1",
            "pack_version": "1.0.0",
            "manifest_sha256": hashlib.sha256(
                b"road-ocr-v1"
            ).hexdigest(),
            "capabilities": ["defect_detection", "ocr"],
            "warning_code": None,
        },
        ocr_inference_service=FakeOcr(),
        ocr_status=lambda: {
            "enabled": True,
            "mode": "local_model",
            "pack_id": "easyocr-v1",
            "pack_version": "1.0.0",
            "engine": "easyocr_ocr_only",
            "capabilities": ["ocr"],
            "manifest_sha256": "e" * 64,
            "warning_code": None,
        },
        analysis_jobs=FakeAnalysisJobs(),
    )
    profile = {
        "detector_pack_id": "road-ocr-v1",
        "ocr_pack_id": "easyocr-v1",
        "sam2_pack_id": "sam2-v1",
        "report_provider": "codex",
        "candidate_rescan_fps": 8,
        "review_mode": "precision",
        "question_policy": "learning_assist",
    }
    capabilities = runtime.capability_snapshot(profile)
    assert capabilities["detector"] == "ready"
    assert capabilities["ocr"] == "ready"
    assert capabilities["dino_review_only_boxes"] == "missing"
    context = {
        "job_id": str(pipeline_job_id),
        "project_id": str(project_id),
        "snapshot_id": str(snapshot_id),
        "file_name": "drive.mp4",
        "profile": profile,
        "capabilities": capabilities,
        "checkpoints": {
            "sha256": {
                "source_sha256": source_sha256,
                "hash_job_id": str(hash_job_id),
            }
        },
        "review_completion_id": None,
    }

    rescan = runtime.execute(
        "candidate_rescan_5_10fps", context, lambda: False
    )
    assert rescan.outcome == "success"
    context["checkpoints"]["candidate_rescan_5_10fps"] = dict(
        rescan.checkpoint
    )
    detection = runtime.execute(
        "detection_ocr", context, lambda: False
    )
    assert detection.outcome == "success"
    assert (
        detection.checkpoint["dedicated_detector_status"]
        == "completed"
    )
    assert detection.checkpoint["ocr_status"] == "completed"
    assert detection.checkpoint["dedicated_prediction_count"] == 2
    assert detection.checkpoint["ocr_observation_count"] == 2
    assert detection.checkpoint["detected_issue_types"] == [
        "sign_damage"
    ]
    assert detection.checkpoint["annotation_count"] == 0
    assert detection.checkpoint["incident_count"] == 0
    context["checkpoints"]["detection_ocr"] = dict(
        detection.checkpoint
    )

    sam2 = runtime.execute(
        "sam2_segmentation_tracking", context, lambda: False
    )
    context["checkpoints"]["sam2_segmentation_tracking"] = dict(
        sam2.checkpoint
    )
    temporal = runtime.execute(
        "temporal_merge", context, lambda: False
    )
    assert temporal.metrics["dedicated_detection_count"] == 2
    assert temporal.metrics["dino_detection_count"] == 0
    assert temporal.metrics["temporal_group_count"] == 1
    assert temporal.checkpoint["question_candidate_ids"] == [
        "a" * 20
    ]
    context["checkpoints"]["temporal_merge"] = dict(
        temporal.checkpoint
    )
    vision = runtime.execute(
        "vision_proposal", context, lambda: False
    )
    context["checkpoints"]["vision_proposal"] = dict(
        vision.checkpoint
    )
    queue = runtime.execute(
        "question_queue", context, lambda: False
    )
    assert queue.checkpoint["dedicated_detector_status"] == "completed"
    assert queue.checkpoint["ocr_status"] == "completed"
    assert queue.checkpoint["ocr_observation_count"] == 2
    assert "dedicated_road_detector" not in queue.checkpoint[
        "capability_missing"
    ]
    assert "korean_ocr" not in queue.checkpoint[
        "capability_missing"
    ]
    assert queue.checkpoint["automatic_labeling_allowed"] is False
    assert queue.checkpoint["annotation_count"] == 0
    assert queue.checkpoint["incident_count"] == 0


def test_embedded_local_vision_distinguishes_unavailable_failed_and_zero():
    profile = {
        "detector_pack_id": "road-ocr-v1",
        "ocr_pack_id": "road-ocr-v1",
    }
    base = {
        "candidate_id": "a" * 20,
        "representative_timestamp_seconds": 1.0,
    }

    missing = BridgePipelineRuntime._summarize_embedded_local_vision(
        [base],
        profile,
    )
    assert missing["detector"]["status"] == "model_unavailable"
    assert missing["ocr"]["status"] == "model_unavailable"

    unavailable = (
        BridgePipelineRuntime._summarize_embedded_local_vision(
            [
                {
                    **base,
                    "ai_inference": _embedded_local_inference(
                        status="provider_unavailable",
                        mode="scene_fallback",
                    ),
                }
            ],
            profile,
        )
    )
    assert unavailable["detector"]["status"] == "model_unavailable"
    assert unavailable["ocr"]["status"] == "model_unavailable"

    failed = BridgePipelineRuntime._summarize_embedded_local_vision(
        [
            {
                **base,
                "ai_inference": _embedded_local_inference(
                    status="failed",
                    mode="scene_fallback",
                ),
            }
        ],
        profile,
    )
    assert failed["detector"]["status"] == "failed"
    assert failed["ocr"]["status"] == "failed"

    zero = BridgePipelineRuntime._summarize_embedded_local_vision(
        [
            {
                **base,
                "ai_inference": _embedded_local_inference(
                    status="no_detection",
                ),
            }
        ],
        profile,
    )
    assert zero["detector"]["status"] == "zero_detections"
    assert zero["ocr"]["status"] == "zero_detections"

    with pytest.raises(PipelineStageFailure) as captured:
        BridgePipelineRuntime._summarize_embedded_local_vision(
            [
                {
                    **base,
                    "representative_timestamp_seconds": None,
                    "ai_inference": _embedded_local_inference(
                        predictions=[
                            {
                                "issue_type": "sign_damage",
                                "confidence": 0.8,
                                "box": None,
                                "source_label": "damaged sign",
                                "calibration": "model_score",
                            }
                        ]
                    ),
                }
            ],
            profile,
        )
    assert captured.value.code == "local_vision_inference_invalid"


def test_bridge_runtime_prepares_honest_dino_review_queue_without_full_pack(
    tmp_path,
):
    project_id = uuid4()
    snapshot_id = uuid4()
    analysis_job_id = uuid4()
    pipeline_job_id = uuid4()
    source_body = (
        b"\x00\x00\x00\x18ftypisom" + (b"\x01" * 4_096)
    )
    source_path = tmp_path / "drive.mp4"
    source_path.write_bytes(source_body)
    source_stat = source_path.stat()
    source_sha256 = hashlib.sha256(source_body).hexdigest()
    jpeg_body = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00"
        b"\x00\x01\x00\x01\x00\x00\xff\xd9"
    )
    frame_root = tmp_path / "analysis-frames"
    frame_root.mkdir()
    frame_paths = {}
    for candidate_id in ("a" * 20, "b" * 20):
        path = frame_root / f"{candidate_id}.jpg"
        path.write_bytes(jpeg_body)
        frame_paths[candidate_id] = path
    jpeg_sha256 = hashlib.sha256(jpeg_body).hexdigest()
    candidate_a = {
        "candidate_id": "a" * 20,
        "representative_timestamp_seconds": 1.0,
    }
    candidate_b = {
        "candidate_id": "b" * 20,
        "representative_timestamp_seconds": 1.5,
    }
    analysis_row = {
        "job_id": analysis_job_id,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "hash_job_id": uuid4(),
        "file_name": "drive.mp4",
        "source_sha256": source_sha256,
        "status": "completed",
        "result": {
            "candidates": [candidate_a, candidate_b],
            "provenance": {
                "effective_cadence_seconds": 15.0,
                "source_sha256": source_sha256,
            },
        },
    }

    class FakeAnalysisJobs:
        def list(self, requested_project_id, *, limit):
            assert requested_project_id == project_id
            assert limit == 128
            return [analysis_row]

        def get(self, requested_project_id, requested_job_id):
            assert requested_project_id == project_id
            assert requested_job_id == analysis_job_id
            return analysis_row

        def resolve_artifact(
            self,
            requested_project_id,
            requested_job_id,
            candidate_id,
            kind,
        ):
            assert requested_project_id == project_id
            assert requested_job_id == analysis_job_id
            assert kind == "image"
            path = frame_paths[candidate_id]
            return ResolvedAnalysisArtifact(
                path=path,
                media_type="image/jpeg",
                size_bytes=len(jpeg_body),
                sha256=jpeg_sha256,
            )

    class FakeGenerator:
        def __init__(self):
            self.generate_calls = 0
            self.taxonomy_calls = 0

        def status(self):
            return {
                "state": "ready",
                "error_code": None,
                "review_only": True,
                "bulk_frame_detection_provider": "local_model",
                "pack_id": "dino-review",
                "pack_version": "1.0",
                "manifest_sha256": hashlib.sha256(b"dino").hexdigest(),
                "supported_issue_types": ["road_debris"],
            }

        def generate(self, *, command_plan, candidates, **_kwargs):
            self.generate_calls += 1
            proposals = ()
            if command_plan["issue_type"] == "road_debris":
                proposals = tuple(
                    {
                        "candidate_id": candidate["candidate_id"],
                        "status": "classified",
                        "predictions": [
                            {
                                "issue_type": "road_debris",
                                "model_score": (
                                    0.8
                                    if candidate["candidate_id"] == "a" * 20
                                    else 0.9
                                ),
                                "relative_score": None,
                                "box": {
                                    "x": 0.2,
                                    "y": 0.2,
                                    "width": 0.2,
                                    "height": 0.2,
                                },
                                "source_label": "road debris",
                                "calibration": "model_score",
                            }
                        ],
                    }
                    for candidate in candidates
                )
            return SimpleNamespace(
                available=True,
                generated=True,
                model={
                    "provider": "transformers_grounding_dino",
                    "pack_id": "dino-review",
                    "pack_version": "1.0",
                    "manifest_sha256": hashlib.sha256(b"dino").hexdigest(),
                },
                evaluation={
                    "evaluation_id": "source",
                    "producer": "test",
                    "producer_version": "1.0",
                },
                proposals=proposals,
            )

        def generate_taxonomy(
            self,
            *,
            command_plans,
            candidates,
            **_kwargs,
        ):
            self.taxonomy_calls += 1
            assert len(command_plans) == 1
            assert command_plans[0]["issue_type"] == "road_debris"
            proposals = tuple(
                {
                    "candidate_id": candidate["candidate_id"],
                    "status": "classified",
                    "predictions": [
                        {
                            "issue_type": "road_debris",
                            "model_score": (
                                0.8
                                if candidate["candidate_id"] == "a" * 20
                                else 0.9
                            ),
                            "relative_score": None,
                            "box": {
                                "x": 0.2,
                                "y": 0.2,
                                "width": 0.2,
                                "height": 0.2,
                            },
                            "source_label": "road debris",
                            "calibration": "model_score",
                        }
                    ],
                }
                for candidate in candidates
            )
            return SimpleNamespace(
                available=True,
                generated=True,
                model={
                    "provider": "transformers_grounding_dino",
                    "pack_id": "dino-review",
                    "pack_version": "1.0",
                    "manifest_sha256": hashlib.sha256(
                        b"dino"
                    ).hexdigest(),
                },
                evaluation={
                    "evaluation_id": "source",
                    "producer": "test",
                    "producer_version": "1.0",
                },
                proposals=proposals,
            )

    class FakeProposalStore:
        def __init__(self):
            self.evaluation_id = None
            self.rows = {}

        def record_run(
            self,
            _project_id,
            _analysis_job_id,
            *,
            evaluation,
            proposals,
            **_kwargs,
        ):
            self.evaluation_id = evaluation["evaluation_id"]
            self.rows = {
                row["candidate_id"]: {
                    "candidate_id": row["candidate_id"],
                    "predictions": row["predictions"],
                }
                for row in proposals
            }
            return list(self.rows.values())

        def latest_for_job(
            self,
            _project_id,
            _analysis_job_id,
            *,
            evaluation_id=None,
        ):
            if (
                evaluation_id is not None
                and evaluation_id != self.evaluation_id
            ):
                return {}
            return self.rows

    class FakeWeakCases:
        def search(self, **_kwargs):
            return {
                "capability_state": "weak_reference_only",
                "cases": [],
            }

    class FakeSam2:
        def status(self):
            return {
                "state": "ready",
                "model_manifest": {
                    "pack_id": "sam2",
                    "pack_version": "1.0",
                    "manifest_sha256": hashlib.sha256(
                        b"sam2-manifest"
                    ).hexdigest(),
                    "model_artifact_sha256": hashlib.sha256(
                        b"sam2-artifact"
                    ).hexdigest(),
                    "provenance_verified": True,
                    "review_only": True,
                },
            }

        def refine(self, *, image_path, point):
            assert image_path.exists()
            assert point == pytest.approx((0.3, 0.3))
            return {
                "polygon": [
                    [0.2, 0.2],
                    [0.4, 0.2],
                    [0.4, 0.4],
                    [0.2, 0.4],
                ]
            }

        def track(
            self,
            *,
            frame_paths,
            prompt_frame_index,
            point,
        ):
            assert all(path.exists() for path in frame_paths)
            assert 0 <= prompt_frame_index < len(frame_paths)
            assert point == pytest.approx((0.3, 0.3))
            return {
                "frames": [
                    {
                        "frame_index": index,
                        "polygon": [
                            [0.2, 0.2],
                            [0.4, 0.2],
                            [0.4, 0.4],
                            [0.2, 0.4],
                        ],
                    }
                    for index in range(len(frame_paths))
                ]
            }

    analysis_jobs = FakeAnalysisJobs()
    grant = CompletedMediaGrant(
        project_id=project_id,
        snapshot_id=snapshot_id,
        hash_job_id=analysis_row["hash_job_id"],
        file_name=source_path.name,
        media_path=source_path,
        size_bytes=source_stat.st_size,
        modified_time_ns=source_stat.st_mtime_ns,
        sha256_lower=source_sha256,
    )
    ffmpeg_path = tmp_path / "ffmpeg.exe"
    ffmpeg_path.write_bytes(b"test-only")

    def resolve_binding(
        requested_project_id,
        requested_analysis_job_id,
        requested_pipeline_job_id,
        requested_source_sha256,
    ):
        assert requested_project_id == project_id
        assert requested_analysis_job_id == analysis_job_id
        assert requested_pipeline_job_id == pipeline_job_id
        assert requested_source_sha256 == source_sha256
        return ResolvedCandidateEvidenceBinding(
            project_id=project_id,
            analysis_job_id=analysis_job_id,
            pipeline_job_id=pipeline_job_id,
            analysis_status="completed",
            grant=grant,
        )

    def resolve_review_frame(
        requested_project_id,
        requested_analysis_job_id,
        requested_pipeline_job_id,
        discovery_sha256,
        frame_id,
        provenance,
    ):
        assert requested_project_id == project_id
        assert requested_analysis_job_id == analysis_job_id
        assert requested_pipeline_job_id == pipeline_job_id
        assert provenance["evidence_source"] == (
            "analysis_representative_frames"
        )
        path = frame_paths[frame_id]
        return ResolvedDenseCandidateFrame(
            project_id=project_id,
            dense_index_sha256=discovery_sha256,
            frame_id=frame_id,
            frames_root=frame_root,
            path=path,
            media_type="image/jpeg",
            size_bytes=len(jpeg_body),
            sha256=jpeg_sha256,
        )

    def run_fake_ffmpeg(command, _cancel):
        command.output_path.write_bytes(
            b"\x00\x00\x00\x18ftypisom" + (b"\x00" * 116)
        )
        return CandidateEvidenceMediaCommandOutput(
            0, b"", b""
        )

    overlay_root = (tmp_path / "candidate-overlays").resolve()
    overlays = CandidateEvidenceOverlayStore(
        overlay_root,
        binding_resolver=resolve_binding,
        frame_resolver=lambda *_args: (_ for _ in ()).throw(
            AssertionError("dense resolver is not used")
        ),
        review_frame_resolver=resolve_review_frame,
        ffmpeg_path=ffmpeg_path.resolve(),
        command_runner=run_fake_ffmpeg,
    )
    registry = CandidateEvidenceRegistry(
        analysis_jobs,
        overlays,
        overlay_root=overlay_root,
    )
    proposal_store = FakeProposalStore()
    labels = CandidateLabelStore(
        (tmp_path / "labels").resolve(),
        registry.resolve_candidate,
    )
    catalog_registrations = []

    def register_questions(
        requested_project_id,
        requested_analysis_job_id,
        requested_source_sha256,
        questions,
    ):
        assert requested_project_id == project_id
        assert requested_analysis_job_id == analysis_job_id
        assert requested_source_sha256 == source_sha256
        catalog_registrations.append(list(questions))
        return {
            "project_id": str(project_id),
            "analysis_job_id": str(analysis_job_id),
            "source_sha256": source_sha256,
            "question_count": len(questions),
            "created_count": len(questions),
            "question_set_sha256": "e" * 64,
        }

    generator = FakeGenerator()
    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        vision_status=lambda: {
            "enabled": False,
            "warning_code": "model_pack_not_configured",
        },
        analysis_jobs=FakeAnalysisJobs(),
        command_proposal_generator=generator,
        experimental_proposals=proposal_store,
        candidate_labels=labels,
        candidate_evidence_overlays=overlays,
        candidate_evidence_registry=registry,
        question_catalog_projector=register_questions,
        weak_case_catalog=FakeWeakCases(),
        sam2_status=FakeSam2().status,
        sam2_adapter=FakeSam2(),
    )
    profile = {
        "detector_pack_id": "full-road-pack",
        "ocr_pack_id": "korean-ocr",
        "sam2_pack_id": "sam2",
        "report_provider": "codex",
        "candidate_rescan_fps": 8,
        "review_mode": "precision",
        "question_policy": "learning_assist",
    }
    capabilities = runtime.capability_snapshot(profile)
    assert capabilities["dino_review_only_boxes"] == "ready"
    assert capabilities["sam2_segmentation_tracking"] == "ready"
    context = {
        "job_id": str(pipeline_job_id),
        "project_id": str(project_id),
        "snapshot_id": str(snapshot_id),
        "file_name": "drive.mp4",
        "profile": profile,
        "capabilities": capabilities,
        "checkpoints": {
            "sha256": {
                "source_sha256": source_sha256,
                "hash_job_id": str(analysis_row["hash_job_id"]),
            }
        },
        "review_completion_id": None,
    }
    rescan = runtime.execute(
        "candidate_rescan_5_10fps", context, lambda: False
    )
    assert rescan.outcome == "success"
    assert rescan.checkpoint["high_frequency_rescan_performed"] is False
    context["checkpoints"]["candidate_rescan_5_10fps"] = dict(
        rescan.checkpoint
    )

    detection = runtime.execute(
        "detection_ocr", context, lambda: False
    )
    assert detection.outcome == "success"
    assert detection.checkpoint["dino_review_only"] is True
    assert detection.checkpoint["ocr_status"] == "model_unavailable"
    assert detection.checkpoint["annotation_count"] == 0
    assert detection.checkpoint["incident_count"] == 0
    assert detection.checkpoint["evaluated_issue_types"] == [
        "road_debris"
    ]
    assert detection.metrics["dino_frame_proposal_count"] == 2
    assert generator.taxonomy_calls == 1
    assert generator.generate_calls == 0
    context["checkpoints"]["detection_ocr"] = dict(
        detection.checkpoint
    )

    sam2 = runtime.execute(
        "sam2_segmentation_tracking", context, lambda: False
    )
    assert sam2.checkpoint["mask_generated"] is True
    assert sam2.metrics["dino_prompt_count"] == 2
    assert sam2.metrics["dino_mask_count"] == 2
    assert sam2.metrics["dino_failure_count"] == 0
    assert sam2.metrics["dino_box_only_review_count"] == 0
    assert {
        binding["prompt_evidence_ref"]
        for binding in sam2.checkpoint["dino_prompt_bindings"]
    } == {
        "candidate:" + ("a" * 20),
        "candidate:" + ("b" * 20),
    }
    assert all(
        binding["evidence_image_sha256"] == jpeg_sha256
        and binding["issue_type"] == "road_debris"
        and binding["normalized_box"]
        == {
            "x": 0.2,
            "y": 0.2,
            "width": 0.2,
            "height": 0.2,
        }
        and binding["sam2_status"] == "available"
        for binding in sam2.checkpoint["dino_prompt_bindings"]
    )
    context["checkpoints"]["sam2_segmentation_tracking"] = dict(
        sam2.checkpoint
    )
    temporal = runtime.execute(
        "temporal_merge", context, lambda: False
    )
    assert temporal.metrics["input_detection_count"] == 2
    assert temporal.metrics["temporal_group_count"] == 1
    assert temporal.metrics["dino_detection_count"] == 2
    assert temporal.metrics["dino_temporal_group_count"] == 1
    assert len(temporal.checkpoint["question_candidate_ids"]) == 1
    synthetic_candidate_id = temporal.checkpoint[
        "question_candidate_ids"
    ][0]
    assert synthetic_candidate_id not in {"a" * 20, "b" * 20}
    dino_group = temporal.checkpoint[
        "dino_temporal_groups_by_candidate"
    ][synthetic_candidate_id]
    assert dino_group["merged_frame_count"] == 2
    assert len(dino_group["sam2_prompt_candidate_ids"]) == 2
    assert dino_group["peak_evidence"]["timestamp_seconds"] == 1.5
    assert dino_group["peak_evidence"]["image_sha256"] == jpeg_sha256
    assert dino_group["peak_evidence"]["normalized_box"] == {
        "x": 0.2,
        "y": 0.2,
        "width": 0.2,
        "height": 0.2,
    }
    assert temporal.checkpoint[
        "dino_candidate_evidence_overlay"
    ]["candidate_count"] == 1
    context["checkpoints"]["temporal_merge"] = dict(
        temporal.checkpoint
    )

    projection_root = (tmp_path / "projection").resolve()
    projection_fixture = _write_completed_analysis(
        projection_root,
        project_id=project_id,
        job_id=analysis_job_id,
        source_seed="dino-projection",
        candidate_count=2,
    )
    projection_payload = projection_fixture["payload"]
    projection_payload["source_sha256"] = source_sha256
    projection_payload["result"]["provenance"].update(
        {
            "source_fingerprint": source_sha256,
            "source_sha256": source_sha256,
        }
    )
    projection_encoded = json.dumps(
        projection_payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    projection_fixture["record_path"].write_bytes(
        projection_encoded
    )
    projection_fixture["digest_path"].write_text(
        hashlib.sha256(projection_encoded).hexdigest(),
        encoding="ascii",
    )
    catalog = ProjectCatalog(
        projection_root / "catalog.sqlite3",
        _project(project_id=project_id),
    )
    assert catalog.list_current_annotations(project_id) == []
    assert catalog.list_incidents(
        project_id, analysis_job_id=analysis_job_id
    ) == []
    resolved_question = labels.resolve_question(
        project_id,
        analysis_job_id,
        synthetic_candidate_id,
    )
    assert resolved_question["candidate_id"] == synthetic_candidate_id
    assert resolved_question["ai_original_candidate"][
        "issue_type"
    ] == "road_debris"
    registered = catalog.register_label_questions(
        project_id,
        analysis_job_id,
        source_sha256,
        [resolved_question],
    )
    assert registered["question_count"] == 1
    question_rows = catalog.list_label_questions(
        project_id, analysis_job_id=analysis_job_id
    )
    assert [
        row["candidate_id"] for row in question_rows
    ] == [synthetic_candidate_id]
    assert catalog.list_current_annotations(project_id) == []
    assert catalog.list_incidents(
        project_id, analysis_job_id=analysis_job_id
    ) == []

    weak_cases = runtime.execute(
        "similar_case_search", context, lambda: False
    )
    assert weak_cases.checkpoint["approved_reference_count"] == 0
    context["checkpoints"]["similar_case_search"] = dict(
        weak_cases.checkpoint
    )
    vision = runtime.execute(
        "vision_proposal", context, lambda: False
    )
    assert vision.checkpoint["llm_bulk_frame_detection_used"] is False
    context["checkpoints"]["vision_proposal"] = dict(
        vision.checkpoint
    )
    queue = runtime.execute(
        "question_queue", context, lambda: False
    )
    assert queue.metrics["question_count"] == 1
    assert queue.metrics["catalog_question_count"] == 1
    assert queue.checkpoint["question_catalog_state"] == "registered"
    assert queue.checkpoint["question_catalog_revision"] == "e" * 64
    assert queue.checkpoint["annotation_count"] == 0
    assert queue.checkpoint["incident_count"] == 0
    assert "korean_ocr" in queue.checkpoint["capability_missing"]
    assert len(catalog_registrations) == 1
    assert [
        item["candidate_id"]
        for item in catalog_registrations[0]
    ] == [synthetic_candidate_id]
    context["checkpoints"]["question_queue"] = dict(queue.checkpoint)
    legacy_checkpoint = {
        key: value
        for key, value in queue.checkpoint.items()
        if key
        not in {
            "detection_revision",
            "temporal_merge_revision",
            "question_catalog_state",
            "question_catalog_revision",
        }
    }
    context["checkpoints"]["question_queue"] = legacy_checkpoint
    migrated = runtime.reconcile_question_catalog(context)
    assert migrated.checkpoint["analysis_job_id"] == str(
        analysis_job_id
    )
    assert migrated.checkpoint["source_sha256"] == source_sha256
    assert migrated.checkpoint["question_candidate_ids"] == [
        synthetic_candidate_id
    ]
    assert migrated.checkpoint["question_queue_revision"] == (
        queue.checkpoint["question_queue_revision"]
    )
    assert migrated.checkpoint["question_catalog_state"] == "registered"
    assert migrated.checkpoint["question_catalog_revision"] == "e" * 64
    assert migrated.metrics["catalog_question_count"] == 1
    assert len(catalog_registrations) == 2
    context["checkpoints"]["question_queue"] = dict(
        migrated.checkpoint
    )

    assert labels.latest_answers(
        project_id, analysis_job_id=analysis_job_id
    ) == []
    recorded = labels.record_answer(
        project_id,
        analysis_job_id,
        synthetic_candidate_id,
        answer="yes",
        selected_issue_type="road_debris",
        severity="urgent",
        maintenance_action="remove_debris",
    )
    record_id = recorded["record_id"]
    record_hash = recorded["record_hash"]
    proof_line = "\0".join(
        (
            synthetic_candidate_id,
            record_id,
            record_hash,
            "1",
        )
    )
    completion_id = (
        "review:"
        + hashlib.sha256(proof_line.encode("utf-8")).hexdigest()
    )
    proof = runtime.validate_review_completion(
        context, completion_id
    )
    assert proof["all_required_questions_answered"] is True
    assert "annotation_count_before_projection" not in proof
    assert "incident_count_before_projection" not in proof
    projected = catalog.project_label_records(
        project_id,
        labels.audit_records(project_id),
        materialize_analysis_job_ids=[analysis_job_id],
    )
    assert len(projected) == 1
    annotations = catalog.list_current_annotations(project_id)
    incidents = catalog.list_incidents(
        project_id, analysis_job_id=analysis_job_id
    )
    assert len(annotations) == 1
    assert annotations[0]["candidate_id"] == synthetic_candidate_id
    assert annotations[0]["selected_issue_type"] == "road_debris"
    assert annotations[0]["severity"] == "urgent"
    # This is the initial detector-review/training path, not an independent
    # promoted-model survey with final QA provenance.
    assert incidents == []


def test_similar_case_stage_preserves_project_approved_provenance(
    tmp_path,
):
    project_id = uuid4()

    class FakeWeakCases:
        def search(self, **_kwargs):
            return {
                "capability_state": "weak_reference_only",
                "cases": [
                    {
                        "source_id": (
                            "sample-hwpx-3q-0123456789abcdef"
                        ),
                        "case_id": (
                            "hwpx-weak-case-" + "a" * 32
                        ),
                        "quarter": 3,
                        "document_sha256": "b" * 64,
                        "image_sha256": "c" * 64,
                        "index_manifest_sha256": "d" * 64,
                        "picture_target_id": (
                            "hwpx-picture-" + "e" * 24
                        ),
                        "cell_target_id": (
                            "hwpx-cell-" + "f" * 24
                        ),
                        "reference_state": "weak_unconfirmed",
                        "review_required": True,
                        "approved_reference": False,
                    }
                ],
            }

    class FakeApprovals:
        def decorate_search(self, actual_project_id, result):
            assert actual_project_id == project_id
            decorated = dict(result)
            decorated["capability_state"] = (
                "approved_reference_available"
            )
            decorated["cases"] = [
                {
                    **result["cases"][0],
                    "reference_state": "approved_reference",
                    "review_required": False,
                    "approved_reference": True,
                    "approval_record_sha256": "1" * 64,
                }
            ]
            decorated["approved_reference_count"] = 1
            return decorated

    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        weak_case_catalog=FakeWeakCases(),
        weak_case_approvals=FakeApprovals(),
    )
    result = runtime._search_weak_reference_cases(
        {
            "project_id": str(project_id),
            "checkpoints": {
                "detection_ocr": {
                    "detected_issue_types": [
                        "roadside_mowing_required"
                    ]
                }
            },
        }
    )

    assert result.checkpoint["approved_reference_count"] == 1
    # Approved historical cases may inform the current proposal, but they do
    # not make the current-video finding approved truth.
    assert result.checkpoint["review_only"] is True
    assert result.checkpoint["references"] == [
        {
            "source_id": "sample-hwpx-3q-0123456789abcdef",
            "case_id": "hwpx-weak-case-" + "a" * 32,
            "issue_type": "roadside_mowing_required",
            "quarter": 3,
            "document_sha256": "b" * 64,
            "image_sha256": "c" * 64,
            "index_manifest_sha256": "d" * 64,
            "picture_target_id": "hwpx-picture-" + "e" * 24,
            "cell_target_id": "hwpx-cell-" + "f" * 24,
                "reference_state": "approved_reference",
                "review_required": False,
                "approval_record_sha256": "1" * 64,
                "approved_reference": True,
                "training_eligible": False,
                "approved_issue_type": None,
                "approved_action": None,
                "caption": None,
                "route": None,
                "location": None,
                "opinion": None,
            }
        ]


def test_visual_reference_search_keeps_weak_taxonomy_fallible_and_uses_only_approved_truth(
    tmp_path,
):
    project_id = uuid4()
    analysis_job_id = uuid4()
    candidate_id = "a" * 20
    source_sha256 = hashlib.sha256(b"visual-reference-video").hexdigest()
    image_body = b"bounded candidate image evidence"
    image_path = tmp_path / "candidate.jpg"
    image_path.write_bytes(image_body)
    image_sha256 = hashlib.sha256(image_body).hexdigest()
    candidate = {
        "candidate_id": candidate_id,
        "representative_timestamp_seconds": 12.0,
        "ai_inference": _embedded_local_inference(
            predictions=[
                {
                    "issue_type": "sign_damage",
                    "confidence": 0.82,
                    "box": {
                        "x": 0.2,
                        "y": 0.2,
                        "width": 0.3,
                        "height": 0.3,
                    },
                    "source_label": "damaged sign",
                    "calibration": "model_score",
                }
            ],
        ),
    }
    analysis_row = {
        "job_id": analysis_job_id,
        "project_id": project_id,
        "source_sha256": source_sha256,
        "status": "completed",
        "result": {"candidates": [candidate]},
    }
    approval_sha256 = hashlib.sha256(
        b"approved-visual-reference"
    ).hexdigest()
    approved_identity = (
        "sample-hwpx-4q-approved",
        "hwpx-weak-case-" + "f" * 32,
    )

    def visual_case(
        index,
        *,
        approved=False,
        caption="포트홀 즉시 보수 필요",
    ):
        source_id = (
            approved_identity[0]
            if approved
            else f"sample-hwpx-3q-weak-{index}"
        )
        case_id = (
            approved_identity[1]
            if approved
            else "hwpx-weak-case-" + f"{index:032x}"
        )
        return {
            "rank": index,
            "source_id": source_id,
            "case_id": case_id,
            "quarter": 4 if approved else 3,
            "caption": caption,
            "route": "시험고속도로",
            "opinion": "캡션 문구만으로 유형과 조치를 확정하지 않음",
            "document_sha256": hashlib.sha256(
                f"document-{index}".encode()
            ).hexdigest(),
            "image_sha256": hashlib.sha256(
                f"image-{index}".encode()
            ).hexdigest(),
            "index_manifest_sha256": hashlib.sha256(
                f"index-{index}".encode()
            ).hexdigest(),
            "reference_state": (
                "approved_reference"
                if approved
                else "weak_unconfirmed"
            ),
            "review_required": not approved,
            "approved_reference": approved,
            "training_eligible": approved,
            "approval_record_sha256": (
                approval_sha256 if approved else None
            ),
            # Deliberately disagree with the sign-damage retrieval query.
            # These fields are trustworthy only on the approved case.
            "approved_issue_type": (
                "road_debris" if approved else None
            ),
            "approved_action": (
                "낙하물 제거" if approved else None
            ),
            "visual_cosine_score": 0.9 - index * 0.01,
            "text_structure_score": 0.5,
            "combined_similarity_score": 0.8 - index * 0.01,
        }

    cases = [
        visual_case(1),
        visual_case(2, caption="차선 재도색 필요"),
        visual_case(4, approved=True, caption="과거 승인 낙하물"),
        visual_case(3, caption="삭초 필요"),
    ]

    class FakeAnalysisJobs:
        def get(self, actual_project_id, actual_analysis_job_id):
            assert actual_project_id == project_id
            assert actual_analysis_job_id == analysis_job_id
            return analysis_row

        def resolve_artifact(
            self,
            actual_project_id,
            actual_analysis_job_id,
            actual_candidate_id,
            artifact_kind,
        ):
            assert actual_project_id == project_id
            assert actual_analysis_job_id == analysis_job_id
            assert actual_candidate_id == candidate_id
            assert artifact_kind == "image"
            return SimpleNamespace(
                path=image_path,
                media_type="image/jpeg",
                size_bytes=len(image_body),
                sha256=image_sha256,
            )

    class FakeVisualIndex:
        def build(self, actual_project_id):
            assert actual_project_id == project_id
            return {
                "index_manifest_sha256": hashlib.sha256(
                    b"visual-index"
                ).hexdigest(),
                "model_identity_sha256": hashlib.sha256(
                    b"visual-model"
                ).hexdigest(),
            }

        def query(
            self,
            actual_project_id,
            evidence,
            *,
            issue_type,
            query_text,
            limit,
        ):
            assert actual_project_id == project_id
            assert evidence.candidate_id == candidate_id
            assert evidence.expected_sha256 == image_sha256
            assert issue_type == "sign_damage"
            assert query_text is None
            assert limit == 3
            return {
                "issue_type": issue_type,
                "cases": list(cases[:limit]),
            }

    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        analysis_jobs=FakeAnalysisJobs(),
        weak_case_visual_index=FakeVisualIndex(),
    )
    context = {
        "profile": {
            "detector_pack_id": "road-ocr-v1",
            "ocr_pack_id": "road-ocr-v1",
        },
        "checkpoints": {
            "candidate_rescan_5_10fps": {
                "analysis_job_id": str(analysis_job_id),
                "source_sha256": source_sha256,
            },
            "detection_ocr": {},
            "temporal_merge": {
                "question_candidate_ids": [candidate_id],
            },
        },
    }

    search = runtime._search_visual_reference_cases(
        project_id,
        context,
    )

    matches = search.checkpoint["matches_by_candidate"][
        candidate_id
    ]
    assert len(matches) == 3
    weak_matches = [
        match
        for match in matches
        if match["reference_state"] == "weak_unconfirmed"
    ]
    assert len(weak_matches) == 2
    assert all(
        match["issue_type"] == "sign_damage"
        and match.get("maintenance_action") is None
        and match["approved_reference"] is False
        for match in weak_matches
    )
    assert {match["caption"] for match in weak_matches} == {
        "포트홀 즉시 보수 필요",
        "차선 재도색 필요",
    }
    approved_match = next(
        match
        for match in matches
        if match["reference_state"] == "approved_reference"
    )
    assert approved_match["issue_type"] == "road_debris"
    assert approved_match["maintenance_action"] == "낙하물 제거"
    assert search.checkpoint["approved_reference_count"] == 1
    assert search.checkpoint["automatic_annotation_allowed"] is False
    assert search.checkpoint["automatic_incident_creation_allowed"] is False

    approved_case = next(
        case for case in cases if case["approved_reference"] is True
    )
    approved_record = {
        "source_id": approved_identity[0],
        "case_id": approved_identity[1],
        "record_sha256": approval_sha256,
        "reference_state": "approved_reference",
        "approved_reference": True,
        "user_confirmed": True,
        "document_sha256": approved_case["document_sha256"],
        "image_sha256": approved_case["image_sha256"],
        "quarter": 4,
        "route": "시험고속도로",
        "caption": "사용자 승인 낙하물",
        "opinion": "낙하물 제거",
        "issue_type": "road_debris",
        "maintenance_action": "낙하물 제거",
    }
    structured = runtime._structured_similar_cases(
        candidate_id=candidate_id,
        checkpoint=search.checkpoint,
        approved_by_identity={
            approved_identity: approved_record,
        },
    )

    assert len(structured) == 3
    structured_weak = [
        case
        for case in structured
        if case["reference_state"] == "weak_unconfirmed"
    ]
    assert len(structured_weak) == 2
    assert all(
        case["issue_type"] == "sign_damage"
        and case["maintenance_action"] == "표지판 및 지주 보수"
        and case["approved_reference"] is False
        and case["user_verified"] is False
        for case in structured_weak
    )
    # The validator requires an action string, so weak rows receive the
    # bounded taxonomy default only after retrieval; no historical caption
    # or opinion is promoted to an approved action.
    assert {
        case["maintenance_action"] for case in structured_weak
    } == {"표지판 및 지주 보수"}
    structured_approved = next(
        case
        for case in structured
        if case["reference_state"] == "approved_reference"
    )
    assert structured_approved["issue_type"] == "road_debris"
    assert structured_approved["maintenance_action"] == "낙하물 제거"
    assert structured_approved["approved_reference"] is True
    assert structured_approved["user_verified"] is True


def test_visual_reference_search_batches_all_143_candidates_compactly_and_oom_fails_closed(
    tmp_path,
):
    project_id = uuid4()
    analysis_job_id = uuid4()
    source_sha256 = hashlib.sha256(
        b"all-temporal-events-video"
    ).hexdigest()
    candidate_ids = [
        f"{index:020x}" for index in range(1, 144)
    ]
    candidates = [
        {
            "candidate_id": candidate_id,
            "representative_timestamp_seconds": float(index),
            "ai_inference": _embedded_local_inference(
                predictions=[
                    {
                        "issue_type": "sign_damage",
                        "confidence": 0.81,
                        "box": {
                            "x": 0.2,
                            "y": 0.2,
                            "width": 0.3,
                            "height": 0.3,
                        },
                        "source_label": "damaged sign",
                        "calibration": "model_score",
                    }
                ],
            ),
        }
        for index, candidate_id in enumerate(
            candidate_ids, start=1
        )
    ]
    image_body = b"bounded shared candidate image evidence"
    image_path = (tmp_path / "candidate.jpg").resolve()
    image_path.write_bytes(image_body)
    image_sha256 = hashlib.sha256(image_body).hexdigest()

    def weak_case(index):
        return {
            "rank": index,
            "source_id": (
                f"sample-hwpx-3q-{index:016x}"
            ),
            "case_id": (
                "hwpx-weak-case-" + f"{index:032x}"
            ),
            "quarter": 3,
            "photo_page_number": index,
            "caption": "표지판 점검 문구 " * 80,
            "route": "시험고속도로 노선 " * 40,
            "location": "시험 위치 " * 40,
            "opinion": "유지관리 점검 의견 " * 200,
            "evaluation": "checkpoint must drop this field",
            "route_name": "checkpoint must drop this alias",
            "document_sha256": hashlib.sha256(
                f"document-{index}".encode()
            ).hexdigest(),
            "image_sha256": hashlib.sha256(
                f"image-{index}".encode()
            ).hexdigest(),
            "index_manifest_sha256": hashlib.sha256(
                f"index-{index}".encode()
            ).hexdigest(),
            "picture_target_id": (
                f"hwpx-picture-{index:024x}"
            ),
            "cell_target_id": f"hwpx-cell-{index:024x}",
            "reference_state": "weak_unconfirmed",
            "review_required": True,
            "approved_reference": False,
            "training_eligible": False,
            "approval_id": "drop-approval-id",
            "approval_revision": 99,
            "visual_cosine_score": 0.9 - index * 0.01,
            "text_structure_score": 1.0,
            "combined_similarity_score": 0.8 - index * 0.01,
            "matched_terms": ["drop", "matched", "terms"],
        }

    cases = [weak_case(index) for index in range(1, 4)]

    class FakeAnalysisJobs:
        def get(self, actual_project_id, actual_analysis_job_id):
            assert actual_project_id == project_id
            assert actual_analysis_job_id == analysis_job_id
            return {
                "job_id": analysis_job_id,
                "project_id": project_id,
                "source_sha256": source_sha256,
                "status": "completed",
                "result": {"candidates": candidates},
            }

        def resolve_artifact(
            self,
            actual_project_id,
            actual_analysis_job_id,
            actual_candidate_id,
            artifact_kind,
        ):
            assert actual_project_id == project_id
            assert actual_analysis_job_id == analysis_job_id
            assert actual_candidate_id in candidate_ids
            assert artifact_kind == "image"
            return SimpleNamespace(
                path=image_path,
                media_type="image/jpeg",
                size_bytes=len(image_body),
                sha256=image_sha256,
            )

    class FakeVisualIndex:
        def __init__(self):
            self.batch_calls = []
            self.single_calls = 0
            self.raise_oom = False

        def build(self, actual_project_id):
            assert actual_project_id == project_id
            return {
                "index_manifest_sha256": hashlib.sha256(
                    b"all-event-visual-index"
                ).hexdigest(),
                "model_identity_sha256": hashlib.sha256(
                    b"all-event-visual-model"
                ).hexdigest(),
            }

        def query_many(
            self,
            actual_project_id,
            evidences,
            *,
            issue_types_by_candidate,
            query_texts_by_candidate,
            limit,
        ):
            assert actual_project_id == project_id
            assert 1 <= len(evidences) <= 128
            assert query_texts_by_candidate is None
            assert limit == 3
            ids = [
                evidence.candidate_id for evidence in evidences
            ]
            assert list(issue_types_by_candidate) == ids
            assert set(issue_types_by_candidate.values()) == {
                "sign_damage"
            }
            self.batch_calls.append(ids)
            if self.raise_oom:
                raise WeakCaseVisualIndexError(
                    "embedding_batch_oom",
                    "test-only CUDA OOM",
                )
            return {
                candidate_id: {
                    "issue_type": "sign_damage",
                    "cases": list(cases),
                }
                for candidate_id in ids
            }

        def query(self, *_args, **_kwargs):
            self.single_calls += 1
            raise AssertionError(
                "query_many must not fall back to serial query"
            )

    visual_index = FakeVisualIndex()
    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        analysis_jobs=FakeAnalysisJobs(),
        weak_case_visual_index=visual_index,
    )
    context = {
        "profile": {
            "detector_pack_id": "road-ocr-v1",
            "ocr_pack_id": "road-ocr-v1",
        },
        "checkpoints": {
            "candidate_rescan_5_10fps": {
                "analysis_job_id": str(analysis_job_id),
                "source_sha256": source_sha256,
            },
            "detection_ocr": {},
            "temporal_merge": {
                "question_candidate_ids": candidate_ids,
            },
        },
    }

    result = runtime._search_visual_reference_cases(
        project_id, context
    )

    assert [len(batch) for batch in visual_index.batch_calls] == [
        128,
        15,
    ]
    assert [
        candidate_id
        for batch in visual_index.batch_calls
        for candidate_id in batch
    ] == candidate_ids
    assert visual_index.single_calls == 0
    assert "references" not in result.checkpoint
    assert result.checkpoint["candidate_count"] == 143
    assert list(
        result.checkpoint["matches_by_candidate"]
    ) == candidate_ids
    assert result.metrics["candidate_search_count"] == 143
    assert result.metrics["query_chunk_count"] == 2
    assert result.metrics["reference_count"] == 143 * 3
    compact = result.checkpoint["matches_by_candidate"][
        candidate_ids[0]
    ][0]
    assert {
        "rank",
        "evaluation",
        "route_name",
        "opinion",
        "approval_id",
        "approval_revision",
        "matched_terms",
    }.isdisjoint(compact)
    assert len(compact["caption"].encode("utf-8")) <= 256
    assert len(compact["route"].encode("utf-8")) <= 128
    assert len(compact["location"].encode("utf-8")) <= 128
    assert (
        len(compact["inspection_opinion"].encode("utf-8"))
        <= 96
    )
    assert compact["reference_state"] == "weak_unconfirmed"
    assert compact["approved_reference"] is False
    assert compact["training_eligible"] is False
    assert result.checkpoint["automatic_annotation_allowed"] is False
    assert (
        result.checkpoint[
            "automatic_incident_creation_allowed"
        ]
        is False
    )
    assert result.metrics["annotation_count"] == 0
    assert result.metrics["incident_count"] == 0

    visual_index.raise_oom = True
    with pytest.raises(PipelineStageFailure) as caught:
        runtime._search_visual_reference_cases(
            project_id, context
        )
    assert (
        caught.value.code
        == "visual_reference_embedding_batch_oom"
    )
    assert visual_index.single_calls == 0


def test_oversized_stage_result_is_replaced_by_durable_failure_and_worker_survives(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "app.pipeline_jobs.MAX_RECORD_BYTES", 64 * 1024
    )

    class OversizeOnceRuntime(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.oversize_pending = True

        def execute(self, stage, context, cancel):
            if stage == "intake_validation" and self.oversize_pending:
                self.oversize_pending = False
                self.calls.append(stage)
                self.contexts.append((stage, context))
                return PipelineStageResult(
                    "success",
                    checkpoint={
                        "duration_seconds": 60.0,
                        "unpersistable_payload": "x" * (80 * 1024),
                    },
                    metrics={"completed": 1},
                )
            return super().execute(stage, context, cancel)

    data_root = (tmp_path / "bridge-data").resolve()
    project_id = uuid4()
    runtime = OversizeOnceRuntime()
    service = PipelineJobService(
        runtime,
        data_root=data_root,
    )
    _configure(service, project_id)
    started, _replayed = _start(
        service, project_id, suffix="oversized-result"
    )
    job_id = started["job_id"]
    failed = _wait(
        service,
        project_id,
        job_id,
        {"failed"},
    )

    assert failed["status"] == "failed"
    assert failed["outcome_code"] == "failed"
    assert failed["error_code"] == "pipeline_record_too_large"
    intake = failed["stages"][0]
    assert intake["status"] == "failed"
    assert intake["error_code"] == "pipeline_record_too_large"
    assert intake["checkpoint"]["state"] == "persistence_failed"
    assert (
        intake["checkpoint"][
            "discarded_stage_result_used_for_truth"
        ]
        is False
    )
    assert (
        intake["checkpoint"]["automatic_annotation_allowed"]
        is False
    )
    assert (
        intake["checkpoint"][
            "automatic_incident_creation_allowed"
        ]
        is False
    )
    assert (
        intake["checkpoint"]["attempted_record_bytes"]
        > 64 * 1024
    )
    assert service._worker is not None
    assert service._worker.is_alive()

    record_path = service._job_path(
        project_id, job_id
    )
    record_body = record_path.read_bytes()
    assert len(record_body) <= 64 * 1024
    assert hashlib.sha256(record_body).hexdigest() == (
        record_path.with_suffix(".json.sha256")
        .read_text(encoding="ascii")
        .strip()
    )
    service.shutdown()

    restarted = PipelineJobService(
        FakeRuntime(),
        data_root=data_root,
    )
    recovered = restarted.get(
        project_id, job_id
    )
    assert recovered["status"] == "failed"
    assert recovered["error_code"] == "pipeline_record_too_large"
    assert restarted.recovery_summary()["records_rejected"] == 0
    restarted.shutdown()
