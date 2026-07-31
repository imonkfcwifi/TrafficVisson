from __future__ import annotations

from uuid import uuid4, uuid5

import pytest

from app.structured_assist_provenance import (
    SCHEMA_VERSION,
    StructuredAssistProvenanceError,
    canonical_structured_assist_provenance,
    per_item_user_grant_sha256,
    project_profile_delegation_authority,
    seal_structured_assist_provenance,
)


def _value(*, authority_kind="project_profile_delegation"):
    project_id = uuid4()
    analysis_job_id = uuid4()
    candidate_id = "a" * 20
    question_id = uuid5(analysis_job_id, candidate_id)
    authority = (
        {
            "kind": "project_profile_delegation",
            "profile_schema_version": (
                "trafficvisson.pipeline-profile.v4"
            ),
            "profile_revision": 3,
            "profile_sha256": "1" * 64,
            "grant_sha256": None,
        }
        if authority_kind == "project_profile_delegation"
        else {
            "kind": "per_item_user_grant",
            "profile_schema_version": None,
            "profile_revision": None,
            "profile_sha256": None,
            "grant_sha256": per_item_user_grant_sha256(
                project_id=project_id,
                analysis_job_id=analysis_job_id,
                question_id=question_id,
                candidate_id=candidate_id,
            ),
        }
    )
    return seal_structured_assist_provenance(
        {
            "schema_version": SCHEMA_VERSION,
            "project_id": str(project_id),
            "analysis_job_id": str(analysis_job_id),
            "question_id": str(question_id),
            "candidate_id": candidate_id,
            "authority": authority,
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
            "provider": "codex",
            "model": "gpt-5.6-codex",
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
            "proposal_sha256": "f" * 64,
        }
    )


@pytest.mark.parametrize(
    "authority_kind",
    ["project_profile_delegation", "per_item_user_grant"],
)
def test_authority_modes_are_canonical(authority_kind):
    value = _value(authority_kind=authority_kind)

    assert canonical_structured_assist_provenance(value) == value
    assert value["authority"]["kind"] == authority_kind


def test_tampered_prompt_is_rejected():
    value = _value()
    value["prompt_sha256"] = "0" * 64

    with pytest.raises(
        StructuredAssistProvenanceError,
        match="digest",
    ):
        canonical_structured_assist_provenance(value)


def test_cross_question_binding_is_rejected():
    value = _value()
    value["question_id"] = str(uuid4())
    value.pop("provenance_sha256")

    with pytest.raises(
        StructuredAssistProvenanceError,
        match="deterministic",
    ):
        seal_structured_assist_provenance(value)


def test_attachment_hash_lists_must_match():
    value = _value()
    value["attachment_verification_record_sha256s"].pop()
    value.pop("provenance_sha256")

    with pytest.raises(
        StructuredAssistProvenanceError,
        match="incomplete",
    ):
        seal_structured_assist_provenance(value)


def test_project_delegation_requires_separate_provider_transmission_consent():
    project_id = uuid4()
    base = {
        "schema_version": "trafficvisson.pipeline-profile.v4",
        "project_id": str(project_id),
        "label_confirmation_authority": (
            "structured_llm_training_assist"
        ),
        "report_provider": "codex",
        "report_transmission_consent": True,
        "revision": 1,
    }

    authority = project_profile_delegation_authority(
        base,
        project_id=project_id,
    )
    assert authority["kind"] == "project_profile_delegation"

    for unsafe in (
        {**base, "report_transmission_consent": False},
        {**base, "report_provider": "none"},
    ):
        with pytest.raises(
            StructuredAssistProvenanceError,
            match="not authorized",
        ):
            project_profile_delegation_authority(
                unsafe,
                project_id=project_id,
            )
