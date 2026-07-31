from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4, uuid5

from app.structured_assist_provenance import (
    SCHEMA_VERSION,
    per_item_user_grant_authority,
    project_profile_delegation_authority,
    seal_structured_assist_provenance,
)


def structured_profile(project_id: UUID, *, revision: int = 1) -> dict[str, Any]:
    return {
        "schema_version": "trafficvisson.pipeline-profile.v4",
        "project_id": str(project_id),
        "label_confirmation_authority": (
            "structured_llm_training_assist"
        ),
        "report_provider": "codex",
        "report_transmission_consent": True,
        "revision": revision,
    }


def assist_evidence(
    project_id: UUID,
    analysis_job_id: UUID,
    candidate_id: str,
    *,
    provider: str = "codex",
    proposal_sha256: str = "f" * 64,
) -> dict[str, Any]:
    question_id = uuid5(analysis_job_id, candidate_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": str(project_id),
        "analysis_job_id": str(analysis_job_id),
        "question_id": str(question_id),
        "candidate_id": candidate_id,
        "review_event": {
            "pipeline_job_id": str(uuid4()),
            "overlay_id": f"ceo_{'3' * 32}",
            "review_event_set_id": f"res_{'4' * 32}",
            "review_event_set_revision": "5" * 64,
            "review_event_set_manifest_sha256": "6" * 64,
            "review_event_set_source_sha256": "7" * 64,
            "review_event_id": f"rve_{'8' * 32}",
            "review_event_revision": "9" * 64,
        },
        "provider": provider,
        "model": "test-structured-vision-model",
        "input_sha256": "a" * 64,
        "prompt_sha256": "b" * 64,
        "request_provenance_sha256": "c" * 64,
        "execution_provenance_sha256": "d" * 64,
        "attachment_manifest_sha256": "e" * 64,
        "attachment_sha256s": [
            f"{index:064x}" for index in range(1, 5)
        ],
        "attachment_verification_record_sha256s": [
            f"{index:064x}" for index in range(11, 15)
        ],
        "proposal_sha256": proposal_sha256,
    }


def assist_provenance(
    project_id: UUID,
    analysis_job_id: UUID,
    candidate_id: str,
    *,
    authority_kind: str = "project_profile_delegation",
    proposal_sha256: str = "f" * 64,
) -> dict[str, Any]:
    question_id = uuid5(analysis_job_id, candidate_id)
    if authority_kind == "project_profile_delegation":
        authority = project_profile_delegation_authority(
            structured_profile(project_id),
            project_id=project_id,
        )
    elif authority_kind == "per_item_user_grant":
        authority = per_item_user_grant_authority(
            project_id=project_id,
            analysis_job_id=analysis_job_id,
            question_id=question_id,
            candidate_id=candidate_id,
        )
    else:
        raise ValueError("unsupported test authority")
    return seal_structured_assist_provenance(
        {
            **assist_evidence(
                project_id,
                analysis_job_id,
                candidate_id,
                proposal_sha256=proposal_sha256,
            ),
            "authority": authority,
        }
    )
