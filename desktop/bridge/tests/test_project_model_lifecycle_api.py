from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.candidate_window_rescan import (
    CandidateWindow,
    CandidateWindowFrame,
    CandidateWindowRescanResult,
)
from app.dataset_versions import (
    DATASET_SCHEMA_VERSION,
    canonical_json_bytes,
    freeze_approved_reference_records,
)
from app.dense_candidate_discovery import (
    ActiveProjectModelDenseProposalProvider,
    DenseCandidateDiscoveryEngine,
    DenseDiscoveryLimits,
)
from app.main import create_app
from app.media_intake import CompletedMediaGrant
from app.pipeline_jobs import (
    BridgePipelineRuntime,
    PipelineJobService,
    PipelineStageResult,
)
from app.project_model_lifecycle import (
    DeterministicTestTrainer,
    LOCAL_OBJECT_DETECTOR_MODEL_FORMAT,
    ProjectModelLifecycleError,
    ProjectModelLifecycleStore,
    TrainerInvocation,
    TrainerResult,
)
from app.source_candidate_probe import SourceCandidateProbe
from app.store import DEMO_PROJECT_ID
from app.training_provenance import (
    ANNOTATION_PROVENANCE_SCHEMA,
    build_dataset_provenance_graph,
)


TOKEN = "model-lifecycle-api-test-token-with-at-least-256-bits-000"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _sha(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _dataset(
    project_id: UUID,
    *,
    count: int = 130,
    validation_count: int = 30,
):
    project_text = str(project_id)
    memberships = []
    provenance_members = []
    annotations = []
    for index in range(count):
        annotation_id = str(uuid5(project_id, f"api-annotation:{index}"))
        answer_record_id = str(uuid5(project_id, f"api-answer:{index}"))
        analysis_job_id = str(
            uuid5(project_id, f"api-analysis:{index // 10}")
        )
        candidate_id = hashlib.sha256(
            f"api-candidate:{project_id}:{index}".encode()
        ).hexdigest()[:20]
        question_id = str(
            uuid5(UUID(analysis_job_id), candidate_id)
        )
        split = "validation" if index < validation_count else "train"
        source_hash = hashlib.sha256(
            f"api-source-record:{index}".encode()
        ).hexdigest()
        annotation_hash = hashlib.sha256(
            f"api-annotation-record:{index}".encode()
        ).hexdigest()
        membership = {
            "analysis_job_id": analysis_job_id,
            "annotation_hash": annotation_hash,
            "annotation_id": annotation_id,
            "answer_record_id": answer_record_id,
            "answer_revision": 1,
            "candidate_id": candidate_id,
            "include_in_training": True,
            "partition": "approved",
            "question_id": question_id,
            "source_record_hash": source_hash,
            "split": split,
        }
        memberships.append(membership)
        graph_base = {
            "schema_version": ANNOTATION_PROVENANCE_SCHEMA,
            "project_id": project_text,
            "pipeline_job_id": str(
                uuid5(project_id, f"api-pipeline:{index // 10}")
            ),
            "analysis_job_id": analysis_job_id,
            "overlay_id": (
                "ceo_"
                + hashlib.sha256(
                    f"api-overlay:{index}".encode()
                ).hexdigest()[:32]
            ),
            "overlay_manifest_sha256": hashlib.sha256(
                f"api-overlay-manifest:{index}".encode()
            ).hexdigest(),
            "candidate_set_metadata_sha256": hashlib.sha256(
                f"api-candidate-set:{index}".encode()
            ).hexdigest(),
            "review_event_set_id": (
                "res_"
                + hashlib.sha256(
                    f"api-review-set:{index}".encode()
                ).hexdigest()[:32]
            ),
            "review_event_set_revision": hashlib.sha256(
                f"api-review-set-revision:{index}".encode()
            ).hexdigest(),
            "review_event_set_manifest_sha256": hashlib.sha256(
                f"api-review-set-manifest:{index}".encode()
            ).hexdigest(),
            "review_event_id": (
                "rve_"
                + hashlib.sha256(
                    f"api-review-event:{index}".encode()
                ).hexdigest()[:32]
            ),
            "review_event_revision": hashlib.sha256(
                f"api-review-event-revision:{index}".encode()
            ).hexdigest(),
            "question_id": question_id,
            "candidate_id": candidate_id,
            "answer_record_id": answer_record_id,
            "answer_revision": 1,
            "source_sha256": hashlib.sha256(
                f"api-source:{index}".encode()
            ).hexdigest(),
            "candidate_metadata_sha256": hashlib.sha256(
                f"api-candidate-metadata:{index}".encode()
            ).hexdigest(),
            "image_sha256": hashlib.sha256(
                f"api-image:{index}".encode()
            ).hexdigest(),
            "mask_sha256": None,
            "proposal_sha256": hashlib.sha256(
                f"api-proposal:{index}".encode()
            ).hexdigest(),
            "geometry_sha256": None,
            "answer_authority": {
                "answered_by": "local_user",
                "include_in_training": True,
                "user_confirmed": True,
            },
            "delegation_authority": None,
        }
        provenance_graph = {
            **graph_base,
            "provenance_graph_sha256": _sha(graph_base),
        }
        provenance_members.append(
            {
                **membership,
                "provenance_graph": provenance_graph,
            }
        )
        annotations.append(
            {
                "project_id": project_text,
                "annotation_id": annotation_id,
                "question_id": question_id,
                "answer_record_id": answer_record_id,
                "answer_revision": 1,
                "analysis_job_id": analysis_job_id,
                "candidate_id": candidate_id,
                "partition": "approved",
                "include_in_training": True,
                "selected_issue_type": "pothole",
                "severity": "moderate",
                "maintenance_action": "repair",
                "source_record_hash": source_hash,
                "annotation_hash": annotation_hash,
                "provenance_graph": provenance_graph,
            }
        )
    fingerprint = {
        "members": memberships,
        "project_id": project_text,
        "schema_version": DATASET_SCHEMA_VERSION,
        "split_strategy": "analysis_job_hash_v1",
        "validation_fraction": 0.3,
    }
    dataset_hash = _sha(fingerprint)
    dataset_id = str(
        uuid5(
            project_id,
            f"{DATASET_SCHEMA_VERSION}:{dataset_hash}",
        )
    )
    manifest = {
        **fingerprint,
        "dataset_hash": dataset_hash,
        "dataset_id": dataset_id,
        "counts": {
            "excluded": 0,
            "source_answers": count,
            "train": count - validation_count,
            "training_members": count,
            "validation": validation_count,
        },
    }
    provenance_graph = build_dataset_provenance_graph(
        project_id=project_text,
        members=provenance_members,
    )
    return (
        {
            "dataset_version_id": dataset_id,
            "project_id": project_text,
            "dataset_hash": dataset_hash,
            "manifest": manifest,
            "memberships": memberships,
            "provenance_graph": provenance_graph,
        },
        annotations,
    )


def _approved_reference(project_id: UUID):
    case_hash = hashlib.sha256(f"api-case:{project_id}".encode()).hexdigest()
    source_id = f"sample-hwpx-3q-{case_hash[:16]}"
    case_id = f"hwpx-weak-case-{case_hash[:32]}"
    table_id = "api-table-1"
    semantic_cells = [
        {
            "role": role,
            "cell_target_id": (
                "hwpx-cell-"
                + hashlib.sha256(
                    f"api-case:{project_id}:{role}".encode()
                ).hexdigest()[:24]
            ),
            "table_id": table_id,
            "table_index": 1,
            "page_number": 2 if role == "photo_caption" else 1,
            "row": index,
            "column": 0,
            "row_span": 1,
            "column_span": 1,
            "text_excerpt": role,
            "text_sha256": hashlib.sha256(role.encode()).hexdigest(),
        }
        for index, role in enumerate(
            (
                "photo_caption",
                "inspection_route",
                "inspection_opinion_label",
                "inspection_opinion_body",
            )
        )
    ]
    square_markers = [
        {
            "cell_target_id": (
                "hwpx-cell-"
                + hashlib.sha256(
                    f"api-case:{project_id}:marker".encode()
                ).hexdigest()[:24]
            ),
            "table_id": table_id,
            "table_index": 1,
            "page_number": 1,
            "row": 4,
            "column": 0,
            "marker": "■",
            "text_sha256": hashlib.sha256(
                f"api-case:{project_id}:marker-text".encode()
            ).hexdigest(),
        }
    ]
    value = {
        "schema_version": "trafficvisson.approved-reference.v2",
        "approval_id": f"approved-reference-{case_hash[:24]}",
        "project_id": str(project_id),
        "source_id": source_id,
        "case_id": case_id,
        "revision": 1,
        "reference_state": "approved_reference",
        "approved_reference": True,
        "training_eligible": True,
        "training_scope": "approved_reference_only",
        "annotation_created": False,
        "incident_created": False,
        "issue_type": "pothole",
        "maintenance_action": "repair",
        "document_sha256": hashlib.sha256(
            b"api-document"
        ).hexdigest(),
        "structure_target_summary_sha256": hashlib.sha256(
            b"api-structure"
        ).hexdigest(),
        "index_manifest_sha256": hashlib.sha256(
            b"api-index"
        ).hexdigest(),
        "image_sha256": hashlib.sha256(b"api-image").hexdigest(),
        "document_name": "3분기 실사점검.hwpx",
        "image_media_type": "image/jpeg",
        "image_width": 1920,
        "image_height": 1080,
        "quarter": 3,
        "section_path": "Contents/section0.xml",
        "section_index": 0,
        "picture_target_id": (
            f"hwpx-picture-{case_hash[:24]}"
        ),
        "picture_index": 1,
        "picture_xml_id": "api-picture-1",
        "table_id": table_id,
        "table_index": 1,
        "cell_target_id": f"hwpx-cell-{case_hash[:24]}",
        "cell_row": 0,
        "cell_column": 0,
        "page_hint": 2,
        "page_hint_source": "api_fixture_page_hint",
        "photo_page_number": 2,
        "inspection_page_number": 1,
        "sample_layout_binding_schema": (
            "trafficvisson.hwpx-sample-layout.v2"
        ),
        "sample_layout_binding_revision": 2,
        "sample_layout_binding_sha256": hashlib.sha256(
            b"api-binding"
        ).hexdigest(),
        "semantic_projection_revision": (
            "audited-photo-caption-evidence-group-v2"
        ),
        "semantic_attestation_sha256": hashlib.sha256(
            b"api-semantic"
        ).hexdigest(),
        "photo_slot_id": f"sample-photo-slot-{case_hash[:24]}",
        "sample_evidence_group_id": (
            f"sample-evidence-group-{case_hash[:24]}"
        ),
        "group_photo_index": 1,
        "group_photo_count": 1,
        "duplicate_image_occurrence_count": 1,
        "duplicate_image_review_required": False,
        "duplicate_image_review_confirmed": False,
        "duplicate_image_case_refs": [f"{source_id}/{case_id}"],
        "square_markers": square_markers,
        "semantic_cells": semantic_cells,
        "weak_candidates": {
            "caption": "포트홀",
            "route": "테스트 노선",
            "location": None,
            "evaluation": "포트홀",
            "opinion": "보수 필요",
        },
        "caption": "포트홀",
        "route": "테스트 노선",
        "location": None,
        "evaluation": "포트홀",
        "opinion": "보수 필요",
        "user_confirmed": True,
    }
    return {**value, "record_sha256": _sha(value)}


def _configured_app(
    tmp_path,
    lifecycle: ProjectModelLifecycleStore,
    *,
    dataset,
    annotations,
    reference,
    fixture_mode=True,
):
    member = freeze_approved_reference_records(
        dataset["project_id"],
        [reference],
    )[0]
    fingerprint = {
        "approved_reference_members": [member],
        "members": dataset["memberships"],
        "project_id": dataset["project_id"],
        "provenance_graph": dataset["provenance_graph"],
        "schema_version": DATASET_SCHEMA_VERSION,
        "split_strategy": dataset["manifest"]["split_strategy"],
        "validation_fraction": dataset["manifest"][
            "validation_fraction"
        ],
    }
    dataset_hash = _sha(fingerprint)
    dataset_id = str(
        uuid5(
            UUID(dataset["project_id"]),
            f"{DATASET_SCHEMA_VERSION}:{dataset_hash}",
        )
    )
    counts = {
        **dataset["manifest"]["counts"],
        "approved_reference_training_events": 1,
        "approved_reference_training_members": 1,
        "training_gate_members": dataset["manifest"]["counts"]["train"],
    }
    members_sha256 = _sha([member])
    dataset.update(
        {
            "dataset_version_id": dataset_id,
            "dataset_hash": dataset_hash,
            "approved_reference_members": [member],
            "approved_reference_member_count": 1,
            "approved_reference_training_event_count": 1,
            "approved_reference_members_sha256": members_sha256,
            "training_gate_member_count": counts[
                "training_gate_members"
            ],
            "approved_references_count_toward_training_gate": False,
            "approved_references_count_toward_validation": False,
            "manifest": {
                **fingerprint,
                "dataset_hash": dataset_hash,
                "dataset_id": dataset_id,
                "approved_reference_members_sha256": members_sha256,
                "approved_references_count_toward_training_gate": False,
                "approved_references_count_toward_validation": False,
                "provenance_graph": dataset["provenance_graph"],
                "counts": counts,
            },
        }
    )
    app = create_app(
        TOKEN,
        fixture_mode=fixture_mode,
        allow_test_client=True,
        bridge_data_root=str(tmp_path / "bridge"),
        source_candidate_probe=SourceCandidateProbe(None),
        project_model_lifecycle_store=lifecycle,
    )
    app.state.store.list_dataset_versions = (
        lambda project_id: [dataset]
    )
    app.state.store.list_dataset_annotations = (
        lambda project_id, dataset_version_id: list(annotations)
    )
    app.state.weak_case_approvals.list_project = (
        lambda project_id: {
            "schema_version": (
                "trafficvisson.approved-reference-list.v1"
            ),
            "project_id": str(project_id),
            "approved_reference_count": 1,
            "training_eligible_count": 1,
            "annotation_count": 0,
            "incident_count": 0,
            "records": [reference],
        }
    )
    return app


def _start_payload(dataset, *, key="api-training-0001"):
    return {
        "dataset_version_id": dataset["dataset_version_id"],
        "idempotency_key": key,
        "user_confirmed": True,
    }


def _wait_training_job_api(
    client: TestClient,
    training_job_id: str,
    *,
    terminal=("succeeded", "failed", "interrupted", "cancelled"),
):
    deadline = time.monotonic() + 10
    latest = None
    while time.monotonic() < deadline:
        response = client.get(
            f"/projects/{DEMO_PROJECT_ID}/model-training-jobs",
            headers=AUTH,
        )
        assert response.status_code == 200, response.text
        latest = next(
            row
            for row in response.json()["jobs"]
            if row["training_job_id"] == training_job_id
        )
        if latest["status"] in terminal:
            return latest
        time.sleep(0.01)
    raise AssertionError(f"training job did not finish: {latest}")


def test_fixed_validation_history_api_exposes_only_safe_lineage(tmp_path):
    record = {
        "schema_version": "trafficvisson.project-fixed-validation.v2",
        "project_id": str(DEMO_PROJECT_ID),
        "validation_set_version": 1,
        "parent_validation_set_version": None,
        "parent_membership_sha256": None,
        "parent_fixed_validation_sha256": None,
        "origin_dataset_version_id": str(uuid4()),
        "required_issue_types": ["pothole"],
        "added_issue_types": ["pothole"],
        "fixed_validation_annotations": [{"secret": "annotation"}],
        "fixed_validation_truth": [{"secret": "truth"}],
        "validation_sample_counts": {"pothole": 30},
        "membership_sha256": "a" * 64,
        "fixed_validation_sha256": "b" * 64,
        "extension_approval": None,
        "extension_approval_sha256": None,
        "approved_reference_member_count": 0,
        "approved_references_count_toward_validation": False,
        "created_at": "2026-07-30T00:00:00Z",
        "persisted": True,
    }
    lifecycle = SimpleNamespace(
        resolve_active_model=lambda project_id: None,
        shutdown=lambda: None,
        list_fixed_validation_versions=lambda project_id: {
            "schema_version": (
                "trafficvisson.project-fixed-validation-list.v2"
            ),
            "project_id": str(project_id),
            "count": 1,
            "versions": [record],
        },
    )
    app = create_app(
        TOKEN,
        fixture_mode=True,
        allow_test_client=True,
        bridge_data_root=str(tmp_path / "bridge"),
        source_candidate_probe=SourceCandidateProbe(None),
        project_model_lifecycle_store=lifecycle,
    )

    with TestClient(app) as client:
        response = client.get(
            f"/projects/{DEMO_PROJECT_ID}/fixed-validation-sets",
            headers=AUTH,
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["count"] == 1
    head = payload["versions"][0]
    assert head["validation_set_version"] == 1
    assert head["validation_sample_counts"] == {"pothole": 30}
    assert head["minimum_positive_samples_per_type"] == 30
    assert "fixed_validation_annotations" not in head
    assert "fixed_validation_truth" not in head


class _LocalDetectorContractTrainer(DeterministicTestTrainer):
    """Exercise the production detector format without running a real GPU."""

    trainer_id = "local-detector-contract-test"

    def train(self, invocation: TrainerInvocation) -> TrainerResult:
        result = super().train(invocation)
        return TrainerResult(
            model_version=result.model_version,
            model_format=LOCAL_OBJECT_DETECTOR_MODEL_FORMAT,
            primary_artifact_relative_path=(
                result.primary_artifact_relative_path
            ),
            active_prediction_records=result.active_prediction_records,
            candidate_prediction_records=(
                result.candidate_prediction_records
            ),
            metadata={
                **result.metadata,
                "adapter_role": "fixture-local-object-detector",
            },
        )


class _BlockingApiTrainer(DeterministicTestTrainer):
    trainer_id = "blocking-api-training-test"

    def __init__(self):
        super().__init__()
        self.executions = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def train(self, invocation: TrainerInvocation) -> TrainerResult:
        self.executions += 1
        checkpoint = invocation.output_directory / "checkpoint.pt"
        checkpoint.write_bytes(b"api-checkpoint")
        assert invocation.checkpoint_callback is not None
        invocation.checkpoint_callback(checkpoint)
        assert invocation.progress_callback is not None
        invocation.progress_callback(
            {
                "stage": "fine_tune",
                "current": 1,
                "total": 2,
            }
        )
        self.started.set()
        while not self.release.wait(0.01):
            if (
                invocation.cancel_callback is not None
                and invocation.cancel_callback()
            ):
                raise ProjectModelLifecycleError(
                    "test_training_cancelled",
                    "API training was cancelled",
                )
        return super().train(invocation)


def test_create_app_initializes_durable_production_lifecycle_store(
    tmp_path,
):
    bridge_root = tmp_path / "default-bridge"
    app = create_app(
        TOKEN,
        fixture_mode=True,
        allow_test_client=True,
        bridge_data_root=str(bridge_root),
        source_candidate_probe=SourceCandidateProbe(None),
    )
    assert isinstance(
        app.state.project_models,
        ProjectModelLifecycleStore,
    )
    assert (
        app.state.project_models.supports_bound_artifact_roots
        is True
    )
    assert (
        bridge_root
        / "project-model-lifecycle"
        / "lifecycle.sqlite3"
    ).is_file()
    with TestClient(app) as client:
        response = client.get(
            f"/projects/{DEMO_PROJECT_ID}/model-state",
            headers=AUTH,
        )
    assert response.status_code == 404


def test_nonfixture_injected_unbound_model_store_fails_without_artifact(
    tmp_path,
):
    dataset, annotations = _dataset(DEMO_PROJECT_ID)
    lifecycle = ProjectModelLifecycleStore(
        tmp_path / "unbound-models",
        trainer=DeterministicTestTrainer(),
        allow_test_trainer=True,
    )
    assert lifecycle.supports_bound_artifact_roots is False
    app = _configured_app(
        tmp_path,
        lifecycle,
        dataset=dataset,
        annotations=annotations,
        reference=_approved_reference(DEMO_PROJECT_ID),
        fixture_mode=False,
    )

    with TestClient(app) as client:
        response = client.post(
            f"/projects/{DEMO_PROJECT_ID}/model-training-jobs",
            headers=AUTH,
            json=_start_payload(
                dataset,
                key="api-unbound-production-root-0001",
            ),
        )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "artifact_root_unavailable"
    )
    assert lifecycle.list_training_jobs(DEMO_PROJECT_ID)["count"] == 0
    assert not any(
        (tmp_path / "unbound-models" / "project-model-lifecycle" / "artifacts")
        .iterdir()
    )


def test_http_start_returns_queued_promptly_and_cancel_is_durable(
    tmp_path,
):
    dataset, annotations = _dataset(DEMO_PROJECT_ID)
    trainer = _BlockingApiTrainer()
    lifecycle = ProjectModelLifecycleStore(
        tmp_path / "models",
        trainer=trainer,
        allow_test_trainer=True,
    )
    app = _configured_app(
        tmp_path,
        lifecycle,
        dataset=dataset,
        annotations=annotations,
        reference=_approved_reference(DEMO_PROJECT_ID),
    )

    with TestClient(app) as client:
        started_at = time.monotonic()
        started = client.post(
            f"/projects/{DEMO_PROJECT_ID}/model-training-jobs",
            headers=AUTH,
            json=_start_payload(
                dataset,
                key="api-async-cancel-0001",
            ),
        )
        elapsed = time.monotonic() - started_at
        assert started.status_code == 202, started.text
        queued = started.json()
        assert elapsed < 1.0
        assert queued["status"] == "queued"
        assert queued["attempt_count"] == 0
        assert trainer.started.wait(2)

        replay = client.post(
            f"/projects/{DEMO_PROJECT_ID}/model-training-jobs",
            headers=AUTH,
            json=_start_payload(
                dataset,
                key="api-async-cancel-0001",
            ),
        )
        assert replay.status_code == 202, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert (
            replay.json()["training_job_id"]
            == queued["training_job_id"]
        )
        assert trainer.executions == 1

        cancelled = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/model-training-jobs/"
                f"{queued['training_job_id']}/cancel"
            ),
            headers=AUTH,
            json={"user_confirmed": True},
        )
        assert cancelled.status_code == 202, cancelled.text
        assert cancelled.json()["cancel_requested"] is True
        terminal = _wait_training_job_api(
            client,
            queued["training_job_id"],
            terminal=("cancelled",),
        )
        assert terminal["error"]["code"] == "training_cancelled"
        assert terminal["checkpoint"] is not None
        assert isinstance(
            terminal["checkpoint"]["modified_time_ns"],
            str,
        )
        assert terminal["checkpoint"]["modified_time_ns"].isdigit()
        assert trainer.executions == 1


def test_server_owned_start_list_promote_and_rollback(tmp_path):
    dataset, annotations = _dataset(DEMO_PROJECT_ID)
    lifecycle = ProjectModelLifecycleStore(
        tmp_path / "models",
        trainer=DeterministicTestTrainer(),
        allow_test_trainer=True,
    )
    app = _configured_app(
        tmp_path,
        lifecycle,
        dataset=dataset,
        annotations=annotations,
        reference=_approved_reference(DEMO_PROJECT_ID),
    )

    with TestClient(app) as client:
        forged_types = client.post(
            f"/projects/{DEMO_PROJECT_ID}/model-training-jobs",
            headers=AUTH,
            json={
                **_start_payload(
                    dataset,
                    key="api-forged-types-0001",
                ),
                "required_issue_types": ["lane_marking_faded"],
            },
        )
        assert forged_types.status_code == 422

        started = client.post(
            f"/projects/{DEMO_PROJECT_ID}/model-training-jobs",
            headers=AUTH,
            json=_start_payload(dataset),
        )
        assert started.status_code == 202, started.text
        queued = started.json()
        assert queued["status"] == "queued"
        job = _wait_training_job_api(
            client,
            queued["training_job_id"],
        )
        assert job["status"] == "succeeded"
        assert job["dataset_hash"] == dataset["dataset_hash"]
        assert job["current_confirmed_label_count"] == 100
        assert job["validation_sample_counts"] == {"pothole": 30}
        assert job["fixed_validation_set_version"] == 1
        assert job["fixed_validation_parent_version"] is None
        assert (
            job["fixed_validation_set_schema_version"]
            == "trafficvisson.project-fixed-validation.v2"
        )
        assert len(job["fixed_validation_membership_sha256"]) == 64
        assert job["auxiliary_reference_count"] == 1
        assert job["trainer_test_only"] is True
        assert job["automatic_training_started"] is False
        assert "request" not in job
        assert "current_training_annotations" not in job

        validation_sets = client.get(
            f"/projects/{DEMO_PROJECT_ID}/fixed-validation-sets",
            headers=AUTH,
        )
        assert validation_sets.status_code == 200, validation_sets.text
        validation_payload = validation_sets.json()
        assert validation_payload["count"] == 1
        validation_head = validation_payload["versions"][0]
        assert validation_head["validation_set_version"] == 1
        assert validation_head["validation_sample_counts"] == {
            "pothole": 30
        }
        assert validation_head["membership_sha256"] == (
            job["fixed_validation_membership_sha256"]
        )
        assert validation_head["fixed_validation_sha256"] == (
            job["fixed_validation_sha256"]
        )
        assert validation_head["minimum_positive_samples_per_type"] == 30
        assert "fixed_validation_annotations" not in validation_head
        assert "fixed_validation_truth" not in validation_head

        replayed = client.post(
            f"/projects/{DEMO_PROJECT_ID}/model-training-jobs",
            headers=AUTH,
            json=_start_payload(dataset),
        )
        assert replayed.status_code == 202, replayed.text
        assert replayed.json()["idempotent_replay"] is True
        assert (
            replayed.json()["training_job_id"]
            == job["training_job_id"]
        )

        jobs = client.get(
            f"/projects/{DEMO_PROJECT_ID}/model-training-jobs",
            headers=AUTH,
        )
        assert jobs.status_code == 200, jobs.text
        assert jobs.json()["count"] == 1

        models = client.get(
            f"/projects/{DEMO_PROJECT_ID}/model-versions",
            headers=AUTH,
        )
        assert models.status_code == 200, models.text
        model_list = models.json()
        assert model_list["count"] == 1
        model = model_list["models"][0]
        assert model["state"] == "candidate"
        assert model["validation_decision"] == "eligible"
        assert model["fixed_validation_set_version"] == 1
        assert (
            model["fixed_validation_membership_sha256"]
            == job["fixed_validation_membership_sha256"]
        )
        assert "artifact_directory" not in model
        assert "primary_artifact_relative_path" not in model

        state = client.get(
            f"/projects/{DEMO_PROJECT_ID}/model-state",
            headers=AUTH,
        )
        assert state.status_code == 200, state.text
        assert state.json()["revision"] == 1
        assert (
            state.json()["candidate_model_id"]
            == model["model_version_id"]
        )

        promoted = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/model-versions/"
                f"{model['model_version_id']}/promote"
            ),
            headers=AUTH,
            json={
                "expected_revision": 1,
                "idempotency_key": "api-promote-0001",
                "user_confirmed": True,
            },
        )
        assert promoted.status_code == 200, promoted.text
        assert (
            promoted.json()["after_state"]["active_model_id"]
            == model["model_version_id"]
        )
        assert promoted.json()["automatic_transition"] is False

        rolled_back = client.post(
            f"/projects/{DEMO_PROJECT_ID}/model-state/rollback",
            headers=AUTH,
            json={
                "expected_revision": 2,
                "idempotency_key": "api-rollback-001",
                "user_confirmed": True,
            },
        )
        assert rolled_back.status_code == 200, rolled_back.text
        assert (
            rolled_back.json()["after_state"][
                "active_model_version"
            ]
            == "common-initial@1.0.0"
        )

        not_resumable = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/model-training-jobs/"
                f"{job['training_job_id']}/resume"
            ),
            headers=AUTH,
            json={"user_confirmed": True},
        )
        assert not_resumable.status_code == 409
        assert not_resumable.json()["detail"]["code"] == (
            "training_not_resumable"
        )


def test_renderer_cannot_inject_training_evidence_or_base_model(tmp_path):
    dataset, annotations = _dataset(DEMO_PROJECT_ID)
    lifecycle = ProjectModelLifecycleStore(
        tmp_path / "models",
        trainer=DeterministicTestTrainer(),
        allow_test_trainer=True,
    )
    app = _configured_app(
        tmp_path,
        lifecycle,
        dataset=dataset,
        annotations=annotations,
        reference=_approved_reference(DEMO_PROJECT_ID),
    )
    forged = {
        **_start_payload(dataset),
        "base_model_version": "renderer-forged@9",
        "current_annotations": [{"forged": True}],
        "auxiliary_references": [{"forged": True}],
        "validation_truth_records": [{"forged": True}],
        "artifact_path": r"C:\forged\model.bin",
    }
    with TestClient(app) as client:
        response = client.post(
            f"/projects/{DEMO_PROJECT_ID}/model-training-jobs",
            headers=AUTH,
            json=forged,
        )
        unconfirmed = client.post(
            f"/projects/{DEMO_PROJECT_ID}/model-training-jobs",
            headers=AUTH,
            json={
                **_start_payload(
                    dataset,
                    key="unconfirmed-api-01",
                ),
                "user_confirmed": False,
            },
        )
    assert response.status_code == 422
    assert unconfirmed.status_code == 422
    fields = {
        ".".join(str(part) for part in item["loc"])
        for item in response.json()["detail"]
    }
    assert any("base_model_version" in field for field in fields)
    assert any("current_annotations" in field for field in fields)
    assert lifecycle.list_training_jobs(DEMO_PROJECT_ID)["count"] == 0


def test_missing_dataset_and_state_are_not_cross_project_fallbacks(
    tmp_path,
):
    dataset, annotations = _dataset(DEMO_PROJECT_ID)
    lifecycle = ProjectModelLifecycleStore(
        tmp_path / "models",
        trainer=DeterministicTestTrainer(),
        allow_test_trainer=True,
    )
    app = _configured_app(
        tmp_path,
        lifecycle,
        dataset=dataset,
        annotations=annotations,
        reference=_approved_reference(DEMO_PROJECT_ID),
    )
    with TestClient(app) as client:
        state = client.get(
            f"/projects/{DEMO_PROJECT_ID}/model-state",
            headers=AUTH,
        )
        assert state.status_code == 404
        assert state.json()["detail"]["code"] == (
            "model_state_not_found"
        )

        missing = client.post(
            f"/projects/{DEMO_PROJECT_ID}/model-training-jobs",
            headers=AUTH,
            json={
                **_start_payload(dataset),
                "dataset_version_id": str(uuid4()),
            },
        )
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == (
            "dataset_version_not_found"
        )
        assert lifecycle.list_training_jobs(DEMO_PROJECT_ID)["count"] == 0


def test_production_unavailable_and_validation_reject_stay_honest(
    tmp_path,
):
    dataset, annotations = _dataset(DEMO_PROJECT_ID)
    unavailable_lifecycle = ProjectModelLifecycleStore(
        tmp_path / "unavailable-models"
    )
    unavailable_app = _configured_app(
        tmp_path / "unavailable-app",
        unavailable_lifecycle,
        dataset=dataset,
        annotations=annotations,
        reference=_approved_reference(DEMO_PROJECT_ID),
    )
    with TestClient(unavailable_app) as client:
        unavailable = client.post(
            f"/projects/{DEMO_PROJECT_ID}/model-training-jobs",
            headers=AUTH,
            json=_start_payload(dataset),
        )
        assert unavailable.status_code == 202, unavailable.text
        payload = unavailable.json()
        assert payload["status"] == "blocked"
        assert payload["trainer_available"] is False
        assert {
            blocker["code"] for blocker in payload["blockers"]
        } == {"trainer_unavailable"}
        models = client.get(
            f"/projects/{DEMO_PROJECT_ID}/model-versions",
            headers=AUTH,
        )
        assert models.json()["count"] == 0

    rejected_lifecycle = ProjectModelLifecycleStore(
        tmp_path / "rejected-models",
        trainer=DeterministicTestTrainer(candidate_mode="empty"),
        allow_test_trainer=True,
    )
    rejected_app = _configured_app(
        tmp_path / "rejected-app",
        rejected_lifecycle,
        dataset=dataset,
        annotations=annotations,
        reference=_approved_reference(DEMO_PROJECT_ID),
    )
    with TestClient(rejected_app) as client:
        started = client.post(
            f"/projects/{DEMO_PROJECT_ID}/model-training-jobs",
            headers=AUTH,
            json=_start_payload(dataset, key="api-rejected-0001"),
        )
        assert started.status_code == 202, started.text
        _wait_training_job_api(
            client,
            started.json()["training_job_id"],
        )
        models = client.get(
            f"/projects/{DEMO_PROJECT_ID}/model-versions",
            headers=AUTH,
        ).json()
        model = models["models"][0]
        assert model["validation_decision"] == "rejected"
        assert model["state"] == "rejected"
        assert model["automatic_rejection"] is True
        assert model["user_promotion_required"] is False
        project_state = client.get(
            f"/projects/{DEMO_PROJECT_ID}/model-state",
            headers=AUTH,
        )
        assert project_state.status_code == 200
        assert project_state.json()["candidate_model_id"] is None

        blocked = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/model-versions/"
                f"{model['model_version_id']}/promote"
            ),
            headers=AUTH,
            json={
                "expected_revision": 1,
                "idempotency_key": "api-bad-promote-1",
                "user_confirmed": True,
            },
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == (
            "candidate_not_found"
        )

        redundant_reject = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/model-versions/"
                f"{model['model_version_id']}/reject"
            ),
            headers=AUTH,
            json={
                "expected_revision": 1,
                "idempotency_key": "api-reject-00001",
                "user_confirmed": True,
            },
        )
        assert redundant_reject.status_code == 409
        assert redundant_reject.json()["detail"]["code"] == (
            "candidate_not_found"
        )


class _SecondVideoProjectInference:
    def __init__(self) -> None:
        self.models = []

    def status(self):
        return {
            "provider": "promoted_local_object_detector",
            "state": "ready",
            "backend_id": "fixture-detector-inference",
            "backend_version": "1.0",
        }

    def classify(
        self,
        model,
        *,
        survey_analysis_job_id: str | None = None,
        body: bytes,
        media_type: str,
        expected_sha256: str,
    ):
        assert hashlib.sha256(body).hexdigest() == expected_sha256
        assert media_type == "image/jpeg"
        if survey_analysis_job_id is not None:
            assert survey_analysis_job_id not in {
                *model.training_analysis_job_ids,
                *model.fixed_validation_analysis_job_ids,
            }
        self.models.append(model)
        return {
            "model_version_id": model.model_version_id,
            "model_version": model.model_version,
            "dataset_version_id": model.dataset_version_id,
            "candidate_image_sha256": expected_sha256,
            "predictions": [
                {
                    "issue_type": "pothole",
                    "score": 0.94,
                    "box": {
                        "x": 0.2,
                        "y": 0.5,
                        "width": 0.3,
                        "height": 0.2,
                    },
                }
            ],
        }


class _SecondVideoRoadSceneObserver:
    """Keep the lifecycle fixture explicit about usable road evidence."""

    def observe_road_scene(
        self,
        body: bytes,
        media_type: str,
        expected_sha256: str,
    ):
        assert media_type == "image/jpeg"
        assert hashlib.sha256(body).hexdigest() == expected_sha256
        return {
            "status": "completed",
            "image_sha256": expected_sha256,
            "model_identity_sha256": "6" * 64,
            "prompt_set_sha256": "7" * 64,
            "road_scene_probability": 0.96,
            "non_road_view_probability": 0.04,
            "local_only": True,
            "review_only": True,
        }


class _SecondVideoAnalysisJobs:
    def __init__(self, row):
        self.row = row

    def list(self, project_id, *, limit):
        assert project_id == self.row["project_id"]
        assert limit == 128
        return [self.row]

    def get(self, project_id, job_id):
        assert project_id == self.row["project_id"]
        assert job_id == self.row["job_id"]
        return self.row


class _SecondVideoMediaIntake:
    def __init__(self, path: Path, source_sha256: str) -> None:
        self.path = path
        self.source_sha256 = source_sha256

    def resolve_completed_media(
        self,
        project_id,
        snapshot_id,
        file_name,
        hash_job_id,
        source_sha256,
    ):
        assert file_name == "second-video.mp4"
        assert source_sha256 == self.source_sha256
        stat = self.path.stat()
        return CompletedMediaGrant(
            project_id=project_id,
            snapshot_id=snapshot_id,
            file_name=file_name,
            hash_job_id=hash_job_id,
            sha256_lower=source_sha256,
            media_path=self.path,
            size_bytes=stat.st_size,
            modified_time_ns=stat.st_mtime_ns,
        )


class _SecondVideoOverlayStore:
    def __init__(self) -> None:
        self.discovery = None

    def materialize(
        self,
        *,
        project_id,
        analysis_job_id,
        pipeline_job_id,
        discovery,
        cancel,
    ):
        assert not cancel()
        self.discovery = discovery
        overlay_id = "ceo_" + "a" * 32
        manifest_sha256 = "b" * 64
        metadata_sha256 = "c" * 64
        checkpoint = {
            "project_id": str(project_id),
            "analysis_job_id": str(analysis_job_id),
            "pipeline_job_id": str(pipeline_job_id),
            "source_sha256": discovery.source_sha256,
            "overlay_id": overlay_id,
            "manifest_sha256": manifest_sha256,
            "candidate_metadata_sha256": metadata_sha256,
        }
        return SimpleNamespace(
            overlay_id=overlay_id,
            manifest_sha256=manifest_sha256,
            candidate_metadata_sha256=metadata_sha256,
            candidates=discovery.candidates,
            idempotent_replay=False,
            to_checkpoint=lambda: checkpoint,
        )


def _write_second_video_road_frame(path: Path) -> tuple[bytes, str]:
    image = Image.new("RGB", (320, 180), (128, 185, 224))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 72, 320, 180), fill=(74, 119, 66))
    draw.polygon(
        ((112, 72), (208, 72), (296, 180), (24, 180)),
        fill=(65, 68, 72),
    )
    draw.polygon(
        ((157, 76), (163, 76), (170, 180), (150, 180)),
        fill=(238, 226, 148),
    )
    draw.rectangle((36, 48, 43, 118), fill=(105, 105, 105))
    draw.rectangle((18, 48, 62, 78), fill=(42, 92, 163))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="JPEG", quality=92)
    body = path.read_bytes()
    return body, hashlib.sha256(body).hexdigest()


def _write_second_video_dense_index(
    root: Path,
    *,
    project_id: UUID,
    pipeline_job_id: UUID,
    dense_run_id: str,
    source_sha256: str,
) -> tuple[Path, str]:
    index_root = (
        root
        / "projects"
        / str(project_id)
        / "pipeline"
        / "dense"
        / str(pipeline_job_id)
        / dense_run_id
    )
    frames_root = index_root / "frames"
    frames_root.mkdir(parents=True)
    frame_path = frames_root / "frame-00000000.jpg"
    frame_body, frame_sha256 = _write_second_video_road_frame(frame_path)
    frame_id = hashlib.sha256(
        (
            "trafficvisson.dense-frame-index.v1"
            f"\0{source_sha256}\0fps=2\0frame=0"
        ).encode("ascii")
    ).hexdigest()[:24]
    payload = {
        "schema_version": "trafficvisson.dense-frame-index.v1",
        "frame_count": 1,
        "frames": [
            {
                "frame_id": frame_id,
                "frame_index": 0,
                "timestamp_seconds": 0.0,
                "timestamp_numerator": 0,
                "timestamp_denominator": 2,
                "relative_path": "frames/frame-00000000.jpg",
                "width": 1920,
                "height": 1080,
                "size_bytes": len(frame_body),
                "sha256": frame_sha256,
                "media_type": "image/jpeg",
                "evidence_status": "unreviewed_candidate",
            }
        ],
        "provenance": {
            "schema_version": "trafficvisson.dense-frame-index.v1",
            "source_sha256": source_sha256,
            "sampling_fps": 2,
            "frame_count": 1,
            "timestamp_method": (
                "zero_based_frame_index_divided_by_integer_sampling_fps"
            ),
        },
    }
    body = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    index_path = index_root / "frame-index.json"
    index_path.write_bytes(body)
    return index_path, hashlib.sha256(body).hexdigest()


class _SecondPipelineRuntime:
    def __init__(
        self,
        bridge_runtime: BridgePipelineRuntime,
        *,
        data_root: Path,
        source_sha256: str,
        hash_job_id: UUID,
    ) -> None:
        self.bridge_runtime = bridge_runtime
        self.data_root = data_root
        self.source_sha256 = source_sha256
        self.hash_job_id = hash_job_id
        self.candidate_result = None

    def capability_snapshot(self, profile):
        return self.bridge_runtime.capability_snapshot(profile)

    def execute(self, stage, context, cancel):
        if stage == "sha256":
            return PipelineStageResult(
                "success",
                checkpoint={
                    "source_sha256": self.source_sha256,
                    "hash_job_id": str(self.hash_job_id),
                    "total_bytes": len(b"synthetic-second-video"),
                },
            )
        if stage == "dense_index_2fps":
            dense_run_id = "dense-frame-index-second01"
            _, dense_index_sha256 = _write_second_video_dense_index(
                self.data_root,
                project_id=UUID(str(context["project_id"])),
                pipeline_job_id=UUID(str(context["job_id"])),
                dense_run_id=dense_run_id,
                source_sha256=self.source_sha256,
            )
            return PipelineStageResult(
                "success",
                checkpoint={
                    "dense_run_id": dense_run_id,
                    "source_sha256": self.source_sha256,
                    "sampling_fps": 2,
                    "dense_index_sha256": dense_index_sha256,
                },
            )
        if stage == "candidate_rescan_5_10fps":
            self.candidate_result = self.bridge_runtime.execute(
                stage,
                context,
                cancel,
            )
            return self.candidate_result
        if stage == "question_queue":
            return PipelineStageResult(
                "success",
                checkpoint={
                    "question_count": 1,
                    "annotation_count": 0,
                    "incident_count": 0,
                },
                metrics={
                    "question_count": 1,
                    "annotation_count": 0,
                    "incident_count": 0,
                },
            )
        return PipelineStageResult("success")


def _wait_for_pipeline_status(
    service: PipelineJobService,
    project_id: UUID,
    job_id: UUID,
    expected: str,
):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = service.get(project_id, job_id)
        if row["status"] == expected:
            return row
        if row["status"] in {"blocked", "failed", "cancelled"}:
            raise AssertionError(row)
        time.sleep(0.01)
    raise AssertionError(service.get(project_id, job_id))


def test_dataset_to_promoted_model_to_second_pipeline_and_rollback(
    tmp_path,
    monkeypatch,
):
    """Close the project-model loop without NAS, GPU, or document I/O."""

    dataset, annotations = _dataset(DEMO_PROJECT_ID)
    reference = _approved_reference(DEMO_PROJECT_ID)
    lifecycle = ProjectModelLifecycleStore(
        tmp_path / "models",
        trainer=_LocalDetectorContractTrainer(),
        allow_test_trainer=True,
    )
    app = _configured_app(
        tmp_path,
        lifecycle,
        dataset=dataset,
        annotations=annotations,
        reference=reference,
    )
    # Training must consume only the immutable DatasetVersion reference
    # membership, never a later mutable approval-store view.
    app.state.weak_case_approvals.list_project = lambda _project_id: (
        (_ for _ in ()).throw(
            AssertionError("live approvals must not be read for training")
        )
    )

    with TestClient(app) as client:
        started = client.post(
            f"/projects/{DEMO_PROJECT_ID}/model-training-jobs",
            headers=AUTH,
            json=_start_payload(
                dataset,
                key="closed-loop-training-0001",
            ),
        )
        assert started.status_code == 202, started.text
        training = _wait_training_job_api(
            client,
            started.json()["training_job_id"],
        )
        assert training["status"] == "succeeded"
        assert training["required_issue_types"] == ["pothole"]
        assert training["dataset_hash"] == dataset["dataset_hash"]
        assert training["approved_reference_training_member_count"] == 1
        assert training["approved_reference_training_event_count"] == 1
        assert training["validation_sample_counts"] == {"pothole": 30}
        assert len(training["fixed_validation_sha256"]) == 64

        models = client.get(
            f"/projects/{DEMO_PROJECT_ID}/model-versions",
            headers=AUTH,
        ).json()["models"]
        assert len(models) == 1
        model = models[0]
        assert model["model_format"] == LOCAL_OBJECT_DETECTOR_MODEL_FORMAT
        assert model["state"] == "candidate"
        assert model["user_promotion_required"] is True
        assert model["automatic_promotion"] is False
        assert model["validation_decision"] == "eligible"
        assert (
            model["fixed_validation_sha256"]
            == training["fixed_validation_sha256"]
        )
        promoted = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/model-versions/"
                f"{model['model_version_id']}/promote"
            ),
            headers=AUTH,
            json={
                "expected_revision": 1,
                "idempotency_key": "closed-loop-promote-0001",
                "user_confirmed": True,
            },
        )
        assert promoted.status_code == 200, promoted.text
        assert promoted.json()["automatic_transition"] is False

        source_body = b"synthetic-second-video"
        source_sha256 = hashlib.sha256(source_body).hexdigest()
        source_path = tmp_path / "second-video.mp4"
        source_path.write_bytes(source_body)
        snapshot_id = uuid4()
        analysis_job_id = uuid4()
        hash_job_id = uuid4()
        analysis_jobs = _SecondVideoAnalysisJobs(
            {
                "job_id": analysis_job_id,
                "project_id": DEMO_PROJECT_ID,
                "snapshot_id": snapshot_id,
                "hash_job_id": hash_job_id,
                "file_name": "second-video.mp4",
                "source_sha256": source_sha256,
                "status": "completed",
                "result": {
                    "candidate_count": 0,
                    "candidates": [],
                    "provenance": {
                        "effective_cadence_seconds": 15.0
                    },
                },
            }
        )
        project_inference = _SecondVideoProjectInference()
        provider = ActiveProjectModelDenseProposalProvider(
            lifecycle.resolve_active_model,
            detector_inference=project_inference,
        )
        dense_discovery = DenseCandidateDiscoveryEngine(
            [provider],
            limits=DenseDiscoveryLimits(
                batch_size=2,
                max_frames=8,
                max_proposals_per_frame=2,
                max_candidates=8,
                temporal_gap_seconds=1.5,
                minimum_score=0.5,
            ),
        )
        overlays = _SecondVideoOverlayStore()
        second_pipeline_root = (tmp_path / "second-pipeline").resolve()
        second_pipeline_root.mkdir()
        bridge_runtime = BridgePipelineRuntime(
            _SecondVideoMediaIntake(source_path, source_sha256),
            data_root=second_pipeline_root,
            ffmpeg_path="ffmpeg",
            analysis_jobs=analysis_jobs,
            dense_candidate_discovery=dense_discovery,
            candidate_evidence_overlays=overlays,
            visual_evidence_semantic_observer=(
                _SecondVideoRoadSceneObserver()
            ),
        )

        class _FakeRescanEngine:
            def __init__(self, **_kwargs):
                pass

            def build(self, _media_path, **kwargs):
                candidate_id = kwargs["candidates"][0]["candidate_id"]
                output = (
                    Path(kwargs["output_directory"])
                    / ("candidate-window-rescan-" + "d" * 24)
                )
                output.mkdir(parents=True, exist_ok=True)
                window_id = "e" * 20
                relative_path = (
                    f"windows/{window_id}/frame-000000.jpg"
                )
                frame_body, frame_sha256 = (
                    _write_second_video_road_frame(
                        output / relative_path
                    )
                )
                frame = CandidateWindowFrame(
                    evidence_id="f" * 20,
                    window_id=window_id,
                    frame_index=0,
                    timestamp_seconds=0.0,
                    timestamp_numerator=0,
                    timestamp_denominator=8,
                    source_candidate_ids=(candidate_id,),
                    primary_candidate_id=candidate_id,
                    relative_path=relative_path,
                    width=320,
                    height=180,
                    size_bytes=len(frame_body),
                    sha256=frame_sha256,
                )
                window = CandidateWindow(
                    window_id=window_id,
                    start_seconds=0.0,
                    end_seconds=0.5,
                    duration_seconds=0.5,
                    source_candidate_ids=(candidate_id,),
                    planned_frame_limit=4,
                )
                return CandidateWindowRescanResult(
                    output_directory=str(output),
                    manifest_relative_path="candidate-window-rescan.json",
                    manifest_sha256="2" * 64,
                    input_revision="3" * 64,
                    windows=(window,),
                    frames=(frame,),
                    downstream_evidence_ids=(frame.evidence_id,),
                    cached=False,
                )

        monkeypatch.setattr(
            "app.pipeline_jobs.CandidateWindowRescanEngine",
            _FakeRescanEngine,
        )
        second_runtime = _SecondPipelineRuntime(
            bridge_runtime,
            data_root=second_pipeline_root,
            source_sha256=source_sha256,
            hash_job_id=hash_job_id,
        )
        pipeline = PipelineJobService(
            second_runtime,
            data_root=second_pipeline_root,
            poll_interval_seconds=0.01,
        )
        try:
            pipeline.configure_profile(
                DEMO_PROJECT_ID,
                question_policy="learning_assist",
                review_mode="precision",
                default_template_id="sample-4q-v1",
                default_template_sha256="4" * 64,
                detector_pack_id="road-detector-v1",
                ocr_pack_id="korean-ocr-v1",
                sam2_pack_id="sam2-v1",
                report_provider="none",
                output_location_id="project-default",
                candidate_rescan_fps=8,
            )
            second_job, replayed = pipeline.start(
                DEMO_PROJECT_ID,
                snapshot_id=snapshot_id,
                file_name="second-video.mp4",
                rights_confirmed=True,
                idempotency_key="second-video-pipeline-0001",
            )
            assert replayed is False
            terminal = _wait_for_pipeline_status(
                pipeline,
                DEMO_PROJECT_ID,
                second_job["job_id"],
                "awaiting_review",
            )
            candidate_stage = next(
                stage
                for stage in terminal["stages"]
                if stage["name"] == "candidate_rescan_5_10fps"
            )
            discovery = candidate_stage["checkpoint"][
                "dense_candidate_discovery"
            ]
            provider_state = discovery["provider_states"][0]
            proposal = discovery["candidates"][0][
                "representative_proposal_provenance"
            ]
            assert provider_state["state"] == "ready"
            assert (
                provider_state["model_version_id"]
                == model["model_version_id"]
            )
            assert provider_state["dataset_hash"] == dataset["dataset_hash"]
            assert (
                provider_state["fixed_validation_sha256"]
                == training["fixed_validation_sha256"]
            )
            assert proposal["model_version_id"] == model["model_version_id"]
            assert proposal["dataset_hash"] == dataset["dataset_hash"]
            assert (
                proposal["fixed_validation_sha256"]
                == training["fixed_validation_sha256"]
            )
            assert project_inference.models
            assert discovery["annotations_created"] == 0
            assert discovery["incidents_created"] == 0
            assert terminal["review_completion_id"] is None
        finally:
            pipeline.shutdown()

        rolled_back = client.post(
            f"/projects/{DEMO_PROJECT_ID}/model-state/rollback",
            headers=AUTH,
            json={
                "expected_revision": 2,
                "idempotency_key": "closed-loop-rollback-0001",
                "user_confirmed": True,
            },
        )
        assert rolled_back.status_code == 200, rolled_back.text
        rollback = rolled_back.json()
        assert rollback["automatic_transition"] is False
        assert (
            rollback["after_state"]["previous_model_id"]
            == model["model_version_id"]
        )
        assert rollback["after_state"]["active_model_id"] is None
        assert provider.status(DEMO_PROJECT_ID) == {
            "provider": "promoted_project_model",
            "state": "unavailable",
            "reason": "no_promoted_project_model",
        }
