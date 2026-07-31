import hashlib
from copy import deepcopy
from uuid import UUID, uuid4, uuid5

import pytest

from app.dataset_versions import LabelProjectionError
from app.project_catalog import ProjectCatalog
from test_project_catalog_v2 import _project, _write_completed_analysis


OVERLAY_SCHEMA = "trafficvisson.candidate-evidence-overlay.v1"


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _catalog_with_completed_job(tmp_path, *, candidate_count: int):
    project_id = uuid4()
    fixture = _write_completed_analysis(
        tmp_path,
        project_id=project_id,
        source_seed=f"overlay-question-{candidate_count}",
        candidate_count=candidate_count,
    )
    catalog = ProjectCatalog(
        tmp_path / "catalog.sqlite3",
        _project(project_id=project_id),
    )
    return catalog, project_id, fixture


def _overlay_binding(*, pipeline_job_id=None):
    return {
        "schema_version": OVERLAY_SCHEMA,
        "overlay_id": f"ceo_{_sha('overlay')[:32]}",
        "pipeline_job_id": str(pipeline_job_id or uuid4()),
        "manifest_sha256": _sha("manifest"),
        "discovery_revision": _sha("discovery"),
        "dense_index_sha256": _sha("dense-index"),
        "provider_provenance_sha256": _sha("provider-provenance"),
        "model_provenance_sha256": _sha("model-provenance"),
        "candidate_set_metadata_sha256": _sha(
            "candidate-set-metadata"
        ),
        "review_only": True,
    }


def _review_event_binding(candidate_id, *, index, set_seed="set"):
    return {
        "schema_version": "trafficvisson.review-event-binding.v1",
        "review_event_set_id": f"res_{_sha(set_seed)[:32]}",
        "review_event_set_revision": _sha(
            f"{set_seed}:revision"
        ),
        "review_event_set_manifest_sha256": _sha(
            f"{set_seed}:manifest"
        ),
        "review_event_id": f"rve_{_sha(f'event:{index}')[:32]}",
        "review_event_revision": _sha(
            f"event:{index}:revision"
        ),
        "representative_candidate_id": candidate_id,
        "review_only": True,
    }


def _overlay_questions(job_id, *, count: int, binding=None):
    evidence_overlay = binding or _overlay_binding()
    questions = []
    for index in range(count):
        candidate_id = _sha(f"overlay-candidate-{index}")[:20]
        candidate = {
            "candidate_id": candidate_id,
            "candidate_kind": "dense_candidate_evidence_overlay",
            "issue_type": "pothole",
            "score": 0.75,
            "evidence_overlay": deepcopy(evidence_overlay),
        }
        questions.append(
            {
                "question_id": str(
                    uuid5(UUID(str(job_id)), candidate_id)
                ),
                "candidate_id": candidate_id,
                "ai_original_candidate": candidate,
            }
        )
    return questions


def test_composite_review_set_allows_two_verified_overlay_bindings(
    tmp_path,
):
    catalog, project_id, fixture = _catalog_with_completed_job(
        tmp_path, candidate_count=1
    )
    pipeline_job_id = uuid4()
    questions = _overlay_questions(
        fixture["job_id"],
        count=2,
        binding=_overlay_binding(
            pipeline_job_id=pipeline_job_id
        ),
    )
    questions[1]["ai_original_candidate"]["candidate_kind"] = (
        "dino_temporal_review_evidence_overlay"
    )
    questions[1]["ai_original_candidate"]["evidence_overlay"] = (
        _overlay_binding(pipeline_job_id=pipeline_job_id)
    )
    questions[1]["ai_original_candidate"]["evidence_overlay"][
        "overlay_id"
    ] = f"ceo_{_sha('dino-overlay')[:32]}"
    questions[1]["ai_original_candidate"]["evidence_overlay"][
        "manifest_sha256"
    ] = _sha("dino-manifest")
    for index, question in enumerate(questions):
        candidate_id = question["candidate_id"]
        question["ai_original_candidate"][
            "review_event_binding"
        ] = _review_event_binding(candidate_id, index=index)

    first = catalog.register_label_questions(
        project_id,
        fixture["job_id"],
        fixture["source_sha256"],
        questions,
    )
    replay = catalog.register_label_questions(
        project_id,
        fixture["job_id"],
        fixture["source_sha256"],
        questions,
    )

    assert first["question_count"] == 2
    assert first["created_count"] == 2
    assert first["overlay_binding_count"] == 2
    assert replay["created_count"] == 0
    assert replay["evidence_binding_sha256"] == first[
        "evidence_binding_sha256"
    ]
    assert catalog.list_current_annotations(project_id) == []
    assert catalog.list_incidents(
        project_id,
        analysis_job_id=fixture["job_id"],
    ) == []


def test_multiple_overlays_require_one_exact_review_event_set(
    tmp_path,
):
    catalog, project_id, fixture = _catalog_with_completed_job(
        tmp_path, candidate_count=1
    )
    pipeline_job_id = uuid4()
    questions = _overlay_questions(
        fixture["job_id"],
        count=2,
        binding=_overlay_binding(
            pipeline_job_id=pipeline_job_id
        ),
    )
    questions[1]["ai_original_candidate"]["evidence_overlay"] = (
        _overlay_binding(pipeline_job_id=pipeline_job_id)
    )
    for index, question in enumerate(questions):
        question["ai_original_candidate"][
            "review_event_binding"
        ] = _review_event_binding(
            question["candidate_id"],
            index=index,
            set_seed=f"set-{index}",
        )

    with pytest.raises(
        LabelProjectionError,
        match="review-event bindings",
    ):
        catalog.register_label_questions(
            project_id,
            fixture["job_id"],
            fixture["source_sha256"],
            questions,
        )

    assert catalog.list_label_questions(project_id) == []


def test_single_verified_overlay_may_exceed_immutable_base_candidate_count(
    tmp_path,
):
    catalog, project_id, fixture = _catalog_with_completed_job(
        tmp_path, candidate_count=1
    )
    questions = _overlay_questions(fixture["job_id"], count=3)

    first = catalog.register_label_questions(
        project_id,
        fixture["job_id"],
        fixture["source_sha256"],
        questions,
    )
    replay = catalog.register_label_questions(
        project_id,
        fixture["job_id"],
        fixture["source_sha256"],
        questions,
    )

    assert first["question_count"] == 3
    assert first["created_count"] == 3
    assert first["overlay_binding_count"] == 1
    assert len(first["evidence_binding_sha256"]) == 64
    assert replay["created_count"] == 0
    assert replay["question_set_sha256"] == first["question_set_sha256"]
    assert replay["evidence_binding_sha256"] == first[
        "evidence_binding_sha256"
    ]
    assert len(
        catalog.list_label_questions(
            project_id,
            analysis_job_id=fixture["job_id"],
        )
    ) == 3
    assert catalog.list_label_answers(project_id) == []
    assert catalog.list_current_annotations(project_id) == []
    assert catalog.list_incidents(
        project_id,
        analysis_job_id=fixture["job_id"],
    ) == []


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("overlay_id", f"ceo_{_sha('another-overlay')[:32]}"),
        ("pipeline_job_id", str(uuid4())),
        ("manifest_sha256", _sha("changed-manifest")),
        ("discovery_revision", _sha("changed-discovery")),
        ("dense_index_sha256", _sha("changed-dense-index")),
        (
            "provider_provenance_sha256",
            _sha("changed-provider-provenance"),
        ),
        (
            "model_provenance_sha256",
            _sha("changed-model-provenance"),
        ),
        (
            "candidate_set_metadata_sha256",
            _sha("changed-candidate-set"),
        ),
    ],
)
def test_exceeding_overlay_queue_rejects_mixed_or_tampered_binding(
    tmp_path,
    field,
    replacement,
):
    catalog, project_id, fixture = _catalog_with_completed_job(
        tmp_path, candidate_count=1
    )
    questions = _overlay_questions(fixture["job_id"], count=3)
    questions[1]["ai_original_candidate"]["evidence_overlay"][
        field
    ] = replacement

    with pytest.raises(
        LabelProjectionError,
        match="candidate evidence binding",
    ):
        catalog.register_label_questions(
            project_id,
            fixture["job_id"],
            fixture["source_sha256"],
            questions,
        )

    assert catalog.list_label_questions(project_id) == []


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("schema_version", "trafficvisson.candidate-evidence-overlay.v0"),
        ("review_only", False),
    ],
)
def test_overlay_queue_rejects_invalid_binding_contract(
    tmp_path,
    field,
    replacement,
):
    catalog, project_id, fixture = _catalog_with_completed_job(
        tmp_path, candidate_count=1
    )
    questions = _overlay_questions(fixture["job_id"], count=2)
    questions[0]["ai_original_candidate"]["evidence_overlay"][
        field
    ] = replacement

    with pytest.raises(
        LabelProjectionError,
        match="candidate evidence binding",
    ):
        catalog.register_label_questions(
            project_id,
            fixture["job_id"],
            fixture["source_sha256"],
            questions,
        )

    assert catalog.list_label_questions(project_id) == []


def test_base_and_overlay_candidates_cannot_share_one_question_set(
    tmp_path,
):
    catalog, project_id, fixture = _catalog_with_completed_job(
        tmp_path, candidate_count=3
    )
    questions = _overlay_questions(fixture["job_id"], count=2)
    base_candidate = fixture["payload"]["result"]["candidates"][0]
    questions.append(
        {
            "question_id": str(
                uuid5(
                    UUID(str(fixture["job_id"])),
                    base_candidate["candidate_id"],
                )
            ),
            "candidate_id": base_candidate["candidate_id"],
            "ai_original_candidate": base_candidate,
        }
    )

    with pytest.raises(
        LabelProjectionError,
        match="candidate evidence binding",
    ):
        catalog.register_label_questions(
            project_id,
            fixture["job_id"],
            fixture["source_sha256"],
            questions,
        )

    assert catalog.list_label_questions(project_id) == []
