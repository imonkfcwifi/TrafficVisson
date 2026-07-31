from __future__ import annotations

from copy import deepcopy
from uuid import UUID, uuid5

import pytest

from app.approved_incident_authority import (
    build_approved_incident_output_authority,
)
from app.current_incident_report_binder import (
    CurrentIncidentReportBindingError,
    _normalize_incidents,
)
from app.derived_outputs import _report_incident_action
from app.pipeline_report_preview import (
    DurablePipelineReportPreviewPreparer,
    PipelineReportPreviewError,
)


PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
PIPELINE_JOB_ID = UUID("22222222-2222-4222-8222-222222222222")
ANALYSIS_JOB_ID = UUID("33333333-3333-4333-8333-333333333333")
SOURCE_SHA256 = "4" * 64
CANDIDATE_ID = "a" * 20
INCIDENT_ID = str(uuid5(ANALYSIS_JOB_ID, CANDIDATE_ID))
APPROVAL_RECORD_ID = "55555555-5555-4555-8555-555555555555"
EVIDENCE_REF = f"analysis:{CANDIDATE_ID}:representative"


def _output_authority() -> dict:
    survey = {
        "project_id": str(PROJECT_ID),
        "pipeline_job_id": str(PIPELINE_JOB_ID),
        "analysis_job_id": str(ANALYSIS_JOB_ID),
        "source_sha256": SOURCE_SHA256,
        "model_version_id": "model-promoted-v1",
        "dataset_version_id": "dataset-promoted-v1",
        "dataset_hash": "6" * 64,
        "dataset_provenance_graph_sha256": "7" * 64,
        "fixed_validation_sha256": "8" * 64,
        "primary_artifact_sha256": "9" * 64,
        "artifact_manifest_sha256": "a" * 64,
        "source_analysis_lineage_sha256": "b" * 64,
        "promoted_model_inference_sha256": "c" * 64,
        "question_queue_revision": "d" * 64,
        "review_completion_id": "review:" + ("e" * 64),
        "answer_ledger_sha256": "e" * 64,
        "survey_qa_revision": "f" * 64,
        "final_qa_choice": "no_miss_complete",
    }
    return build_approved_incident_output_authority(
        survey_authority=survey,
        survey_incident_authority_sha256="1" * 64,
        incident_id=INCIDENT_ID,
        question_id=INCIDENT_ID,
        candidate_id=CANDIDATE_ID,
        review_event_binding={
            "schema_version": "trafficvisson.review-event-binding.v1",
            "review_event_set_id": "res_" + ("2" * 32),
            "review_event_set_revision": "3" * 64,
            "review_event_set_manifest_sha256": "4" * 64,
            "review_event_id": "rve_" + ("5" * 32),
            "review_event_revision": "6" * 64,
            "representative_candidate_id": CANDIDATE_ID,
            "review_only": True,
        },
        approval_record_id=APPROVAL_RECORD_ID,
        approval_record_sha256="5" * 64,
        answer_revision=1,
        candidate_metadata_sha256="7" * 64,
        representative_image_ref=EVIDENCE_REF,
        representative_image_sha256="8" * 64,
        preview_clip_ref=f"analysis:{CANDIDATE_ID}:preview",
        preview_clip_sha256="9" * 64,
    )


def _approved_incident() -> dict:
    return {
        "incident_id": INCIDENT_ID,
        "project_id": str(PROJECT_ID),
        "pipeline_job_id": str(PIPELINE_JOB_ID),
        "analysis_job_id": str(ANALYSIS_JOB_ID),
        "source_sha256": SOURCE_SHA256,
        "review_status": "approved",
        "approval_actor": "local_user",
        "approval_record_id": APPROVAL_RECORD_ID,
        "approval_record_sha256": "5" * 64,
        "incident_type": "pothole",
        "severity": "moderate",
        "maintenance_action": "노면 보수",
        "ocr_text": None,
        "time_ms": 12_345,
        "evidence_refs": [EVIDENCE_REF],
        "approved_output_authority": _output_authority(),
    }


def test_exact_report_boundary_accepts_only_local_user_approved_incident():
    incident = _approved_incident()

    preview_rows = (
        DurablePipelineReportPreviewPreparer._normalize_incidents(
            [incident]
        )
    )
    bound_rows = _normalize_incidents(
        preview_rows,
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        source_sha256=SOURCE_SHA256,
    )

    assert [row["incident_id"] for row in bound_rows] == [
        INCIDENT_ID
    ]
    assert bound_rows[0]["review_status"] == "approved"
    assert bound_rows[0]["approval_actor"] == "local_user"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("review_status", "proposed"),
        ("approval_actor", "structured_llm_assisted"),
        ("pipeline_job_id", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        ("analysis_job_id", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        ("source_sha256", "c" * 64),
    ],
)
def test_exact_report_boundary_rejects_unapproved_or_cross_job_truth(
    field,
    value,
):
    incident = _approved_incident()
    incident[field] = value

    if field in {"review_status", "approval_actor"}:
        with pytest.raises(PipelineReportPreviewError):
            DurablePipelineReportPreviewPreparer._normalize_incidents(
                [incident]
            )
        return

    with pytest.raises(PipelineReportPreviewError):
        DurablePipelineReportPreviewPreparer._normalize_incidents(
            [incident]
        )


def test_report_boundary_requires_approval_and_evidence_hashes():
    incident = _approved_incident()
    missing = deepcopy(incident)
    missing.pop("approval_record_sha256")
    with pytest.raises(PipelineReportPreviewError):
        DurablePipelineReportPreviewPreparer._normalize_incidents(
            [missing]
        )

    invalid = deepcopy(incident)
    invalid["approval_record_sha256"] = "not-a-hash"
    with pytest.raises(PipelineReportPreviewError):
        DurablePipelineReportPreviewPreparer._normalize_incidents(
            [invalid]
        )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("model_version_id",), "model-promoted-v2"),
        (("dataset_version_id",), "dataset-promoted-v2"),
        (("dataset_provenance_graph_sha256",), "0" * 64),
        (("fixed_validation_sha256",), "0" * 64),
        (("source_analysis_lineage_sha256",), "0" * 64),
        (
            ("review_event_binding", "review_event_revision"),
            "0" * 64,
        ),
        (("approval_record_sha256",), "0" * 64),
        (("representative_image_sha256",), "0" * 64),
    ],
)
def test_report_boundary_rejects_tampered_final_authority(
    path,
    value,
):
    incident = _approved_incident()
    target = incident["approved_output_authority"]
    for field in path[:-1]:
        target = target[field]
    target[path[-1]] = value

    with pytest.raises(PipelineReportPreviewError):
        DurablePipelineReportPreviewPreparer._normalize_incidents(
            [incident]
        )


@pytest.mark.parametrize(
    ("issue_type", "expected"),
    [
        ("guide_sign_identity", "이정표 표기 및 위치 확인"),
        ("tunnel_identity", "터널명 표기 확인"),
    ],
)
def test_nonmaintenance_identity_incident_gets_report_observation_text_only(
    issue_type,
    expected,
):
    answer = {
        "selected_issue_type": issue_type,
        "maintenance_action": None,
    }

    assert _report_incident_action(answer) == expected
    assert answer["maintenance_action"] is None
