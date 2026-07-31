from __future__ import annotations

import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import pytest

from app.candidate_labels import (
    ANSWER_VALUES,
    ISSUE_TYPE_ALIASES,
    ISSUE_TYPES,
    CandidateLabelConflict,
    CandidateLabelCorrupt,
    CandidateLabelError,
    CandidateLabelStore,
    CandidateResolutionError,
    CompletedCandidateMetadata,
)
from app.training_geometry import SCHEMA_VERSION as GEOMETRY_SCHEMA
from app.training_geometry import seal_training_geometry
from structured_assist_fixtures import assist_provenance


CANDIDATE_ID = "0123456789abcdefabcd"


def candidate_metadata(candidate_id: str = CANDIDATE_ID):
    return {
        "candidate_id": candidate_id,
        "start_seconds": 12.5,
        "end_seconds": 16.0,
        "representative_timestamp_seconds": 14.0,
        "scene_score": 0.83,
        "sharpness_score": 112.4,
        "triggers": ["scene_change", "model_uncertainty"],
        "representative_image": {
            "artifact_ref": f"analysis:job:{candidate_id}.jpg",
            "sha256": "a" * 64,
            "size_bytes": 12_345,
        },
    }


class Resolver:
    def __init__(self):
        self.calls = []
        self.overrides = {}

    def __call__(self, project_id, analysis_job_id, candidate_id):
        self.calls.append((project_id, analysis_job_id, candidate_id))
        override = self.overrides.get((project_id, analysis_job_id, candidate_id))
        if override is not None:
            return override
        return CompletedCandidateMetadata(
            project_id=project_id,
            analysis_job_id=analysis_job_id,
            candidate_id=candidate_id,
            metadata=candidate_metadata(candidate_id),
        )


@pytest.fixture()
def ids():
    return uuid4(), uuid4()


@pytest.fixture()
def resolver():
    return Resolver()


@pytest.fixture()
def store(tmp_path, resolver):
    return CandidateLabelStore(tmp_path, resolver)


def test_question_id_is_exact_uuid5_of_job_and_candidate(store, ids):
    _, job_id = ids

    actual = store.question_id(job_id, CANDIDATE_ID)

    assert actual == uuid5(job_id, CANDIDATE_ID)
    assert actual == store.question_id(str(job_id), CANDIDATE_ID)


@pytest.mark.parametrize(
    "candidate_id",
    [
        "",
        "A" * 20,
        "a" * 19,
        "a" * 21,
        "../0123456789abcdefa",
        "g" * 20,
    ],
)
def test_question_id_rejects_noncanonical_candidate_ids(store, ids, candidate_id):
    with pytest.raises(CandidateLabelError, match="candidate_id"):
        store.question_id(ids[1], candidate_id)


def test_pre_resolved_56_question_batch_does_not_call_item_resolver(
    store,
    resolver,
    ids,
):
    project_id, job_id = ids
    candidate_ids = [
        f"{index + 1:020x}" for index in range(56)
    ]
    resolved = [
        CompletedCandidateMetadata(
            project_id=project_id,
            analysis_job_id=job_id,
            candidate_id=candidate_id,
            metadata=candidate_metadata(candidate_id),
        )
        for candidate_id in candidate_ids
    ]

    questions = store.resolve_questions_from_candidates(
        project_id,
        job_id,
        candidate_ids,
        resolved,
    )

    assert [row["candidate_id"] for row in questions] == candidate_ids
    assert resolver.calls == []


def test_records_answers_append_only_and_reloads_latest(tmp_path, resolver, ids):
    project_id, job_id = ids
    first_store = CandidateLabelStore(tmp_path, resolver)
    first = first_store.record_answer(
        project_id,
        job_id,
        CANDIDATE_ID,
        answer="yes",
        selected_issue_type="sign_vegetation",
        severity="major",
        maintenance_action="표지판 주변 삭초",
        ocr_text="천안 방향",
        answered_by="structured_llm_assisted",
        assist_provenance=assist_provenance(
            project_id,
            job_id,
            CANDIDATE_ID,
        ),
    )
    second = first_store.record_answer(
        project_id,
        job_id,
        CANDIDATE_ID,
        answer="edit",
        selected_issue_type="mowing",
        severity="moderate",
        maintenance_action="갓길 예초",
        normalized_box={"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.5},
        answered_by="local_user",
    )

    reloaded = CandidateLabelStore(tmp_path, resolver)
    audit = reloaded.audit_records(project_id)
    latest = reloaded.latest_answers(project_id)

    assert len(audit) == 2
    assert [item["answer_revision"] for item in audit] == [1, 2]
    assert first["previous_record_hash"] is None
    assert second["previous_record_hash"] == first["record_hash"]
    assert latest == [second]
    assert latest[0]["partition"] == "approved"
    assert (
        latest[0]["ai_original_candidate"]
        == first["ai_original_candidate"]
        == candidate_metadata()
    )
    assert latest[0]["final_answer"]["normalized_box"] == {
        "x": 0.1,
        "y": 0.2,
        "width": 0.4,
        "height": 0.5,
    }
    assert first["final_answer"]["answered_by"] == "structured_llm_assisted"
    assert second["final_answer"]["answered_by"] == "local_user"


def test_approved_geometry_is_hash_chained_and_restored(
    tmp_path,
    resolver,
    ids,
):
    project_id, job_id = ids
    metadata = candidate_metadata()
    metadata.update(
        {
            "normalized_box": {
                "x": 0.1,
                "y": 0.2,
                "width": 0.4,
                "height": 0.5,
            },
            "proposal_provenance_sha256": "b" * 64,
        }
    )
    resolver.overrides[(project_id, job_id, CANDIDATE_ID)] = (
        CompletedCandidateMetadata(
            project_id=project_id,
            analysis_job_id=job_id,
            candidate_id=CANDIDATE_ID,
            metadata=metadata,
        )
    )
    geometry = seal_training_geometry(
        {
            "schema_version": GEOMETRY_SCHEMA,
            "source_kind": "accepted_ai_proposal",
            "approval_authority": "local_user",
            "image_sha256": "a" * 64,
            "image_artifact_ref": (
                f"analysis:job:{CANDIDATE_ID}.jpg"
            ),
            "normalized_box": metadata["normalized_box"],
            "polygon": None,
            "mask_artifact_ref": None,
            "mask_sha256": None,
            "tracked_frame_refs": [],
            "proposal_sha256": "b" * 64,
            "sam2_output_sha256": None,
            "sam2_model_manifest_sha256": None,
        }
    )

    record = CandidateLabelStore(tmp_path, resolver).record_answer(
        project_id,
        job_id,
        CANDIDATE_ID,
        answer="yes",
        selected_issue_type="pothole",
        training_geometry=geometry,
    )
    reloaded = CandidateLabelStore(tmp_path, resolver).latest_answers(
        project_id
    )[0]

    assert record["schema_version"] == "1.2"
    assert record["final_answer"]["normalized_box"] == (
        metadata["normalized_box"]
    )
    assert reloaded["training_geometry"] == geometry


def test_geometry_authority_cannot_differ_from_answer(store, ids):
    geometry = seal_training_geometry(
        {
            "schema_version": GEOMETRY_SCHEMA,
            "source_kind": "delegated_ai_proposal",
            "approval_authority": "structured_llm_assisted",
            "image_sha256": "a" * 64,
            "image_artifact_ref": "analysis:job:frame.jpg",
            "normalized_box": {
                "x": 0.1,
                "y": 0.2,
                "width": 0.4,
                "height": 0.5,
            },
            "polygon": None,
            "mask_artifact_ref": None,
            "mask_sha256": None,
            "tracked_frame_refs": [],
            "proposal_sha256": "b" * 64,
            "sam2_output_sha256": None,
            "sam2_model_manifest_sha256": None,
        }
    )

    with pytest.raises(CandidateLabelError, match="authority"):
        store.record_answer(
            ids[0],
            ids[1],
            CANDIDATE_ID,
            answer="yes",
            selected_issue_type="pothole",
            answered_by="local_user",
            training_geometry=geometry,
        )


def test_structured_llm_cannot_supersede_a_human_revision(store, ids):
    project_id, job_id = ids
    store.record_answer(
        project_id,
        job_id,
        CANDIDATE_ID,
        answer="yes",
        selected_issue_type="pothole",
        answered_by="local_user",
    )

    with pytest.raises(
        CandidateLabelConflict,
        match="cannot supersede human truth",
    ):
        store.record_answer(
            project_id,
            job_id,
            CANDIDATE_ID,
            answer="no",
            answered_by="structured_llm_assisted",
            assist_provenance=assist_provenance(
                project_id,
                job_id,
                CANDIDATE_ID,
            ),
        )

    assert len(store.audit_records(project_id)) == 1


def test_structured_answer_without_execution_provenance_is_rejected(
    store,
    ids,
):
    with pytest.raises(CandidateLabelError, match="assist provenance"):
        store.record_answer(
            ids[0],
            ids[1],
            CANDIDATE_ID,
            answer="no",
            answered_by="structured_llm_assisted",
        )


@pytest.mark.parametrize(
    ("answer", "partition", "training"),
    [
        ("yes", "approved", True),
        ("edit", "approved", True),
        ("no", "negative", True),
        ("unsure", "held", False),
        ("skipped", "excluded", False),
    ],
)
def test_answer_partitions_are_explicit(store, ids, answer, partition, training):
    project_id, job_id = ids
    kwargs = {}
    if answer in {"yes", "edit"}:
        kwargs["selected_issue_type"] = "pothole"
    if answer == "edit":
        kwargs["normalized_box"] = {
            "x": 0.25,
            "y": 0.25,
            "width": 0.5,
            "height": 0.5,
        }

    record = store.record_answer(
        project_id,
        job_id,
        CANDIDATE_ID,
        answer=answer,
        **kwargs,
    )

    assert answer in ANSWER_VALUES
    assert record["partition"] == partition
    assert record["final_answer"]["include_in_training"] is training
    assert store.latest_answers(project_id, partition=partition) == [record]


@pytest.mark.parametrize("issue_type", sorted(ISSUE_TYPES))
def test_all_road_issue_types_are_allowed(tmp_path, issue_type):
    project_id, job_id = uuid4(), uuid4()
    resolver = Resolver()
    store = CandidateLabelStore(tmp_path, resolver)

    record = store.record_answer(
        project_id,
        job_id,
        CANDIDATE_ID,
        answer="yes",
        selected_issue_type=issue_type,
    )

    assert record["final_answer"]["selected_issue_type"] == issue_type


@pytest.mark.parametrize(
    ("legacy_issue_type", "canonical_issue_type"),
    sorted(ISSUE_TYPE_ALIASES.items()),
)
def test_legacy_issue_type_inputs_are_persisted_as_canonical_codes(
    tmp_path,
    legacy_issue_type,
    canonical_issue_type,
):
    project_id, job_id = uuid4(), uuid4()
    store = CandidateLabelStore(tmp_path, Resolver())

    record = store.record_answer(
        project_id,
        job_id,
        CANDIDATE_ID,
        answer="yes",
        selected_issue_type=legacy_issue_type,
    )

    assert record["final_answer"]["selected_issue_type"] == canonical_issue_type


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"answer": "maybe"}, "answer"),
        ({"answer": "yes"}, "selected_issue_type"),
        (
            {"answer": "yes", "selected_issue_type": "road_crack_unknown"},
            "selected_issue_type",
        ),
        (
            {
                "answer": "yes",
                "selected_issue_type": "pothole",
                "severity": "catastrophic",
            },
            "severity",
        ),
        (
            {
                "answer": "edit",
                "selected_issue_type": "pothole",
            },
            "normalized_box",
        ),
        (
            {
                "answer": "no",
                "selected_issue_type": "pothole",
            },
            "cannot carry",
        ),
        (
            {
                "answer": "yes",
                "selected_issue_type": "pothole",
                "maintenance_action": " ",
            },
            "blank",
        ),
        (
            {
                "answer": "yes",
                "selected_issue_type": "pothole",
                "ocr_text": "a" * 501,
            },
            "500",
        ),
        (
            {
                "answer": "yes",
                "selected_issue_type": "pothole",
                "answered_by": "../operator",
            },
            "answered_by",
        ),
    ],
)
def test_answer_field_validation_happens_before_persistence(
    store, ids, kwargs, message
):
    with pytest.raises(CandidateLabelError, match=message):
        store.record_answer(ids[0], ids[1], CANDIDATE_ID, **kwargs)

    assert store.audit_records(ids[0]) == []


@pytest.mark.parametrize(
    "box",
    [
        {"x": -0.01, "y": 0, "width": 0.2, "height": 0.2},
        {"x": 0, "y": 1.01, "width": 0.2, "height": 0.2},
        {"x": 0, "y": 0, "width": 0, "height": 0.2},
        {"x": 0, "y": 0, "width": 0.2, "height": -0.2},
        {"x": 0.9, "y": 0, "width": 0.2, "height": 0.2},
        {"x": 0, "y": 0.9, "width": 0.2, "height": 0.2},
        {"x": True, "y": 0, "width": 0.2, "height": 0.2},
        {"x": math.nan, "y": 0, "width": 0.2, "height": 0.2},
        {"x": 0, "y": 0, "width": 0.2, "height": 0.2, "z": 0},
    ],
)
def test_normalized_box_must_be_finite_and_inside_image(store, ids, box):
    with pytest.raises(CandidateLabelError, match="normalized_box"):
        store.record_answer(
            ids[0],
            ids[1],
            CANDIDATE_ID,
            answer="edit",
            selected_issue_type="surface_damage",
            normalized_box=box,
        )


def test_mapping_resolver_requires_completed_status_and_matching_identity(
    tmp_path, ids
):
    project_id, job_id = ids

    def mapping_resolver(request_project, request_job, candidate_id):
        return {
            "status": "completed",
            "project_id": str(request_project),
            "analysis_job_id": str(request_job),
            "candidate_id": candidate_id,
            "candidate": candidate_metadata(candidate_id),
        }

    store = CandidateLabelStore(tmp_path, mapping_resolver)
    resolved = store.resolve_question(project_id, job_id, CANDIDATE_ID)

    assert resolved["question_id"] == str(uuid5(job_id, CANDIDATE_ID))
    assert resolved["ai_original_candidate"] == candidate_metadata()


@pytest.mark.parametrize(
    "override",
    [
        {
            "status": "running",
            "project_id": None,
            "analysis_job_id": None,
            "candidate_id": CANDIDATE_ID,
            "candidate": candidate_metadata(),
        },
        {
            "status": "completed",
            "project_id": None,
            "analysis_job_id": None,
            "candidate_id": "f" * 20,
            "candidate": candidate_metadata(),
        },
        {
            "status": "completed",
            "project_id": None,
            "analysis_job_id": None,
            "candidate_id": CANDIDATE_ID,
            "candidate": candidate_metadata("f" * 20),
        },
    ],
)
def test_resolver_must_prove_completed_matching_candidate(tmp_path, ids, override):
    project_id, job_id = ids
    override["project_id"] = str(project_id)
    override["analysis_job_id"] = str(job_id)

    store = CandidateLabelStore(tmp_path, lambda *_: override)

    with pytest.raises(CandidateResolutionError):
        store.record_answer(
            project_id,
            job_id,
            CANDIDATE_ID,
            answer="yes",
            selected_issue_type="debris",
        )


def test_resolver_failure_is_sanitized_and_nothing_is_written(tmp_path, ids):
    def broken_resolver(*_):
        raise RuntimeError(r"C:\secret\source-video.mp4")

    store = CandidateLabelStore(tmp_path, broken_resolver)

    with pytest.raises(CandidateResolutionError) as captured:
        store.record_answer(
            ids[0],
            ids[1],
            CANDIDATE_ID,
            answer="yes",
            selected_issue_type="debris",
        )

    assert "secret" not in str(captured.value)
    assert store.audit_records(ids[0]) == []


@pytest.mark.parametrize(
    "unsafe_metadata",
    [
        {"candidate_id": CANDIDATE_ID, "value": math.inf},
        {"candidate_id": CANDIDATE_ID, "value": Path("private.mp4")},
        {"candidate_id": CANDIDATE_ID, "value": object()},
        {"candidate_id": CANDIDATE_ID, "value": {"x": {"y": {"z": {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": 1}}}}}}}}}}}},
    ],
)
def test_candidate_metadata_must_be_bounded_json(tmp_path, ids, unsafe_metadata):
    project_id, job_id = ids
    resolver = lambda *_: CompletedCandidateMetadata(
        project_id, job_id, CANDIDATE_ID, unsafe_metadata
    )
    store = CandidateLabelStore(tmp_path, resolver)

    with pytest.raises(CandidateResolutionError):
        store.resolve_question(project_id, job_id, CANDIDATE_ID)


def test_mutating_resolver_data_cannot_change_persisted_original(tmp_path, ids):
    project_id, job_id = ids
    mutable = candidate_metadata()

    def resolver(*_):
        return CompletedCandidateMetadata(
            project_id, job_id, CANDIDATE_ID, mutable
        )

    store = CandidateLabelStore(tmp_path, resolver)
    record = store.record_answer(
        project_id,
        job_id,
        CANDIDATE_ID,
        answer="yes",
        selected_issue_type="sign_tunnel_identity",
        ocr_text="정안터널",
    )
    mutable["scene_score"] = 0.01
    mutable["triggers"].append("mutated")

    persisted = store.latest_answers(project_id)[0]
    assert persisted["ai_original_candidate"] == record["ai_original_candidate"]
    assert persisted["ai_original_candidate"]["scene_score"] == 0.83
    assert "mutated" not in persisted["ai_original_candidate"]["triggers"]


def test_candidate_metadata_cannot_change_between_answer_revisions(
    tmp_path, ids
):
    project_id, job_id = ids
    mutable = candidate_metadata()

    def resolver(*_):
        return CompletedCandidateMetadata(
            project_id, job_id, CANDIDATE_ID, mutable
        )

    store = CandidateLabelStore(tmp_path, resolver)
    store.record_answer(
        project_id,
        job_id,
        CANDIDATE_ID,
        answer="yes",
        selected_issue_type="pothole",
    )
    mutable["scene_score"] = 0.01

    with pytest.raises(CandidateLabelConflict, match="metadata changed"):
        store.record_answer(
            project_id,
            job_id,
            CANDIDATE_ID,
            answer="yes",
            selected_issue_type="pothole",
        )

    assert len(store.audit_records(project_id)) == 1


def test_projects_are_isolated_and_job_identity_has_one_owner(tmp_path):
    project_a, project_b, job_id = uuid4(), uuid4(), uuid4()

    def resolver(project_id, analysis_job_id, candidate_id):
        return CompletedCandidateMetadata(
            project_id,
            analysis_job_id,
            candidate_id,
            candidate_metadata(candidate_id),
        )

    store = CandidateLabelStore(tmp_path, resolver)
    record_a = store.record_answer(
        project_a,
        job_id,
        CANDIDATE_ID,
        answer="no",
    )

    assert store.latest_answers(project_a) == [record_a]
    assert store.latest_answers(project_b) == []
    with pytest.raises(CandidateLabelConflict, match="another project"):
        store.record_answer(
            project_b,
            job_id,
            CANDIDATE_ID,
            answer="yes",
            selected_issue_type="other",
        )
    assert store.latest_answers(project_b) == []


def test_latest_and_csv_filters_never_mix_jobs(tmp_path):
    project_id, job_a, job_b = uuid4(), uuid4(), uuid4()

    def resolver(project, job, candidate):
        return CompletedCandidateMetadata(
            project, job, candidate, candidate_metadata(candidate)
        )

    store = CandidateLabelStore(tmp_path, resolver)
    approved = store.record_answer(
        project_id,
        job_a,
        CANDIDATE_ID,
        answer="yes",
        selected_issue_type="mowing",
        maintenance_action="=HYPERLINK(\"bad\")",
        ocr_text="+1",
    )
    negative = store.record_answer(
        project_id,
        job_b,
        "f" * 20,
        answer="no",
    )

    assert store.latest_answers(project_id, analysis_job_id=job_a) == [approved]
    assert store.latest_answers(project_id, partition="negative") == [negative]
    rows = store.csv_rows(project_id, analysis_job_id=job_a)
    assert len(rows) == 1
    assert rows[0]["candidate_id"] == CANDIDATE_ID
    assert rows[0]["maintenance_action"].startswith("'=")
    assert rows[0]["ocr_text"] == "'+1"
    assert json.loads(rows[0]["ai_original_candidate_json"]) == candidate_metadata()


def test_full_audit_csv_contains_all_revisions(store, ids):
    project_id, job_id = ids
    store.record_answer(project_id, job_id, CANDIDATE_ID, answer="no")
    store.record_answer(
        project_id,
        job_id,
        CANDIDATE_ID,
        answer="yes",
        selected_issue_type="debris",
    )

    assert len(store.csv_rows(project_id)) == 1
    rows = store.csv_rows(project_id, latest_only=False)
    assert [row["answer_revision"] for row in rows] == [1, 2]
    assert [row["partition"] for row in rows] == ["negative", "approved"]


def test_hash_chain_detects_tampering(tmp_path, resolver, ids):
    project_id, job_id = ids
    store = CandidateLabelStore(tmp_path, resolver)
    store.record_answer(project_id, job_id, CANDIDATE_ID, answer="no")
    log = (
        tmp_path
        / "candidate-labels"
        / "projects"
        / str(project_id)
        / "answers.jsonl"
    )
    record = json.loads(log.read_text(encoding="utf-8"))
    record["partition"] = "approved"
    log.write_text(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(CandidateLabelCorrupt):
        store.audit_records(project_id)


def test_truncated_jsonl_is_never_silently_ignored(tmp_path, resolver, ids):
    project_id, job_id = ids
    store = CandidateLabelStore(tmp_path, resolver)
    store.record_answer(project_id, job_id, CANDIDATE_ID, answer="no")
    log = (
        tmp_path
        / "candidate-labels"
        / "projects"
        / str(project_id)
        / "answers.jsonl"
    )
    with log.open("ab") as handle:
        handle.write(b'{"partial":')
        handle.flush()
        os.fsync(handle.fileno())

    with pytest.raises(CandidateLabelCorrupt, match="invalid"):
        store.latest_answers(project_id)


def test_concurrent_answer_revisions_remain_complete_and_ordered(
    tmp_path, resolver, ids
):
    project_id, job_id = ids
    store = CandidateLabelStore(tmp_path, resolver)

    def write(index):
        return store.record_answer(
            project_id,
            job_id,
            CANDIDATE_ID,
            answer="yes",
            selected_issue_type="surface_damage",
            answered_by="local_user",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(write, range(24)))

    audit = store.audit_records(project_id)
    assert len(records) == 24
    assert len(audit) == 24
    assert [record["answer_revision"] for record in audit] == list(range(1, 25))
    assert len({record["record_id"] for record in audit}) == 24
    assert len({record["record_hash"] for record in audit}) == 24


def test_data_root_must_be_absolute(tmp_path, resolver, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(CandidateLabelError, match="absolute"):
        CandidateLabelStore(Path("relative"), resolver)


def test_symlinked_data_root_is_rejected(tmp_path, resolver):
    real_root = tmp_path / "real"
    real_root.mkdir()
    linked_root = tmp_path / "linked"
    try:
        linked_root.symlink_to(real_root, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this Windows account")

    with pytest.raises(CandidateLabelError, match="real directory"):
        CandidateLabelStore(linked_root, resolver)


def test_project_path_replacement_cannot_escape_store(tmp_path, resolver, ids):
    project_id, _ = ids
    store = CandidateLabelStore(tmp_path, resolver)
    assert store.latest_answers(project_id) == []
    project_root = (
        tmp_path
        / "candidate-labels"
        / "projects"
        / str(project_id)
    )
    (project_root / "answers.lock").unlink()
    project_root.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        project_root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this Windows account")

    with pytest.raises(CandidateLabelError):
        store.latest_answers(project_id)
    assert list(outside.iterdir()) == []
