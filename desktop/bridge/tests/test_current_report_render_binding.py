from __future__ import annotations

from dataclasses import replace

import pytest

from app.current_incident_report_binder import (
    CURRENT_INCIDENT_REPORT_BINDING_SCHEMA,
    CurrentIncidentReportBindingManifest,
    IncidentSlotBinding,
    ReportTargetMutation,
)
from app.current_report_render_binding import (
    CurrentReportRenderBindingError,
    build_report_render_binding,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _mutation(
    *,
    pair: int,
    role: str,
    text: str,
) -> ReportTargetMutation:
    return ReportTargetMutation(
        role=role,
        operation="replace_text",
        target_id=f"cell:{pair}:{role}",
        page_pair_index=pair,
        page_number=pair * 2 + 1,
        expected_value={"text": "과거 값", "text_sha256": SHA_A},
        current_value={"text": text, "text_sha256": SHA_B},
        current_source_refs=("incident:incident-1",),
    )


def _neutral_picture_mutation(
    *,
    pair: int,
    role: str = "overlaid_stale_incident_photo",
    target_id: str = "picture:stale:1",
) -> ReportTargetMutation:
    return ReportTargetMutation(
        role=role,
        operation="remove_picture",
        target_id=target_id,
        page_pair_index=pair,
        page_number=pair * 2 + 2,
        expected_value={
            "picture_target_id": target_id,
            "template_sha256": SHA_B,
        },
        current_value={"neutralized": True},
        current_source_refs=("incident:incident-1",),
    )


def _binding() -> CurrentIncidentReportBindingManifest:
    incident = IncidentSlotBinding(
        incident_id="incident-1",
        incident_order=0,
        page_pair_index=0,
        inspection_page_number=1,
        photo_page_number=2,
        photo_slot_id="photo-slot-1",
        photo_cell_target_id="cell:photo:1",
        primary_picture_target_id="picture:1",
        additional_picture_target_ids=(),
        caption_cell_target_id="cell:caption:1",
        selected_evidence_sha256=SHA_A,
        selected_evidence_ref="evidence-1",
        evidence_policy="internal_original",
        approved_incident_authority_sha256=SHA_C,
        photo_binding_sha256=SHA_B,
    )
    return CurrentIncidentReportBindingManifest(
        schema_version=CURRENT_INCIDENT_REPORT_BINDING_SCHEMA,
        state="current_incidents_bound_writer_required",
        project_id="00000000-0000-0000-0000-000000000001",
        pipeline_job_id="00000000-0000-0000-0000-000000000002",
        analysis_job_id="00000000-0000-0000-0000-000000000003",
        source_sha256=SHA_A,
        template_id="trafficvisson-3q-sample",
        document_snapshot_id="snapshot-1",
        template_sha256=SHA_B,
        template_semantic_map_sha256=SHA_C,
        structure_target_summary_sha256=SHA_D,
        incident_capacity=123,
        approved_incident_count=1,
        approved_incident_ids=("incident-1",),
        occupied_page_pair_indices=(0,),
        incident_bindings=(incident,),
        replacements=(
            _mutation(
                pair=0,
                role="inspection_route",
                text="노선명 : 현재 노선",
            ),
            _mutation(
                pair=0,
                role="inspection_opinion",
                text="ㅇ 00:00:12 · 포트홀 보수 필요\nㅇ 차선 도색 필요",
            ),
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


def test_builds_all_24_page_pairs_and_preserves_incident_truth():
    result = build_report_render_binding(_binding())

    assert len(result.expected_page_pair_slots) == 24
    first = result.expected_page_pair_slots[0]
    last = result.expected_page_pair_slots[-1]
    assert (
        first.page_pair_index,
        first.inspection_page_number,
        first.photo_page_number,
    ) == (1, 1, 2)
    assert first.incident_ids == ("incident-1",)
    assert first.expected_photo_count == 1
    assert first.expected_caption_count == 1
    assert first.maximum_neutral_picture_count == 0
    assert first.expected_text_evidence == (
        "노선명 : 현재 노선",
        "ㅇ 00:00:12 · 포트홀 보수 필요",
        "ㅇ 차선 도색 필요",
    )
    assert (
        last.page_pair_index,
        last.inspection_page_number,
        last.photo_page_number,
        last.expected_photo_count,
    ) == (24, 47, 48, 0)
    assert result.current_incident_ids == ("incident-1",)
    assert result.expected_photo_count == 1
    assert result.expected_caption_count == 1


def test_binds_photo_page_neutralization_as_a_per_pair_maximum():
    binding = _binding()
    result = build_report_render_binding(
        replace(
            binding,
            neutralizations=(
                _neutral_picture_mutation(pair=0),
                _neutral_picture_mutation(
                    pair=0,
                    role="unassigned_stale_incident_photo",
                    target_id="picture:stale:2",
                ),
            ),
            required_writer_operation_count=4,
        )
    )

    assert (
        result.expected_page_pair_slots[0]
        .maximum_neutral_picture_count
        == 2
    )
    assert all(
        slot.maximum_neutral_picture_count == 0
        for slot in result.expected_page_pair_slots[1:]
    )


@pytest.mark.parametrize(
    "mutation",
    (
        replace(
            _neutral_picture_mutation(pair=0),
            role="unknown_photo_neutralization",
        ),
        replace(
            _neutral_picture_mutation(pair=0),
            page_number=4,
        ),
        replace(
            _neutral_picture_mutation(pair=0),
            current_value={"neutralized": False},
        ),
    ),
)
def test_rejects_unbound_or_malformed_photo_neutralization(mutation):
    binding = _binding()

    with pytest.raises(CurrentReportRenderBindingError) as caught:
        build_report_render_binding(
            replace(
                binding,
                neutralizations=(mutation,),
                required_writer_operation_count=3,
            )
        )

    assert caught.value.code == "neutral_photo_binding_invalid"


def test_rejects_duplicate_neutral_photo_target_allowance():
    mutation = _neutral_picture_mutation(pair=0)
    binding = _binding()

    with pytest.raises(CurrentReportRenderBindingError) as caught:
        build_report_render_binding(
            replace(
                binding,
                neutralizations=(mutation, mutation),
                required_writer_operation_count=4,
            )
        )

    assert caught.value.code == "neutral_photo_binding_invalid"


def test_rejects_incident_bound_to_wrong_physical_pages():
    binding = _binding()
    broken_incident = replace(
        binding.incident_bindings[0],
        photo_page_number=4,
    )

    with pytest.raises(CurrentReportRenderBindingError) as caught:
        build_report_render_binding(
            replace(binding, incident_bindings=(broken_incident,))
        )

    assert caught.value.code == "approved_incident_page_binding_invalid"
    assert caught.value.com_invoked is False


def test_rejects_duplicate_or_missing_approved_incident_projection():
    binding = _binding()

    with pytest.raises(CurrentReportRenderBindingError) as caught:
        build_report_render_binding(
            replace(
                binding,
                approved_incident_count=2,
                approved_incident_ids=("incident-1", "incident-2"),
            )
        )

    assert caught.value.code == "current_report_binding_invalid"


def test_rejects_manifest_that_claims_apply_eligibility_too_early():
    with pytest.raises(CurrentReportRenderBindingError) as caught:
        build_report_render_binding(
            replace(_binding(), apply_eligible=True)
        )

    assert caught.value.code == "current_report_binding_invalid"


def test_omits_empty_and_overlong_text_evidence_without_dropping_counts():
    binding = _binding()
    result = build_report_render_binding(
        replace(
            binding,
            replacements=(
                _mutation(pair=0, role="empty", text="   "),
                _mutation(pair=0, role="long", text="가" * 501),
            ),
        )
    )

    assert result.expected_page_pair_slots[0].expected_text_evidence == ()
    assert result.expected_photo_count == 1
