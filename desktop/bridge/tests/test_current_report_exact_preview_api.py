from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from fastapi.testclient import TestClient

from app.current_report_application import (
    CurrentReportApplicationMetadata,
)
from app.current_report_publication import (
    CurrentReportPublicationMetadata,
)
from app.current_report_draft import CurrentReportDraftError
from app.main import create_app
from app.store import DEMO_PROJECT_ID


TOKEN = "test-current-report-exact-preview-token-" + ("a" * 32)
PIPELINE_JOB_ID = UUID("22222222-2222-4222-8222-222222222222")
ANALYSIS_JOB_ID = UUID("33333333-3333-4333-8333-333333333333")
DRAFT_ID = "current-report-" + ("d" * 32)


class FakePreviewContexts:
    def __init__(self, source_path: Path) -> None:
        self.source_path = source_path
        self.calls = []
        self.binding = object()

    def load_exact_context(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            analysis_job_id=ANALYSIS_JOB_ID,
            source_path=self.source_path,
            binding=self.binding,
        )


class FakeDraftService:
    def __init__(self) -> None:
        self.prepare_calls = []
        self.load_calls = []
        self.error = None
        self.loaded = SimpleNamespace(
            analysis_job_id=ANALYSIS_JOB_ID,
            binding_manifest=SimpleNamespace(
                incident_bindings=(
                    SimpleNamespace(
                        evidence_policy="internal_original"
                    ),
                ),
            ),
            exact_plan=object(),
            document_edit_plan=SimpleNamespace(
                document_edit_plan_sha256="6" * 64,
                accept_reject_rollback_binding_sha256="7" * 64,
            ),
            writer_result=object(),
        )

    def prepare(self, **kwargs):
        self.prepare_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            state=(
                "exact_current_report_draft_ready_"
                "apply_approval_required"
            ),
            draft_id=DRAFT_ID,
            approved_incident_count=1,
            binding_manifest_sha256="a" * 64,
            exact_plan_sha256="b" * 64,
            document_edit_plan_sha256="6" * 64,
            accept_reject_rollback_binding_sha256="7" * 64,
            document_edit_plan={
                "schema_version": (
                    "trafficvisson.current-report-"
                    "document-edit-plan.v1"
                ),
                "state": (
                    "exact_targets_bound_user_review_required"
                ),
                "document_edit_plan_sha256": "6" * 64,
                "accept_reject_rollback_binding_sha256": "7" * 64,
                "path_exposed": False,
            },
            output_document_sha256="c" * 64,
            render_binding_sha256="d" * 64,
            record_sha256="e" * 64,
            operation_count=12,
            expected_rendered_page_count=48,
            idempotent_replay=False,
        )

    def load(self, **kwargs):
        self.load_calls.append(kwargs)
        return self.loaded


class FakeApprovalRegistry:
    def __init__(self) -> None:
        self.calls = []
        self.consume_calls = []

    def issue(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            token="f" * 64,
            expires_at_unix=2_000_000_000.0,
        )

    def consume(self, token, **kwargs):
        self.consume_calls.append((token, kwargs))
        return object()


class FakeApplicationService:
    APPLICATION_ID = (
        "current-report-application-" + ("1" * 32)
    )

    def __init__(self) -> None:
        self.apply_calls = []
        self.decision_calls = []
        self.rollback_calls = []

    @staticmethod
    def _metadata(state, *, accepted=False, rejected=False):
        waiting = state == (
            "render_validated_accept_or_reject_required"
        )
        return CurrentReportApplicationMetadata(
            application_id=FakeApplicationService.APPLICATION_ID,
            state=state,
            project_id=str(DEMO_PROJECT_ID),
            pipeline_job_id=str(PIPELINE_JOB_ID),
            analysis_job_id=str(ANALYSIS_JOB_ID),
            draft_id=DRAFT_ID,
            binding_manifest_sha256="a" * 64,
            exact_plan_sha256="b" * 64,
            document_edit_plan_sha256="6" * 64,
            accept_reject_rollback_binding_sha256="7" * 64,
            output_file_name="current-report.hwpx",
            output_sha256="1" * 64,
            pdf_file_name="current-report.pdf",
            pdf_sha256="2" * 64,
            render_validation_status="validated_48_page_render",
            rendered_page_count=48,
            render_failure_codes=(),
            output_reopen_structure_sha256="3" * 64,
            raster_artifact_manifest_sha256="4" * 64,
            com_publish_completed=True,
            pdf_export_completed=True,
            render_validation_completed=True,
            rollback_completed=rejected,
            accept_reject_required=waiting,
            accepted=accepted,
            rejected=rejected,
        )

    def apply(self, **kwargs):
        self.apply_calls.append(kwargs)
        return self._metadata(
            "render_validated_accept_or_reject_required"
        )

    def decide(self, **kwargs):
        self.decision_calls.append(kwargs)
        if kwargs["accept"]:
            return self._metadata(
                "accepted_render_validated_report",
                accepted=True,
            )
        return self._metadata(
            "rejected_report_rolled_back",
            rejected=True,
        )

    def rollback_accepted(self, **kwargs):
        self.rollback_calls.append(kwargs)
        metadata = self._metadata(
            "accepted_report_rolled_back",
            accepted=True,
        )
        return CurrentReportApplicationMetadata(
            **{
                **metadata.__dict__,
                "rollback_completed": True,
            }
        )


class FakePublicationService:
    PUBLICATION_ID = (
        "current-report-publication-" + ("9" * 32)
    )

    def __init__(self) -> None:
        self.publish_calls = []
        self.rollback_calls = []
        self.metadata_calls = []
        self.state = "published"

    def _metadata(self, *, replayed=False):
        return CurrentReportPublicationMetadata(
            publication_id=self.PUBLICATION_ID,
            application_id=FakeApplicationService.APPLICATION_ID,
            state=self.state,
            project_id=str(DEMO_PROJECT_ID),
            pipeline_job_id=str(PIPELINE_JOB_ID),
            analysis_job_id=str(ANALYSIS_JOB_ID),
            output_location_id="project-default",
            registry_revision=1,
            hwpx_file_name="current-report-1111111111111111.hwpx",
            hwpx_sha256="1" * 64,
            hwpx_size_bytes=123,
            pdf_file_name="current-report-1111111111111111.pdf",
            pdf_sha256="2" * 64,
            pdf_size_bytes=456,
            publication_manifest_sha256="3" * 64,
            rollback_completed=self.state == "rolled_back",
            idempotent_replay=replayed,
        )

    def publish_accepted(self, **kwargs):
        self.publish_calls.append(kwargs)
        self.state = "published"
        return self._metadata()

    def rollback(self, **kwargs):
        self.rollback_calls.append(kwargs)
        self.state = "rolled_back"
        return self._metadata()

    def metadata(self, **kwargs):
        self.metadata_calls.append(kwargs)
        return self._metadata(replayed=True)


class FakeHwpService:
    def preflight(self):
        return SimpleNamespace(
            status="ready",
            reason=None,
            version=None,
        )

    def close(self):
        return None


def _request():
    return {
        "project_id": str(DEMO_PROJECT_ID),
        "pipeline_job_id": str(PIPELINE_JOB_ID),
        "preview_id": "report-preview-attempt-0",
    }


def test_exact_preview_endpoint_persists_draft_and_issues_one_shot_approval(
    tmp_path,
):
    source = tmp_path / "sample.hwpx"
    source.write_bytes(b"unused-by-fake-draft-service")
    drafts = FakeDraftService()
    approvals = FakeApprovalRegistry()
    app = create_app(
        TOKEN,
        fixture_mode=True,
        allow_test_client=True,
        bridge_data_root=str(tmp_path / "bridge-data"),
        current_report_draft_service=drafts,
        current_report_approval_registry=approvals,
    )
    previews = FakePreviewContexts(source)
    app.state.pipeline_report_previews = previews

    with TestClient(app) as client:
        response = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/pipeline-jobs/"
                f"{PIPELINE_JOB_ID}/reports/exact-preview"
            ),
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=_request(),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "exact_current_report_preview"
    assert payload["draft_id"] == DRAFT_ID
    assert payload["exact_writer_completed"] is True
    assert payload["apply_endpoint_available"] is True
    assert payload["remaining_blocker_codes"] == []
    assert payload["provider_invocation_count"] == 0
    assert payload["com_invocation_count"] == 0
    assert payload["document_published"] is False
    assert payload["path_exposed"] is False
    assert payload["approval_token"] == "f" * 64
    assert payload["document_edit_plan_sha256"] == "6" * 64
    assert payload[
        "accept_reject_rollback_binding_sha256"
    ] == "7" * 64
    assert payload["document_edit_plan"]["path_exposed"] is False
    assert drafts.prepare_calls[0]["binding_manifest"] is previews.binding
    assert approvals.calls[0]["writer_result"] is drafts.loaded.writer_result
    assert approvals.calls[0]["document_edit_plan"] is (
        drafts.loaded.document_edit_plan
    )


def test_exact_preview_endpoint_keeps_deidentified_registry_gap_blocked(
    tmp_path,
):
    drafts = FakeDraftService()
    drafts.error = CurrentReportDraftError(
        "deidentified_evidence_registry_unavailable",
        "deidentified report evidence is unavailable",
    )
    app = create_app(
        TOKEN,
        fixture_mode=True,
        allow_test_client=True,
        bridge_data_root=str(tmp_path / "bridge-data"),
        current_report_draft_service=drafts,
        current_report_approval_registry=FakeApprovalRegistry(),
    )
    app.state.pipeline_report_previews = FakePreviewContexts(
        tmp_path / "sample.hwpx"
    )

    with TestClient(app) as client:
        response = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/pipeline-jobs/"
                f"{PIPELINE_JOB_ID}/reports/exact-preview"
            ),
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=_request(),
        )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "deidentified_evidence_registry_unavailable",
        "state": "exact_preview_blocked",
        "provider_invocation_count": 0,
        "com_invocation_count": 0,
        "document_published": False,
    }


def test_exact_apply_and_decision_endpoints_use_one_shot_authority(
    tmp_path,
):
    drafts = FakeDraftService()
    approvals = FakeApprovalRegistry()
    applications = FakeApplicationService()
    publications = FakePublicationService()
    hwp = FakeHwpService()
    app = create_app(
        TOKEN,
        fixture_mode=True,
        allow_test_client=True,
        bridge_data_root=str(tmp_path / "bridge-data"),
        hwp_service=hwp,
        current_report_draft_service=drafts,
        current_report_approval_registry=approvals,
        current_report_application_service=applications,
        current_report_publication_service=publications,
    )
    headers = {"Authorization": f"Bearer {TOKEN}"}
    apply_body = {
        "project_id": str(DEMO_PROJECT_ID),
        "pipeline_job_id": str(PIPELINE_JOB_ID),
        "draft_id": DRAFT_ID,
        "approval_token": "f" * 64,
        "document_edit_plan_sha256": "6" * 64,
        "accept_reject_rollback_binding_sha256": "7" * 64,
        "user_confirmed": True,
    }
    decision_body = {
        "project_id": str(DEMO_PROJECT_ID),
        "pipeline_job_id": str(PIPELINE_JOB_ID),
        "application_id": applications.APPLICATION_ID,
        "document_edit_plan_sha256": "6" * 64,
        "accept_reject_rollback_binding_sha256": "7" * 64,
        "user_confirmed": True,
    }

    with TestClient(app) as client:
        applied = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/pipeline-jobs/"
                f"{PIPELINE_JOB_ID}/reports/exact-apply"
            ),
            headers=headers,
            json=apply_body,
        )
        accepted = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/pipeline-jobs/"
                f"{PIPELINE_JOB_ID}/reports/exact-accept"
            ),
            headers=headers,
            json=decision_body,
        )
        publication_status = client.get(
            (
                f"/projects/{DEMO_PROJECT_ID}/pipeline-jobs/"
                f"{PIPELINE_JOB_ID}/reports/exact-publication/"
                f"{applications.APPLICATION_ID}"
            ),
            headers=headers,
        )
        rejected = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/pipeline-jobs/"
                f"{PIPELINE_JOB_ID}/reports/exact-reject"
            ),
            headers=headers,
            json=decision_body,
        )
        rolled_back = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/pipeline-jobs/"
                f"{PIPELINE_JOB_ID}/reports/exact-rollback"
            ),
            headers=headers,
            json=decision_body,
        )

    assert applied.status_code == 200
    assert applied.json()["state"] == (
        "render_validated_accept_or_reject_required"
    )
    assert applied.json()["rendered_page_count"] == 48
    assert applied.json()["path_exposed"] is False
    assert approvals.consume_calls[0][0] == "f" * 64
    assert approvals.consume_calls[0][1][
        "expected_document_edit_plan_sha256"
    ] == "6" * 64
    assert applications.apply_calls[0]["publisher"] is hwp
    assert accepted.status_code == 200
    assert accepted.json()["accepted"] is True
    assert publication_status.status_code == 200
    assert publication_status.json()["state"] == "published"
    assert publication_status.json()["path_exposed"] is False
    assert "path" not in publication_status.json()
    assert rejected.status_code == 200
    assert rejected.json()["rejected"] is True
    assert rolled_back.status_code == 200
    assert rolled_back.json()["state"] == (
        "accepted_report_rolled_back"
    )
    assert rolled_back.json()["rollback_completed"] is True
    assert [call["accept"] for call in applications.decision_calls] == [
        True,
        False,
    ]
    assert applications.rollback_calls == [
        {
            "project_id": DEMO_PROJECT_ID,
            "pipeline_job_id": PIPELINE_JOB_ID,
            "application_id": applications.APPLICATION_ID,
            "document_edit_plan_sha256": "6" * 64,
            "accept_reject_rollback_binding_sha256": "7" * 64,
        }
    ]
    assert publications.publish_calls == [
        {
            "project_id": DEMO_PROJECT_ID,
            "pipeline_job_id": PIPELINE_JOB_ID,
            "application_id": applications.APPLICATION_ID,
        }
    ]
    assert publications.rollback_calls == [
        {
            "project_id": DEMO_PROJECT_ID,
            "pipeline_job_id": PIPELINE_JOB_ID,
            "application_id": applications.APPLICATION_ID,
        }
    ]


def test_exact_apply_accepts_a_previously_verified_shared_derivative_draft(
    tmp_path,
):
    drafts = FakeDraftService()
    drafts.loaded.binding_manifest.incident_bindings = (
        SimpleNamespace(evidence_policy="shared_deidentified"),
    )
    approvals = FakeApprovalRegistry()
    applications = FakeApplicationService()
    publications = FakePublicationService()
    app = create_app(
        TOKEN,
        fixture_mode=True,
        allow_test_client=True,
        bridge_data_root=str(tmp_path / "bridge-data"),
        hwp_service=FakeHwpService(),
        current_report_draft_service=drafts,
        current_report_approval_registry=approvals,
        current_report_application_service=applications,
        current_report_publication_service=publications,
    )

    with TestClient(app) as client:
        response = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/pipeline-jobs/"
                f"{PIPELINE_JOB_ID}/reports/exact-apply"
            ),
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "pipeline_job_id": str(PIPELINE_JOB_ID),
                "draft_id": DRAFT_ID,
                "approval_token": "f" * 64,
                "document_edit_plan_sha256": "6" * 64,
                "accept_reject_rollback_binding_sha256": "7" * 64,
                "user_confirmed": True,
            },
        )

    assert response.status_code == 200
    assert response.json()["state"] == (
        "render_validated_accept_or_reject_required"
    )
    assert len(approvals.consume_calls) == 1
    assert len(applications.apply_calls) == 1
