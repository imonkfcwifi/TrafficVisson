from __future__ import annotations

from pathlib import Path

import pytest

from app.hwpx_evaluation_grid import (
    GRADE_NAMES,
    HwpxEvaluationGridError,
    build_current_rating_mutation_plan,
    inspect_sample_evaluation_grid,
)


WORKSPACE = Path(__file__).resolve().parents[3]
SAMPLE_3Q = (
    WORKSPACE / "sample" / "3분기 점검 내용 정리.hwpx"
)
SAMPLE_4Q = (
    WORKSPACE / "sample" / "4분기 점검 내용 정리.hwpx"
)


@pytest.fixture(scope="module")
def grid_3q():
    return inspect_sample_evaluation_grid(SAMPLE_3Q)


@pytest.fixture(scope="module")
def grid_4q():
    return inspect_sample_evaluation_grid(SAMPLE_4Q)


@pytest.mark.parametrize(
    ("fixture_name", "template_id", "quarter", "markers"),
    (
        ("grid_3q", "sample-3q-v1", 3, 423),
        ("grid_4q", "sample-4q-v1", 4, 398),
    ),
)
def test_real_samples_map_all_24_inspection_grids(
    request,
    fixture_name,
    template_id,
    quarter,
    markers,
):
    grid = request.getfixturevalue(fixture_name)

    assert grid.template_id == template_id
    assert grid.quarter == quarter
    assert grid.inspection_table_count == 24
    assert grid.evaluation_row_count == 24 * 27
    assert grid.five_grade_row_count == 24 * 25
    assert grid.merged_row_count == 24 * 2
    assert grid.existing_marker_count == markers
    assert len(grid.audit_sha256) == 64
    assert [table.table_index for table in grid.inspection_tables] == [
        pair * 3 + 1 for pair in range(24)
    ]
    assert [
        table.inspection_page_number
        for table in grid.inspection_tables
    ] == list(range(1, 48, 2))


@pytest.mark.parametrize("fixture_name", ("grid_3q", "grid_4q"))
def test_every_table_maps_roles_and_25_plus_2_row_contract(
    request,
    fixture_name,
):
    grid = request.getfixturevalue(fixture_name)

    for table in grid.inspection_tables:
        roles = table.role_cells
        assert roles.route.expected_text.strip().startswith("노선명")
        assert roles.inspected_date.expected_text.strip().startswith(
            "점검일"
        )
        assert roles.inspector.expected_text.strip().startswith(
            "점검자"
        )
        assert roles.opinion_label.expected_text == "점검의견"
        assert roles.opinion_body.row == roles.opinion_label.row + 1
        assert (
            roles.opinion_body.column
            == roles.opinion_label.column
        )
        assert len(table.evaluation_rows) == 27

        five_grade = [
            row
            for row in table.evaluation_rows
            if row.rating_mode == "five_grade"
        ]
        merged = [
            row
            for row in table.evaluation_rows
            if row.rating_mode == "merged_not_five_grade"
        ]
        assert len(five_grade) == 25
        assert len(merged) == 2
        assert [row.row for row in merged] == [21, 27]
        assert all(
            row.five_grade_target_ids is not None
            and len(set(row.five_grade_target_ids)) == 5
            and tuple(
                target.grade
                for target in row.logical_grade_targets
            )
            == GRADE_NAMES
            for row in five_grade
        )
        assert all(
            row.five_grade_target_ids is None
            and len(row.neutralization_targets) == 3
            and len(
                {
                    target.cell.target_id
                    for target in row.logical_grade_targets[1:4]
                }
            )
            == 1
            for row in merged
        )


@pytest.mark.parametrize("fixture_name", ("grid_3q", "grid_4q"))
def test_all_historical_markers_are_captured_once_and_only_in_grid(
    request,
    fixture_name,
):
    grid = request.getfixturevalue(fixture_name)

    table_markers = [
        marker
        for table in grid.inspection_tables
        for marker in table.existing_marker_targets
    ]
    row_markers = [
        marker
        for table in grid.inspection_tables
        for row in table.evaluation_rows
        for marker in row.existing_marker_targets
    ]

    assert table_markers == row_markers
    assert len({marker.target_id for marker in table_markers}) == len(
        table_markers
    )
    assert all(
        marker.expected_text == "■"
        and len(marker.expected_text_sha256) == 64
        for marker in table_markers
    )
    assert all(
        len(row.existing_marker_targets) <= 1
        for table in grid.inspection_tables
        for row in table.evaluation_rows
    )


def test_exact_first_and_last_role_targets_are_pinned(grid_3q, grid_4q):
    first_3q = grid_3q.inspection_tables[0].role_cells
    last_3q = grid_3q.inspection_tables[-1].role_cells
    first_4q = grid_4q.inspection_tables[0].role_cells
    last_4q = grid_4q.inspection_tables[-1].role_cells

    assert (
        first_3q.route.target_id
        == "hwpx-cell-c18e4565894846ed44dfa480"
    )
    assert (
        first_3q.inspected_date.target_id
        == "hwpx-cell-ef6863d03884549de1db419a"
    )
    assert (
        first_3q.inspector.target_id
        == "hwpx-cell-d481c906f35c7bb85a72bb24"
    )
    assert (
        first_3q.opinion_body.target_id
        == "hwpx-cell-87c4f01b68a22b1e2fca33d7"
    )
    assert (
        last_3q.route.target_id
        == "hwpx-cell-64e0caae461f2177cc45fcf6"
    )
    assert (
        last_3q.inspected_date.target_id
        == "hwpx-cell-a23e1cd675d243782060765d"
    )
    assert (
        last_3q.inspector.target_id
        == "hwpx-cell-cbfcd10564b2d34d6daf8f05"
    )
    assert (
        last_3q.opinion_body.target_id
        == "hwpx-cell-7f569af6b2b6d06bfc90deb4"
    )
    assert (
        first_4q.route.target_id
        == "hwpx-cell-dd430ea4902b309c69381b2e"
    )
    assert (
        first_4q.inspected_date.target_id
        == "hwpx-cell-9a7a2031b427cfda0d784a02"
    )
    assert (
        first_4q.inspector.target_id
        == "hwpx-cell-24d65f74192e5ef77f2be164"
    )
    assert (
        first_4q.opinion_body.target_id
        == "hwpx-cell-d9b357b011888a8c07e22789"
    )
    assert (
        last_4q.route.target_id
        == "hwpx-cell-e3cc68c47ff611af7d2bdf5e"
    )
    assert (
        last_4q.inspected_date.target_id
        == "hwpx-cell-7fdb5253e07bca64c8308692"
    )
    assert (
        last_4q.inspector.target_id
        == "hwpx-cell-0c36d5de26a0fbbf59ab2f11"
    )
    assert (
        last_4q.opinion_body.target_id
        == "hwpx-cell-4439c1f30469dbc35600e849"
    )


def test_empty_current_rating_set_clears_every_historical_marker(
    grid_3q,
):
    plan = build_current_rating_mutation_plan(grid_3q, {})

    assert plan.explicit_rating_count == 0
    assert plan.historical_marker_count == 423
    assert plan.cleared_historical_marker_count == 423
    assert plan.selected_marker_count == 0
    assert plan.mutation_count == 423
    assert len({item.target_id for item in plan.mutations}) == 423
    assert all(
        item.value == ""
        and item.action == "clear_historical_marker"
        and item.expected_text == "■"
        for item in plan.mutations
    )


def test_explicit_five_grade_rating_overrides_only_selected_target(
    grid_3q,
):
    table = grid_3q.inspection_tables[0]
    row = next(
        candidate
        for candidate in table.evaluation_rows
        if candidate.row == 3
    )
    assert row.existing_marker_grade == "normal"

    plan = build_current_rating_mutation_plan(
        grid_3q,
        {(0, 3): "poor"},
    )

    selected = [
        item for item in plan.mutations if item.value == "■"
    ]
    cleared_row = [
        item
        for item in plan.mutations
        if item.page_pair_index == 0
        and item.row == 3
        and item.value == ""
    ]
    assert plan.explicit_rating_count == 1
    assert plan.selected_marker_count == 1
    assert len(selected) == 1
    assert selected[0].explicit_grade == "poor"
    assert selected[0].action == "set_explicit_current_rating"
    assert len(cleared_row) == 1
    assert cleared_row[0].explicit_grade is None
    assert len({item.target_id for item in plan.mutations}) == (
        plan.mutation_count
    )
    assert len(plan.plan_sha256) == 64


def test_same_historical_marker_is_retained_only_by_explicit_rating(
    grid_3q,
):
    plan = build_current_rating_mutation_plan(
        grid_3q,
        {(0, 3): "normal"},
    )
    selected = [
        item
        for item in plan.mutations
        if item.page_pair_index == 0
        and item.row == 3
        and item.value == "■"
    ]

    assert len(selected) == 1
    assert (
        selected[0].action
        == "retain_as_explicit_current_rating"
    )
    assert selected[0].expected_text == "■"
    assert plan.cleared_historical_marker_count == 422


@pytest.mark.parametrize(
    ("ratings", "code"),
    (
        ({(0, 21): "very_good"}, "merged_rating_requires"),
        ({(24, 3): "good"}, "explicit_rating_row_unknown"),
        ({(0, 3): "excellent"}, "explicit_rating_grade_invalid"),
        ({"0:3": "good"}, "explicit_rating_key_invalid"),
    ),
)
def test_unsafe_or_unknown_explicit_rating_is_blocked(
    grid_4q,
    ratings,
    code,
):
    with pytest.raises(HwpxEvaluationGridError) as error:
        build_current_rating_mutation_plan(grid_4q, ratings)

    assert error.value.code.startswith(code)
    assert error.value.apply_blocked is True
    assert error.value.provider_called is False
    assert error.value.com_invoked is False
    assert error.value.writer_invoked is False
