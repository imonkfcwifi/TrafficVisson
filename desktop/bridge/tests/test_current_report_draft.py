from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.current_incident_hwpx_plan import ServerOwnedEvidenceFile
from app.current_incident_report_binder import (
    CURRENT_INCIDENT_REPORT_BINDING_SCHEMA,
    CurrentIncidentReportBindingManifest,
    IncidentSlotBinding,
    ReportTargetMutation,
)
from app.current_report_draft import (
    CURRENT_REPORT_DRAFT_SCHEMA,
    CURRENT_REPORT_DRAFT_STATE,
    CurrentReportDraftConflict,
    CurrentReportDraftCorrupt,
    CurrentReportDraftError,
    CurrentReportDraftNotFound,
    CurrentReportDraftService,
    ReopenedHwpxVerification,
)
from app.current_report_approval import CurrentReportApprovalRegistry
from app.hwpx_exact_writer import (
    HWPX_EXACT_WRITER_SCHEMA,
    ExactHwpxTargetVerification,
    ExactHwpxTextOperation,
    ExactHwpxWritePlan,
    ExactHwpxWriteResult,
)
from current_report_document_plan_fixture import (
    document_edit_plan_fixture,
)


PROJECT_ID = UUID("00000000-0000-0000-0000-000000000001")
PIPELINE_JOB_ID = UUID("00000000-0000-0000-0000-000000000002")
ANALYSIS_JOB_ID = UUID("00000000-0000-0000-0000-000000000003")
OTHER_JOB_ID = UUID("00000000-0000-0000-0000-000000000004")
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_sha(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _mutation(role: str, target_suffix: str) -> ReportTargetMutation:
    return ReportTargetMutation(
        role=role,
        operation="replace_text",
        target_id=f"hwpx-cell-{target_suffix * 24}",
        page_pair_index=0,
        page_number=1,
        expected_value={"text": "과거 값", "text_sha256": SHA_A},
        current_value={
            "text": (
                "노선명 : 현재 승인 노선"
                if role == "inspection_route"
                else "ㅇ 00:00:12 · 사용자 승인 포트홀 보수 필요"
            ),
            "text_sha256": SHA_B,
        },
        current_source_refs=("incident:incident-1",),
    )


def _binding(template_sha256: str) -> CurrentIncidentReportBindingManifest:
    incident = IncidentSlotBinding(
        incident_id="incident-1",
        incident_order=0,
        page_pair_index=0,
        inspection_page_number=1,
        photo_page_number=2,
        photo_slot_id="photo-slot-1",
        photo_cell_target_id=f"hwpx-cell-{'c' * 24}",
        primary_picture_target_id=f"hwpx-picture-{'d' * 24}",
        additional_picture_target_ids=(),
        caption_cell_target_id=f"hwpx-cell-{'e' * 24}",
        selected_evidence_sha256=SHA_A,
        selected_evidence_ref="evidence-1",
        evidence_policy="internal_original",
        approved_incident_authority_sha256=SHA_C,
        photo_binding_sha256=SHA_B,
    )
    return CurrentIncidentReportBindingManifest(
        schema_version=CURRENT_INCIDENT_REPORT_BINDING_SCHEMA,
        state="current_incidents_bound_writer_required",
        project_id=str(PROJECT_ID),
        pipeline_job_id=str(PIPELINE_JOB_ID),
        analysis_job_id=str(ANALYSIS_JOB_ID),
        source_sha256=SHA_A,
        template_id="trafficvisson-3q-sample",
        document_snapshot_id="snapshot-1",
        template_sha256=template_sha256,
        template_semantic_map_sha256=SHA_C,
        structure_target_summary_sha256=SHA_D,
        incident_capacity=123,
        approved_incident_count=1,
        approved_incident_ids=("incident-1",),
        occupied_page_pair_indices=(0,),
        incident_bindings=(incident,),
        replacements=(
            _mutation("inspection_route", "a"),
            _mutation("inspection_opinion", "b"),
        ),
        neutralizations=(),
        evaluation_rows=(),
        survey_incident_authority_sha256=SHA_A,
        approved_incident_authority_set_sha256=SHA_B,
        input_sha256=SHA_A,
        content_sha256=SHA_B,
        slot_manifest_sha256=SHA_C,
        binding_manifest_sha256=SHA_D,
        required_writer_operation_count=2,
        legacy_document_edit_plan_operation_limit=100,
        exact_hwpx_writer_operation_limit=5_000,
        legacy_plan_expansion_required=False,
        writer_batching_required=False,
    )


def _plan(binding: CurrentIncidentReportBindingManifest) -> ExactHwpxWritePlan:
    operations = (
        ExactHwpxTextOperation(
            operation_id="op-0001",
            target_id=f"hwpx-cell-{'a' * 24}",
            expected_current_value="과거 값",
            expected_current_sha256=SHA_A,
            value="노선명 : 현재 승인 노선",
            source_ref="incident:incident-1",
        ),
        ExactHwpxTextOperation(
            operation_id="op-0002",
            target_id=f"hwpx-cell-{'b' * 24}",
            expected_current_value="과거 값",
            expected_current_sha256=SHA_A,
            value="ㅇ 00:00:12 · 사용자 승인 포트홀 보수 필요",
            source_ref="incident:incident-1",
        ),
    )
    return ExactHwpxWritePlan(
        plan_id=f"current-report-{binding.binding_manifest_sha256[:32]}",
        source_document_sha256=binding.template_sha256,
        source_target_summary_sha256=(
            binding.structure_target_summary_sha256
        ),
        incident_source_refs=("incident:incident-1",),
        operations=operations,
    )


class FakePlanBuilder:
    def __init__(self) -> None:
        self.calls = 0
        self.last_kwargs = None

    def __call__(self, **kwargs) -> ExactHwpxWritePlan:
        self.calls += 1
        self.last_kwargs = kwargs
        assert kwargs["source_path"].is_absolute()
        assert kwargs["server_evidence_root"].is_absolute()
        assert kwargs["transcode_root"].is_absolute()
        return _plan(kwargs["binding_manifest"])


class FakeDocumentEditPlanBuilder:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        return document_edit_plan_fixture(
            kwargs["binding_manifest"],
            kwargs["exact_plan"],
        )


class FakeWriter:
    def __init__(self, *, returned_path: Path | None = None) -> None:
        self.calls = 0
        self.returned_path = returned_path

    def __call__(
        self,
        source_path: Path,
        output_path: Path,
        plan: ExactHwpxWritePlan,
    ) -> ExactHwpxWriteResult:
        self.calls += 1
        assert source_path.exists()
        output_bytes = b"PK\x03\x04server-owned-exact-current-report"
        output_path.write_bytes(output_bytes)
        verifications = tuple(
            ExactHwpxTargetVerification(
                operation_id=operation.operation_id,
                target_id=operation.target_id,
                kind=operation.kind,
                source_ref=operation.source_ref,
                output_value_sha256=SHA_A,
            )
            for operation in plan.operations
        )
        return ExactHwpxWriteResult(
            schema_version=HWPX_EXACT_WRITER_SCHEMA,
            state="writer_completed_com_validation_required",
            plan_id=plan.plan_id,
            source_document_sha256=plan.source_document_sha256,
            output_document_sha256=_sha(output_bytes),
            output_path=str(self.returned_path or output_path),
            operation_count=len(plan.operations),
            target_verifications=verifications,
            table_count=72,
            expected_rendered_page_count=48,
            output_rendered_page_count=None,
            page_contract_proxy_verified=True,
            output_render_validation_status=(
                "not_run_com_validation_required"
            ),
            structure_projection_sha256=SHA_C,
            source_unchanged=True,
            mimetype_first_and_stored=True,
            com_invocation_count=0,
        )


class FakeReopenVerifier:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, path: Path) -> ReopenedHwpxVerification:
        self.calls += 1
        return ReopenedHwpxVerification(
            document_sha256=_sha(path.read_bytes()),
            table_count=72,
            mimetype_first_and_stored=True,
        )


@pytest.fixture
def prepared_dependencies(tmp_path):
    source = tmp_path / "audited-3q.hwpx"
    source.write_bytes(b"byte-exact-audited-template")
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    draft_root = tmp_path / "drafts"
    builder = FakePlanBuilder()
    document_builder = FakeDocumentEditPlanBuilder()
    writer = FakeWriter()
    reopener = FakeReopenVerifier()
    service = CurrentReportDraftService(
        owned_draft_root=draft_root,
        plan_builder=builder,
        document_edit_plan_builder=document_builder,
        writer=writer,
        reopen_verifier=reopener,
    )
    return SimpleNamespace(
        source=source,
        evidence_root=evidence_root,
        draft_root=draft_root,
        binding=_binding(_sha(source.read_bytes())),
        builder=builder,
        document_builder=document_builder,
        writer=writer,
        reopener=reopener,
        service=service,
    )


def _prepare(deps):
    return deps.service.prepare(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        source_path=deps.source,
        binding_manifest=deps.binding,
        evidence_by_ref={
            "evidence-1": ServerOwnedEvidenceFile(
                evidence_ref="evidence-1",
                absolute_path=str(deps.evidence_root / "unused.jpg"),
                sha256=SHA_A,
            )
        },
        server_evidence_root=deps.evidence_root,
    )


def test_prepares_server_owned_hash_bound_draft_without_provider_or_com(
    prepared_dependencies,
):
    metadata = _prepare(prepared_dependencies)

    assert metadata.schema_version == CURRENT_REPORT_DRAFT_SCHEMA
    assert metadata.state == CURRENT_REPORT_DRAFT_STATE
    assert metadata.idempotent_replay is False
    assert metadata.approved_incident_count == 1
    assert metadata.operation_count == 2
    assert metadata.expected_rendered_page_count == 48
    assert metadata.provider_called is False
    assert metadata.com_invoked is False
    assert metadata.render_validation_completed is False
    assert metadata.apply_approval_required is True
    assert metadata.publication_completed is False
    assert metadata.full_e2e_completed is False
    assert metadata.path_exposed is False
    assert "path" not in metadata.to_dict()
    assert prepared_dependencies.builder.calls == 1
    assert prepared_dependencies.writer.calls == 1
    assert metadata.document_edit_plan_sha256 == (
        metadata.document_edit_plan["document_edit_plan_sha256"]
    )
    assert metadata.accept_reject_rollback_binding_sha256 == (
        metadata.document_edit_plan[
            "accept_reject_rollback_binding_sha256"
        ]
    )
    assert metadata.document_edit_plan["path_exposed"] is False
    assert metadata.document_edit_plan[
        "provider_may_select_target_addresses"
    ] is False

    loaded = prepared_dependencies.service.load(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        draft_id=metadata.draft_id,
    )
    assert loaded.output_path.is_relative_to(
        prepared_dependencies.draft_root
    )
    assert loaded.output_path.exists()
    assert loaded.binding_manifest.approved_incident_ids == (
        "incident-1",
    )
    assert loaded.exact_plan_sha256 == metadata.exact_plan_sha256
    assert loaded.writer_result.com_invocation_count == 0
    record = json.loads(loaded.record_path.read_text(encoding="utf-8"))
    assert record["provider_invocation_count"] == 0
    assert record["com_invocation_count"] == 0
    assert record["render_validation_completed"] is False
    assert record["apply_approval_required"] is True
    assert record["document_edit_plan_sha256"] == (
        metadata.document_edit_plan_sha256
    )
    assert record[
        "accept_reject_rollback_binding_sha256"
    ] == metadata.accept_reject_rollback_binding_sha256
    assert record["record_sha256"] == _canonical_sha(
        {
            key: value
            for key, value in record.items()
            if key != "record_sha256"
        }
    )


def test_injected_server_resolver_rebuilds_exact_evidence_mapping(
    tmp_path,
):
    source = tmp_path / "audited.hwpx"
    source.write_bytes(b"audited")
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    evidence_path = evidence_root / "incident.jpg"
    evidence_path.write_bytes(b"unused-by-fake-plan-builder")
    builder = FakePlanBuilder()
    calls = []

    def resolve(
        project_id,
        pipeline_job_id,
        analysis_job_id,
        selected_ref,
        expected_sha256,
    ):
        calls.append(
            (
                project_id,
                pipeline_job_id,
                analysis_job_id,
                selected_ref,
                expected_sha256,
            )
        )
        return ServerOwnedEvidenceFile(
            evidence_ref=selected_ref,
            absolute_path=str(evidence_path),
            sha256=expected_sha256,
        )

    service = CurrentReportDraftService(
        owned_draft_root=tmp_path / "drafts",
        plan_builder=builder,
        document_edit_plan_builder=FakeDocumentEditPlanBuilder(),
        writer=FakeWriter(),
        reopen_verifier=FakeReopenVerifier(),
        report_evidence_resolver=resolve,
        server_evidence_root=evidence_root,
    )
    binding = _binding(_sha(source.read_bytes()))

    metadata = service.prepare(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        source_path=source,
        binding_manifest=binding,
    )

    assert metadata.exact_writer_completed is True
    assert calls == [
        (
            PROJECT_ID,
            PIPELINE_JOB_ID,
            ANALYSIS_JOB_ID,
            "evidence-1",
            SHA_A,
        )
    ]
    assert builder.last_kwargs["evidence_by_ref"] == {
        "evidence-1": ServerOwnedEvidenceFile(
            evidence_ref="evidence-1",
            absolute_path=str(evidence_path),
            sha256=SHA_A,
        )
    }


def test_same_binding_is_idempotent_and_does_not_reinvoke_writer(
    prepared_dependencies,
):
    first = _prepare(prepared_dependencies)
    second = _prepare(prepared_dependencies)

    assert second.draft_id == first.draft_id
    assert second.output_document_sha256 == first.output_document_sha256
    assert second.record_sha256 == first.record_sha256
    assert second.idempotent_replay is True
    assert prepared_dependencies.builder.calls == 1
    assert prepared_dependencies.writer.calls == 1


def test_loaded_draft_is_typed_for_later_one_shot_approval_registry(
    prepared_dependencies,
):
    metadata = _prepare(prepared_dependencies)
    loaded = prepared_dependencies.service.load(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        draft_id=metadata.draft_id,
    )
    registry = CurrentReportApprovalRegistry(
        owned_preview_root=prepared_dependencies.draft_root,
        token_factory=lambda: "f" * 64,
    )

    grant = registry.issue(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        binding=loaded.binding_manifest,
        plan=loaded.exact_plan,
        document_edit_plan=loaded.document_edit_plan,
        writer_result=loaded.writer_result,
    )

    assert grant.exact_preview_sha256 == metadata.output_document_sha256
    assert grant.binding_manifest_sha256 == metadata.binding_manifest_sha256
    assert grant.operation_count == metadata.operation_count
    assert loaded.public_metadata().com_invoked is False


def test_rejects_cross_job_or_unapproved_binding_before_writer(
    prepared_dependencies,
):
    crossed = replace(
        prepared_dependencies.binding,
        pipeline_job_id=str(OTHER_JOB_ID),
    )
    with pytest.raises(CurrentReportDraftError) as caught:
        prepared_dependencies.service.prepare(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            analysis_job_id=ANALYSIS_JOB_ID,
            source_path=prepared_dependencies.source,
            binding_manifest=crossed,
            evidence_by_ref={},
            server_evidence_root=prepared_dependencies.evidence_root,
        )
    assert caught.value.code == "cross_job_binding"

    unapproved = replace(
        prepared_dependencies.binding,
        approved_incident_count=0,
        approved_incident_ids=(),
        occupied_page_pair_indices=(),
        incident_bindings=(),
    )
    with pytest.raises(CurrentReportDraftError) as caught:
        prepared_dependencies.service.prepare(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            analysis_job_id=ANALYSIS_JOB_ID,
            source_path=prepared_dependencies.source,
            binding_manifest=unapproved,
            evidence_by_ref={},
            server_evidence_root=prepared_dependencies.evidence_root,
        )
    assert caught.value.code == "approved_incident_binding_required"
    assert prepared_dependencies.writer.calls == 0


def test_missing_record_with_existing_output_is_stale_not_overwritten(
    prepared_dependencies,
):
    metadata = _prepare(prepared_dependencies)
    loaded = prepared_dependencies.service.load(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        draft_id=metadata.draft_id,
    )
    output_before = loaded.output_path.read_bytes()
    loaded.record_path.unlink()

    with pytest.raises(CurrentReportDraftConflict) as caught:
        _prepare(prepared_dependencies)

    assert caught.value.code == "stale_existing_output"
    assert loaded.output_path.read_bytes() == output_before
    assert prepared_dependencies.writer.calls == 1


def test_tampered_record_or_output_fails_integrity_verification(
    prepared_dependencies,
):
    metadata = _prepare(prepared_dependencies)
    loaded = prepared_dependencies.service.load(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        draft_id=metadata.draft_id,
    )
    loaded.output_path.write_bytes(b"PK\x03\x04tampered")

    with pytest.raises(CurrentReportDraftError) as caught:
        prepared_dependencies.service.load(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            draft_id=metadata.draft_id,
        )
    assert caught.value.code == "output_reopen_verification_failed"
    assert caught.value.com_invoked is False

    # Restore the output, then corrupt the hash-bound record itself.
    loaded.output_path.write_bytes(
        b"PK\x03\x04server-owned-exact-current-report"
    )
    record = json.loads(loaded.record_path.read_text(encoding="utf-8"))
    record["apply_approval_required"] = False
    loaded.record_path.write_text(
        json.dumps(record, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(CurrentReportDraftCorrupt) as caught:
        prepared_dependencies.service.load(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            draft_id=metadata.draft_id,
        )
    assert caught.value.code == "draft_record_corrupt"


def test_hash_valid_record_cannot_replace_reviewed_document_plan(
    prepared_dependencies,
):
    metadata = _prepare(prepared_dependencies)
    loaded = prepared_dependencies.service.load(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        draft_id=metadata.draft_id,
    )
    record = json.loads(
        loaded.record_path.read_text(encoding="utf-8")
    )
    record["document_edit_plan"]["state"] = "tampered"
    record["record_sha256"] = _canonical_sha(
        {
            key: value
            for key, value in record.items()
            if key != "record_sha256"
        }
    )
    loaded.record_path.write_text(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    with pytest.raises(CurrentReportDraftCorrupt) as caught:
        prepared_dependencies.service.load(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            draft_id=metadata.draft_id,
        )

    assert caught.value.code == "draft_document_edit_plan_changed"


def test_valid_but_different_request_at_same_address_is_conflict(
    prepared_dependencies,
):
    metadata = _prepare(prepared_dependencies)
    loaded = prepared_dependencies.service.load(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        draft_id=metadata.draft_id,
    )
    record = json.loads(loaded.record_path.read_text(encoding="utf-8"))
    record["request"]["source_target_summary_sha256"] = SHA_A
    record["request_sha256"] = _canonical_sha(record["request"])
    without_hash = {
        key: value
        for key, value in record.items()
        if key != "record_sha256"
    }
    record["record_sha256"] = _canonical_sha(without_hash)
    loaded.record_path.write_text(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    with pytest.raises(CurrentReportDraftConflict) as caught:
        _prepare(prepared_dependencies)
    assert caught.value.code == "draft_content_address_conflict"
    assert prepared_dependencies.writer.calls == 1


def test_writer_cannot_redirect_output_outside_owned_job_root(tmp_path):
    source = tmp_path / "audited.hwpx"
    source.write_bytes(b"audited")
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    escaped = tmp_path / "escaped.hwpx"
    writer = FakeWriter(returned_path=escaped)
    service = CurrentReportDraftService(
        owned_draft_root=tmp_path / "drafts",
        plan_builder=FakePlanBuilder(),
        document_edit_plan_builder=FakeDocumentEditPlanBuilder(),
        writer=writer,
        reopen_verifier=FakeReopenVerifier(),
    )

    with pytest.raises(CurrentReportDraftError) as caught:
        service.prepare(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            analysis_job_id=ANALYSIS_JOB_ID,
            source_path=source,
            binding_manifest=_binding(_sha(source.read_bytes())),
            evidence_by_ref={},
            server_evidence_root=evidence_root,
        )

    assert caught.value.code == "exact_writer_result_invalid"
    assert list((tmp_path / "drafts").rglob("*.hwpx")) == []
    assert caught.value.provider_called is False
    assert caught.value.com_invoked is False


def test_symlink_source_is_rejected_when_platform_supports_it(tmp_path):
    actual = tmp_path / "actual.hwpx"
    actual.write_bytes(b"audited")
    linked = tmp_path / "linked.hwpx"
    try:
        linked.symlink_to(actual)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    writer = FakeWriter()
    service = CurrentReportDraftService(
        owned_draft_root=tmp_path / "drafts",
        plan_builder=FakePlanBuilder(),
        document_edit_plan_builder=FakeDocumentEditPlanBuilder(),
        writer=writer,
        reopen_verifier=FakeReopenVerifier(),
    )

    with pytest.raises(CurrentReportDraftError) as caught:
        service.prepare(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            analysis_job_id=ANALYSIS_JOB_ID,
            source_path=linked,
            binding_manifest=_binding(_sha(actual.read_bytes())),
            evidence_by_ref={},
            server_evidence_root=evidence_root,
        )

    assert caught.value.code == "unsafe_file"
    assert writer.calls == 0


def test_load_is_scoped_to_the_exact_project_and_pipeline_job(
    prepared_dependencies,
):
    metadata = _prepare(prepared_dependencies)

    with pytest.raises(CurrentReportDraftNotFound) as caught:
        prepared_dependencies.service.load(
            project_id=PROJECT_ID,
            pipeline_job_id=OTHER_JOB_ID,
            draft_id=metadata.draft_id,
        )

    assert caught.value.code == "draft_not_found"


def test_draft_artifacts_use_pipeline_report_root_and_restart_locator(
    tmp_path,
):
    bridge_root = (tmp_path / "bridge-private").resolve()
    category_root = (
        tmp_path
        / "E-like-output"
        / "pipeline-jobs"
        / str(PIPELINE_JOB_ID)
        / "reports"
    ).resolve()
    category_root.mkdir(parents=True)
    source = (tmp_path / "audited-3q.hwpx").resolve()
    source.write_bytes(b"byte-exact-audited-template")
    evidence_root = (tmp_path / "evidence").resolve()
    evidence_root.mkdir()
    binding = _binding(_sha(source.read_bytes()))

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
        return category_root

    def template_source_resolver(template_id, template_sha256):
        assert template_id == binding.template_id
        assert template_sha256 == binding.template_sha256
        return source

    def build_service():
        return CurrentReportDraftService(
            owned_draft_root=(
                bridge_root / "current-report-drafts"
            ),
            plan_builder=FakePlanBuilder(),
            document_edit_plan_builder=(
                FakeDocumentEditPlanBuilder()
            ),
            writer=FakeWriter(),
            reopen_verifier=FakeReopenVerifier(),
            artifact_root_resolver=artifact_root_resolver,
            template_source_resolver=template_source_resolver,
        )

    service = build_service()
    metadata = service.prepare(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        source_path=source,
        binding_manifest=binding,
        evidence_by_ref={
            "evidence-1": ServerOwnedEvidenceFile(
                evidence_ref="evidence-1",
                absolute_path=str(evidence_root / "unused.jpg"),
                sha256=SHA_A,
            )
        },
        server_evidence_root=evidence_root,
    )
    loaded = service.load(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        draft_id=metadata.draft_id,
    )

    assert loaded.output_path.is_relative_to(category_root)
    assert loaded.transcode_root.is_relative_to(category_root)
    assert not list(
        (bridge_root / "current-report-drafts").rglob("*.hwpx")
    )
    record_text = loaded.record_path.read_text(encoding="utf-8")
    record = json.loads(record_text)
    assert str(bridge_root) not in record_text
    assert str(category_root) not in record_text
    assert str(source) not in record_text
    assert record["source_path"] == (
        f"audited-template:{binding.template_id}"
    )
    assert record["transcode_root"] == "transcoded"
    assert record["writer_result"]["output_path"] == (
        loaded.output_path.name
    )

    restarted = build_service()
    restarted_loaded = restarted.load(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        draft_id=metadata.draft_id,
    )
    assert restarted_loaded.output_path == loaded.output_path


def test_draft_restart_rejects_recreated_report_category(
    tmp_path,
):
    bridge_root = (tmp_path / "bridge-private").resolve()
    category_root = (tmp_path / "E-like-output" / "reports").resolve()
    category_root.mkdir(parents=True)
    source = (tmp_path / "audited-3q.hwpx").resolve()
    source.write_bytes(b"byte-exact-audited-template")
    evidence_root = (tmp_path / "evidence").resolve()
    evidence_root.mkdir()
    binding = _binding(_sha(source.read_bytes()))

    def build_service():
        return CurrentReportDraftService(
            owned_draft_root=(
                bridge_root / "current-report-drafts"
            ),
            plan_builder=FakePlanBuilder(),
            document_edit_plan_builder=(
                FakeDocumentEditPlanBuilder()
            ),
            writer=FakeWriter(),
            reopen_verifier=FakeReopenVerifier(),
            artifact_root_resolver=(
                lambda *_identities: category_root
            ),
            template_source_resolver=(
                lambda _template_id, _template_sha256: source
            ),
        )

    service = build_service()
    metadata = service.prepare(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        source_path=source,
        binding_manifest=binding,
        evidence_by_ref={},
        server_evidence_root=evidence_root,
    )
    moved = category_root.with_name("reports-original")
    category_root.rename(moved)
    category_root.mkdir()

    with pytest.raises(CurrentReportDraftCorrupt) as caught:
        build_service().load(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            draft_id=metadata.draft_id,
        )

    assert caught.value.code == "draft_artifact_root_changed"
