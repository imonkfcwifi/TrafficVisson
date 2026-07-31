from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

import app.current_report_application as application_module
from app.current_incident_report_binder import (
    CURRENT_INCIDENT_REPORT_BINDING_SCHEMA,
    CurrentIncidentReportBindingManifest,
)
from app.current_report_application import (
    CurrentReportApplicationError,
    CurrentReportApplicationService,
)
from app.current_report_approval import (
    CurrentReportApplyAuthorization,
    canonical_exact_hwpx_plan_sha256,
)
from app.current_report_draft import CurrentReportDraft
from app.hwp_com import (
    ExactHwpxPublishResult,
    StructureSummary,
    fingerprint_document,
)
from app.hwp_render_validation import (
    ExplicitApplyApproval,
    ExpectedPagePairSlot,
    HwpRenderValidationResult,
    RenderValidationLimits,
    RenderedPageEvidence,
    ReportBindingManifest,
    rendered_page_manifest_sha256,
)
from app.hwpx_exact_writer import (
    HWPX_EXACT_WRITER_SCHEMA,
    ExactHwpxWritePlan,
    ExactHwpxWriteResult,
)
from app.hwpx_sample_layout import SAMPLE_LAYOUT_BASELINES
from app.project_artifact_roots import (
    ProjectArtifactRootRegistry,
)
from current_report_document_plan_fixture import (
    document_edit_plan_fixture,
)


WORKSPACE = Path(__file__).resolve().parents[3]
PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
PIPELINE_JOB_ID = UUID("22222222-2222-4222-8222-222222222222")
ANALYSIS_JOB_ID = UUID("33333333-3333-4333-8333-333333333333")
SAMPLES = {
    "sample-3q-v1": (
        WORKSPACE / "sample" / "3분기 점검 내용 정리.hwpx",
        WORKSPACE / "sample" / "3분기 점검 내용 정리.pdf",
        (
            4, 4, 7, 6, 5, 4, 4, 4, 6, 6, 1, 3,
            4, 3, 4, 6, 6, 6, 6, 6, 8, 8, 8, 4,
        ),
    ),
    "sample-4q-v1": (
        WORKSPACE / "sample" / "4분기 점검 내용 정리.hwpx",
        WORKSPACE / "sample" / "4분기 점검 내용 정리.pdf",
        (
            7, 8, 10, 8, 6, 5, 6, 6, 4, 6, 3, 3,
            8, 8, 8, 6, 6, 3, 7, 8, 6, 6, 4, 7,
        ),
    ),
}


class SampleCopyPublisher:
    def __init__(self, pdf_source: Path) -> None:
        self.pdf_source = pdf_source
        self.publish_calls = 0
        self.export_calls = 0

    def publish_approved_exact_hwpx(
        self,
        *,
        approval_id,
        source_hwpx,
        destination_hwpx,
    ):
        assert approval_id
        self.publish_calls += 1
        shutil.copyfile(source_hwpx, destination_hwpx)
        fingerprint = fingerprint_document(destination_hwpx)
        return ExactHwpxPublishResult(
            output_path=destination_hwpx,
            size_bytes=fingerprint.size,
            sha256=fingerprint.sha256,
            readback=StructureSummary(
                observed_characters=1,
                truncated=False,
                paragraph_count=1,
                table_count=72,
                picture_count=1,
                field_names=(),
                bookmark_names=(),
            ),
        )

    def export_applied_hwpx_to_pdf(
        self,
        *,
        approval_id,
        source_hwpx,
        destination_pdf,
    ):
        assert approval_id
        assert source_hwpx.is_file()
        self.export_calls += 1
        shutil.copyfile(self.pdf_source, destination_pdf)
        return destination_pdf


def _mock_render_validator(
    *,
    pdf_sha256: str,
    passed: bool,
    binding_manifest_sha256: str | None = None,
):
    def validate(**kwargs):
        render_directory = (
            Path(kwargs["owned_render_root"])
            / ("trafficvisson-render-" + ("a" * 32))
        )
        render_directory.mkdir()
        pages = []
        if passed:
            for page_number in range(1, 49):
                path = (
                    render_directory
                    / f"page-{page_number:02d}.png"
                )
                payload = f"mock-page-{page_number:02d}".encode()
                path.write_bytes(payload)
                pages.append(
                    RenderedPageEvidence(
                        page_number=page_number,
                        png_path=path,
                        png_sha256=hashlib.sha256(payload).hexdigest(),
                        width_pixels=595,
                        height_pixels=842,
                        size_bytes=len(payload),
                        nonwhite_ratio=0.1,
                        edge_ink_ratio=0.1,
                        text_character_count=1,
                        significant_photo_count=0,
                        neutral_picture_count=0,
                        caption_proxy_count=0,
                        extracted_object_count=0,
                    )
                )
        page_tuple = tuple(pages)
        actual_binding_sha256 = hashlib.sha256(
            json.dumps(
                asdict(kwargs["binding_manifest"]),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return HwpRenderValidationResult(
            status=(
                "validated_48_page_render"
                if passed
                else "render_failed"
            ),
            failure_codes=() if passed else ("render_failed",),
            output_hwpx_sha256=(
                kwargs["output_hwpx_fingerprint"].sha256
            ),
            output_reopen_structure_sha256="8" * 64,
            exported_pdf_sha256=pdf_sha256,
            binding_manifest_sha256=(
                binding_manifest_sha256 or actual_binding_sha256
            ),
            raster_artifact_manifest_sha256=(
                rendered_page_manifest_sha256(page_tuple)
            ),
            rendered_page_count=48,
            render_directory=render_directory,
            pages=page_tuple,
            diagnostics=(),
            limitations=(),
            elapsed_ms=1,
        )

    return validate


def _binding(template_sha256: str) -> CurrentIncidentReportBindingManifest:
    return CurrentIncidentReportBindingManifest(
        schema_version=CURRENT_INCIDENT_REPORT_BINDING_SCHEMA,
        state="current_incidents_bound_writer_required",
        project_id=str(PROJECT_ID),
        pipeline_job_id=str(PIPELINE_JOB_ID),
        analysis_job_id=str(ANALYSIS_JOB_ID),
        source_sha256="a" * 64,
        template_id="sample",
        document_snapshot_id="snapshot",
        template_sha256=template_sha256,
        template_semantic_map_sha256="b" * 64,
        structure_target_summary_sha256="c" * 64,
        incident_capacity=1,
        approved_incident_count=0,
        approved_incident_ids=(),
        occupied_page_pair_indices=(),
        incident_bindings=(),
        replacements=(),
        neutralizations=(),
        evaluation_rows=(),
        survey_incident_authority_sha256="2" * 64,
        approved_incident_authority_set_sha256="3" * 64,
        input_sha256="d" * 64,
        content_sha256="e" * 64,
        slot_manifest_sha256="f" * 64,
        binding_manifest_sha256="1" * 64,
        required_writer_operation_count=0,
        legacy_document_edit_plan_operation_limit=100,
        exact_hwpx_writer_operation_limit=5_000,
        legacy_plan_expansion_required=False,
        writer_batching_required=False,
    )


def _render_binding(
    template_id: str,
    counts: tuple[int, ...],
) -> ReportBindingManifest:
    baseline = next(
        item
        for item in SAMPLE_LAYOUT_BASELINES
        if item.template_id == template_id
    )
    slots = tuple(
        ExpectedPagePairSlot(
            page_pair_index=index,
            inspection_page_number=index * 2 - 1,
            photo_page_number=index * 2,
            incident_ids=(),
            expected_photo_count=count,
            expected_caption_count=count,
            expected_text_evidence=("현황사진",),
        )
        for index, count in enumerate(counts, start=1)
    )
    return ReportBindingManifest(
        template_id=template_id,
        template_sha256=baseline.document_sha256,
        expected_page_pair_slots=slots,
        current_incident_ids=(),
        expected_photo_count=sum(counts),
        expected_caption_count=sum(counts),
    )


def _draft_and_authorization(
    tmp_path: Path,
    *,
    template_id: str,
):
    hwpx, _pdf, counts = SAMPLES[template_id]
    preview = fingerprint_document(hwpx)
    binding = _binding(preview.sha256)
    plan = ExactHwpxWritePlan(
        plan_id="current-report-" + ("2" * 32),
        source_document_sha256=preview.sha256,
        source_target_summary_sha256="c" * 64,
        incident_source_refs=(),
        operations=(),
    )
    plan_sha = canonical_exact_hwpx_plan_sha256(plan)
    document_edit_plan = document_edit_plan_fixture(
        binding, plan
    )
    result = ExactHwpxWriteResult(
        schema_version=HWPX_EXACT_WRITER_SCHEMA,
        state="writer_completed_com_validation_required",
        plan_id=plan.plan_id,
        source_document_sha256=preview.sha256,
        output_document_sha256=preview.sha256,
        output_path=str(hwpx),
        operation_count=0,
        target_verifications=(),
        table_count=72,
        expected_rendered_page_count=48,
        output_rendered_page_count=None,
        page_contract_proxy_verified=True,
        output_render_validation_status=(
            "not_run_com_validation_required"
        ),
        structure_projection_sha256="3" * 64,
        source_unchanged=True,
        mimetype_first_and_stored=True,
        com_invocation_count=0,
    )
    render_binding = _render_binding(template_id, counts)
    render_binding_sha256 = hashlib.sha256(
        json.dumps(
            asdict(render_binding),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    draft = CurrentReportDraft(
        draft_id="current-report-" + ("4" * 32),
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        source_path=hwpx,
        output_path=hwpx,
        transcode_root=tmp_path,
        request_sha256="5" * 64,
        binding_manifest=binding,
        exact_plan=plan,
        document_edit_plan=document_edit_plan,
        writer_result=result,
        render_binding=render_binding,
        exact_plan_sha256=plan_sha,
        render_binding_sha256=render_binding_sha256,
        record_sha256="7" * 64,
        record_path=tmp_path / "draft-record.json",
    )
    authorization = CurrentReportApplyAuthorization(
        approval=ExplicitApplyApproval(
            approval_id="approval-1",
            output_hwpx_sha256=preview.sha256,
            granted=True,
        ),
        project_id=str(PROJECT_ID),
        pipeline_job_id=str(PIPELINE_JOB_ID),
        analysis_job_id=str(ANALYSIS_JOB_ID),
        binding_manifest_sha256=binding.binding_manifest_sha256,
        exact_plan_sha256=plan_sha,
        document_edit_plan_sha256=(
            document_edit_plan.document_edit_plan_sha256
        ),
        accept_reject_rollback_binding_sha256=(
            document_edit_plan
            .accept_reject_rollback_binding_sha256
        ),
        exact_preview_fingerprint=preview,
    )
    return draft, authorization


@pytest.mark.parametrize(
    ("template_id", "decision"),
    (
        ("sample-3q-v1", "accept"),
        ("sample-4q-v1", "reject"),
    ),
)
def test_real_sample_copy_apply_render_ledger_and_decision(
    tmp_path,
    template_id,
    decision,
):
    draft, authorization = _draft_and_authorization(
        tmp_path,
        template_id=template_id,
    )
    _hwpx, pdf, _counts = SAMPLES[template_id]
    root = (tmp_path / "applications").resolve()
    service = CurrentReportApplicationService(
        owned_application_root=root,
        render_limits=RenderValidationLimits(
            render_dpi=72,
            max_duration_seconds=180,
        ),
    )
    publisher = SampleCopyPublisher(pdf)

    applied = service.apply(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        draft=draft,
        authorization=authorization,
        publisher=publisher,
    )

    assert applied.state == (
        "render_validated_accept_or_reject_required"
    )
    assert applied.render_validation_status == (
        "validated_48_page_render"
    )
    assert applied.rendered_page_count == 48
    assert applied.com_publish_completed is True
    assert applied.pdf_export_completed is True
    assert applied.render_validation_completed is True
    assert applied.accept_reject_required is True
    assert applied.binding_manifest_sha256 == (
        draft.binding_manifest.binding_manifest_sha256
    )
    assert applied.exact_plan_sha256 == draft.exact_plan_sha256
    assert applied.document_edit_plan_sha256 == (
        draft.document_edit_plan.document_edit_plan_sha256
    )
    assert applied.accept_reject_rollback_binding_sha256 == (
        draft.document_edit_plan
        .accept_reject_rollback_binding_sha256
    )
    assert publisher.publish_calls == 1
    assert publisher.export_calls == 1
    output = next(root.rglob("current-report.hwpx"))
    rendered_pdf = next(root.rglob("current-report.pdf"))
    rendered_pages = sorted(root.rglob("page-*.png"))
    assert len(rendered_pages) == 48

    restarted = CurrentReportApplicationService(
        owned_application_root=root,
        render_limits=RenderValidationLimits(render_dpi=72),
    )
    decided = restarted.decide(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        application_id=applied.application_id,
        accept=decision == "accept",
        document_edit_plan_sha256=(
            draft.document_edit_plan.document_edit_plan_sha256
        ),
        accept_reject_rollback_binding_sha256=(
            draft.document_edit_plan
            .accept_reject_rollback_binding_sha256
        ),
    )
    if decision == "accept":
        assert decided.state == "accepted_render_validated_report"
        assert decided.accepted is True
        assert output.is_file()
        assert rendered_pdf.is_file()
        assert all(path.is_file() for path in rendered_pages)
    else:
        assert decided.state == "rejected_report_rolled_back"
        assert decided.rejected is True
        assert decided.rollback_completed is True
        assert not output.exists()
        assert not rendered_pdf.exists()
        assert not list(root.rglob("page-*.png"))
        assert not list(root.rglob("trafficvisson-render-*"))
        assert not list(root.rglob("rendered-pages"))


def test_render_failure_rolls_back_published_hwpx_and_pdf(
    tmp_path,
    monkeypatch,
):
    draft, authorization = _draft_and_authorization(
        tmp_path,
        template_id="sample-3q-v1",
    )
    _hwpx, pdf, _counts = SAMPLES["sample-3q-v1"]
    root = (tmp_path / "applications").resolve()
    service = CurrentReportApplicationService(
        owned_application_root=root,
    )
    publisher = SampleCopyPublisher(pdf)
    pdf_sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    monkeypatch.setattr(
        "app.current_report_application."
        "validate_saved_hwpx_pdf_render",
        _mock_render_validator(
            pdf_sha256=pdf_sha256,
            passed=False,
        ),
    )

    result = service.apply(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        draft=draft,
        authorization=authorization,
        publisher=publisher,
    )

    assert result.state == "render_validation_failed_rolled_back"
    assert result.rejected is True
    assert result.rollback_completed is True
    assert result.accept_reject_required is False
    assert not list(root.rglob("current-report.hwpx"))
    assert not list(root.rglob("current-report.pdf"))
    assert not list(root.rglob("page-*.png"))
    assert not list(root.rglob("trafficvisson-render-*"))
    assert not list(root.rglob("rendered-pages"))
    reloaded = service.metadata(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        application_id=result.application_id,
    )
    assert reloaded.state == result.state
    assert reloaded.render_failure_codes == (
        "render_failed",
    )


def test_foreign_render_binding_cannot_be_accepted_as_current_job_validation(
    tmp_path,
    monkeypatch,
):
    draft, authorization = _draft_and_authorization(
        tmp_path,
        template_id="sample-3q-v1",
    )
    _hwpx, pdf, _counts = SAMPLES["sample-3q-v1"]
    root = (tmp_path / "applications").resolve()
    pdf_sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
    monkeypatch.setattr(
        "app.current_report_application."
        "validate_saved_hwpx_pdf_render",
        _mock_render_validator(
            pdf_sha256=pdf_sha256,
            passed=True,
            binding_manifest_sha256="7" * 64,
        ),
    )
    service = CurrentReportApplicationService(
        owned_application_root=root,
    )

    with pytest.raises(CurrentReportApplicationError) as caught:
        service.apply(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            draft=draft,
            authorization=authorization,
            publisher=SampleCopyPublisher(pdf),
        )

    assert caught.value.code == "render_validation_binding_conflict"
    assert not list(root.rglob("current-report.hwpx"))
    assert not list(root.rglob("current-report.pdf"))
    assert not list(root.rglob("page-*.png"))
    assert not list(root.rglob("trafficvisson-render-*"))
    record_path = next(root.rglob("record.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["state"] == "apply_failed_rolled_back"
    assert record["rollback_completed"] is True
    assert record["accepted"] is False
    assert record["error_code"] == (
        "render_validation_binding_conflict"
    )


def test_decision_rejects_hash_from_another_review_plan(
    tmp_path,
    monkeypatch,
):
    draft, authorization = _draft_and_authorization(
        tmp_path,
        template_id="sample-3q-v1",
    )
    _hwpx, pdf, _counts = SAMPLES["sample-3q-v1"]
    root = (tmp_path / "applications").resolve()
    pdf_sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
    monkeypatch.setattr(
        "app.current_report_application."
        "validate_saved_hwpx_pdf_render",
        _mock_render_validator(
            pdf_sha256=pdf_sha256,
            passed=True,
        ),
    )
    service = CurrentReportApplicationService(
        owned_application_root=root,
    )
    applied = service.apply(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        draft=draft,
        authorization=authorization,
        publisher=SampleCopyPublisher(pdf),
    )

    with pytest.raises(CurrentReportApplicationError) as caught:
        service.decide(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            application_id=applied.application_id,
            accept=True,
            document_edit_plan_sha256="0" * 64,
            accept_reject_rollback_binding_sha256=(
                draft.document_edit_plan
                .accept_reject_rollback_binding_sha256
            ),
        )

    assert caught.value.code == "decision_binding_invalid"
    accepted = service.decide(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        application_id=applied.application_id,
        accept=True,
        document_edit_plan_sha256=(
            draft.document_edit_plan.document_edit_plan_sha256
        ),
        accept_reject_rollback_binding_sha256=(
            draft.document_edit_plan
            .accept_reject_rollback_binding_sha256
        ),
    )
    assert accepted.accepted is True


def test_accept_rechecks_all_persisted_render_page_hashes(
    tmp_path,
    monkeypatch,
):
    draft, authorization = _draft_and_authorization(
        tmp_path,
        template_id="sample-3q-v1",
    )
    _hwpx, pdf, _counts = SAMPLES["sample-3q-v1"]
    root = (tmp_path / "applications").resolve()
    pdf_sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
    monkeypatch.setattr(
        "app.current_report_application."
        "validate_saved_hwpx_pdf_render",
        _mock_render_validator(
            pdf_sha256=pdf_sha256,
            passed=True,
        ),
    )
    service = CurrentReportApplicationService(
        owned_application_root=root,
    )
    applied = service.apply(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        draft=draft,
        authorization=authorization,
        publisher=SampleCopyPublisher(pdf),
    )
    first_page = next(root.rglob("page-01.png"))
    original = first_page.read_bytes()
    first_page.write_bytes(b"X" * len(original))

    with pytest.raises(CurrentReportApplicationError) as caught:
        service.decide(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            application_id=applied.application_id,
            accept=True,
            document_edit_plan_sha256=(
                draft.document_edit_plan.document_edit_plan_sha256
            ),
            accept_reject_rollback_binding_sha256=(
                draft.document_edit_plan
                .accept_reject_rollback_binding_sha256
            ),
        )

    assert caught.value.code == "accepted_render_artifacts_changed"
    assert service.metadata(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        application_id=applied.application_id,
    ).state == "render_validated_accept_or_reject_required"
    assert first_page.is_file()
    assert len(list(root.rglob("page-*.png"))) == 48


def test_reject_never_follows_render_ledger_outside_application_root(
    tmp_path,
    monkeypatch,
):
    draft, authorization = _draft_and_authorization(
        tmp_path,
        template_id="sample-3q-v1",
    )
    _hwpx, pdf, _counts = SAMPLES["sample-3q-v1"]
    root = (tmp_path / "applications").resolve()
    pdf_sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
    monkeypatch.setattr(
        "app.current_report_application."
        "validate_saved_hwpx_pdf_render",
        _mock_render_validator(
            pdf_sha256=pdf_sha256,
            passed=True,
        ),
    )
    service = CurrentReportApplicationService(
        owned_application_root=root,
    )
    applied = service.apply(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        draft=draft,
        authorization=authorization,
        publisher=SampleCopyPublisher(pdf),
    )
    output = next(root.rglob("current-report.hwpx"))
    rendered_pdf = next(root.rglob("current-report.pdf"))
    original_pages = sorted(root.rglob("page-*.png"))
    outside = tmp_path / "outside-render"
    outside.mkdir()
    outside_page = outside / "page-01.png"
    outside_page.write_bytes(b"must-survive")

    record_path = next(root.rglob("record.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["render_directory"] = str(outside.resolve())
    without_hash = {
        key: value
        for key, value in record.items()
        if key != "record_sha256"
    }
    record["record_sha256"] = hashlib.sha256(
        json.dumps(
            without_hash,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    record_path.write_text(
        json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    with pytest.raises(CurrentReportApplicationError) as caught:
        service.decide(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            application_id=applied.application_id,
            accept=False,
            document_edit_plan_sha256=(
                draft.document_edit_plan.document_edit_plan_sha256
            ),
            accept_reject_rollback_binding_sha256=(
                draft.document_edit_plan
                .accept_reject_rollback_binding_sha256
            ),
        )

    assert caught.value.code == "reject_rollback_blocked"
    assert outside_page.read_bytes() == b"must-survive"
    assert output.is_file()
    assert rendered_pdf.is_file()
    assert all(path.is_file() for path in original_pages)


def test_accepted_report_can_be_hash_verified_and_rolled_back_after_restart(
    tmp_path,
):
    draft, authorization = _draft_and_authorization(
        tmp_path,
        template_id="sample-3q-v1",
    )
    _hwpx, pdf, _counts = SAMPLES["sample-3q-v1"]
    root = (tmp_path / "applications").resolve()
    service = CurrentReportApplicationService(
        owned_application_root=root,
        render_limits=RenderValidationLimits(
            render_dpi=72,
            max_duration_seconds=180,
        ),
    )
    applied = service.apply(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        draft=draft,
        authorization=authorization,
        publisher=SampleCopyPublisher(pdf),
    )
    accepted = service.decide(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        application_id=applied.application_id,
        accept=True,
        document_edit_plan_sha256=(
            draft.document_edit_plan.document_edit_plan_sha256
        ),
        accept_reject_rollback_binding_sha256=(
            draft.document_edit_plan
            .accept_reject_rollback_binding_sha256
        ),
    )
    assert accepted.state == "accepted_render_validated_report"
    output = next(root.rglob("current-report.hwpx"))
    rendered_pdf = next(root.rglob("current-report.pdf"))
    assert len(list(root.rglob("page-*.png"))) == 48

    restarted = CurrentReportApplicationService(
        owned_application_root=root,
    )
    rolled_back = restarted.rollback_accepted(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        application_id=accepted.application_id,
        document_edit_plan_sha256=(
            draft.document_edit_plan.document_edit_plan_sha256
        ),
        accept_reject_rollback_binding_sha256=(
            draft.document_edit_plan
            .accept_reject_rollback_binding_sha256
        ),
    )
    replayed = restarted.rollback_accepted(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        application_id=accepted.application_id,
        document_edit_plan_sha256=(
            draft.document_edit_plan.document_edit_plan_sha256
        ),
        accept_reject_rollback_binding_sha256=(
            draft.document_edit_plan
            .accept_reject_rollback_binding_sha256
        ),
    )

    assert rolled_back.state == "accepted_report_rolled_back"
    assert replayed == rolled_back
    assert rolled_back.accepted is True
    assert rolled_back.rejected is False
    assert rolled_back.rollback_completed is True
    assert not output.exists()
    assert not rendered_pdf.exists()
    assert not list(root.rglob("page-*.png"))
    assert not list(root.rglob("trafficvisson-render-*"))
    assert not list(root.rglob("rendered-pages"))
    assert (
        restarted.metadata(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            application_id=accepted.application_id,
        )
        == rolled_back
    )


def test_accepted_publication_source_is_reverified_and_accept_is_idempotent(
    tmp_path,
    monkeypatch,
):
    draft, authorization = _draft_and_authorization(
        tmp_path,
        template_id="sample-3q-v1",
    )
    _hwpx, pdf, _counts = SAMPLES["sample-3q-v1"]
    root = (tmp_path / "applications").resolve()
    pdf_sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
    monkeypatch.setattr(
        "app.current_report_application."
        "validate_saved_hwpx_pdf_render",
        _mock_render_validator(
            pdf_sha256=pdf_sha256,
            passed=True,
        ),
    )
    service = CurrentReportApplicationService(
        owned_application_root=root,
    )
    applied = service.apply(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        draft=draft,
        authorization=authorization,
        publisher=SampleCopyPublisher(pdf),
    )
    decision = {
        "project_id": PROJECT_ID,
        "pipeline_job_id": PIPELINE_JOB_ID,
        "application_id": applied.application_id,
        "accept": True,
        "document_edit_plan_sha256": (
            draft.document_edit_plan.document_edit_plan_sha256
        ),
        "accept_reject_rollback_binding_sha256": (
            draft.document_edit_plan
            .accept_reject_rollback_binding_sha256
        ),
    }
    accepted = service.decide(**decision)
    replayed = service.decide(**decision)
    source = service.resolve_accepted_publication_source(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        application_id=applied.application_id,
    )

    assert replayed == accepted
    assert source.output_path.is_relative_to(root)
    assert source.pdf_path.is_relative_to(root)
    assert source.output_sha256 == accepted.output_sha256
    assert source.pdf_sha256 == accepted.pdf_sha256
    assert source.rendered_page_count == 48

    changed_page = next(root.rglob("page-24.png"))
    original = changed_page.read_bytes()
    changed_page.write_bytes(b"Z" * len(original))
    with pytest.raises(CurrentReportApplicationError) as caught:
        service.resolve_accepted_publication_source(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            application_id=applied.application_id,
        )
    assert caught.value.code == "accepted_publication_source_changed"


def test_accepted_rollback_is_blocked_when_a_render_page_changed(
    tmp_path,
    monkeypatch,
):
    draft, authorization = _draft_and_authorization(
        tmp_path,
        template_id="sample-3q-v1",
    )
    _hwpx, pdf, _counts = SAMPLES["sample-3q-v1"]
    root = (tmp_path / "applications").resolve()
    pdf_sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
    monkeypatch.setattr(
        "app.current_report_application."
        "validate_saved_hwpx_pdf_render",
        _mock_render_validator(
            pdf_sha256=pdf_sha256,
            passed=True,
        ),
    )
    service = CurrentReportApplicationService(
        owned_application_root=root,
    )
    applied = service.apply(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        draft=draft,
        authorization=authorization,
        publisher=SampleCopyPublisher(pdf),
    )
    accepted = service.decide(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        application_id=applied.application_id,
        accept=True,
        document_edit_plan_sha256=(
            draft.document_edit_plan.document_edit_plan_sha256
        ),
        accept_reject_rollback_binding_sha256=(
            draft.document_edit_plan
            .accept_reject_rollback_binding_sha256
        ),
    )
    output = next(root.rglob("current-report.hwpx"))
    rendered_pdf = next(root.rglob("current-report.pdf"))
    changed_page = next(root.rglob("page-48.png"))
    original = changed_page.read_bytes()
    changed_page.write_bytes(b"Y" * len(original))

    with pytest.raises(CurrentReportApplicationError) as caught:
        service.rollback_accepted(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            application_id=accepted.application_id,
            document_edit_plan_sha256=(
                draft.document_edit_plan.document_edit_plan_sha256
            ),
            accept_reject_rollback_binding_sha256=(
                draft.document_edit_plan
                .accept_reject_rollback_binding_sha256
            ),
        )

    assert caught.value.code == "accepted_rollback_blocked"
    assert output.is_file()
    assert rendered_pdf.is_file()
    assert changed_page.is_file()
    assert len(list(root.rglob("page-*.png"))) == 48


def test_application_scope_does_not_follow_a_linked_project_directory(
    tmp_path,
):
    root = (tmp_path / "applications").resolve()
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    project_link = root / str(PROJECT_ID)
    service = CurrentReportApplicationService(
        owned_application_root=root,
    )
    try:
        project_link.symlink_to(
            outside,
            target_is_directory=True,
        )
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(CurrentReportApplicationError) as caught:
        service.metadata(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            application_id=(
                "current-report-application-" + ("f" * 32)
            ),
        )

    assert caught.value.code == "application_scope_unavailable"


def _access_denied_error() -> PermissionError:
    error = PermissionError(13, "record temporarily locked")
    error.winerror = 5
    return error


def test_record_replace_retries_transient_windows_access_denied(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "record.json"
    before = {
        "application_id": (
            "current-report-application-" + ("a" * 32)
        ),
        "state": "before",
    }
    after = {**before, "state": "after"}
    application_module._write_new_record(path, before)
    original_replace = application_module.os.replace
    calls = 0
    delays = []

    def transient_replace(source, destination):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise _access_denied_error()
        return original_replace(source, destination)

    monkeypatch.setattr(
        application_module.os,
        "replace",
        transient_replace,
    )
    monkeypatch.setattr(
        application_module.time,
        "sleep",
        delays.append,
    )

    application_module._replace_record(path, after)

    assert calls == 3
    assert delays == list(
        application_module
        ._WINDOWS_REPLACE_RETRY_DELAYS_SECONDS[:2]
    )
    assert json.loads(path.read_text("utf-8"))["state"] == "after"
    assert not list(tmp_path.glob(".record.json.*.tmp"))


def test_record_replace_fails_closed_after_bounded_access_denied(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "record.json"
    before = {
        "application_id": (
            "current-report-application-" + ("b" * 32)
        ),
        "state": "before",
    }
    after = {**before, "state": "after"}
    application_module._write_new_record(path, before)
    original_bytes = path.read_bytes()
    calls = 0
    delays = []

    def persistently_locked(_source, _destination):
        nonlocal calls
        calls += 1
        raise _access_denied_error()

    monkeypatch.setattr(
        application_module.os,
        "replace",
        persistently_locked,
    )
    monkeypatch.setattr(
        application_module.time,
        "sleep",
        delays.append,
    )

    with pytest.raises(CurrentReportApplicationError) as caught:
        application_module._replace_record(path, after)

    assert caught.value.code == "application_record_write_failed"
    assert calls == (
        len(
            application_module
            ._WINDOWS_REPLACE_RETRY_DELAYS_SECONDS
        )
        + 1
    )
    assert delays == list(
        application_module
        ._WINDOWS_REPLACE_RETRY_DELAYS_SECONDS
    )
    assert path.read_bytes() == original_bytes
    assert not list(tmp_path.glob(".record.json.*.tmp"))


def _external_report_application_service(
    tmp_path,
    *,
    free_bytes: int,
):
    bridge_root = (tmp_path / "bridge-private").resolve()
    selected_root = (tmp_path / "E-like-output").resolve()
    selected_root.mkdir()
    usage = lambda _path: SimpleNamespace(
        total=32 * 1024**3,
        used=0,
        free=free_bytes,
    )
    registry = ProjectArtifactRootRegistry(
        data_root=bridge_root,
        disk_usage=usage,
    )
    location = registry.register(
        PROJECT_ID,
        selected_directory=selected_root,
    )
    registry.capture_profile_binding(
        PROJECT_ID,
        profile_revision=1,
        output_location_id=location["output_location_id"],
    )
    bound = registry.capture_job_binding(
        PROJECT_ID,
        profile_revision=1,
        output_location_id=location["output_location_id"],
    )

    def artifact_root_resolver(
        project_id,
        pipeline_job_id,
        analysis_job_id,
    ):
        assert (
            project_id,
            pipeline_job_id,
            analysis_job_id,
        ) == (
            PROJECT_ID,
            PIPELINE_JOB_ID,
            ANALYSIS_JOB_ID,
        )
        root = (
            bound.root
            / "pipeline-jobs"
            / str(PIPELINE_JOB_ID)
            / "reports"
        )
        root.mkdir(parents=True, exist_ok=True)
        return root

    job = {
        "project_id": str(PROJECT_ID),
        "job_id": str(PIPELINE_JOB_ID),
        "profile_snapshot": {
            "output_location_id": location[
                "output_location_id"
            ],
        },
        "provenance": {"artifact_root": bound.binding},
        "stages": [
            {
                "name": "derived_outputs",
                "checkpoint": {
                    "analysis_job_id": str(ANALYSIS_JOB_ID)
                },
            }
        ],
    }
    service = CurrentReportApplicationService(
        owned_application_root=(
            bridge_root / "current-report-applications"
        ),
        artifact_root_resolver=artifact_root_resolver,
        artifact_root_registry=registry,
        pipeline_job_resolver=(
            lambda project_id, pipeline_job_id: job
        ),
    )
    return (
        service,
        registry,
        bridge_root,
        selected_root,
        artifact_root_resolver(
            PROJECT_ID,
            PIPELINE_JOB_ID,
            ANALYSIS_JOB_ID,
        ),
    )


def test_pipeline_bound_report_artifacts_and_ledger_are_path_free(
    tmp_path,
    monkeypatch,
):
    draft, authorization = _draft_and_authorization(
        tmp_path,
        template_id="sample-3q-v1",
    )
    _hwpx, pdf, _counts = SAMPLES["sample-3q-v1"]
    pdf_sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
    monkeypatch.setattr(
        application_module,
        "validate_saved_hwpx_pdf_render",
        _mock_render_validator(
            pdf_sha256=pdf_sha256,
            passed=True,
        ),
    )
    (
        service,
        registry,
        bridge_root,
        selected_root,
        category_root,
    ) = _external_report_application_service(
        tmp_path,
        free_bytes=16 * 1024**3,
    )
    applied = service.apply(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        draft=draft,
        authorization=authorization,
        publisher=SampleCopyPublisher(pdf),
    )

    application_root = (
        category_root / "applications" / applied.application_id
    )
    assert (application_root / "current-report.hwpx").is_file()
    assert (application_root / "current-report.pdf").is_file()
    assert len(list(application_root.rglob("page-*.png"))) == 48
    assert not list(
        (bridge_root / "current-report-applications").rglob(
            "current-report.hwpx"
        )
    )
    record_path = next(
        (bridge_root / "current-report-applications").rglob(
            "record.json"
        )
    )
    record_text = record_path.read_text(encoding="utf-8")
    record = json.loads(record_text)
    assert str(bridge_root) not in record_text
    assert str(selected_root) not in record_text
    assert record["output_path"] == "current-report.hwpx"
    assert record["pdf_path"] == "current-report.pdf"
    assert record["render_directory"].startswith(
        "trafficvisson-render-"
    )
    assert record["capacity_plan"]["safety_reserve_bytes"] == (
        4 * 1024**3
    )
    assert record["capacity_reservation_released"] is True
    assert registry._capacity_reservations == {}


def test_report_capacity_blocks_before_publisher_invocation(
    tmp_path,
):
    draft, authorization = _draft_and_authorization(
        tmp_path,
        template_id="sample-3q-v1",
    )
    _hwpx, pdf, _counts = SAMPLES["sample-3q-v1"]
    (
        service,
        registry,
        _bridge_root,
        _selected_root,
        category_root,
    ) = _external_report_application_service(
        tmp_path,
        free_bytes=5 * 1024**3,
    )
    publisher = SampleCopyPublisher(pdf)

    with pytest.raises(CurrentReportApplicationError) as caught:
        service.apply(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            draft=draft,
            authorization=authorization,
            publisher=publisher,
        )

    assert caught.value.code == (
        "current_report_capacity_insufficient"
    )
    assert publisher.publish_calls == 0
    assert publisher.export_calls == 0
    assert not list(
        (category_root / "applications").glob(
            "current-report-application-*"
        )
    )
    assert registry._capacity_reservations == {}


def test_apply_rollback_releases_current_report_reservation(
    tmp_path,
):
    draft, authorization = _draft_and_authorization(
        tmp_path,
        template_id="sample-3q-v1",
    )
    service, registry, *_rest = (
        _external_report_application_service(
            tmp_path,
            free_bytes=16 * 1024**3,
        )
    )

    class FailingPublisher:
        publish_calls = 0

        def publish_approved_exact_hwpx(self, **_kwargs):
            self.publish_calls += 1
            raise RuntimeError("simulated COM failure")

        def export_applied_hwpx_to_pdf(self, **_kwargs):
            raise AssertionError("PDF export must not run")

    publisher = FailingPublisher()
    with pytest.raises(CurrentReportApplicationError) as caught:
        service.apply(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            draft=draft,
            authorization=authorization,
            publisher=publisher,
        )

    assert caught.value.code == "exact_report_apply_failed"
    assert publisher.publish_calls == 1
    assert registry._capacity_reservations == {}
    record_path = next(service._root.rglob("record.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["rollback_completed"] is True
    assert record["capacity_reservation_released"] is True


def test_report_capacity_restart_reconstructs_then_rolls_back_hold(
    tmp_path,
    monkeypatch,
):
    draft, authorization = _draft_and_authorization(
        tmp_path,
        template_id="sample-3q-v1",
    )
    _hwpx, pdf, _counts = SAMPLES["sample-3q-v1"]
    pdf_sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
    monkeypatch.setattr(
        application_module,
        "validate_saved_hwpx_pdf_render",
        _mock_render_validator(
            pdf_sha256=pdf_sha256,
            passed=True,
        ),
    )
    service, registry, *_rest = (
        _external_report_application_service(
            tmp_path,
            free_bytes=16 * 1024**3,
        )
    )
    applied = service.apply(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        draft=draft,
        authorization=authorization,
        publisher=SampleCopyPublisher(pdf),
    )
    record_path = next(service._root.rglob("record.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["state"] = "validating_48_page_render"
    record["capacity_reservation_released"] = False
    without_hash = {
        key: value
        for key, value in record.items()
        if key != "record_sha256"
    }
    record["record_sha256"] = hashlib.sha256(
        json.dumps(
            without_hash,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    record_path.write_text(
        json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    restarted = CurrentReportApplicationService(
        owned_application_root=service._root,
        artifact_root_resolver=service._artifact_root_resolver,
        artifact_root_registry=registry,
        pipeline_job_resolver=service._pipeline_job_resolver,
    )

    recovery = restarted.startup_capacity_recovery

    assert recovery == {
        "restored": 1,
        "released": 1,
        "blocked": 0,
    }
    assert registry._capacity_reservations == {}
    recovered = restarted.metadata(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        application_id=applied.application_id,
    )
    assert recovered.state == "apply_interrupted_rolled_back"
    assert recovered.rollback_completed is True
    assert not list(
        _rest[-1].rglob("current-report.hwpx")
    )
    assert not list(_rest[-1].rglob("page-*.png"))


def test_report_application_rejects_category_root_swap_and_locator_tamper(
    tmp_path,
    monkeypatch,
):
    draft, authorization = _draft_and_authorization(
        tmp_path,
        template_id="sample-3q-v1",
    )
    _hwpx, pdf, _counts = SAMPLES["sample-3q-v1"]
    pdf_sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
    monkeypatch.setattr(
        application_module,
        "validate_saved_hwpx_pdf_render",
        _mock_render_validator(
            pdf_sha256=pdf_sha256,
            passed=True,
        ),
    )
    service, registry, *_prefix, category_root = (
        _external_report_application_service(
            tmp_path,
            free_bytes=16 * 1024**3,
        )
    )
    applied = service.apply(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        draft=draft,
        authorization=authorization,
        publisher=SampleCopyPublisher(pdf),
    )
    record_path = next(service._root.rglob("record.json"))
    original_record = record_path.read_bytes()
    record = json.loads(original_record.decode("utf-8"))
    record["category_root_identity_sha256"] = "0" * 64
    without_hash = {
        key: value
        for key, value in record.items()
        if key != "record_sha256"
    }
    record["record_sha256"] = hashlib.sha256(
        json.dumps(
            without_hash,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    record_path.write_text(
        json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    with pytest.raises(CurrentReportApplicationError) as tampered:
        service.metadata(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            application_id=applied.application_id,
        )
    assert tampered.value.code == (
        "application_artifact_root_changed"
    )

    record_path.write_bytes(original_record)
    moved = category_root.with_name("reports-original")
    category_root.rename(moved)
    category_root.mkdir()
    restarted = CurrentReportApplicationService(
        owned_application_root=service._root,
        artifact_root_resolver=service._artifact_root_resolver,
        artifact_root_registry=registry,
        pipeline_job_resolver=service._pipeline_job_resolver,
    )
    with pytest.raises(CurrentReportApplicationError) as swapped:
        restarted.metadata(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            application_id=applied.application_id,
        )
    assert swapped.value.code == (
        "application_artifact_root_changed"
    )
