from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from app.auto_label_runs import (
    AutoLabelRunConflict,
    AutoLabelRunError,
    AutoLabelRunStore,
    ISSUE_TYPES,
)


PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_PROJECT_ID = UUID("22222222-2222-4222-8222-222222222222")
JOB_ID = UUID("33333333-3333-4333-8333-333333333333")
OTHER_JOB_ID = UUID("44444444-4444-4444-8444-444444444444")
RUN_ID = UUID("55555555-5555-4555-8555-555555555555")
CANDIDATE_ID = "0123456789abcdefabcd"


def _match(**overrides):
    return {
        "project_id": str(PROJECT_ID),
        "analysis_job_id": str(JOB_ID),
        "candidate_id": CANDIDATE_ID,
        "issue_type": "pothole",
        "review_status": "pending",
        "decision_hash": "d" * 64,
        **overrides,
    }


def _record(store: AutoLabelRunStore, **overrides):
    values = {
        "run_id": RUN_ID,
        "recipe_id": "a" * 64,
        "command_sha256": "b" * 64,
        "scope": "current_job",
        "mode_requested": "proposal_only",
        "mode_applied": "proposal_only",
        "issue_type": "pothole",
        "job_id": JOB_ID,
        "matches": [_match()],
        **overrides,
    }
    return store.record_run(PROJECT_ID, **values)


def test_run_is_durable_project_scoped_and_review_only(tmp_path):
    root = (tmp_path / "bridge-data").resolve()
    store = AutoLabelRunStore(root)
    record = _record(store)

    assert record["match_count"] == 1
    assert record["review_only"] is True
    assert record["include_in_training"] is False
    assert record["matches"][0]["review_only"] is True
    assert record["matches"][0]["include_in_training"] is False
    assert len(record["run_sha256"]) == 64

    restarted = AutoLabelRunStore(root)
    assert restarted.get(PROJECT_ID, RUN_ID) == record
    assert restarted.latest(PROJECT_ID) == record
    assert restarted.list(PROJECT_ID) == [record]
    assert restarted.list(OTHER_PROJECT_ID) == []


def test_same_run_is_idempotent_but_changed_content_conflicts(tmp_path):
    store = AutoLabelRunStore((tmp_path / "data").resolve())
    first = _record(store)
    assert _record(store) == first

    with pytest.raises(AutoLabelRunConflict):
        _record(store, command_sha256="c" * 64)


def test_project_scope_accepts_multiple_jobs_and_zero_matches(tmp_path):
    store = AutoLabelRunStore((tmp_path / "data").resolve())
    run = _record(
        store,
        scope="project",
        job_id=None,
        matches=[
            _match(),
            _match(
                analysis_job_id=str(OTHER_JOB_ID),
                candidate_id="fedcba9876543210abcd",
                decision_hash="e" * 64,
            ),
        ],
    )
    assert run["match_count"] == 2

    empty = _record(
        store,
        run_id=UUID("66666666-6666-4666-8666-666666666666"),
        scope="project",
        job_id=None,
        matches=[],
    )
    assert empty["matches"] == []


@pytest.mark.parametrize(
    ("legacy_issue_type", "canonical_issue_type"),
    [
        ("surface_damage", "road_surface_damage"),
        ("debris", "road_debris"),
        ("sign_vegetation", "sign_vegetation_occlusion"),
        ("mowing", "roadside_mowing_required"),
        ("sign_tunnel_identity", "guide_sign_identity"),
        ("sign_identity", "guide_sign_identity"),
        ("lane_color_line_fading", "colored_guidance_faded"),
        ("color_guidance_line_faded", "colored_guidance_faded"),
        ("colored_guidance_line_faded", "colored_guidance_faded"),
    ],
)
def test_legacy_issue_aliases_are_ingress_only(
    tmp_path,
    legacy_issue_type,
    canonical_issue_type,
):
    root = (tmp_path / "data").resolve()
    store = AutoLabelRunStore(root)
    record = _record(
        store,
        issue_type=legacy_issue_type,
        matches=[_match(issue_type=legacy_issue_type)],
    )

    assert record["issue_type"] == canonical_issue_type
    assert record["matches"][0]["issue_type"] == canonical_issue_type
    persisted = json.loads(
        (
            root
            / "auto-label-runs"
            / "projects"
            / str(PROJECT_ID)
            / f"{RUN_ID}.json"
        ).read_text(encoding="utf-8")
    )
    assert persisted["issue_type"] == canonical_issue_type
    assert persisted["matches"][0]["issue_type"] == canonical_issue_type


def test_run_store_exposes_twelve_canonical_types_plus_other():
    assert ISSUE_TYPES == {
        "pothole",
        "road_surface_damage",
        "road_debris",
        "median_barrier_damage",
        "sign_damage",
        "sign_vegetation_occlusion",
        "roadside_mowing_required",
        "guide_sign_identity",
        "tunnel_identity",
        "tunnel_name_occlusion",
        "lane_marking_faded",
        "colored_guidance_faded",
        "other",
    }
    assert "sign_identity" not in ISSUE_TYPES
    assert "color_guidance_line_faded" not in ISSUE_TYPES


@pytest.mark.parametrize(
    "overrides",
    [
        {"matches": [_match(project_id=str(OTHER_PROJECT_ID))]},
        {"matches": [_match(analysis_job_id=str(OTHER_JOB_ID))]},
        {"matches": [_match(), _match()]},
        {"matches": [_match(issue_type="road_debris")]},
        {"matches": [_match(review_status="approved")]},
        {"matches": [_match(decision_hash="bad")]},
        {"scope": "current_job", "job_id": None},
        {"scope": "project", "job_id": JOB_ID},
        {
            "mode_requested": "proposal_only",
            "mode_applied": "assisted_auto",
        },
    ],
)
def test_run_rejects_cross_scope_or_untrusted_matches(tmp_path, overrides):
    store = AutoLabelRunStore((tmp_path / "data").resolve())
    with pytest.raises(AutoLabelRunError):
        _record(store, **overrides)


def test_match_cannot_supply_training_or_review_boundary_fields(tmp_path):
    store = AutoLabelRunStore((tmp_path / "data").resolve())
    with pytest.raises(AutoLabelRunError):
        _record(
            store,
            matches=[
                _match(
                    review_only=False,
                    include_in_training=True,
                )
            ],
        )


def test_tampered_record_is_not_returned(tmp_path):
    root = (tmp_path / "data").resolve()
    store = AutoLabelRunStore(root)
    _record(store)
    path = (
        root
        / "auto-label-runs"
        / "projects"
        / str(PROJECT_ID)
        / f"{RUN_ID}.json"
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    value["matches"][0]["include_in_training"] = True
    path.write_text(
        json.dumps(value, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(AutoLabelRunError):
        store.get(PROJECT_ID, RUN_ID)
    assert store.list(PROJECT_ID) == []


def test_duplicate_json_keys_and_path_links_fail_closed(tmp_path):
    root = (tmp_path / "data").resolve()
    store = AutoLabelRunStore(root)
    _record(store)
    path = (
        root
        / "auto-label-runs"
        / "projects"
        / str(PROJECT_ID)
        / f"{RUN_ID}.json"
    )
    original = path.read_text(encoding="utf-8")
    path.write_text(
        original.replace(
            '"schema_version":',
            '"schema_version":"duplicate","schema_version":',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(AutoLabelRunError, match="duplicate"):
        store.get(PROJECT_ID, RUN_ID)

    unsafe_root = (tmp_path / "linked-data").resolve()
    unsafe_root.mkdir()
    linked = tmp_path / "linked-root"
    try:
        linked.symlink_to(unsafe_root, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are not available")
    with pytest.raises(AutoLabelRunError):
        AutoLabelRunStore(Path(linked).absolute())
