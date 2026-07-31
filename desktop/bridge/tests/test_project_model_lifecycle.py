from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import pytest
from structured_assist_fixtures import assist_provenance

from app.dataset_versions import (
    DATASET_SCHEMA_VERSION,
    canonical_json_bytes,
    freeze_approved_reference_records,
)
from app.project_model_lifecycle import (
    DeterministicTestTrainer,
    ProjectModelLifecycleError,
    ProjectModelLifecycleStore,
    TrainerInvocation,
    TrainerResult,
    VISUAL_CENTROID_MODEL_FORMAT,
)
from app.training_geometry import (
    SCHEMA_VERSION as TRAINING_GEOMETRY_SCHEMA,
    TRACKED_FRAME_BINDING_SCHEMA,
    seal_training_geometry,
)


def _sha(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _dataset(
    project_id: UUID,
    *,
    count: int = 130,
    validation_count: int = 30,
    issue_type: str = "pothole",
):
    project_text = str(project_id)
    memberships = []
    annotations = []
    for index in range(count):
        annotation_id = str(uuid5(project_id, f"annotation:{index}"))
        answer_record_id = str(uuid5(project_id, f"answer:{index}"))
        analysis_job_id = str(
            uuid5(project_id, f"analysis:{index // 10}")
        )
        candidate_id = hashlib.sha256(
            f"candidate:{project_text}:{index}".encode()
        ).hexdigest()[:20]
        question_id = str(
            uuid5(UUID(analysis_job_id), candidate_id)
        )
        split = "validation" if index < validation_count else "train"
        annotation_value = {
            "project_id": project_text,
            "annotation_id": annotation_id,
            "question_id": question_id,
            "answer_record_id": answer_record_id,
            "answer_revision": 1,
            "analysis_job_id": analysis_job_id,
            "candidate_id": candidate_id,
            "partition": "approved",
            "include_in_training": True,
            "selected_issue_type": issue_type,
            "severity": "moderate",
            "maintenance_action": "repair",
            "source_record_hash": hashlib.sha256(
                f"answer-record:{index}".encode()
            ).hexdigest(),
            "annotation_hash": hashlib.sha256(
                f"annotation-record:{index}".encode()
            ).hexdigest(),
            "user_confirmed": True,
            "source_kind": "current_video_confirmed",
        }
        member = {
            "analysis_job_id": analysis_job_id,
            "annotation_hash": annotation_value["annotation_hash"],
            "annotation_id": annotation_id,
            "answer_record_id": answer_record_id,
            "answer_revision": 1,
            "candidate_id": candidate_id,
            "include_in_training": True,
            "partition": "approved",
            "question_id": question_id,
            "source_record_hash": annotation_value[
                "source_record_hash"
            ],
            "split": split,
        }
        memberships.append(member)
        annotations.append(annotation_value)
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
    return (
        {
            "dataset_version_id": dataset_id,
            "project_id": project_text,
            "dataset_hash": dataset_hash,
            "manifest": manifest,
            "memberships": memberships,
        },
        annotations,
    )


def _reseal_dataset_memberships(project_id, dataset, memberships):
    values = copy.deepcopy(dataset)
    values["memberships"] = copy.deepcopy(memberships)
    counts = {
        "excluded": sum(
            member["split"] == "excluded" for member in memberships
        ),
        "source_answers": len(memberships),
        "train": sum(
            member["split"] == "train" for member in memberships
        ),
        "training_members": sum(
            member["include_in_training"] for member in memberships
        ),
        "validation": sum(
            member["split"] == "validation" for member in memberships
        ),
    }
    fingerprint = {
        "members": values["memberships"],
        "project_id": str(project_id),
        "schema_version": DATASET_SCHEMA_VERSION,
        "split_strategy": "analysis_job_hash_v1",
        "validation_fraction": values["manifest"][
            "validation_fraction"
        ],
    }
    dataset_hash = _sha(fingerprint)
    dataset_id = str(
        uuid5(
            project_id,
            f"{DATASET_SCHEMA_VERSION}:{dataset_hash}",
        )
    )
    values.update(
        {
            "dataset_version_id": dataset_id,
            "dataset_hash": dataset_hash,
            "manifest": {
                **fingerprint,
                "dataset_hash": dataset_hash,
                "dataset_id": dataset_id,
                "counts": counts,
            },
        }
    )
    return values


def _positive_training_geometry(seed: str, *, image_seed: str):
    return seal_training_geometry(
        {
            "schema_version": TRAINING_GEOMETRY_SCHEMA,
            "source_kind": "accepted_ai_proposal",
            "approval_authority": "local_user",
            "image_sha256": hashlib.sha256(
                image_seed.encode()
            ).hexdigest(),
            "image_artifact_ref": f"analysis:{seed}:representative",
            "normalized_box": {
                "x": 0.1,
                "y": 0.2,
                "width": 0.3,
                "height": 0.2,
            },
            "polygon": None,
            "mask_artifact_ref": None,
            "mask_sha256": None,
            "tracked_frame_refs": [],
            "proposal_sha256": hashlib.sha256(
                f"proposal:{seed}".encode()
            ).hexdigest(),
            "sam2_output_sha256": None,
            "sam2_model_manifest_sha256": None,
        }
    )


def _approved_reference(
    project_id: UUID,
    *,
    index: int = 0,
    issue_type: str = "pothole",
    group_seed: str | None = None,
    group_photo_index: int = 1,
    group_photo_count: int = 1,
):
    case_hash = hashlib.sha256(
        f"case:{project_id}:{index}".encode()
    ).hexdigest()
    group_hash = hashlib.sha256(
        f"group:{project_id}:{group_seed or index}".encode()
    ).hexdigest()
    source_id = f"sample-hwpx-3q-{case_hash[:16]}"
    case_id = f"hwpx-weak-case-{case_hash[:32]}"
    table_id = f"hwpx-table-{case_hash[:24]}"
    semantic_cells = [
        {
            "role": role,
            "cell_target_id": (
                "hwpx-cell-"
                + hashlib.sha256(
                    f"{case_hash}:{role}".encode()
                ).hexdigest()[:24]
            ),
            "table_id": table_id,
            "table_index": 1,
            "page_number": 2 if role == "photo_caption" else 1,
            "row": row,
            "column": 0,
            "row_span": 1,
            "column_span": 1,
            "text_excerpt": role,
            "text_sha256": _sha(role),
        }
        for row, role in enumerate(
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
                    f"{case_hash}:marker".encode()
                ).hexdigest()[:24]
            ),
            "table_id": table_id,
            "table_index": 1,
            "page_number": 1,
            "row": 4,
            "column": 0,
            "marker": "■",
            "text_sha256": _sha(f"{case_hash}:marker-text"),
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
        "issue_type": issue_type,
        "maintenance_action": "repair",
        "document_name": "3분기 점검 내용 정리.hwpx",
        "document_sha256": hashlib.sha256(
            f"document:{index}".encode()
        ).hexdigest(),
        "structure_target_summary_sha256": hashlib.sha256(
            f"structure:{index}".encode()
        ).hexdigest(),
        "index_manifest_sha256": hashlib.sha256(
            f"index:{index}".encode()
        ).hexdigest(),
        "image_sha256": hashlib.sha256(
            f"image:{index}".encode()
        ).hexdigest(),
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
        "picture_xml_id": "1",
        "table_id": table_id,
        "table_index": 1,
        "cell_target_id": f"hwpx-cell-{case_hash[:24]}",
        "cell_row": 0,
        "cell_column": 0,
        "page_hint": 2,
        "page_hint_source": "fixture",
        "photo_page_number": 2,
        "inspection_page_number": 1,
        "sample_layout_binding_schema": (
            "trafficvisson.hwpx-sample-layout.v2"
        ),
        "sample_layout_binding_revision": 2,
        "sample_layout_binding_sha256": hashlib.sha256(
            f"binding:{index}".encode()
        ).hexdigest(),
        "semantic_projection_revision": (
            "audited-photo-caption-evidence-group-v2"
        ),
        "semantic_attestation_sha256": hashlib.sha256(
            f"semantic:{index}".encode()
        ).hexdigest(),
        "photo_slot_id": (
            f"sample-photo-slot-{case_hash[:24]}"
        ),
        "sample_evidence_group_id": (
            f"sample-evidence-group-{group_hash[:24]}"
        ),
        "group_photo_index": group_photo_index,
        "group_photo_count": group_photo_count,
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


def _freeze_references(project_id, dataset, references):
    members = freeze_approved_reference_records(
        project_id,
        references,
    )
    fingerprint = {
        "approved_reference_members": members,
        "members": dataset["memberships"],
        "project_id": str(project_id),
        "schema_version": DATASET_SCHEMA_VERSION,
        "split_strategy": "analysis_job_hash_v1",
        "validation_fraction": dataset["manifest"][
            "validation_fraction"
        ],
    }
    dataset_hash = _sha(fingerprint)
    dataset_id = str(
        uuid5(
            project_id,
            f"{DATASET_SCHEMA_VERSION}:{dataset_hash}",
        )
    )
    counts = {
        **dataset["manifest"]["counts"],
        "approved_reference_training_members": len(members),
        "approved_reference_training_events": len(
            {
                member["sample_evidence_group_id"]
                for member in members
            }
        ),
        "training_gate_members": dataset["manifest"]["counts"]["train"],
    }
    members_sha256 = _sha(members)
    return (
        {
            **dataset,
            "dataset_version_id": dataset_id,
            "dataset_hash": dataset_hash,
            "approved_reference_members": members,
            "approved_reference_member_count": len(members),
            "approved_reference_training_event_count": counts[
                "approved_reference_training_events"
            ],
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
                "counts": counts,
            },
        },
        members,
    )


def _start(
    store: ProjectModelLifecycleStore,
    project_id: UUID,
    dataset,
    annotations,
    *,
    references=None,
    key: str = "train-request-0001",
    fixed_validation_extension_approval=None,
    wait_for_terminal: bool = True,
):
    source_references = (
        references
        if references is not None
        else [_approved_reference(project_id)]
    )
    frozen_dataset, frozen_references = _freeze_references(
        project_id,
        dataset,
        source_references,
    )
    started = store.start_training(
        project_id,
        dataset_version=frozen_dataset,
        current_annotations=annotations,
        auxiliary_references=frozen_references,
        base_model_version="common-initial@1.0.0",
        required_issue_types=["pothole"],
        idempotency_key=key,
        user_confirmed=True,
        fixed_validation_extension_approval=(
            fixed_validation_extension_approval
        ),
    )
    if (
        not wait_for_terminal
        or started["status"]
        not in {"queued", "running"}
    ):
        return started
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = store.get_training_job(
            project_id,
            UUID(started["training_job_id"]),
        )
        if current["status"] not in {"queued", "running"}:
            return {
                **current,
                "idempotent_replay": started.get(
                    "idempotent_replay",
                    False,
                ),
            }
        time.sleep(0.01)
    raise AssertionError("training job did not reach a terminal state")


def _append_fixed_validation_issue(
    project_id: UUID,
    dataset,
    annotations,
    *,
    issue_type: str,
    count: int = 30,
):
    """Append a distinct, user-confirmed validation cohort."""

    values = copy.deepcopy(dataset)
    memberships = copy.deepcopy(dataset["memberships"])
    next_annotations = copy.deepcopy(annotations)
    project_text = str(project_id)
    for index in range(count):
        seed = f"fixed-extension:{issue_type}:{index}"
        annotation_id = str(uuid5(project_id, f"annotation:{seed}"))
        answer_record_id = str(uuid5(project_id, f"answer:{seed}"))
        analysis_job_id = str(
            uuid5(project_id, f"analysis:{seed}:{index // 10}")
        )
        candidate_id = hashlib.sha256(
            f"candidate:{project_text}:{seed}".encode()
        ).hexdigest()[:20]
        question_id = str(uuid5(UUID(analysis_job_id), candidate_id))
        source_record_hash = hashlib.sha256(
            f"answer-record:{seed}".encode()
        ).hexdigest()
        annotation_hash = hashlib.sha256(
            f"annotation-record:{seed}".encode()
        ).hexdigest()
        next_annotations.append(
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
                "selected_issue_type": issue_type,
                "severity": "moderate",
                "maintenance_action": "repair",
                "source_record_hash": source_record_hash,
                "annotation_hash": annotation_hash,
                "user_confirmed": True,
                "source_kind": "current_video_confirmed",
            }
        )
        memberships.append(
            {
                "analysis_job_id": analysis_job_id,
                "annotation_hash": annotation_hash,
                "annotation_id": annotation_id,
                "answer_record_id": answer_record_id,
                "answer_revision": 1,
                "candidate_id": candidate_id,
                "include_in_training": True,
                "partition": "approved",
                "question_id": question_id,
                "source_record_hash": source_record_hash,
                "split": "validation",
            }
        )
    return (
        _reseal_dataset_memberships(project_id, values, memberships),
        next_annotations,
    )


def _extension_approval(
    first_job,
    *added_issue_types: str,
    analysis_job_ids,
):
    request = first_job["request"]
    return {
        "schema_version": (
            "trafficvisson.fixed-validation-extension-approval.v1"
        ),
        "expected_parent_version": request[
            "fixed_validation_set_version"
        ],
        "expected_parent_membership_sha256": request[
            "fixed_validation_membership_sha256"
        ],
        "expected_parent_fixed_validation_sha256": request[
            "fixed_validation_sha256"
        ],
        "added_issue_types": list(added_issue_types),
        "added_validation_analysis_job_ids": sorted(
            {str(value) for value in analysis_job_ids}
        ),
        "user_confirmed": True,
    }


def _wait_for_job(
    store: ProjectModelLifecycleStore,
    project_id: UUID,
    training_job_id: str,
    *,
    terminal: set[str],
    timeout: float = 10.0,
):
    deadline = time.monotonic() + timeout
    current = None
    while time.monotonic() < deadline:
        current = store.get_training_job(
            project_id,
            UUID(training_job_id),
        )
        if current["status"] in terminal:
            return current
        time.sleep(0.01)
    raise AssertionError(
        f"training job did not reach {sorted(terminal)}: {current}"
    )


def test_real_artifact_fixed_validation_and_restart_idempotency(tmp_path):
    project_id = uuid4()
    dataset, annotations = _dataset(project_id)
    trainer = DeterministicTestTrainer()
    store = ProjectModelLifecycleStore(
        tmp_path,
        trainer=trainer,
        allow_test_trainer=True,
    )

    job = _start(store, project_id, dataset, annotations)
    assert job["status"] == "succeeded"
    assert job["automatic_training_started"] is False
    assert job["user_confirmed_start"] is True
    assert job["request"]["minimum_candidate_recall"] == 0.80
    assert job["request"]["minimum_candidate_precision"] == 0.50
    assert job["request"]["maximum_recall_drop"] == 0.02
    assert job["request"]["current_confirmed_label_count"] == 100
    assert job["request"]["validation_sample_counts"] == {
        "pothole": 30
    }
    seed = job["request"]["auxiliary_reference_seeds"][0]
    assert seed["training_weight"] == 0.25
    assert seed["counts_toward_training_gate"] is False
    assert seed["counts_toward_fixed_validation"] is False
    assert seed["quarter"] == 3
    assert seed["photo_page_number"] == 2
    assert seed["inspection_page_number"] == 1
    assert seed["table_id"]
    assert seed["cell_target_id"]
    assert seed["picture_target_id"]
    assert seed["square_marker_target_ids"]
    assert seed["evaluation_marker_target_ids"] == (
        seed["square_marker_target_ids"]
    )
    assert seed["source_address_sha256"]
    assert seed["approved_reference_member_sha256"]
    assert trainer.calls == 1

    models = store.list_model_versions(project_id)
    assert models["count"] == 1
    model = models["models"][0]
    assert model["state"] == "candidate"
    assert model["validation_decision"] == "eligible"
    assert model["promotion_review"]["auto_promotion_allowed"] is False
    assert (
        model["promotion_review"]["validation_set_sha256"]
        == model["fixed_validation_sha256"]
    )
    artifact_dir = (
        tmp_path
        / "project-model-lifecycle"
        / Path(model["artifact_directory"])
    )
    primary = artifact_dir / model["primary_artifact_relative_path"]
    manifest_path = artifact_dir / "artifact-manifest.json"
    assert primary.is_file() and primary.stat().st_size > 0
    assert (
        hashlib.sha256(primary.read_bytes()).hexdigest()
        == model["primary_artifact_sha256"]
    )
    manifest_bytes = manifest_path.read_bytes()
    assert (
        hashlib.sha256(manifest_bytes).hexdigest()
        == model["artifact_manifest_sha256"]
    )
    manifest = json.loads(manifest_bytes)
    assert manifest["auxiliary_reference_count"] == 1
    assert (
        manifest["approved_references_count_toward_training_gate"]
        is False
    )
    assert manifest[
        "auxiliary_references_count_toward_validation"
    ] is False
    assert set(model["training_analysis_job_ids"]).isdisjoint(
        model["fixed_validation_analysis_job_ids"]
    )
    assert model["training_analysis_job_ids"]
    assert model["fixed_validation_analysis_job_ids"]
    assert manifest["source_analysis_lineage"] == {
        "training_analysis_job_ids": model[
            "training_analysis_job_ids"
        ],
        "fixed_validation_analysis_job_ids": model[
            "fixed_validation_analysis_job_ids"
        ],
    }
    assert manifest["source_analysis_lineage_sha256"] == model[
        "source_analysis_lineage_sha256"
    ]
    assert manifest["trainer"]["test_only"] is True

    restarted_trainer = DeterministicTestTrainer()
    restarted = ProjectModelLifecycleStore(
        tmp_path,
        trainer=restarted_trainer,
        allow_test_trainer=True,
    )
    replay = _start(
        restarted,
        project_id,
        dataset,
        annotations,
        key="different-key-same-request",
    )
    assert replay["idempotent_replay"] is True
    assert replay["training_job_id"] == job["training_job_id"]
    assert restarted_trainer.calls == 0


@pytest.mark.parametrize(
    "overlap_kind",
    [
        "annotation_hash",
        "question_id",
        "source_record_hash",
        "analysis_job_id",
        "training_evidence",
    ],
)
def test_pretrainer_boundary_rejects_member_question_evidence_and_source_overlap(
    tmp_path,
    overlap_kind,
):
    project_id = uuid4()
    dataset, annotations = _dataset(project_id)
    memberships = copy.deepcopy(dataset["memberships"])
    annotations = copy.deepcopy(annotations)
    fixed_index = 0
    train_index = 30

    if overlap_kind in {
        "annotation_hash",
        "question_id",
        "source_record_hash",
        "analysis_job_id",
    }:
        shared = annotations[fixed_index][overlap_kind]
        annotations[train_index][overlap_kind] = shared
        memberships[train_index][overlap_kind] = shared
    else:
        annotations[fixed_index]["training_geometry"] = (
            _positive_training_geometry(
                "fixed-evidence",
                image_seed="shared-validation-image",
            )
        )
        annotations[train_index]["training_geometry"] = (
            _positive_training_geometry(
                "train-evidence",
                image_seed="shared-validation-image",
            )
        )
        for index in (fixed_index, train_index):
            annotations[index]["normalized_box"] = {
                "x": 0.1,
                "y": 0.2,
                "width": 0.3,
                "height": 0.2,
            }

    dataset = _reseal_dataset_memberships(
        project_id,
        dataset,
        memberships,
    )
    trainer = DeterministicTestTrainer()
    store = ProjectModelLifecycleStore(
        tmp_path / overlap_kind,
        trainer=trainer,
        allow_test_trainer=True,
    )

    with pytest.raises(ProjectModelLifecycleError) as blocked:
        _start(
            store,
            project_id,
            dataset,
            annotations,
            key=f"pretrainer-overlap-{overlap_kind}",
        )

    assert blocked.value.code == "fixed_validation_training_leak"
    assert trainer.calls == 0
    assert store.list_training_jobs(project_id)["count"] == 0
    assert store.list_model_versions(project_id)["count"] == 0
    artifact_root = (
        tmp_path
        / overlap_kind
        / "project-model-lifecycle"
        / "artifacts"
    )
    assert list(artifact_root.iterdir()) == []


def test_reclassified_fixed_validation_source_is_blocked_before_retraining(
    tmp_path,
):
    project_id = uuid4()
    dataset, annotations = _dataset(project_id)
    trainer = DeterministicTestTrainer()
    store = ProjectModelLifecycleStore(
        tmp_path,
        trainer=trainer,
        allow_test_trainer=True,
    )
    first = _start(store, project_id, dataset, annotations)
    first_model = store.list_model_versions(project_id)["models"][0]
    store.reject(
        project_id,
        UUID(first_model["model_version_id"]),
        expected_revision=1,
        idempotency_key="reject-before-resplit-leak",
        user_confirmed=True,
    )

    memberships = copy.deepcopy(dataset["memberships"])
    old_validation_job = memberships[0]["analysis_job_id"]
    old_train_job = memberships[30]["analysis_job_id"]
    for member in memberships:
        if member["analysis_job_id"] == old_validation_job:
            member["split"] = "train"
        elif member["analysis_job_id"] == old_train_job:
            member["split"] = "validation"
    reclassified = _reseal_dataset_memberships(
        project_id,
        dataset,
        memberships,
    )

    with pytest.raises(ProjectModelLifecycleError) as blocked:
        _start(
            store,
            project_id,
            reclassified,
            annotations,
            key="reclassified-fixed-source-0001",
        )

    assert blocked.value.code == "fixed_validation_training_leak"
    assert trainer.calls == 1
    jobs = store.list_training_jobs(project_id)
    assert jobs["count"] == 1
    assert jobs["jobs"][0]["training_job_id"] == first["training_job_id"]


def test_changed_validation_membership_is_stale_before_retraining(tmp_path):
    project_id = uuid4()
    dataset, annotations = _dataset(project_id)
    trainer = DeterministicTestTrainer()
    store = ProjectModelLifecycleStore(
        tmp_path,
        trainer=trainer,
        allow_test_trainer=True,
    )
    first = _start(store, project_id, dataset, annotations)
    first_model = store.list_model_versions(project_id)["models"][0]
    store.reject(
        project_id,
        UUID(first_model["model_version_id"]),
        expected_revision=1,
        idempotency_key="reject-before-stale-binding",
        user_confirmed=True,
    )

    memberships = copy.deepcopy(dataset["memberships"])
    removed_validation_job = memberships[0]["analysis_job_id"]
    for member in memberships:
        if member["analysis_job_id"] == removed_validation_job:
            member["split"] = "excluded"
    stale = _reseal_dataset_memberships(
        project_id,
        dataset,
        memberships,
    )

    with pytest.raises(ProjectModelLifecycleError) as blocked:
        _start(
            store,
            project_id,
            stale,
            annotations,
            key="stale-fixed-binding-0001",
        )

    assert blocked.value.code == "fixed_validation_binding_stale"
    assert trainer.calls == 1
    jobs = store.list_training_jobs(project_id)
    assert jobs["count"] == 1
    assert jobs["jobs"][0]["training_job_id"] == first["training_job_id"]


def test_validation_members_do_not_count_toward_100_train_member_gate(
    tmp_path,
):
    project_id = uuid4()
    dataset, annotations = _dataset(
        project_id,
        count=100,
        validation_count=30,
    )
    trainer = DeterministicTestTrainer()
    store = ProjectModelLifecycleStore(
        tmp_path,
        trainer=trainer,
        allow_test_trainer=True,
    )

    job = _start(store, project_id, dataset, annotations)

    assert job["status"] == "blocked"
    assert trainer.calls == 0
    assert job["request"]["current_train_member_count"] == 70
    assert job["request"]["current_validation_member_count"] == 30
    assert job["request"]["training_gate_member_count"] == 70
    assert job["request"]["current_confirmed_label_count"] == 70
    assert job["request"]["validation_sample_counts"] == {"pothole": 30}
    assert job["blockers"] == [
        {
            "code": "insufficient_current_confirmed_labels",
            "observed": 70,
            "current_video_members": 70,
            "approved_reference_members": 1,
            "approved_reference_events": 1,
            "required": 100,
        }
    ]


def test_approved_references_do_not_count_toward_training_or_validation(
    tmp_path,
):
    project_id = uuid4()
    dataset, annotations = _dataset(
        project_id,
        count=129,
        validation_count=30,
    )
    trainer = DeterministicTestTrainer()
    store = ProjectModelLifecycleStore(
        tmp_path,
        trainer=trainer,
        allow_test_trainer=True,
    )
    references = [
        _approved_reference(project_id, index=index)
        for index in range(5)
    ]

    job = _start(
        store,
        project_id,
        dataset,
        annotations,
        references=references,
    )
    assert job["status"] == "blocked"
    assert trainer.calls == 0
    assert job["request"]["current_confirmed_label_count"] == 99
    assert job["request"]["auxiliary_reference_count"] == 5
    assert (
        job["request"][
            "approved_references_count_toward_training_gate"
        ]
        is False
    )
    assert job["request"]["training_gate_member_count"] == 99
    assert job["request"][
        "auxiliary_references_count_toward_validation"
    ] is False
    assert job["status"] == "blocked"
    assert job["blockers"][0]["code"] == (
        "insufficient_current_confirmed_labels"
    )
    assert store.list_model_versions(project_id)["count"] == 0


def test_tracked_fit_geometry_does_not_inflate_the_100_annotation_gate(
    tmp_path,
):
    project_id = uuid4()
    dataset, annotations = _dataset(
        project_id,
        count=129,
        validation_count=30,
    )
    tracked = annotations[-1]
    tracked["normalized_box"] = {
        "x": 0.1,
        "y": 0.2,
        "width": 0.3,
        "height": 0.4,
    }
    tracked["training_geometry"] = seal_training_geometry(
        {
            "schema_version": TRAINING_GEOMETRY_SCHEMA,
            "source_kind": "user_sam2_refinement",
            "approval_authority": "local_user",
            "image_sha256": "1" * 64,
            "image_artifact_ref": "candidate-evidence:representative",
            "normalized_box": {
                "x": 0.1,
                "y": 0.2,
                "width": 0.3,
                "height": 0.4,
            },
            "polygon": [[0.1, 0.2], [0.4, 0.2], [0.4, 0.6]],
            "mask_artifact_ref": "sam2-mask:approved",
            "mask_sha256": "2" * 64,
            "tracked_frame_refs": [
                {
                    "binding_schema": TRACKED_FRAME_BINDING_SCHEMA,
                    "evidence_candidate_id": f"{index + 2}" * 20,
                    "image_artifact_ref": (
                        f"candidate-evidence:tracked:{index}"
                    ),
                    "frame_id": f"tracked-frame-{index}",
                    "image_sha256": str(index + 2) * 64,
                    "timestamp_seconds": 10.0 + index * 0.5,
                    "normalized_box": {
                        "x": 0.1,
                        "y": 0.2,
                        "width": 0.3,
                        "height": 0.4,
                    },
                    "polygon": None,
                    "mask_sha256": None,
                }
                for index in range(8)
            ],
            "proposal_sha256": "3" * 64,
            "sam2_output_sha256": "4" * 64,
            "sam2_model_manifest_sha256": "5" * 64,
        }
    )
    trainer = DeterministicTestTrainer()
    store = ProjectModelLifecycleStore(
        tmp_path,
        trainer=trainer,
        allow_test_trainer=True,
    )

    job = _start(store, project_id, dataset, annotations)

    assert job["status"] == "blocked"
    assert job["request"]["training_gate_member_count"] == 99
    assert job["request"]["current_confirmed_label_count"] == 99
    assert job["blockers"][0]["code"] == (
        "insufficient_current_confirmed_labels"
    )
    assert trainer.calls == 0


def test_training_authorized_structured_llm_annotation_counts_toward_100_gate(
    tmp_path,
):
    project_id = uuid4()
    dataset, annotations = _dataset(project_id)
    structured = annotations[-1]
    assert dataset["memberships"][-1]["split"] == "train"
    structured.update(
        {
            "answered_by": "structured_llm_assisted",
            "label_source": "structured_llm_assisted",
            "source_kind": "structured_llm_assisted",
            "user_confirmed": False,
            "training_authorized": True,
            "assist_provenance": assist_provenance(
                project_id,
                UUID(structured["analysis_job_id"]),
                structured["candidate_id"],
            ),
        }
    )
    store = ProjectModelLifecycleStore(
        tmp_path,
        trainer=DeterministicTestTrainer(),
        allow_test_trainer=True,
    )

    job = _start(store, project_id, dataset, annotations)

    assert job["status"] == "succeeded"
    assert job["request"]["training_gate_member_count"] == 100
    assert job["request"]["current_confirmed_label_count"] == 100
    structured_members = [
        item
        for item in job["request"]["current_training_annotations"]
        if item["answered_by"] == "structured_llm_assisted"
    ]
    assert len(structured_members) == 1
    assert structured_members[0]["training_authorized"] is True
    assert structured_members[0]["user_confirmed"] is False


def test_two_reference_photos_share_group_weight_but_no_gate_credit(
    tmp_path,
):
    project_id = uuid4()
    dataset, annotations = _dataset(
        project_id,
        count=129,
        validation_count=30,
    )
    trainer = DeterministicTestTrainer()
    store = ProjectModelLifecycleStore(
        tmp_path,
        trainer=trainer,
        allow_test_trainer=True,
    )
    references = [
        _approved_reference(
            project_id,
            index=1,
            group_seed="one-event",
            group_photo_index=1,
            group_photo_count=2,
        ),
        _approved_reference(
            project_id,
            index=2,
            group_seed="one-event",
            group_photo_index=2,
            group_photo_count=2,
        ),
    ]

    job = _start(
        store,
        project_id,
        dataset,
        annotations,
        references=references,
    )

    assert job["status"] == "blocked"
    assert job["request"][
        "approved_reference_training_member_count"
    ] == 2
    assert job["request"][
        "approved_reference_training_event_count"
    ] == 1
    assert job["request"]["training_gate_member_count"] == 99
    seeds = job["request"]["auxiliary_reference_seeds"]
    assert [seed["training_weight"] for seed in seeds] == [
        0.125,
        0.125,
    ]
    assert all(
        seed["counts_toward_training_gate"] is False
        for seed in seeds
    )


def test_validation_gate_and_production_unavailable_are_distinct(tmp_path):
    project_id = uuid4()
    dataset, annotations = _dataset(
        project_id,
        count=130,
        validation_count=20,
    )
    available = DeterministicTestTrainer()
    gated = ProjectModelLifecycleStore(
        tmp_path / "gated",
        trainer=available,
        allow_test_trainer=True,
    )
    job = _start(gated, project_id, dataset, annotations)
    assert job["status"] == "blocked"
    assert job["blockers"][0]["code"] == (
        "insufficient_validation_samples"
    )
    assert available.calls == 0

    valid_dataset, valid_annotations = _dataset(project_id)
    production = ProjectModelLifecycleStore(tmp_path / "production")
    unavailable = _start(
        production,
        project_id,
        valid_dataset,
        valid_annotations,
    )
    assert unavailable["status"] == "blocked"
    assert {
        blocker["code"] for blocker in unavailable["blockers"]
    } == {"trainer_unavailable"}
    assert production.list_model_versions(project_id)["count"] == 0


def test_issue_types_are_server_derived_and_validation_stays_project_fixed(
    tmp_path,
):
    project_id = uuid4()
    first_dataset, first_annotations = _dataset(
        project_id,
        issue_type="sign_damage",
    )
    reference = _approved_reference(
        project_id,
        issue_type="sign_damage",
    )
    store = ProjectModelLifecycleStore(
        tmp_path,
        trainer=DeterministicTestTrainer(),
        allow_test_trainer=True,
    )
    first = _start(
        store,
        project_id,
        first_dataset,
        first_annotations,
        references=[reference],
        key="server-derived-types-01",
    )
    assert first["status"] == "succeeded"
    assert first["request"]["required_issue_types"] == [
        "sign_damage"
    ]
    assert first["request"]["fixed_validation_persisted"] is True
    assert first["request"]["current_train_member_count"] == 100
    assert first["request"]["current_validation_member_count"] == 30
    assert first["request"]["training_gate_member_count"] == 100
    first_fixed_hash = first["request"]["fixed_validation_sha256"]
    first_membership_hash = first["request"][
        "fixed_validation_membership_sha256"
    ]
    origin_dataset_id = first["request"]["dataset_version_id"]

    candidate = store.list_model_versions(project_id)["models"][0]
    store.reject(
        project_id,
        UUID(candidate["model_version_id"]),
        expected_revision=1,
        idempotency_key="reject-first-fixed-set",
        user_confirmed=True,
    )

    second_dataset, second_annotations = _dataset(
        project_id,
        count=131,
        issue_type="sign_damage",
    )
    second = _start(
        store,
        project_id,
        second_dataset,
        second_annotations,
        references=[reference],
        key="server-derived-types-02",
    )
    assert second["status"] == "succeeded"
    assert second["request"]["dataset_version_id"] != origin_dataset_id
    assert (
        second["request"]["fixed_validation_sha256"]
        == first_fixed_hash
    )
    assert (
        second["request"]["fixed_validation_membership_sha256"]
        == first_membership_hash
    )
    assert (
        second["request"][
            "fixed_validation_origin_dataset_version_id"
        ]
        == origin_dataset_id
    )
    assert all(
        row.get("source_scope") == "current_video_confirmed"
        for row in second["request"]["fixed_validation_annotations"]
    )
    assert second["request"][
        "auxiliary_references_count_toward_validation"
    ] is False


def test_new_issue_type_requires_separate_fixed_validation_approval(
    tmp_path,
):
    project_id = uuid4()
    dataset, annotations = _dataset(project_id)
    trainer = DeterministicTestTrainer()
    store = ProjectModelLifecycleStore(
        tmp_path,
        trainer=trainer,
        allow_test_trainer=True,
    )
    first = _start(
        store,
        project_id,
        dataset,
        annotations,
        key="fixed-validation-v2-parent",
    )
    candidate = store.list_model_versions(project_id)["models"][0]
    store.reject(
        project_id,
        UUID(candidate["model_version_id"]),
        expected_revision=1,
        idempotency_key="reject-fixed-validation-v2-parent",
        user_confirmed=True,
    )
    expanded_dataset, expanded_annotations = (
        _append_fixed_validation_issue(
            project_id,
            dataset,
            annotations,
            issue_type="sign_damage",
        )
    )

    with pytest.raises(ProjectModelLifecycleError) as blocked:
        _start(
            store,
            project_id,
            expanded_dataset,
            expanded_annotations,
            key="fixed-validation-v2-no-approval",
        )

    assert blocked.value.code == (
        "fixed_validation_extension_approval_required"
    )
    assert trainer.calls == 1
    history = store.list_fixed_validation_versions(project_id)
    assert history["count"] == 1
    assert history["versions"][0]["validation_set_version"] == 1
    assert first["request"]["fixed_validation_set_version"] == 1


def test_fixed_validation_extension_requires_thirty_positive_samples(
    tmp_path,
):
    project_id = uuid4()
    dataset, annotations = _dataset(project_id)
    trainer = DeterministicTestTrainer()
    store = ProjectModelLifecycleStore(
        tmp_path,
        trainer=trainer,
        allow_test_trainer=True,
    )
    first = _start(
        store,
        project_id,
        dataset,
        annotations,
        key="fixed-validation-v2-insufficient-parent",
    )
    candidate = store.list_model_versions(project_id)["models"][0]
    store.reject(
        project_id,
        UUID(candidate["model_version_id"]),
        expected_revision=1,
        idempotency_key="reject-fixed-validation-v2-insufficient",
        user_confirmed=True,
    )
    expanded_dataset, expanded_annotations = (
        _append_fixed_validation_issue(
            project_id,
            dataset,
            annotations,
            issue_type="sign_damage",
            count=29,
        )
    )

    with pytest.raises(ProjectModelLifecycleError) as blocked:
        _start(
            store,
            project_id,
            expanded_dataset,
            expanded_annotations,
            key="fixed-validation-v2-insufficient-child",
            fixed_validation_extension_approval=_extension_approval(
                first,
                "sign_damage",
                analysis_job_ids={
                    item["analysis_job_id"]
                    for item in expanded_annotations[len(annotations) :]
                },
            ),
        )

    assert blocked.value.code == "fixed_validation_extension_insufficient"
    assert trainer.calls == 1
    assert store.list_fixed_validation_versions(project_id)["count"] == 1


def test_approved_fixed_validation_extension_creates_immutable_child_version(
    tmp_path,
):
    project_id = uuid4()
    dataset, annotations = _dataset(project_id)
    trainer = DeterministicTestTrainer()
    store = ProjectModelLifecycleStore(
        tmp_path,
        trainer=trainer,
        allow_test_trainer=True,
    )
    first = _start(
        store,
        project_id,
        dataset,
        annotations,
        key="fixed-validation-v2-valid-parent",
    )
    candidate = store.list_model_versions(project_id)["models"][0]
    store.reject(
        project_id,
        UUID(candidate["model_version_id"]),
        expected_revision=1,
        idempotency_key="reject-fixed-validation-v2-valid-parent",
        user_confirmed=True,
    )
    expanded_dataset, expanded_annotations = (
        _append_fixed_validation_issue(
            project_id,
            dataset,
            annotations,
            issue_type="sign_damage",
        )
    )
    extension_job_ids = {
        item["analysis_job_id"]
        for item in expanded_annotations[len(annotations) :]
    }
    stale_approval = _extension_approval(
        first,
        "sign_damage",
        analysis_job_ids=extension_job_ids,
    )
    stale_approval["expected_parent_membership_sha256"] = "0" * 64
    with pytest.raises(ProjectModelLifecycleError) as stale:
        _start(
            store,
            project_id,
            expanded_dataset,
            expanded_annotations,
            key="fixed-validation-v2-stale-child",
            fixed_validation_extension_approval=stale_approval,
        )
    assert stale.value.code == "fixed_validation_extension_approval_stale"
    assert trainer.calls == 1

    child = _start(
        store,
        project_id,
        expanded_dataset,
        expanded_annotations,
        key="fixed-validation-v2-valid-child",
        fixed_validation_extension_approval=_extension_approval(
            first,
            "sign_damage",
            analysis_job_ids=extension_job_ids,
        ),
    )

    assert child["status"] == "succeeded"
    request = child["request"]
    assert request["fixed_validation_set_version"] == 2
    assert request["fixed_validation_parent_version"] == 1
    assert request["fixed_validation_extension_approval_sha256"]
    assert request["required_issue_types"] == [
        "pothole",
        "sign_damage",
    ]
    assert request["validation_sample_counts"] == {
        "pothole": 30,
        "sign_damage": 30,
    }
    history = store.list_fixed_validation_versions(project_id)
    assert history["count"] == 2
    parent, child_version = history["versions"]
    assert child_version["parent_membership_sha256"] == (
        parent["membership_sha256"]
    )
    assert child_version["parent_fixed_validation_sha256"] == (
        parent["fixed_validation_sha256"]
    )
    assert child_version["fixed_validation_truth"][
        : len(parent["fixed_validation_truth"])
    ] == parent["fixed_validation_truth"]
    assert child_version["extension_positive_sample_counts"] == {
        "sign_damage": 30
    }
    new_model = next(
        model
        for model in store.list_model_versions(project_id)["models"]
        if model["training_job_id"] == child["training_job_id"]
    )
    assert new_model["fixed_validation_set_version"] == 2
    assert trainer.calls == 2


def test_confirmation_unconfirmed_reference_and_project_boundaries(tmp_path):
    project_id = uuid4()
    other_project_id = uuid4()
    dataset, annotations = _dataset(project_id)
    store = ProjectModelLifecycleStore(
        tmp_path,
        trainer=DeterministicTestTrainer(),
        allow_test_trainer=True,
    )

    with pytest.raises(
        ProjectModelLifecycleError,
        match="explicit user confirmation",
    ) as confirmation:
        store.start_training(
            project_id,
            dataset_version=dataset,
            current_annotations=annotations,
            auxiliary_references=[_approved_reference(project_id)],
            base_model_version="common-initial@1.0.0",
            required_issue_types=["pothole"],
            idempotency_key="no-confirmation-01",
            user_confirmed=False,
        )
    assert confirmation.value.code == "training_confirmation_required"

    frozen, frozen_references = _freeze_references(
        project_id,
        dataset,
        [_approved_reference(project_id)],
    )
    frozen["approved_reference_members"][0][
        "user_confirmed"
    ] = False
    with pytest.raises(ProjectModelLifecycleError) as unconfirmed:
        store.start_training(
            project_id,
            dataset_version=frozen,
            current_annotations=annotations,
            auxiliary_references=frozen_references,
            base_model_version="common-initial@1.0.0",
            required_issue_types=["pothole"],
            idempotency_key="unconfirmed-reference-01",
            user_confirmed=True,
        )
    assert unconfirmed.value.code == "dataset_integrity_failed"

    with pytest.raises(ProjectModelLifecycleError) as crossing:
        _start(
            store,
            other_project_id,
            dataset,
            annotations,
            references=[_approved_reference(other_project_id)],
        )
    assert crossing.value.code == "project_boundary_violation"


def test_training_rejects_tampered_address_and_stale_reference_revision(
    tmp_path,
):
    project_id = uuid4()
    dataset, annotations = _dataset(project_id)
    original = _approved_reference(project_id)
    frozen, frozen_references = _freeze_references(
        project_id,
        dataset,
        [original],
    )
    store = ProjectModelLifecycleStore(
        tmp_path,
        trainer=DeterministicTestTrainer(),
        allow_test_trainer=True,
    )

    address_tampered = copy.deepcopy(frozen)
    address_tampered["approved_reference_members"][0][
        "cell_row"
    ] += 1
    with pytest.raises(ProjectModelLifecycleError) as address_error:
        store.start_training(
            project_id,
            dataset_version=address_tampered,
            current_annotations=annotations,
            auxiliary_references=frozen_references,
            base_model_version="common-initial@1.0.0",
            required_issue_types=["pothole"],
            idempotency_key="tampered-reference-address-01",
            user_confirmed=True,
        )
    assert address_error.value.code == "dataset_integrity_failed"

    revised_value = {
        **original,
        "revision": 2,
        "maintenance_action": "resurface",
    }
    revised_value.pop("record_sha256")
    revised = {
        **revised_value,
        "record_sha256": _sha(revised_value),
    }
    revised_members = freeze_approved_reference_records(
        project_id,
        [revised],
    )
    with pytest.raises(ProjectModelLifecycleError) as stale_error:
        store.start_training(
            project_id,
            dataset_version=frozen,
            current_annotations=annotations,
            auxiliary_references=revised_members,
            base_model_version="common-initial@1.0.0",
            required_issue_types=["pothole"],
            idempotency_key="stale-reference-revision-01",
            user_confirmed=True,
        )
    assert stale_error.value.code == "dataset_integrity_failed"


def test_explicit_promote_and_rollback_preserve_active_previous(tmp_path):
    project_id = uuid4()
    dataset, annotations = _dataset(project_id)
    store = ProjectModelLifecycleStore(
        tmp_path,
        trainer=DeterministicTestTrainer(),
        allow_test_trainer=True,
    )
    job = _start(store, project_id, dataset, annotations)
    model = store.list_model_versions(project_id)["models"][0]
    state = store.get_project_state(project_id)
    assert state["revision"] == 1
    assert state["active_model_version"] == "common-initial@1.0.0"
    assert state["candidate_model_id"] == model["model_version_id"]

    promoted = store.promote(
        project_id,
        UUID(model["model_version_id"]),
        expected_revision=1,
        idempotency_key="promote-model-0001",
        user_confirmed=True,
    )
    assert promoted["automatic_transition"] is False
    after_promote = promoted["after_state"]
    assert after_promote["revision"] == 2
    assert after_promote["active_model_id"] == model["model_version_id"]
    assert (
        after_promote["previous_model_version"]
        == "common-initial@1.0.0"
    )
    assert after_promote["candidate_model_id"] is None
    assert store.list_model_versions(project_id)["models"][0]["state"] == (
        "active"
    )
    resolved = store.resolve_active_model(project_id)
    assert resolved.model_version_id == model["model_version_id"]
    assert resolved.primary_artifact_path.is_file()
    assert (
        hashlib.sha256(resolved.primary_artifact_path.read_bytes()).hexdigest()
        == resolved.primary_artifact_sha256
    )

    replay = store.promote(
        project_id,
        UUID(model["model_version_id"]),
        expected_revision=1,
        idempotency_key="promote-model-0001",
        user_confirmed=True,
    )
    assert replay["idempotent_replay"] is True
    assert replay["event_id"] == promoted["event_id"]

    rolled_back = store.rollback(
        project_id,
        expected_revision=2,
        idempotency_key="rollback-model-001",
        user_confirmed=True,
    )
    after_rollback = rolled_back["after_state"]
    assert after_rollback["revision"] == 3
    assert after_rollback["active_model_id"] is None
    assert (
        after_rollback["active_model_version"]
        == "common-initial@1.0.0"
    )
    assert (
        after_rollback["previous_model_id"]
        == model["model_version_id"]
    )
    assert store.list_model_versions(project_id)["models"][0]["state"] == (
        "previous"
    )
    with pytest.raises(ProjectModelLifecycleError) as baseline:
        store.resolve_active_model(project_id)
    assert baseline.value.code == (
        "active_project_model_artifact_unavailable"
    )
    assert job["model_version_id"] == model["model_version_id"]


def test_rejected_validation_is_automatically_rejected_without_candidate_slot(
    tmp_path,
):
    project_id = uuid4()
    dataset, annotations = _dataset(project_id)
    store = ProjectModelLifecycleStore(
        tmp_path,
        trainer=DeterministicTestTrainer(candidate_mode="empty"),
        allow_test_trainer=True,
    )
    _start(store, project_id, dataset, annotations)
    model = store.list_model_versions(project_id)["models"][0]
    assert model["validation_decision"] == "rejected"
    assert model["state"] == "rejected"
    assert model["automatic_rejection"] is True
    assert model["user_promotion_required"] is False
    assert {
        reason["code"]
        for reason in model["promotion_review"]["reasons"]
    } == {
        "candidate_recall_below_absolute_minimum",
        "candidate_precision_below_absolute_minimum",
        "recall_drop_at_or_above_limit",
    }
    assert store.get_project_state(project_id)["candidate_model_id"] is None
    artifact_directory = (
        tmp_path
        / "project-model-lifecycle"
        / Path(model["artifact_directory"])
    )
    assert (
        artifact_directory / model["primary_artifact_relative_path"]
    ).is_file()
    rejected_manifest = json.loads(
        (artifact_directory / "artifact-manifest.json").read_bytes()
    )
    assert rejected_manifest["promotion_review"]["decision"] == (
        "rejected"
    )
    assert rejected_manifest["files"]

    with pytest.raises(ProjectModelLifecycleError) as blocked:
        store.promote(
            project_id,
            UUID(model["model_version_id"]),
            expected_revision=1,
            idempotency_key="bad-promote-0001",
            user_confirmed=True,
        )
    assert blocked.value.code == "candidate_not_found"

    with pytest.raises(ProjectModelLifecycleError) as redundant_reject:
        store.reject(
            project_id,
            UUID(model["model_version_id"]),
            expected_revision=1,
            idempotency_key="reject-model-0001",
            user_confirmed=True,
        )
    assert redundant_reject.value.code == "candidate_not_found"

    next_dataset, next_annotations = _dataset(
        project_id,
        count=131,
        validation_count=30,
    )
    next_job = _start(
        store,
        project_id,
        next_dataset,
        next_annotations,
        key="train-after-auto-reject-0001",
    )
    assert next_job["status"] == "succeeded"
    assert store.get_project_state(project_id)["candidate_model_id"] is None
    assert store.list_model_versions(project_id)["count"] == 2


class _CrashOnceTrainer(DeterministicTestTrainer):
    trainer_id = "crash-once-test"

    def __init__(self):
        super().__init__()
        self.crash_next = True

    def train(self, invocation: TrainerInvocation) -> TrainerResult:
        if self.crash_next:
            self.crash_next = False
            self.calls += 1
            raise SystemExit("simulated process termination")
        return super().train(invocation)


class _AsyncControlTrainer(DeterministicTestTrainer):
    trainer_id = "durable-async-control-test"

    def __init__(self, *, crash_first: bool = False):
        super().__init__()
        self.crash_first = crash_first
        self.executions = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.resume_checkpoint_sha256 = None

    def train(self, invocation: TrainerInvocation) -> TrainerResult:
        self.executions += 1
        self.resume_checkpoint_sha256 = (
            invocation.resume_checkpoint_sha256
        )
        checkpoint = invocation.output_directory / "checkpoint.pt"
        checkpoint.write_bytes(
            canonical_json_bytes(
                {
                    "request_manifest_sha256": (
                        invocation.request_manifest_sha256
                    ),
                    "execution": self.executions,
                }
            )
        )
        assert invocation.checkpoint_callback is not None
        invocation.checkpoint_callback(checkpoint)
        assert invocation.progress_callback is not None
        invocation.progress_callback(
            {
                "stage": "fine_tune",
                "epoch": 1,
                "epochs": 2,
                "current": 1,
                "total": 2,
                "loss": 0.25,
            }
        )
        self.started.set()
        if self.crash_first and self.executions == 1:
            raise SystemExit("simulated CUDA worker termination")
        while not self.release.wait(0.01):
            if (
                invocation.cancel_callback is not None
                and invocation.cancel_callback()
            ):
                raise ProjectModelLifecycleError(
                    "test_training_cancelled",
                    "controlled training was cancelled",
                )
        if (
            invocation.cancel_callback is not None
            and invocation.cancel_callback()
        ):
            raise ProjectModelLifecycleError(
                "test_training_cancelled",
                "controlled training was cancelled",
            )
        return super().train(invocation)


def test_async_start_returns_queued_persists_progress_and_runs_once(
    tmp_path,
):
    project_id = uuid4()
    dataset, annotations = _dataset(project_id)
    trainer = _AsyncControlTrainer()
    store = ProjectModelLifecycleStore(
        tmp_path,
        trainer=trainer,
        allow_test_trainer=True,
    )

    started_at = time.monotonic()
    queued = _start(
        store,
        project_id,
        dataset,
        annotations,
        wait_for_terminal=False,
    )
    elapsed = time.monotonic() - started_at
    assert elapsed < 1.0
    assert queued["status"] == "queued"
    assert queued["attempt_count"] == 0
    assert trainer.started.wait(2)

    running = _wait_for_job(
        store,
        project_id,
        queued["training_job_id"],
        terminal={"running"},
    )
    assert running["progress"]["stage"] == "fine_tune"
    assert running["progress"]["current"] == 1
    assert running["progress"]["total"] == 2
    assert running["checkpoint"]["schema_version"] == (
        "trafficvisson.project-model-training-checkpoint.v1"
    )
    assert running["checkpoint"]["relative_path"].endswith(
        "/staging/checkpoint.pt"
    )

    replay = _start(
        store,
        project_id,
        dataset,
        annotations,
        wait_for_terminal=False,
    )
    assert replay["idempotent_replay"] is True
    assert replay["training_job_id"] == queued["training_job_id"]
    assert trainer.executions == 1

    trainer.release.set()
    succeeded = _wait_for_job(
        store,
        project_id,
        queued["training_job_id"],
        terminal={"succeeded"},
    )
    assert succeeded["progress"]["stage"] == "completed"
    assert succeeded["checkpoint"] is None
    assert trainer.executions == 1
    store.shutdown()


def test_running_training_cancel_is_durable_and_resumable(tmp_path):
    project_id = uuid4()
    dataset, annotations = _dataset(project_id)
    trainer = _AsyncControlTrainer()
    store = ProjectModelLifecycleStore(
        tmp_path,
        trainer=trainer,
        allow_test_trainer=True,
    )
    queued = _start(
        store,
        project_id,
        dataset,
        annotations,
        wait_for_terminal=False,
    )
    assert trainer.started.wait(2)

    acknowledged = store.cancel_training(
        project_id,
        UUID(queued["training_job_id"]),
        user_confirmed=True,
    )
    assert acknowledged["cancel_requested"] is True
    cancelled = _wait_for_job(
        store,
        project_id,
        queued["training_job_id"],
        terminal={"cancelled"},
    )
    assert cancelled["error"]["code"] == "training_cancelled"
    assert cancelled["checkpoint"] is not None
    assert store.list_model_versions(project_id)["count"] == 0

    trainer.started.clear()
    trainer.release.set()
    with pytest.raises(
        ProjectModelLifecycleError,
        match="explicit user confirmation",
    ):
        store.resume_training(
            project_id,
            UUID(queued["training_job_id"]),
            user_confirmed=False,
        )
    resumed = store.resume_training(
        project_id,
        UUID(queued["training_job_id"]),
        user_confirmed=True,
    )
    assert resumed["status"] == "queued"
    assert resumed["resume_count"] == 1
    succeeded = _wait_for_job(
        store,
        project_id,
        queued["training_job_id"],
        terminal={"succeeded"},
    )
    assert trainer.resume_checkpoint_sha256 == (
        cancelled["checkpoint"]["sha256"]
    )
    assert succeeded["attempt_count"] == 2
    assert trainer.executions == 2
    store.shutdown()


def test_worker_crash_checkpoint_survives_restart_and_explicit_resume(
    tmp_path,
):
    project_id = uuid4()
    dataset, annotations = _dataset(project_id)
    crashing = _AsyncControlTrainer(crash_first=True)
    first = ProjectModelLifecycleStore(
        tmp_path,
        trainer=crashing,
        allow_test_trainer=True,
    )
    queued = _start(
        first,
        project_id,
        dataset,
        annotations,
        wait_for_terminal=False,
    )
    interrupted = _wait_for_job(
        first,
        project_id,
        queued["training_job_id"],
        terminal={"interrupted"},
    )
    checkpoint_sha = interrupted["checkpoint"]["sha256"]
    first.shutdown()

    resumed_trainer = _AsyncControlTrainer()
    resumed_trainer.release.set()
    restarted = ProjectModelLifecycleStore(
        tmp_path,
        trainer=resumed_trainer,
        allow_test_trainer=True,
    )
    recovered = restarted.get_training_job(
        project_id,
        UUID(queued["training_job_id"]),
    )
    assert recovered["status"] == "interrupted"
    assert resumed_trainer.executions == 0

    resume_ack = restarted.resume_training(
        project_id,
        UUID(queued["training_job_id"]),
        user_confirmed=True,
    )
    assert resume_ack["status"] == "queued"
    succeeded = _wait_for_job(
        restarted,
        project_id,
        queued["training_job_id"],
        terminal={"succeeded"},
    )
    assert succeeded["attempt_count"] == 2
    assert resumed_trainer.resume_checkpoint_sha256 == checkpoint_sha
    restarted.shutdown()


def test_running_job_is_recovered_as_interrupted_and_resumes(tmp_path):
    project_id = uuid4()
    dataset, annotations = _dataset(project_id)
    trainer = _CrashOnceTrainer()
    store = ProjectModelLifecycleStore(
        tmp_path,
        trainer=trainer,
        allow_test_trainer=True,
    )
    interrupted = _start(store, project_id, dataset, annotations)
    assert interrupted["status"] == "interrupted"
    assert interrupted["error"]["code"] == "worker_boundary_interrupted"
    store.shutdown()

    recovered = ProjectModelLifecycleStore(
        tmp_path,
        trainer=trainer,
        allow_test_trainer=True,
    )
    interrupted = recovered.list_training_jobs(project_id)["jobs"][0]
    assert interrupted["status"] == "interrupted"
    assert interrupted["error"]["code"] == "worker_boundary_interrupted"

    queued = recovered.resume_training(
        project_id,
        UUID(interrupted["training_job_id"]),
        user_confirmed=True,
    )
    assert queued["status"] == "queued"
    deadline = time.monotonic() + 10
    resumed = queued
    while time.monotonic() < deadline:
        resumed = recovered.get_training_job(
            project_id,
            UUID(interrupted["training_job_id"]),
        )
        if resumed["status"] not in {"queued", "running"}:
            break
        time.sleep(0.01)
    assert resumed["status"] == "succeeded"
    assert resumed["attempt_count"] == 2
    assert recovered.list_model_versions(project_id)["count"] == 1


class _MissingArtifactTrainer:
    trainer_id = "missing-artifact-test"
    trainer_version = "1.0"
    available = True
    test_only = True

    def train(self, invocation: TrainerInvocation) -> TrainerResult:
        return TrainerResult(
            model_version=(
                f"missing@{invocation.request_manifest_sha256[:12]}"
            ),
            model_format=VISUAL_CENTROID_MODEL_FORMAT,
            primary_artifact_relative_path="does-not-exist.bin",
            active_prediction_records=(),
            candidate_prediction_records=(),
            metadata={"test_only": True},
        )


def test_candidate_requires_an_actual_hash_verified_artifact(tmp_path):
    project_id = uuid4()
    dataset, annotations = _dataset(project_id)
    store = ProjectModelLifecycleStore(
        tmp_path,
        trainer=_MissingArtifactTrainer(),
        allow_test_trainer=True,
    )
    job = _start(store, project_id, dataset, annotations)
    assert job["status"] == "failed"
    assert job["error"]["code"] == "artifact_integrity_failed"
    assert store.list_model_versions(project_id)["count"] == 0
    assert store.get_project_state(project_id)["candidate_model_id"] is None


def test_tampered_candidate_artifact_cannot_be_promoted(tmp_path):
    project_id = uuid4()
    dataset, annotations = _dataset(project_id)
    store = ProjectModelLifecycleStore(
        tmp_path,
        trainer=DeterministicTestTrainer(),
        allow_test_trainer=True,
    )
    _start(store, project_id, dataset, annotations)
    model = store.list_model_versions(project_id)["models"][0]
    primary = (
        tmp_path
        / "project-model-lifecycle"
        / Path(model["artifact_directory"])
        / model["primary_artifact_relative_path"]
    )
    primary.write_bytes(primary.read_bytes() + b"\ntampered")

    with pytest.raises(ProjectModelLifecycleError) as blocked:
        store.promote(
            project_id,
            UUID(model["model_version_id"]),
            expected_revision=1,
            idempotency_key="tampered-promote-01",
            user_confirmed=True,
        )
    assert blocked.value.code == "artifact_integrity_failed"
    state = store.get_project_state(project_id)
    assert state["active_model_version"] == "common-initial@1.0.0"
    assert state["candidate_model_id"] == model["model_version_id"]
