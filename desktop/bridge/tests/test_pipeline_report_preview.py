from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from PIL import Image

from app.current_incident_hwpx_plan import ServerOwnedEvidenceFile
from app.current_incident_report_binder import (
    CurrentIncidentReportBindingError,
    _audit_template,
    deidentification_record_sha256,
)
from app.current_report_draft import CurrentReportDraftService
from app.hwpx_sample_layout import inspect_sample_layout
from app.pipeline_report_preview import (
    DurablePipelineReportPreviewPreparer,
    PipelineReportAttemptConflict,
    PipelineReportPreviewError,
    PipelineReportProfileError,
    ProviderDeidentifiedEvidenceRequired,
    ProviderReportPreviewValidationError,
    SampleTemplateSemanticMapRegistry,
)
from app.report_generation import (
    ReportGenerationResult,
)
from approved_incident_authority_fixture import (
    fixture_approval_record_id,
    fixture_incident_id,
    fixture_output_authority,
)


WORKSPACE = Path(__file__).resolve().parents[3]
SAMPLE_ROOT = WORKSPACE / "sample"
SAMPLE_3Q = SAMPLE_ROOT / "3분기 점검 내용 정리.hwpx"
PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
PIPELINE_JOB_ID = UUID("22222222-2222-4222-8222-222222222222")
ANALYSIS_JOB_ID = UUID("33333333-3333-4333-8333-333333333333")
INCIDENT_LABEL = "pipeline-report-preview"
INCIDENT_ID = fixture_incident_id(
    INCIDENT_LABEL,
    ANALYSIS_JOB_ID,
)
SOURCE_SHA256 = hashlib.sha256(b"current-video").hexdigest()


class FakeReportGenerator:
    def __init__(self):
        self.calls = []

    def generate(self, provider, payload, *, cancel_event=None):
        self.calls.append(
            (
                provider,
                json.loads(json.dumps(payload, ensure_ascii=False)),
            )
        )
        incident_ids = [
            item["incident_id"]
            for item in payload["approved_incidents"]
        ]
        operations = [
            {
                "operation_id": str(uuid4()),
                "type": "fill_cell",
                "target": target,
                "value": "현재 승인 사건에 근거한 점검 의견",
                "source_ref": "summary:approved-incidents",
                "style_policy": "preserve",
                "preview": "점검 의견 미리보기",
            }
            for target in payload[
                "document_structure_summary"
            ]["cell_targets"]
        ]
        return ReportGenerationResult(
            provider=provider,
            model="provider-test-model",
            usage={"input_tokens": 10, "output_tokens": 20},
            draft={
                "title": "현재 영상 도로 점검 보고",
                "sections": [
                    {
                        "heading": "승인 사건",
                        "body": "사용자 승인 사건만 반영했습니다.",
                        "incident_ids": incident_ids,
                    }
                ],
            },
            plan={
                "schema_version": "1.0",
                "plan_id": str(uuid4()),
                "document_snapshot_id": payload[
                    "document_structure_summary"
                ]["document_snapshot_id"],
                "operations": operations,
            },
        )


class InvalidTargetReportGenerator(FakeReportGenerator):
    def generate(self, provider, payload, *, cancel_event=None):
        result = super().generate(
            provider, payload, cancel_event=cancel_event
        )
        plan = dict(result.plan)
        operations = [dict(item) for item in plan["operations"]]
        operations[0]["target"] = "cell:not-bound-by-current-incidents"
        plan["operations"] = operations
        return ReportGenerationResult(
            provider=provider,
            draft=result.draft,
            plan=plan,
        )


def _incident(
    *,
    representative_image_ref: str | None = None,
    representative_image_sha256: str = "a" * 64,
    **overrides,
):
    approval_record_id = fixture_approval_record_id(
        INCIDENT_LABEL,
        PIPELINE_JOB_ID,
    )
    evidence_ref = (
        representative_image_ref
        or f"analysis:{INCIDENT_ID}:representative"
    )
    value = {
        "incident_id": INCIDENT_ID,
        "project_id": str(PROJECT_ID),
        "pipeline_job_id": str(PIPELINE_JOB_ID),
        "analysis_job_id": str(ANALYSIS_JOB_ID),
        "source_sha256": SOURCE_SHA256,
        "review_status": "approved",
        "approval_actor": "local_user",
        "approval_record_id": approval_record_id,
        "approval_record_sha256": "9" * 64,
        "incident_type": "pothole",
        "severity": "moderate",
        "maintenance_action": "포트홀 보수",
        "ocr_text": None,
        "time_ms": 12_000,
        # This internal candidate reference is never sent to the Provider.
        "evidence_refs": [evidence_ref],
        "approved_output_authority": fixture_output_authority(
            label=INCIDENT_LABEL,
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            analysis_job_id=ANALYSIS_JOB_ID,
            source_sha256=SOURCE_SHA256,
            approval_record_id=approval_record_id,
            approval_record_sha256="9" * 64,
            representative_image_ref=evidence_ref,
            representative_image_sha256=(
                representative_image_sha256
            ),
        ),
    }
    value.update(overrides)
    return value


def _evidence(
    incident_id: str = INCIDENT_ID,
    *,
    verified_deidentified: bool = True,
    renamed_original: bool = False,
    deidentified_ref: str | None = None,
):
    original_ref = f"analysis:{incident_id}:representative"
    original_sha256 = "a" * 64
    if not verified_deidentified:
        return {
            "incident_id": incident_id,
            "project_id": str(PROJECT_ID),
            "pipeline_job_id": str(PIPELINE_JOB_ID),
            "analysis_job_id": str(ANALYSIS_JOB_ID),
            "source_sha256": SOURCE_SHA256,
            "original_ref": original_ref,
            "original_sha256": original_sha256,
            "deidentified_ref": None,
            "deidentified_sha256": None,
            "deidentification_record_sha256": None,
            "deidentified_verified": False,
        }
    selected_ref = (
        original_ref
        if renamed_original
        else (
            deidentified_ref
            or f"deid:{incident_id}:representative"
        )
    )
    selected_sha256 = (
        original_sha256 if renamed_original else "b" * 64
    )
    record_sha256 = deidentification_record_sha256(
        incident_id=incident_id,
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        source_sha256=SOURCE_SHA256,
        original_ref=original_ref,
        original_sha256=original_sha256,
        deidentified_ref=selected_ref,
        deidentified_sha256=selected_sha256,
    )
    return {
        "incident_id": incident_id,
        "project_id": str(PROJECT_ID),
        "pipeline_job_id": str(PIPELINE_JOB_ID),
        "analysis_job_id": str(ANALYSIS_JOB_ID),
        "source_sha256": SOURCE_SHA256,
        "original_ref": original_ref,
        "original_sha256": original_sha256,
        "deidentified_ref": selected_ref,
        "deidentified_sha256": selected_sha256,
        "deidentification_record_sha256": record_sha256,
        "deidentified_verified": True,
    }


@pytest.fixture(scope="module")
def registry():
    return SampleTemplateSemanticMapRegistry(SAMPLE_ROOT)


@pytest.fixture(scope="module")
def semantic_map(registry):
    template_sha256 = hashlib.sha256(SAMPLE_3Q.read_bytes()).hexdigest()
    return registry.resolve(
        template_id="sample-q3",
        template_sha256=template_sha256,
        semantic_map_id="sample-q3-semantic-v1",
    )


@pytest.fixture(scope="module")
def inspection():
    return inspect_sample_layout(SAMPLE_3Q)


@pytest.fixture(scope="module")
def evaluation_ratings(semantic_map, inspection):
    _, page_pairs = _audit_template(semantic_map, inspection)
    first_page_rows = page_pairs[0].evaluation_rows
    return tuple(
        (
            "clear_unassessed"
            if not row.five_grade_grid_available
            else "normal"
        )
        for row in first_page_rows
    )


@pytest.fixture(autouse=True)
def cached_inspection(monkeypatch, inspection):
    monkeypatch.setattr(
        "app.pipeline_report_preview.inspect_sample_layout",
        lambda _path: inspection,
    )


def _prepare(
    preparer,
    semantic_map,
    evaluation_ratings,
    *,
    incidents=None,
    **overrides,
):
    supplied_evidence = overrides.get(
        "evidence_by_incident_id",
        {INCIDENT_ID: _evidence()},
    )
    default_evidence = supplied_evidence.get(INCIDENT_ID)
    default_incident = _incident()
    if isinstance(default_evidence, dict):
        default_incident = _incident(
            representative_image_ref=default_evidence[
                "original_ref"
            ],
            representative_image_sha256=default_evidence[
                "original_sha256"
            ],
        )
    values = {
        "project_id": PROJECT_ID,
        "pipeline_job_id": PIPELINE_JOB_ID,
        "analysis_job_id": ANALYSIS_JOB_ID,
        "source_sha256": SOURCE_SHA256,
        "provider": "codex",
        "template_id": "sample-q3",
        "template_sha256": semantic_map.template_sha256,
        "template_semantic_map_id": semantic_map.semantic_map_id,
        "template_semantic_map_sha256": (
            semantic_map.semantic_map_sha256
        ),
        "approved_incidents": incidents or [default_incident],
        "evidence_by_incident_id": supplied_evidence,
        "route_name": "테스트 고속도로",
        "report_inspector_name": "홍길동",
        "inspection_date": "2026-07-29",
        "locations_by_incident_id": {
            INCIDENT_ID: "상행 1.0km"
        },
        "report_policy": {
            "evidence_mode": "shared_deidentified",
            "evaluation_ratings": evaluation_ratings,
        },
        "explicit_transmission_consent": True,
        "report_attempt": 0,
    }
    values.update(overrides)
    return preparer.prepare(**values)


def _preparer(tmp_path, registry, generator):
    return DurablePipelineReportPreviewPreparer(
        data_root=tmp_path.resolve(),
        semantic_maps=registry,
        report_generator=generator,
    )


def test_semantic_map_is_bound_to_exact_audited_48_page_sample(
    semantic_map,
):
    snapshot = semantic_map.profile_snapshot()
    assert snapshot["state"] == "ready"
    assert snapshot["canonical_template_id"] == "sample-3q-v1"
    assert snapshot["template_sha256"] == hashlib.sha256(
        SAMPLE_3Q.read_bytes()
    ).hexdigest()
    assert snapshot["path_exposed"] is False
    assert len(
        semantic_map.document_structure_summary["cell_targets"]
    ) > 100
    assert len(
        semantic_map.document_structure_summary["image_targets"]
    ) > 100


def test_wrong_template_or_semantic_identity_fails_closed(registry):
    with pytest.raises(PipelineReportProfileError):
        registry.resolve(
            template_id="sample-q3",
            template_sha256="0" * 64,
            semantic_map_id="sample-q3-semantic-v1",
        )
    with pytest.raises(PipelineReportProfileError):
        registry.resolve(
            template_id="sample-q3",
            template_sha256=hashlib.sha256(
                SAMPLE_3Q.read_bytes()
            ).hexdigest(),
            semantic_map_id="sample-q4-semantic-v1",
        )


def test_current_binding_runs_before_provider_and_persists_one_record(
    tmp_path,
    registry,
    semantic_map,
    evaluation_ratings,
):
    source_before = hashlib.sha256(SAMPLE_3Q.read_bytes()).hexdigest()
    generator = FakeReportGenerator()
    preparer = _preparer(tmp_path, registry, generator)

    metadata = _prepare(
        preparer, semantic_map, evaluation_ratings
    )

    assert len(generator.calls) == 1
    provider, payload = generator.calls[0]
    assert provider == "codex"
    sent = payload["approved_incidents"][0]
    assert sent["incident_id"] == INCIDENT_ID
    assert sent["evidence_refs"] == [
        f"deid:{INCIDENT_ID}:representative"
    ]
    assert (
        "analysis:internal-candidate-reference"
        not in json.dumps(payload)
    )
    assert "original_ref" not in json.dumps(payload)
    assert set(payload) == {
        "approved_incidents",
        "document_structure_summary",
        "map_refs",
    }
    assert payload["document_structure_summary"][
        "image_targets"
    ] == []
    assert len(
        payload["document_structure_summary"]["cell_targets"]
    ) == 1

    assert metadata["provider_called"] is True
    assert metadata["exact_writer_required"] is True
    assert metadata["exact_writer_completed"] is False
    assert metadata["render_validation_completed"] is False
    assert metadata["com_invoked"] is False
    assert metadata["document_written"] is False
    assert metadata["apply_eligible"] is False
    assert metadata["binding"]["approved_incident_count"] == 1
    assert (
        metadata["binding"]["required_writer_operation_count"]
        <= metadata["binding"][
            "exact_hwpx_writer_operation_limit"
        ]
    )
    assert "writer_batching_required" not in json.dumps(metadata)

    records = list(
        (tmp_path / "report-previews").rglob("*.json")
    )
    assert len(records) == 1
    record = preparer._load_record(records[0])
    binding = record["current_binding_manifest"]
    assert binding["approved_incident_ids"] == [INCIDENT_ID]
    assert binding["approved_incident_bound"] is True
    assert binding["content_provenance_bound"] is True
    assert binding["writer_required"] is True
    assert binding["apply_eligible"] is False
    assert binding["provider_called"] is False
    assert binding["com_invoked"] is False
    assert binding["document_written"] is False
    assert record["expected_rendered_page_count"] == 48
    assert record["render_validation_completed"] is False
    assert record["semantic_provider_plan_sha256"] == (
        metadata["semantic_provider_plan_sha256"]
    )
    assert record["draft_sha256"] == metadata["draft_sha256"]
    record_json = json.dumps(record, ensure_ascii=False)
    assert "source_path" not in record_json
    assert str(SAMPLE_3Q) not in record_json
    assert (
        hashlib.sha256(SAMPLE_3Q.read_bytes()).hexdigest()
        == source_before
    )
    context = preparer.load_exact_context(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        preview_id=metadata["preview_id"],
    )
    assert context.analysis_job_id == ANALYSIS_JOB_ID
    assert context.source_path == SAMPLE_3Q
    assert context.binding.binding_manifest_sha256 == (
        metadata["current_binding_manifest_sha256"]
    )
    assert context.binding.approved_incident_ids == (INCIDENT_ID,)


def test_binder_failure_exposes_safe_code_and_persists_nothing(
    tmp_path,
    registry,
    semantic_map,
    evaluation_ratings,
):
    generator = FakeReportGenerator()
    preparer = _preparer(tmp_path, registry, generator)

    with pytest.raises(
        CurrentIncidentReportBindingError
    ) as caught:
        _prepare(
            preparer,
            semantic_map,
            evaluation_ratings,
            evidence_by_incident_id={
                INCIDENT_ID: _evidence(
                    verified_deidentified=False
                )
            },
        )

    assert caught.value.code == "deidentified_evidence_required"
    assert caught.value.provider_called is False
    assert generator.calls == []
    assert list(
        (tmp_path / "report-previews").rglob("*.json")
    ) == []


def test_internal_original_persists_binding_only_without_provider(
    tmp_path,
    registry,
    semantic_map,
    evaluation_ratings,
):
    generator = FakeReportGenerator()
    preparer = _preparer(tmp_path, registry, generator)
    internal_policy = {
        "evidence_mode": "internal_original",
        "evaluation_ratings": evaluation_ratings,
    }

    metadata = _prepare(
        preparer,
        semantic_map,
        evaluation_ratings,
        evidence_by_incident_id={
            INCIDENT_ID: _evidence(
                verified_deidentified=False
            )
        },
        report_policy=internal_policy,
        explicit_transmission_consent=False,
    )

    assert metadata["provider_called"] is False
    assert metadata["exact_writer_required"] is True
    assert metadata["binding"]["approved_incident_count"] == 1
    assert generator.calls == []
    context = preparer.load_exact_context(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        preview_id=metadata["preview_id"],
    )
    assert context.binding.incident_bindings[
        0
    ].selected_evidence_ref == (
        f"analysis:{INCIDENT_ID}:representative"
    )


def test_internal_binding_writes_real_exact_3q_draft_without_provider(
    tmp_path,
    registry,
    semantic_map,
    evaluation_ratings,
):
    evidence_root = tmp_path / "server-evidence"
    evidence_root.mkdir()
    evidence_path = evidence_root / "incident.jpg"
    Image.new("RGB", (64, 48), (40, 90, 180)).save(
        evidence_path,
        format="JPEG",
    )
    evidence_sha256 = hashlib.sha256(
        evidence_path.read_bytes()
    ).hexdigest()
    original = _evidence(verified_deidentified=False)
    original["original_sha256"] = evidence_sha256
    generator = FakeReportGenerator()
    preparer = _preparer(tmp_path, registry, generator)
    metadata = _prepare(
        preparer,
        semantic_map,
        evaluation_ratings,
        evidence_by_incident_id={INCIDENT_ID: original},
        report_policy={
            "evidence_mode": "internal_original",
            "evaluation_ratings": evaluation_ratings,
        },
        explicit_transmission_consent=False,
    )
    context = preparer.load_exact_context(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        preview_id=metadata["preview_id"],
    )

    def resolve(
        project_id,
        pipeline_job_id,
        analysis_job_id,
        selected_ref,
        expected_sha256,
    ):
        assert project_id == PROJECT_ID
        assert pipeline_job_id == PIPELINE_JOB_ID
        assert analysis_job_id == ANALYSIS_JOB_ID
        assert selected_ref == f"analysis:{INCIDENT_ID}:representative"
        assert expected_sha256 == evidence_sha256
        return ServerOwnedEvidenceFile(
            evidence_ref=selected_ref,
            absolute_path=str(evidence_path.resolve()),
            sha256=evidence_sha256,
        )

    service = CurrentReportDraftService(
        owned_draft_root=tmp_path / "current-report-drafts",
        report_evidence_resolver=resolve,
        server_evidence_root=evidence_root.resolve(),
    )
    draft_metadata = service.prepare(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        source_path=context.source_path,
        binding_manifest=context.binding,
    )

    assert generator.calls == []
    assert draft_metadata.provider_called is False
    assert draft_metadata.com_invoked is False
    assert draft_metadata.exact_writer_completed is True
    assert draft_metadata.expected_rendered_page_count == 48
    loaded = service.load(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        draft_id=draft_metadata.draft_id,
    )
    assert loaded.output_path.is_file()
    assert loaded.writer_result.table_count == 72
    assert loaded.writer_result.output_render_validation_status == (
        "not_run_com_validation_required"
    )


def test_non_provider_opaque_deidentified_ref_is_not_transmitted(
    tmp_path,
    registry,
    semantic_map,
    evaluation_ratings,
):
    generator = FakeReportGenerator()
    preparer = _preparer(tmp_path, registry, generator)

    with pytest.raises(ProviderDeidentifiedEvidenceRequired):
        _prepare(
            preparer,
            semantic_map,
            evaluation_ratings,
            evidence_by_incident_id={
                INCIDENT_ID: _evidence(
                    deidentified_ref=(
                        f"deidentified:{INCIDENT_ID}:representative"
                    )
                )
            },
        )

    assert generator.calls == []


@pytest.mark.parametrize(
    "unsafe",
    [
        {"absolute_path": r"C:\secret\raw.mp4"},
        {"contact": "010-1234-5678"},
        {"token": "secret-token"},
    ],
)
def test_unknown_incident_fields_fail_before_provider(
    tmp_path,
    registry,
    semantic_map,
    evaluation_ratings,
    unsafe,
):
    generator = FakeReportGenerator()
    preparer = _preparer(tmp_path, registry, generator)

    with pytest.raises(PipelineReportPreviewError):
        _prepare(
            preparer,
            semantic_map,
            evaluation_ratings,
            incidents=[{**_incident(), **unsafe}],
        )
    assert generator.calls == []


def test_consent_and_semantic_hash_are_checked_before_provider(
    tmp_path,
    registry,
    semantic_map,
    evaluation_ratings,
):
    generator = FakeReportGenerator()
    preparer = _preparer(tmp_path, registry, generator)

    with pytest.raises(PipelineReportPreviewError):
        _prepare(
            preparer,
            semantic_map,
            evaluation_ratings,
            explicit_transmission_consent=False,
        )
    with pytest.raises(PipelineReportPreviewError):
        _prepare(
            preparer,
            semantic_map,
            evaluation_ratings,
            template_semantic_map_sha256="f" * 64,
        )
    assert generator.calls == []


def test_invalid_provider_plan_is_not_persisted(
    tmp_path,
    registry,
    semantic_map,
    evaluation_ratings,
):
    generator = InvalidTargetReportGenerator()
    preparer = _preparer(tmp_path, registry, generator)

    with pytest.raises(
        ProviderReportPreviewValidationError
    ) as caught:
        _prepare(
            preparer, semantic_map, evaluation_ratings
        )

    assert caught.value.provider_called is True
    assert len(generator.calls) == 1
    assert list(
        (tmp_path / "report-previews").rglob("*.json")
    ) == []


def test_restart_replays_exact_attempt_and_conflicts_on_changed_input(
    tmp_path,
    registry,
    semantic_map,
    evaluation_ratings,
):
    first_generator = FakeReportGenerator()
    first = _preparer(tmp_path, registry, first_generator)
    first_metadata = _prepare(
        first, semantic_map, evaluation_ratings
    )
    assert len(first_generator.calls) == 1

    restarted_generator = FakeReportGenerator()
    restarted = _preparer(
        tmp_path, registry, restarted_generator
    )
    replayed = _prepare(
        restarted, semantic_map, evaluation_ratings
    )
    assert replayed == first_metadata
    assert restarted_generator.calls == []

    with pytest.raises(PipelineReportAttemptConflict) as caught:
        _prepare(
            restarted,
            semantic_map,
            evaluation_ratings,
            route_name="변경된 고속도로",
        )
    assert caught.value.provider_called is False
    assert restarted_generator.calls == []

    next_attempt = _prepare(
        restarted,
        semantic_map,
        evaluation_ratings,
        route_name="변경된 고속도로",
        report_attempt=1,
    )
    assert next_attempt["report_attempt"] == 1
    assert len(restarted_generator.calls) == 1
    assert len(
        list((tmp_path / "report-previews").rglob("*.json"))
    ) == 2
