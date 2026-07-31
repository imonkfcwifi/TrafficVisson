from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from PIL import Image

from app.current_incident_hwpx_plan import (
    CurrentIncidentHwpxPlanError,
    ServerOwnedEvidenceFile,
    _compile_target_mutations,
    _requested_text_targets,
    _resolve_exact_cells,
    _resolve_picture_sources,
    _validate_stale_factual_manifest,
    build_exact_hwpx_write_plan,
)
from app.current_incident_report_binder import (
    ReportTargetMutation,
    _audit_template,
    bind_current_incidents_to_sample,
)
from app.hwpx_exact_writer import (
    ExactHwpxParagraphTextOperation,
    ExactHwpxPictureOperation,
    ExactHwpxTextOperation,
    ExactHwpxWriterLimits,
    write_exact_hwpx,
)
from app.hwpx_sample_layout import inspect_sample_layout
from app.hwpx_stale_factual_targets import audit_stale_factual_targets
from app.hwpx_weak_cases import (
    HwpxWeakCaseLimits,
    _inspect_image_header,
)
from app.pipeline_report_preview import (
    SampleTemplateSemanticMapRegistry,
)
from approved_incident_authority_fixture import (
    fixture_approval_record_id,
    fixture_incident_id,
    fixture_output_authority,
)


ROOT = Path(__file__).resolve().parents[2]
SAMPLE_ROOT = ROOT.parent / "sample"
SAMPLE_3Q = (SAMPLE_ROOT / "3분기 점검 내용 정리.hwpx").resolve()
SAMPLE_4Q = (SAMPLE_ROOT / "4분기 점검 내용 정리.hwpx").resolve()
PROJECT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PIPELINE_JOB_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
ANALYSIS_JOB_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
SOURCE_SHA256 = "d" * 64
INCIDENT_LABEL = "current-video-one"
INCIDENT_ID = fixture_incident_id(
    INCIDENT_LABEL,
    ANALYSIS_JOB_ID,
)
EVIDENCE_REF = f"original:{INCIDENT_ID}"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_exact_context(
    work: Path,
    *,
    sample_path: Path,
    template_id: str,
    inspection_date: str = "2026-07-29",
):
    work.mkdir(parents=True, exist_ok=True)
    evidence_root = work / "server-evidence"
    staging_root = work / "staged"
    evidence_root.mkdir(exist_ok=True)
    staging_root.mkdir(exist_ok=True)
    evidence_path = evidence_root / "current-frame.png"
    Image.new(
        "RGB",
        (640, 360),
        (20, 110, 170),
    ).save(evidence_path, format="PNG")
    evidence_sha = _sha(evidence_path)
    approval_record_id = fixture_approval_record_id(
        INCIDENT_LABEL,
        PIPELINE_JOB_ID,
    )

    inspection = inspect_sample_layout(sample_path)
    stale_audit = audit_stale_factual_targets(sample_path)
    semantic_map = SampleTemplateSemanticMapRegistry(
        SAMPLE_ROOT.resolve()
    ).resolve(
        template_id=template_id,
        template_sha256=inspection.snapshot.document_sha256,
    )
    _slots, pages = _audit_template(semantic_map, inspection)
    ratings = tuple(
        "normal" if row.five_grade_grid_available else "clear_unassessed"
        for row in pages[0].evaluation_rows
    )
    manifest = bind_current_incidents_to_sample(
        semantic_map=semantic_map,
        inspection=inspection,
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        source_sha256=SOURCE_SHA256,
        approved_incidents=(
            {
                "incident_id": INCIDENT_ID,
                "project_id": str(PROJECT_ID),
                "pipeline_job_id": str(PIPELINE_JOB_ID),
                "analysis_job_id": str(ANALYSIS_JOB_ID),
                "source_sha256": SOURCE_SHA256,
                "review_status": "approved",
                "approval_actor": "local_user",
                "approval_record_id": approval_record_id,
                "approval_record_sha256": "e" * 64,
                "incident_type": "pothole",
                "maintenance_action": "포트홀 보수 필요",
                "severity": "moderate",
                "ocr_text": "",
                "time_ms": 1_000,
                "evidence_refs": [EVIDENCE_REF],
                "approved_output_authority": (
                    fixture_output_authority(
                        label=INCIDENT_LABEL,
                        project_id=PROJECT_ID,
                        pipeline_job_id=PIPELINE_JOB_ID,
                        analysis_job_id=ANALYSIS_JOB_ID,
                        source_sha256=SOURCE_SHA256,
                        approval_record_id=approval_record_id,
                        approval_record_sha256="e" * 64,
                        representative_image_ref=EVIDENCE_REF,
                        representative_image_sha256=evidence_sha,
                    )
                ),
            },
        ),
        evidence_by_incident_id={
            INCIDENT_ID: {
                "incident_id": INCIDENT_ID,
                "project_id": str(PROJECT_ID),
                "pipeline_job_id": str(PIPELINE_JOB_ID),
                "analysis_job_id": str(ANALYSIS_JOB_ID),
                "source_sha256": SOURCE_SHA256,
                "original_ref": EVIDENCE_REF,
                "original_sha256": evidence_sha,
                "deidentified_ref": None,
                "deidentified_sha256": None,
                "deidentification_record_sha256": None,
                "deidentified_verified": False,
            }
        },
        route_name="테스트고속도로",
        inspection_date=inspection_date,
        report_inspector_name="현재 점검자",
        locations_by_incident_id={INCIDENT_ID: "상행 1.0km"},
        report_policy={
            "evidence_mode": "internal_original",
            "evaluation_ratings": ratings,
        },
    )
    mapping = {
        EVIDENCE_REF: ServerOwnedEvidenceFile(
            evidence_ref=EVIDENCE_REF,
            absolute_path=str(evidence_path.resolve()),
            sha256=evidence_sha,
        )
    }
    source_sha_before = _sha(sample_path)
    plan = build_exact_hwpx_write_plan(
        source_path=sample_path,
        binding_manifest=manifest,
        evidence_by_ref=mapping,
        server_evidence_root=evidence_root.resolve(),
        transcode_root=staging_root.resolve(),
    )
    return {
        "manifest": manifest,
        "inspection": inspection,
        "stale_audit": stale_audit,
        "plan": plan,
        "mapping": mapping,
        "evidence_root": evidence_root.resolve(),
        "staging_root": staging_root.resolve(),
        "source_sha_before": source_sha_before,
        "sample_path": sample_path,
    }


@pytest.fixture(scope="module")
def exact_context(tmp_path_factory):
    return _build_exact_context(
        tmp_path_factory.mktemp("current-incident-hwpx-plan"),
        sample_path=SAMPLE_3Q,
        template_id="sample-3q-v1",
    )


def test_real_3q_binding_compiles_one_complete_atomic_plan(exact_context):
    manifest = exact_context["manifest"]
    plan = exact_context["plan"]

    assert manifest.approved_incident_count == 1
    assert manifest.required_writer_operation_count == 972
    assert manifest.required_writer_operation_count <= 1_000
    assert len(plan.operations) == manifest.required_writer_operation_count
    assert len({item.target_id for item in plan.operations}) == len(
        plan.operations
    )
    assert plan.incident_source_refs == (f"incident:{INCIDENT_ID}",)
    assert all(
        item.source_ref in plan.incident_source_refs
        for item in plan.operations
    )
    assert _sha(SAMPLE_3Q) == exact_context["source_sha_before"]
    assert not tuple(exact_context["staging_root"].glob("*.hwpx"))
    replay = build_exact_hwpx_write_plan(
        source_path=SAMPLE_3Q,
        binding_manifest=manifest,
        evidence_by_ref=exact_context["mapping"],
        server_evidence_root=exact_context["evidence_root"],
        transcode_root=exact_context["staging_root"],
    )
    assert replay == plan


def test_real_3q_plan_covers_all_120_static_factual_targets(
    exact_context,
):
    audit = exact_context["stale_audit"]
    manifest = exact_context["manifest"]
    operations = {
        operation.target_id: operation
        for operation in exact_context["plan"].operations
    }

    assert len(audit.required_target_ids) == 120
    assert set(audit.required_target_ids).issubset(operations)
    assert len(
        [
            operation
            for operation in operations.values()
            if isinstance(operation, ExactHwpxParagraphTextOperation)
        ]
    ) == 24
    for pair in audit.page_pairs:
        assert operations[pair.title_group.target_id].value == "현재 점검"
        assert (
            operations[pair.title_route_heading.target_id].value
            == "3분기 일반평가(불시점검) 양식(테스트고속도로)"
        )
        assert (
            operations[pair.inspection_inspector.target_id].value
            == "점검자 : 현재 점검자 (인)"
        )
        header_operation = operations[pair.photo_page_header.target_id]
        assert isinstance(
            header_operation,
            ExactHwpxParagraphTextOperation,
        )
        assert header_operation.address == pair.photo_page_header.address
        assert header_operation.value == "□ 테스트고속도로 현황사진"
        signature_operation = operations[
            pair.inspector_signature.target_id
        ]
        assert isinstance(signature_operation, ExactHwpxPictureOperation)
        neutral_media, neutral_width, neutral_height = (
            _inspect_image_header(
                Path(signature_operation.evidence_path).read_bytes(),
                limits=HwpxWeakCaseLimits(),
            )
        )
        assert neutral_media in {"image/bmp", "image/png", "image/jpeg"}
        assert (neutral_width, neutral_height) == (1, 1)

    unoccupied = set(range(24)).difference(
        manifest.occupied_page_pair_indices
    )
    assert len(unoccupied) == 23
    for pair_index in unoccupied:
        route = next(
            mutation
            for mutation in manifest.replacements
            if mutation.page_pair_index == pair_index
            and mutation.role == "inspection_route"
        )
        inspected_date = next(
            mutation
            for mutation in manifest.replacements
            if mutation.page_pair_index == pair_index
            and mutation.role == "inspection_date"
        )
        opinion = next(
            mutation
            for mutation in manifest.neutralizations
            if mutation.page_pair_index == pair_index
            and mutation.role == "unassigned_stale_opinion"
        )
        assert route.current_value["text"] == "노선명 : 테스트고속도로"
        assert inspected_date.current_value["text"] == (
            "점검일 : 2026.07.29."
        )
        assert opinion.current_value == {
            "text": "",
            "neutralized": True,
        }


def test_complete_real_3q_plan_writes_and_reopens_atomically(
    exact_context,
    tmp_path,
):
    output = (tmp_path / "current-report-complete.hwpx").resolve()

    result = write_exact_hwpx(
        SAMPLE_3Q,
        output,
        exact_context["plan"],
    )

    assert result.operation_count == 972
    assert len(result.target_verifications) == 972
    assert result.table_count == 72
    assert result.expected_rendered_page_count == 48
    assert result.page_contract_proxy_verified is True
    assert result.source_unchanged is True
    assert result.output_render_validation_status == (
        "not_run_com_validation_required"
    )


def test_complete_real_4q_plan_covers_static_targets_and_reopens(
    tmp_path,
):
    context = _build_exact_context(
        tmp_path / "current-incident-hwpx-plan-4q",
        sample_path=SAMPLE_4Q,
        template_id="sample-4q-v1",
        inspection_date="2026-10-29",
    )
    output = (tmp_path / "current-report-complete-4q.hwpx").resolve()
    operations = {
        operation.target_id: operation
        for operation in context["plan"].operations
    }

    assert len(context["stale_audit"].required_target_ids) == 120
    assert set(
        context["stale_audit"].required_target_ids
    ).issubset(operations)
    for pair in context["stale_audit"].page_pairs:
        assert (
            operations[pair.title_route_heading.target_id].value
            == "4분기 일반평가(불시점검) 양식(테스트고속도로)"
        )
        assert (
            operations[pair.photo_page_header.target_id].value
            == "□ 테스트고속도로 현황사진"
        )

    result = write_exact_hwpx(
        SAMPLE_4Q,
        output,
        context["plan"],
    )

    assert result.operation_count == len(context["plan"].operations)
    assert len(result.target_verifications) == len(
        context["plan"].operations
    )
    assert result.table_count == 72
    assert result.expected_rendered_page_count == 48
    assert result.page_contract_proxy_verified is True
    assert result.source_unchanged is True


def test_static_factual_coverage_gate_rejects_one_missing_target(
    exact_context,
):
    manifest = exact_context["manifest"]
    audit = exact_context["stale_audit"]
    removed = audit.page_pairs[7].photo_page_header.target_id
    broken = replace(
        manifest,
        replacements=tuple(
            mutation
            for mutation in manifest.replacements
            if mutation.target_id != removed
        ),
    )

    with pytest.raises(CurrentIncidentHwpxPlanError) as caught:
        _validate_stale_factual_manifest(
            broken,
            audit=audit,
            paragraph_targets={
                pair.photo_page_header.target_id: (
                    pair.photo_page_header
                )
                for pair in audit.page_pairs
            },
            signature_targets={
                pair.inspector_signature.target_id: (
                    pair.inspector_signature
                )
                for pair in audit.page_pairs
            },
        )

    assert caught.value.code == "stale_factual_target_coverage_incomplete"


def test_evaluation_rows_emit_one_final_value_per_unique_grade_cell(
    exact_context,
):
    manifest = exact_context["manifest"]
    operations = {
        item.target_id: item
        for item in exact_context["plan"].operations
        if isinstance(item, ExactHwpxTextOperation)
    }
    selected = next(
        row
        for row in manifest.evaluation_rows
        if row.selected_grade == "normal"
    )
    selected_targets = {
        target["target_id"] for target in selected.grade_targets
    }
    assert len(selected_targets) == 5
    assert sum(
        operations[target_id].value == "■"
        for target_id in selected_targets
    ) == 1
    assert operations[selected.selected_target_id].value == "■"

    merged = next(
        row
        for row in manifest.evaluation_rows
        if row.selected_grade == "clear_unassessed"
    )
    merged_targets = {
        target["target_id"] for target in merged.grade_targets
    }
    assert len(merged_targets) == 3
    assert {
        operations[target_id].value for target_id in merged_targets
    } == {""}


def test_picture_replacements_are_same_media_and_removals_are_neutral(
    exact_context,
):
    manifest = exact_context["manifest"]
    picture_operations = {
        item.target_id: item
        for item in exact_context["plan"].operations
        if isinstance(item, ExactHwpxPictureOperation)
    }
    primary = manifest.incident_bindings[0].primary_picture_target_id
    primary_operation = picture_operations[primary]
    primary_payload = Path(primary_operation.evidence_path).read_bytes()
    media, width, height = _inspect_image_header(
        primary_payload,
        limits=HwpxWeakCaseLimits(),
    )
    assert media in {"image/bmp", "image/png", "image/jpeg"}
    assert width <= 1_024
    assert height <= 1_024
    assert primary_operation.evidence_sha256 == hashlib.sha256(
        primary_payload
    ).hexdigest()

    removed = next(
        item
        for item in manifest.neutralizations
        if item.operation == "remove_picture"
    )
    neutral_payload = Path(
        picture_operations[removed.target_id].evidence_path
    ).read_bytes()
    neutral_media, neutral_width, neutral_height = (
        _inspect_image_header(
            neutral_payload,
            limits=HwpxWeakCaseLimits(),
        )
    )
    assert neutral_media in {"image/bmp", "image/png", "image/jpeg"}
    assert (neutral_width, neutral_height) == (1, 1)


def test_server_mapping_must_match_selected_ref_path_and_hash(
    exact_context, tmp_path
):
    evidence_root = tmp_path / "evidence"
    staging_root = tmp_path / "stage"
    evidence_root.mkdir()
    staging_root.mkdir()
    changed = evidence_root / "changed.png"
    Image.new("RGB", (32, 32), (250, 10, 10)).save(changed)
    selected_sha = exact_context["manifest"].incident_bindings[
        0
    ].selected_evidence_sha256

    with pytest.raises(CurrentIncidentHwpxPlanError) as caught:
        build_exact_hwpx_write_plan(
            source_path=SAMPLE_3Q,
            binding_manifest=exact_context["manifest"],
            evidence_by_ref={
                EVIDENCE_REF: ServerOwnedEvidenceFile(
                    evidence_ref=EVIDENCE_REF,
                    absolute_path=str(changed.resolve()),
                    sha256=selected_sha,
                )
            },
            server_evidence_root=evidence_root.resolve(),
            transcode_root=staging_root.resolve(),
        )
    assert caught.value.code == "incident_evidence_hash_mismatch"


def test_duplicate_stale_marker_clear_consolidates_but_conflict_rejects(
    exact_context,
):
    manifest = exact_context["manifest"]
    inspection = exact_context["inspection"]
    stale_audit = exact_context["stale_audit"]
    paragraphs = {
        pair.photo_page_header.target_id: pair.photo_page_header
        for pair in stale_audit.page_pairs
    }
    signatures = {
        pair.inspector_signature.target_id: pair.inspector_signature
        for pair in stale_audit.page_pairs
    }
    requested = _requested_text_targets(manifest)
    cells = _resolve_exact_cells(
        SAMPLE_3Q,
        inspection=inspection,
        requested_targets=tuple(
            target_id
            for target_id in requested
            if target_id not in paragraphs
        ),
        limits=ExactHwpxWriterLimits(),
    )
    pictures = {
        picture.target_id: picture
        for picture in inspection.snapshot.pictures
    }
    stale = next(
        item
        for item in manifest.neutralizations
        if item.role == "stale_evaluation_marker"
    )
    duplicate_manifest = replace(
        manifest,
        neutralizations=(*manifest.neutralizations, stale),
    )
    text, _pictures, consumed = _compile_target_mutations(
        duplicate_manifest,
        cells=cells,
        paragraphs=paragraphs,
        pictures=pictures,
        signature_targets=signatures,
        incident_refs=(f"incident:{INCIDENT_ID}",),
    )
    assert stale.target_id in text
    assert len(text[stale.target_id].consumed_tokens) == 2
    assert (
        f"neutralization:{len(manifest.neutralizations)}"
        in consumed
    )

    caption = next(
        item
        for item in manifest.replacements
        if item.role == "incident_caption"
    )
    conflict = ReportTargetMutation(
        role="unassigned_stale_incident_caption",
        operation="clear_text",
        target_id=caption.target_id,
        page_pair_index=caption.page_pair_index,
        page_number=caption.page_number,
        expected_value=caption.expected_value,
        current_value={"text": "", "neutralized": True},
        current_source_refs=caption.current_source_refs,
    )
    conflict_manifest = replace(
        manifest,
        neutralizations=(*manifest.neutralizations, conflict),
    )
    with pytest.raises(CurrentIncidentHwpxPlanError) as caught:
        _compile_target_mutations(
            conflict_manifest,
            cells=cells,
            paragraphs=paragraphs,
            pictures=pictures,
            signature_targets=signatures,
            incident_refs=(f"incident:{INCIDENT_ID}",),
        )
    assert caught.value.code == "conflicting_target_mutation"


def test_shared_binary_picture_target_is_rejected(exact_context):
    inspection = exact_context["inspection"]
    refs: dict[str, list[str]] = {}
    for picture in inspection.snapshot.pictures:
        for ref in picture.binary_item_refs:
            refs.setdefault(ref, []).append(picture.target_id)
    shared_targets = next(
        targets for targets in refs.values() if len(targets) > 1
    )

    with pytest.raises(CurrentIncidentHwpxPlanError) as caught:
        _resolve_picture_sources(
            SAMPLE_3Q,
            inspection=inspection,
            requested_targets=(shared_targets[0],),
            limits=ExactHwpxWriterLimits(),
        )
    assert caught.value.code == "shared_picture_cross_target_change"


def test_atomic_plan_never_truncates_above_writer_limit(exact_context):
    limits = ExactHwpxWriterLimits(max_operations=5_000)
    oversized = replace(
        exact_context["manifest"],
        required_writer_operation_count=5_001,
    )
    with pytest.raises(CurrentIncidentHwpxPlanError) as caught:
        build_exact_hwpx_write_plan(
            source_path=SAMPLE_3Q,
            binding_manifest=oversized,
            evidence_by_ref=exact_context["mapping"],
            server_evidence_root=exact_context["evidence_root"],
            transcode_root=exact_context["staging_root"],
            limits=limits,
        )
    assert caught.value.code == "operation_capacity_exceeded"
