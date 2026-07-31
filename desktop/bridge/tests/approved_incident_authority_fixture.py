from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID, uuid5

from app.approved_incident_authority import (
    build_approved_incident_output_authority,
)


def fixture_candidate_id(label: str) -> str:
    return hashlib.sha256(
        f"candidate:{label}".encode("utf-8")
    ).hexdigest()[:20]


def fixture_incident_id(label: str, analysis_job_id: UUID) -> str:
    return str(
        uuid5(analysis_job_id, fixture_candidate_id(label))
    )


def fixture_approval_record_id(
    label: str,
    pipeline_job_id: UUID,
) -> str:
    return str(uuid5(pipeline_job_id, f"approval:{label}"))


def fixture_output_authority(
    *,
    label: str,
    project_id: UUID,
    pipeline_job_id: UUID,
    analysis_job_id: UUID,
    source_sha256: str,
    approval_record_sha256: str,
    representative_image_ref: str,
    representative_image_sha256: str,
    incident_id: str | None = None,
    approval_record_id: str | None = None,
    survey_incident_authority_sha256: str = "1" * 64,
) -> dict[str, Any]:
    candidate_id = fixture_candidate_id(label)
    resolved_incident_id = (
        incident_id
        or fixture_incident_id(label, analysis_job_id)
    )
    resolved_approval_record_id = (
        approval_record_id
        or fixture_approval_record_id(label, pipeline_job_id)
    )
    token = hashlib.sha256(
        f"review-event:{label}".encode("utf-8")
    ).hexdigest()
    survey = {
        "project_id": str(project_id),
        "pipeline_job_id": str(pipeline_job_id),
        "analysis_job_id": str(analysis_job_id),
        "source_sha256": source_sha256,
        "model_version_id": "model-promoted-fixture-v1",
        "dataset_version_id": "dataset-promoted-fixture-v1",
        "dataset_hash": "2" * 64,
        "dataset_provenance_graph_sha256": "3" * 64,
        "fixed_validation_sha256": "4" * 64,
        "primary_artifact_sha256": "5" * 64,
        "artifact_manifest_sha256": "6" * 64,
        "source_analysis_lineage_sha256": "7" * 64,
        "promoted_model_inference_sha256": "8" * 64,
        "question_queue_revision": "9" * 64,
        "review_completion_id": "review:" + ("a" * 64),
        "answer_ledger_sha256": "b" * 64,
        "survey_qa_revision": "c" * 64,
        "final_qa_choice": "no_miss_complete",
    }
    return build_approved_incident_output_authority(
        survey_authority=survey,
        survey_incident_authority_sha256=(
            survey_incident_authority_sha256
        ),
        incident_id=resolved_incident_id,
        question_id=resolved_incident_id,
        candidate_id=candidate_id,
        review_event_binding={
            "schema_version": (
                "trafficvisson.review-event-binding.v1"
            ),
            "review_event_set_id": "res_" + token[:32],
            "review_event_set_revision": "d" * 64,
            "review_event_set_manifest_sha256": "e" * 64,
            "review_event_id": "rve_" + token[32:],
            "review_event_revision": "f" * 64,
            "representative_candidate_id": candidate_id,
            "review_only": True,
        },
        approval_record_id=resolved_approval_record_id,
        approval_record_sha256=approval_record_sha256,
        answer_revision=1,
        candidate_metadata_sha256="a" * 64,
        representative_image_ref=representative_image_ref,
        representative_image_sha256=representative_image_sha256,
        preview_clip_ref=f"preview:{candidate_id}",
        preview_clip_sha256="b" * 64,
    )
