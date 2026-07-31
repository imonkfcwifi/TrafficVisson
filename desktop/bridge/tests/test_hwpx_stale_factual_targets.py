from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.hwpx_stale_factual_targets import (
    HwpxStaleFactualTargetError,
    audit_stale_factual_targets,
    check_stale_factual_target_coverage,
    require_stale_factual_target_coverage,
)


WORKSPACE = Path(__file__).resolve().parents[3]
SAMPLE_3Q = (
    WORKSPACE / "sample" / "3분기 점검 내용 정리.hwpx"
)
SAMPLE_4Q = (
    WORKSPACE / "sample" / "4분기 점검 내용 정리.hwpx"
)
PARAGRAPH_TARGET = re.compile(r"^hwpx-paragraph-[a-f0-9]{24}$")


@pytest.fixture(scope="module")
def stale_3q():
    return audit_stale_factual_targets(SAMPLE_3Q)


@pytest.fixture(scope="module")
def stale_4q():
    return audit_stale_factual_targets(SAMPLE_4Q)


@pytest.mark.parametrize(
    ("fixture_name", "template_id", "quarter", "audit_sha256"),
    (
        (
            "stale_3q",
            "sample-3q-v1",
            3,
            "40e623ea40a7c41f53d0a7cda6d79a9626aac4d98944d2edf81fc0b07025cb6c",
        ),
        (
            "stale_4q",
            "sample-4q-v1",
            4,
            "1ecea553ae22e672806b434ea8eee3fbd1fa175d0c7ea9e84caba01aa83c33f3",
        ),
    ),
)
def test_real_samples_map_all_24_omitted_factual_target_sets(
    request,
    fixture_name,
    template_id,
    quarter,
    audit_sha256,
):
    audit = request.getfixturevalue(fixture_name)
    counts = audit.counts

    assert audit.template_id == template_id
    assert audit.quarter == quarter
    assert audit.audit_sha256 == audit_sha256
    assert counts.page_pair_count == 24
    assert counts.required_target_count == 120
    assert counts.cell_target_count == 72
    assert counts.paragraph_target_count == 24
    assert counts.picture_target_count == 24
    assert counts.title_group_count == 24
    assert counts.title_route_heading_count == 24
    assert counts.inspector_cell_count == 24
    assert counts.photo_page_header_count == 24
    assert counts.inspector_signature_picture_count == 24
    assert counts.existing_binder_route_reference_count == 24
    assert counts.complete_route_surface_count == 72
    assert counts.unique_signature_binary_count == 2
    assert counts.unclassified_external_factual_paragraph_count == 0
    assert counts.unclassified_non_photo_picture_count == 0
    assert audit.complete_page_pair_coverage is True
    assert audit.read_only is True
    assert audit.no_archive_extraction is True
    assert audit.no_com is True
    assert audit.no_document_write is True


@pytest.mark.parametrize("fixture_name", ("stale_3q", "stale_4q"))
def test_every_pair_has_unique_exact_targets_and_page_addresses(
    request,
    fixture_name,
):
    audit = request.getfixturevalue(fixture_name)

    assert len(audit.required_target_ids) == 120
    assert len(set(audit.required_target_ids)) == 120
    assert [pair.page_pair_index for pair in audit.page_pairs] == list(
        range(24)
    )
    assert [
        pair.inspection_page_number for pair in audit.page_pairs
    ] == list(range(1, 48, 2))
    assert [
        pair.photo_page_number for pair in audit.page_pairs
    ] == list(range(2, 49, 2))

    for pair in audit.page_pairs:
        assert len(pair.required_target_ids) == 5
        assert len(set(pair.required_target_ids)) == 5
        assert pair.required_target_ids == (
            pair.title_group.target_id,
            pair.title_route_heading.target_id,
            pair.inspection_inspector.target_id,
            pair.photo_page_header.target_id,
            pair.inspector_signature.target_id,
        )
        assert pair.title_group.role == "title_group"
        assert pair.title_route_heading.role == "title_route_heading"
        assert pair.inspection_inspector.role == "inspection_inspector"
        assert pair.photo_page_header.role == "photo_page_header"
        assert pair.inspector_signature.role == "inspector_signature"
        assert pair.title_group.table_index == pair.page_pair_index * 3
        assert (
            pair.inspection_inspector.table_index
            == pair.page_pair_index * 3 + 1
        )
        assert PARAGRAPH_TARGET.fullmatch(
            pair.photo_page_header.target_id
        )
        assert len(pair.page_pair_manifest_sha256) == 64


@pytest.mark.parametrize("fixture_name", ("stale_3q", "stale_4q"))
def test_all_three_historical_route_surfaces_are_explicit(
    request,
    fixture_name,
):
    audit = request.getfixturevalue(fixture_name)
    all_surface_ids = []

    for pair in audit.page_pairs:
        assert pair.complete_route_surface_target_ids == (
            pair.title_route_heading.target_id,
            pair.existing_binder_route.target_id,
            pair.photo_page_header.target_id,
        )
        assert "양식(" in pair.title_route_heading.expected_text
        assert pair.existing_binder_route.expected_text.strip().startswith(
            "노선명"
        )
        assert pair.photo_page_header.expected_text.strip().startswith("□")
        assert "현황사진" in pair.photo_page_header.expected_text
        assert len(pair.title_route_heading.expected_text_sha256) == 64
        assert len(pair.existing_binder_route.expected_text_sha256) == 64
        assert len(pair.photo_page_header.expected_text_sha256) == 64
        all_surface_ids.extend(pair.complete_route_surface_target_ids)

    assert len(all_surface_ids) == 72
    assert len(set(all_surface_ids)) == 72


@pytest.mark.parametrize("fixture_name", ("stale_3q", "stale_4q"))
def test_all_inspector_signatures_are_bound_and_hashed(
    request,
    fixture_name,
):
    audit = request.getfixturevalue(fixture_name)
    pictures = [
        pair.inspector_signature for pair in audit.page_pairs
    ]

    assert len(pictures) == 24
    assert len({picture.target_id for picture in pictures}) == 24
    assert sum(
        len(group.picture_target_ids)
        for group in audit.signature_binary_groups
    ) == 24
    assert {
        group.binary_sha256 for group in audit.signature_binary_groups
    } == {
        "37a20ca93d79c1919901211978e6e323dab9d62fd613251b426f172aabb1e29e",
        "a57151063c228d5545c670fcefaae0ca09c1e7070c50aed70806209edf7ed235",
    }
    for pair, picture in zip(audit.page_pairs, pictures):
        assert picture.cell_target_id == pair.inspection_inspector.target_id
        assert picture.table_index == pair.page_pair_index * 3 + 1
        assert picture.binary_item_refs
        assert picture.binaries
        assert {
            binary.binary_item_ref for binary in picture.binaries
        } == set(picture.binary_item_refs)
        assert all(
            binary.package_member.startswith("BinData/")
            and binary.size_bytes > 0
            and len(binary.sha256) == 64
            for binary in picture.binaries
        )
        assert len(picture.expected_binding_sha256) == 64


def test_irregular_headers_still_have_exact_standalone_text_addresses(
    stale_3q,
    stale_4q,
):
    header_3q = stale_3q.page_pairs[3].photo_page_header
    header_4q = stale_4q.page_pairs[21].photo_page_header

    assert header_3q.descendant_table_indexes == (11,)
    assert header_3q.address.paragraph_ordinal == 1106
    assert header_3q.address.section_child_index == 15
    assert (
        header_3q.expected_text
        == "□ 제2영동고속도로 (광주-원주) 현황사진"
    )
    assert header_4q.descendant_table_indexes == (65,)
    assert header_4q.address.paragraph_ordinal == 6198
    assert header_4q.address.section_child_index == 86
    assert (
        header_4q.expected_text
        == "□ 신대구부산 고속도로 현황사진"
    )


def test_first_and_last_exact_targets_are_pinned(stale_3q, stale_4q):
    first_3q = stale_3q.page_pairs[0]
    last_3q = stale_3q.page_pairs[-1]
    first_4q = stale_4q.page_pairs[0]
    last_4q = stale_4q.page_pairs[-1]

    assert (
        first_3q.title_group.target_id
        == "hwpx-cell-0e4adbea94347c76275595e0"
    )
    assert (
        first_3q.title_route_heading.target_id
        == "hwpx-cell-8031725c39839a0fd808add6"
    )
    assert (
        first_3q.inspection_inspector.target_id
        == "hwpx-cell-d481c906f35c7bb85a72bb24"
    )
    assert (
        first_3q.photo_page_header.target_id
        == "hwpx-paragraph-ff8814a8c13696da13a33b84"
    )
    assert (
        first_3q.inspector_signature.target_id
        == "hwpx-picture-cc4ae106a5aca930437f27c9"
    )
    assert (
        last_3q.title_group.target_id
        == "hwpx-cell-68e286eaf61e69a78c262665"
    )
    assert (
        last_3q.title_route_heading.target_id
        == "hwpx-cell-c71cc6696f6cc54da2bc9ec1"
    )
    assert (
        last_3q.photo_page_header.target_id
        == "hwpx-paragraph-287ae35674823f7534f41852"
    )
    assert (
        last_3q.inspector_signature.target_id
        == "hwpx-picture-f22e16618092a1adf75a3d25"
    )

    assert (
        first_4q.title_group.target_id
        == "hwpx-cell-3e4d1247293f4522b9fb5af8"
    )
    assert (
        first_4q.title_route_heading.target_id
        == "hwpx-cell-c6078643073873b0607d1f5b"
    )
    assert (
        first_4q.inspection_inspector.target_id
        == "hwpx-cell-24d65f74192e5ef77f2be164"
    )
    assert (
        first_4q.photo_page_header.target_id
        == "hwpx-paragraph-b65cb84443eeb575f9829fa6"
    )
    assert (
        first_4q.inspector_signature.target_id
        == "hwpx-picture-d076560d05153038565e1b77"
    )
    assert (
        last_4q.title_group.target_id
        == "hwpx-cell-67f709eacc0b8c059feb12ac"
    )
    assert (
        last_4q.title_route_heading.target_id
        == "hwpx-cell-706a7d6575c8d55b53476b90"
    )
    assert (
        last_4q.photo_page_header.target_id
        == "hwpx-paragraph-55067bd9098d7bf7b6f3422c"
    )
    assert (
        last_4q.inspector_signature.target_id
        == "hwpx-picture-aea31858d7d8d0cac7b2d30d"
    )


@pytest.mark.parametrize("fixture_name", ("stale_3q", "stale_4q"))
def test_text_targets_pin_minimum_paragraph_and_lineseg_policy(
    request,
    fixture_name,
):
    audit = request.getfixturevalue(fixture_name)
    text_targets = [
        target
        for pair in audit.page_pairs
        for target in (
            pair.title_group,
            pair.title_route_heading,
            pair.inspection_inspector,
            pair.photo_page_header,
        )
    ]

    assert audit.title_and_complex_run_deletion_forbidden is True
    assert audit.pre_save_full_residual_scan_required is True
    assert audit.text_edit_policy == (
        "replace_exact_t_nodes_and_remove_only_host_p_direct_linesegarray"
    )
    for target in text_targets:
        addresses = (
            target.host_paragraphs
            if hasattr(target, "host_paragraphs")
            else (target.address,)
        )
        assert target.run_deletion_forbidden is True
        assert addresses
        for address in addresses:
            assert address.selected_text
            assert len(address.selected_text_sha256) == 64
            assert address.selected_text_run_ordinals
            assert address.selected_text_run_paths
            assert address.direct_linesegarray_child_indexes
            assert address.direct_linesegarray_sha256s
            assert address.run_deletion_forbidden is True
            assert address.recursive_lineseg_removal_forbidden is True
            assert address.lineseg_policy == (
                "remove_direct_linesegarray_from_this_p_only"
            )
            assert len(address.run_structure_sha256) == 64
            assert len(address.address_sha256) == 64


@pytest.mark.parametrize("fixture_name", ("stale_3q", "stale_4q"))
def test_complete_exact_operation_coverage_passes(
    request,
    fixture_name,
):
    audit = request.getfixturevalue(fixture_name)

    coverage = require_stale_factual_target_coverage(
        audit,
        audit.required_target_ids,
    )

    assert coverage.complete is True
    assert coverage.covered_target_count == 120
    assert coverage.missing_target_ids == ()
    assert coverage.duplicate_target_ids == ()
    assert coverage.unknown_target_ids == ()


def test_missing_route_and_signature_targets_expose_stale_persistence(
    stale_3q,
):
    route = stale_3q.page_pairs[7].title_route_heading.target_id
    header = stale_3q.page_pairs[7].photo_page_header.target_id
    inspector = stale_3q.page_pairs[7].inspection_inspector.target_id
    signature = stale_3q.page_pairs[7].inspector_signature.target_id
    supplied = tuple(
        target_id
        for target_id in stale_3q.required_target_ids
        if target_id not in {route, header, inspector, signature}
    )

    coverage = check_stale_factual_target_coverage(
        stale_3q,
        supplied,
    )

    assert coverage.complete is False
    assert set(coverage.stale_route_surface_target_ids) == {
        route,
        header,
    }
    assert coverage.stale_inspector_target_ids == (inspector,)
    assert coverage.stale_signature_picture_target_ids == (signature,)
    with pytest.raises(HwpxStaleFactualTargetError) as raised:
        require_stale_factual_target_coverage(stale_3q, supplied)
    assert raised.value.code == "stale_factual_target_coverage_incomplete"


def test_duplicate_and_unknown_operations_fail_closed(stale_4q):
    duplicate = (
        *stale_4q.required_target_ids,
        stale_4q.required_target_ids[0],
    )
    with pytest.raises(HwpxStaleFactualTargetError) as raised:
        require_stale_factual_target_coverage(stale_4q, duplicate)
    assert (
        raised.value.code
        == "duplicate_stale_factual_operation_target"
    )

    unknown = (
        *stale_4q.required_target_ids,
        "hwpx-paragraph-" + "f" * 24,
    )
    with pytest.raises(HwpxStaleFactualTargetError) as raised:
        require_stale_factual_target_coverage(stale_4q, unknown)
    assert raised.value.code == "unknown_stale_factual_operation_target"
