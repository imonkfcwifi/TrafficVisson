from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from app.dataset_versions import (
    DATASET_SCHEMA_VERSION,
    canonical_json_bytes,
    freeze_approved_reference_records,
)
from app.project_model_lifecycle import ProjectModelLifecycleStore
from app.visual_centroid_trainer import (
    VisualEmbeddingCentroidHeadTrainer,
)
from app.weak_case_catalog import ResolvedWeakCaseImage


def _sha(value) -> str:
    body = (
        value
        if isinstance(value, bytes)
        else canonical_json_bytes(value)
    )
    return hashlib.sha256(body).hexdigest()


class _RuleEmbedder:
    def model_identity(self):
        return {
            "schema_version": "trafficvisson.image-embedding-model.v1",
            "pack_id": "fixture-local-clip",
            "pack_version": "1.0",
            "manifest_sha256": "1" * 64,
        }

    def execution_provenance(self):
        return {
            "schema_version": (
                "trafficvisson.embedding-execution-provenance.v1"
            ),
            "requested_device": "cuda",
            "resolved_device": "cuda",
            "accelerator": "cuda",
            "accelerator_used": True,
            "torch_version": "fixture",
            "torch_cuda_version": "fixture",
            "cuda_device_name": "fixture GPU",
            "cuda_total_memory_bytes": 12_000_000_000,
            "model_pack_identity_sha256": _sha(
                self.model_identity()
            ),
        }

    def embed_image(self, body: bytes, media_type: str):
        assert media_type == "image/jpeg"
        if body.startswith(b"pothole"):
            return (1.0, 0.0)
        if body.startswith(b"debris"):
            return (0.0, 1.0)
        return (-1.0, -1.0)


@dataclass(frozen=True)
class _Artifact:
    path: Path
    media_type: str
    size_bytes: int
    sha256: str


class _References:
    def __init__(self):
        source_3q = _sha(b"sample-3q")
        case_pothole = _sha(b"case-pothole")
        source_4q = _sha(b"sample-4q")
        case_debris = _sha(b"case-debris")
        self.rows = {
            (
                f"sample-hwpx-3q-{source_3q[:16]}",
                f"hwpx-weak-case-{case_pothole[:32]}",
            ): b"pothole-reference",
            (
                f"sample-hwpx-3q-{source_4q[:16]}",
                f"hwpx-weak-case-{case_debris[:32]}",
            ): b"debris-reference",
        }

    def resolve_case_image_for_visual_index(
        self, source_id: str, case_id: str
    ):
        body = self.rows[(source_id, case_id)]
        return ResolvedWeakCaseImage(
            body=body,
            media_type="image/jpeg",
            size_bytes=len(body),
            sha256=_sha(body),
        )


def _approved_reference(
    project_id: UUID,
    *,
    index: int,
    source_id: str,
    case_id: str,
    issue_type: str,
    image_body: bytes,
):
    source_digest = _sha(source_id.encode())
    case_digest = _sha(case_id.encode())
    normalized_source_id = (
        f"sample-hwpx-3q-{source_digest[:16]}"
    )
    normalized_case_id = (
        f"hwpx-weak-case-{case_digest[:32]}"
    )
    table_id = f"hwpx-table-{case_digest[:24]}"
    semantic_cells = [
        {
            "role": role,
            "cell_target_id": (
                "hwpx-cell-"
                + _sha(f"{case_id}:{role}".encode())[:24]
            ),
            "table_id": table_id,
            "table_index": 1,
            "page_number": 2 if role == "photo_caption" else 1,
            "row": row,
            "column": 0,
            "row_span": 1,
            "column_span": 1,
            "text_excerpt": role,
            "text_sha256": _sha(role.encode()),
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
                + _sha(f"{case_id}:marker".encode())[:24]
            ),
            "table_id": table_id,
            "table_index": 1,
            "page_number": 1,
            "row": 4,
            "column": 0,
            "marker": "■",
            "text_sha256": _sha(
                f"{case_id}:marker-text".encode()
            ),
        }
    ]
    base = {
        "schema_version": "trafficvisson.approved-reference.v2",
        "approval_id": (
            "approved-reference-"
            + hashlib.sha256(
                f"{project_id}:{index}".encode()
            ).hexdigest()[:24]
        ),
        "project_id": str(project_id),
        "source_id": normalized_source_id,
        "case_id": normalized_case_id,
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
        "document_sha256": _sha(
            f"document:{source_id}".encode()
        ),
        "structure_target_summary_sha256": _sha(
            f"structure:{source_id}".encode()
        ),
        "index_manifest_sha256": _sha(
            f"index:{source_id}".encode()
        ),
        "image_sha256": _sha(image_body),
        "image_media_type": "image/jpeg",
        "image_width": 1920,
        "image_height": 1080,
        "quarter": 3,
        "section_path": "Contents/section0.xml",
        "section_index": 0,
        "picture_target_id": (
            f"hwpx-picture-{case_digest[:24]}"
        ),
        "picture_index": 1,
        "picture_xml_id": "1",
        "table_id": table_id,
        "table_index": 1,
        "cell_target_id": (
            f"hwpx-cell-{case_digest[:24]}"
        ),
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
        "sample_layout_binding_sha256": _sha(
            f"binding:{source_id}".encode()
        ),
        "semantic_projection_revision": (
            "audited-photo-caption-evidence-group-v2"
        ),
        "semantic_attestation_sha256": _sha(
            f"semantic:{source_id}".encode()
        ),
        "photo_slot_id": (
            f"sample-photo-slot-{case_digest[:24]}"
        ),
        "sample_evidence_group_id": (
            f"sample-evidence-group-{case_digest[:24]}"
        ),
        "group_photo_index": 1,
        "group_photo_count": 1,
        "duplicate_image_occurrence_count": 1,
        "duplicate_image_review_required": False,
        "duplicate_image_review_confirmed": False,
        "duplicate_image_case_refs": [
            f"{normalized_source_id}/{normalized_case_id}"
        ],
        "square_markers": square_markers,
        "semantic_cells": semantic_cells,
        "weak_candidates": {
            "caption": issue_type,
            "route": "테스트 노선",
            "location": None,
            "evaluation": issue_type,
            "opinion": "보수 필요",
        },
        "caption": issue_type,
        "route": "테스트 노선",
        "location": None,
        "evaluation": issue_type,
        "opinion": "보수 필요",
        "user_confirmed": True,
    }
    return {**base, "record_sha256": _sha(base)}


def _freeze_approved_references(
    project_id: UUID,
    dataset,
    approved,
):
    members = freeze_approved_reference_records(
        project_id,
        approved,
    )
    fingerprint = {
        "approved_reference_members": members,
        "members": dataset["memberships"],
        "project_id": str(project_id),
        "schema_version": DATASET_SCHEMA_VERSION,
        "split_strategy": dataset["manifest"]["split_strategy"],
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
    members_sha256 = _sha(members)
    counts = {
        **dataset["manifest"]["counts"],
        "approved_reference_training_events": len(
            {
                member["sample_evidence_group_id"]
                for member in members
            }
        ),
        "approved_reference_training_members": len(members),
        "training_gate_members": dataset["manifest"]["counts"]["train"],
    }
    return {
        **dataset,
        "dataset_version_id": dataset_id,
        "dataset_hash": dataset_hash,
        "approved_reference_members": members,
        "approved_reference_member_count": len(members),
        "approved_reference_training_event_count": counts[
            "approved_reference_training_events"
        ],
        "approved_reference_members_sha256": members_sha256,
        "training_gate_member_count": counts["training_gate_members"],
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
    }


def _dataset_and_images(project_id: UUID, tmp_path: Path):
    memberships = []
    annotations = []
    artifacts = {}
    for index in range(130):
        if index % 5 in {0, 1}:
            issue_type = "pothole"
            prefix = "pothole"
            partition = "approved"
        elif index % 5 in {2, 3}:
            issue_type = "road_debris"
            prefix = "debris"
            partition = "approved"
        else:
            issue_type = None
            prefix = "negative"
            partition = "negative"
        annotation_id = str(uuid5(project_id, f"annotation:{index}"))
        question_id = str(uuid5(project_id, f"question:{index}"))
        answer_id = str(uuid5(project_id, f"answer:{index}"))
        analysis_id = str(
            uuid5(
                project_id,
                (
                    f"validation-job:{index // 5}"
                    if index < 30
                    else f"train-job:{index // 10}"
                ),
            )
        )
        candidate_id = hashlib.sha256(
            f"candidate:{index}".encode()
        ).hexdigest()[:20]
        source_hash = hashlib.sha256(
            f"answer:{index}".encode()
        ).hexdigest()
        annotation_hash = hashlib.sha256(
            f"annotation:{index}".encode()
        ).hexdigest()
        split = "validation" if index < 30 else "train"
        annotations.append(
            {
                "project_id": str(project_id),
                "annotation_id": annotation_id,
                "question_id": question_id,
                "answer_record_id": answer_id,
                "answer_revision": 1,
                "analysis_job_id": analysis_id,
                "candidate_id": candidate_id,
                "partition": partition,
                "include_in_training": True,
                "selected_issue_type": issue_type,
                "severity": None,
                "maintenance_action": (
                    "repair" if issue_type is not None else None
                ),
                "source_record_hash": source_hash,
                "annotation_hash": annotation_hash,
                "user_confirmed": True,
                "source_kind": "current_video_confirmed",
            }
        )
        memberships.append(
            {
                "analysis_job_id": analysis_id,
                "annotation_hash": annotation_hash,
                "annotation_id": annotation_id,
                "answer_record_id": answer_id,
                "answer_revision": 1,
                "candidate_id": candidate_id,
                "include_in_training": True,
                "partition": partition,
                "question_id": question_id,
                "source_record_hash": source_hash,
                "split": split,
            }
        )
        body = f"{prefix}-{index}".encode()
        path = tmp_path / f"{candidate_id}.jpg"
        path.write_bytes(body)
        artifacts[(analysis_id, candidate_id)] = _Artifact(
            path=path,
            media_type="image/jpeg",
            size_bytes=len(body),
            sha256=_sha(body),
        )
    fingerprint = {
        "members": memberships,
        "project_id": str(project_id),
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
    dataset = {
        "dataset_version_id": dataset_id,
        "project_id": str(project_id),
        "dataset_hash": dataset_hash,
        "manifest": {
            **fingerprint,
            "dataset_hash": dataset_hash,
            "dataset_id": dataset_id,
            "counts": {
                "excluded": 0,
                "source_answers": 130,
                "train": 100,
                "training_members": 130,
                "validation": 30,
            },
        },
        "memberships": memberships,
    }
    return dataset, annotations, artifacts


def test_actual_centroid_trainer_reaches_candidate_then_explicit_promotion(
    tmp_path,
):
    project_id = uuid4()
    dataset, annotations, artifacts = _dataset_and_images(
        project_id,
        tmp_path,
    )

    def resolve(_project_id, analysis_job_id, candidate_id, kind):
        assert kind == "image"
        return artifacts[(str(analysis_job_id), candidate_id)]

    holder = {}

    def resolve_active(active_project_id):
        return holder["store"].resolve_active_model(active_project_id)

    references = _References()
    trainer = VisualEmbeddingCentroidHeadTrainer(
        embedder=_RuleEmbedder(),
        candidate_artifact_resolver=resolve,
        approved_reference_catalog=references,
        active_model_resolver=resolve_active,
        require_gpu_execution=True,
    )
    store = ProjectModelLifecycleStore(
        tmp_path / "lifecycle",
        trainer=trainer,
    )
    holder["store"] = store
    approved = [
        _approved_reference(
            project_id,
            index=1,
            source_id="sample-3q",
            case_id="case-pothole",
            issue_type="pothole",
            image_body=b"pothole-reference",
        ),
        _approved_reference(
            project_id,
            index=2,
            source_id="sample-4q",
            case_id="case-debris",
            issue_type="road_debris",
            image_body=b"debris-reference",
        ),
    ]
    dataset = _freeze_approved_references(
        project_id,
        dataset,
        approved,
    )

    job = store.start_training(
        project_id,
        dataset_version=dataset,
        current_annotations=annotations,
        auxiliary_references=dataset["approved_reference_members"],
        base_model_version="common-initial@1.0.0",
        required_issue_types=["pothole", "road_debris"],
        idempotency_key="actual-centroid-training-0001",
        user_confirmed=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = store.get_training_job(
            project_id,
            UUID(job["training_job_id"]),
        )
        if job["status"] not in {"queued", "running"}:
            break
        time.sleep(0.01)

    assert job["status"] == "succeeded"
    models = store.list_model_versions(project_id)["models"]
    assert len(models) == 1
    assert models[0]["state"] == "candidate"
    assert models[0]["validation_decision"] == "eligible"
    assert models[0]["promotion_review"][
        "auto_promotion_allowed"
    ] is False
    artifact_directory = (
        tmp_path
        / "lifecycle"
        / "project-model-lifecycle"
        / Path(models[0]["artifact_directory"])
    )
    manifest = json.loads(
        (artifact_directory / "artifact-manifest.json").read_bytes()
    )
    assert manifest["metadata"]["fit_split"] == "train_only"
    assert manifest["metadata"]["execution_provenance"][
        "accelerator"
    ] == "cuda"
    assert manifest["metadata"]["execution_provenance"][
        "accelerator_used"
    ] is True

    state = store.get_project_state(project_id)
    promoted = store.promote(
        project_id,
        UUID(models[0]["model_version_id"]),
        expected_revision=state["revision"],
        idempotency_key="actual-centroid-promote-0001",
        user_confirmed=True,
    )
    assert promoted["after_state"]["active_model_id"] == (
        models[0]["model_version_id"]
    )
    resolved = store.resolve_active_model(project_id)
    assert resolved.primary_artifact_path.is_file()
    assert _sha(resolved.primary_artifact_path.read_bytes()) == (
        resolved.primary_artifact_sha256
    )
