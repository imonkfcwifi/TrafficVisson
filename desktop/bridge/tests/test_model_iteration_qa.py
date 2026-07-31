from __future__ import annotations

import copy
import hashlib
import json
from uuid import UUID, uuid4, uuid5

import pytest

from app.model_iteration_qa import (
    ModelIterationQaError,
    ModelIterationQaStore,
    build_model_iteration,
    build_reviewed_survey_outcomes,
    build_survey_model_qa,
    canonical_model_iteration,
    canonical_survey_model_qa,
)
from app.training_provenance import (
    ANNOTATION_PROVENANCE_SCHEMA,
    build_dataset_provenance_graph,
    canonical_annotation_provenance_graph,
)


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _annotation_graph(
    *,
    project_id: UUID,
    pipeline_job_id: UUID,
    analysis_job_id: UUID,
    candidate_id: str,
    annotation_index: int,
) -> tuple[dict, dict]:
    question_id = uuid5(analysis_job_id, candidate_id)
    annotation_id = uuid5(
        project_id, f"iteration-annotation:{annotation_index}"
    )
    answer_record_id = uuid5(
        project_id, f"iteration-answer:{annotation_index}"
    )
    graph_base = {
        "schema_version": ANNOTATION_PROVENANCE_SCHEMA,
        "project_id": str(project_id),
        "pipeline_job_id": str(pipeline_job_id),
        "analysis_job_id": str(analysis_job_id),
        "overlay_id": f"ceo_{_sha(['overlay', annotation_index])[:32]}",
        "overlay_manifest_sha256": _sha(
            ["overlay-manifest", annotation_index]
        ),
        "candidate_set_metadata_sha256": _sha(
            ["candidate-set", annotation_index]
        ),
        "review_event_set_id": (
            f"res_{_sha(['review-set', annotation_index])[:32]}"
        ),
        "review_event_set_revision": _sha(
            ["review-set-revision", annotation_index]
        ),
        "review_event_set_manifest_sha256": _sha(
            ["review-set-manifest", annotation_index]
        ),
        "review_event_id": (
            f"rve_{_sha(['review-event', annotation_index])[:32]}"
        ),
        "review_event_revision": _sha(
            ["review-event-revision", annotation_index]
        ),
        "question_id": str(question_id),
        "candidate_id": candidate_id,
        "answer_record_id": str(answer_record_id),
        "answer_revision": 1,
        "source_sha256": _sha(["source", annotation_index]),
        "candidate_metadata_sha256": _sha(
            ["candidate-metadata", annotation_index]
        ),
        "image_sha256": _sha(["image", annotation_index]),
        "mask_sha256": _sha(["mask", annotation_index]),
        "proposal_sha256": _sha(["proposal", annotation_index]),
        "geometry_sha256": _sha(["geometry", annotation_index]),
        "answer_authority": {
            "answered_by": "local_user",
            "include_in_training": True,
            "user_confirmed": True,
        },
        "delegation_authority": None,
    }
    graph = canonical_annotation_provenance_graph(
        {
            **graph_base,
            "provenance_graph_sha256": _sha(graph_base),
        }
    )
    membership = {
        "annotation_id": str(annotation_id),
        "question_id": str(question_id),
        "analysis_job_id": str(analysis_job_id),
        "candidate_id": candidate_id,
        "answer_record_id": str(answer_record_id),
        "answer_revision": 1,
        "partition": "approved",
        "include_in_training": True,
        "split": "train",
        "source_record_hash": _sha(
            ["source-record", annotation_index]
        ),
        "annotation_hash": _sha(
            ["annotation", annotation_index]
        ),
    }
    return membership, graph


def _dataset(
    *,
    project_id: UUID,
    rows: list[tuple[dict, dict]],
    name: str,
) -> dict:
    memberships = [copy.deepcopy(row[0]) for row in rows]
    graph = build_dataset_provenance_graph(
        project_id=str(project_id),
        members=[
            {
                **membership,
                "provenance_graph": provenance,
            }
            for membership, provenance in rows
            if membership["split"] in {"train", "validation"}
        ],
    )
    dataset_hash = _sha(
        {
            "name": name,
            "memberships": memberships,
            "provenance_graph": graph,
        }
    )
    dataset_id = str(
        uuid5(project_id, f"model-iteration-dataset:{dataset_hash}")
    )
    return {
        "project_id": str(project_id),
        "dataset_version_id": dataset_id,
        "dataset_hash": dataset_hash,
        "split_strategy": "project_fixed_analysis_job_v1",
        "memberships": memberships,
        "manifest": {
            "dataset_id": dataset_id,
            "dataset_hash": dataset_hash,
            "provenance_graph": graph,
        },
    }


def _fixtures():
    project_id = uuid4()
    training_job_id = uuid4()
    validation_job_id = uuid4()
    survey_job_id = uuid4()
    training_pipeline_id = uuid4()
    validation_pipeline_id = uuid4()
    survey_pipeline_id = uuid4()

    train_row = _annotation_graph(
        project_id=project_id,
        pipeline_job_id=training_pipeline_id,
        analysis_job_id=training_job_id,
        candidate_id=_sha("training-candidate-0001")[:20],
        annotation_index=1,
    )
    validation_row = _annotation_graph(
        project_id=project_id,
        pipeline_job_id=validation_pipeline_id,
        analysis_job_id=validation_job_id,
        candidate_id=_sha("validation-candidate-0001")[:20],
        annotation_index=2,
    )
    validation_row[0]["split"] = "validation"
    parent_dataset = _dataset(
        project_id=project_id,
        rows=[train_row, validation_row],
        name="parent",
    )
    model = {
        "project_id": str(project_id),
        "state_revision": 1,
        "model_version_id": "project-detector-v1",
        "model_version": "project-detector@1.0.0",
        "model_format": "trafficvisson.local-object-detector.v1",
        "state": "promoted",
        "dataset_version_id": parent_dataset[
            "dataset_version_id"
        ],
        "dataset_hash": parent_dataset["dataset_hash"],
        "dataset_provenance_graph_sha256": parent_dataset[
            "manifest"
        ]["provenance_graph"]["provenance_graph_sha256"],
        "fixed_validation_sha256": _sha("fixed-validation-v1"),
        "primary_artifact_sha256": _sha("model-weights-v1"),
        "artifact_manifest_sha256": _sha("model-manifest-v1"),
        "training_analysis_job_ids": [str(training_job_id)],
        "fixed_validation_analysis_job_ids": [
            str(validation_job_id)
        ],
        "source_analysis_lineage_sha256": _sha(
            {
                "training_analysis_job_ids": [
                    str(training_job_id)
                ],
                "fixed_validation_analysis_job_ids": [
                    str(validation_job_id)
                ],
            }
        ),
        "fixed_validation_set_version": 1,
    }
    survey_completion_base = {
        "schema_version": (
            "trafficvisson.promoted-model-survey-qa-completion.v1"
        ),
        "project_id": str(project_id),
        "pipeline_job_id": str(survey_pipeline_id),
        "analysis_job_id": str(survey_job_id),
        "source_sha256": _sha("survey-source"),
        "dense_index_sha256": _sha("survey-dense-index"),
        "dense_candidate_discovery_revision": _sha(
            "survey-dense-discovery"
        ),
        "question_queue_revision": _sha("survey-question-queue"),
        "model_version_id": model["model_version_id"],
        "dataset_version_id": model["dataset_version_id"],
        "primary_artifact_sha256": model[
            "primary_artifact_sha256"
        ],
        "artifact_manifest_sha256": model[
            "artifact_manifest_sha256"
        ],
        "dataset_hash": model["dataset_hash"],
        "fixed_validation_sha256": model[
            "fixed_validation_sha256"
        ],
        "source_analysis_lineage_sha256": _sha(
            "survey-analysis-lineage"
        ),
        "dataset_provenance_graph_sha256": model[
            "dataset_provenance_graph_sha256"
        ],
        "promoted_model_inference_sha256": _sha(
            "survey-promoted-inference"
        ),
        "sampling_fps": 2,
        "scanned_frame_count": 1000,
        "final_qa_choice": "miss_candidates_reviewed",
        "review_completion_id": "review-promoted-survey-v1",
        "server_answer_ledger_revision": _sha(
            "survey-answer-ledger"
        ),
        "review_event_set_collection_revision": _sha(
            "survey-review-sets"
        ),
        "question_catalog_collection_revision": _sha(
            "survey-question-catalog"
        ),
        "user_discovered_registrations_sha256": _sha(
            "survey-user-discovered"
        ),
        "user_discovered_candidate_count": 1,
        "actor": "local_user",
        "recorded_at": "2026-07-30T12:00:00.000Z",
    }
    survey_completion = {
        **survey_completion_base,
        "survey_qa_revision": _sha(survey_completion_base),
    }
    outcome_specs = [
        (
            "promoted_model_candidate",
            "pothole",
            "yes",
            "pothole",
        ),
        (
            "promoted_model_candidate",
            "sign_damage",
            "no",
            None,
        ),
        (
            "promoted_model_candidate",
            "lane_marking_faded",
            "edit",
            "road_surface_damage",
        ),
        (
            "local_user_discovered_false_negative",
            None,
            "yes",
            "pothole",
        ),
        (
            "promoted_model_candidate",
            "tunnel_identity",
            "unsure",
            None,
        ),
    ]
    outcomes = []
    feedback_rows = []
    for index, (origin, proposed, answer, final) in enumerate(
        outcome_specs,
        start=10,
    ):
        candidate_id = _sha(f"survey-candidate-{index:04d}")[:20]
        membership, graph = _annotation_graph(
            project_id=project_id,
            pipeline_job_id=survey_pipeline_id,
            analysis_job_id=survey_job_id,
            candidate_id=candidate_id,
            annotation_index=index,
        )
        if answer == "no":
            membership["partition"] = "negative"
        resolved = answer != "unsure"
        outcomes.append(
            {
                "project_id": str(project_id),
                "analysis_job_id": str(survey_job_id),
                "review_event_id": graph["review_event_id"],
                "review_event_revision": graph[
                    "review_event_revision"
                ],
                "question_id": membership["question_id"],
                "candidate_id": candidate_id,
                "candidate_metadata_sha256": graph[
                    "candidate_metadata_sha256"
                ],
                "evidence_sha256": graph["image_sha256"],
                "origin": origin,
                "proposed_issue_type": proposed,
                "review_answer": answer,
                "final_issue_type": final,
                "annotation_id": (
                    membership["annotation_id"] if resolved else None
                ),
                "annotation_hash": (
                    membership["annotation_hash"] if resolved else None
                ),
                "training_authorized": resolved,
                "answered_by": "local_user",
            }
        )
        if resolved:
            feedback_rows.append((membership, graph))
    return {
        "project_id": project_id,
        "model": model,
        "survey_completion": survey_completion,
        "outcomes": outcomes,
        "parent_dataset": parent_dataset,
        "parent_rows": [train_row, validation_row],
        "feedback_rows": feedback_rows,
        "survey_job_id": survey_job_id,
        "training_job_id": training_job_id,
    }


def test_survey_snapshot_freezes_event_fp_fn_and_training_only_feedback(
    tmp_path,
):
    fixture = _fixtures()
    snapshot = build_survey_model_qa(
        project_id=fixture["project_id"],
        promoted_model_binding=fixture["model"],
        survey_qa_completion=fixture["survey_completion"],
        reviewed_outcomes=fixture["outcomes"],
        qa_scope_sha256=_sha("same-route-and-review-protocol"),
    )

    metrics = {
        row["issue_type"]: row
        for row in snapshot["per_type_metrics"]
    }
    assert metrics["pothole"]["true_positive_count"] == 1
    assert metrics["pothole"]["false_negative_count"] == 1
    assert metrics["pothole"]["precision"] == 1.0
    assert metrics["pothole"]["recall"] == 0.5
    assert metrics["sign_damage"]["false_positive_count"] == 1
    assert (
        metrics["lane_marking_faded"]["false_positive_count"]
        == 1
    )
    assert (
        metrics["road_surface_damage"]["false_negative_count"]
        == 1
    )
    assert metrics["tunnel_identity"]["held_count"] == 1
    assert snapshot["summary"] == {
        "model_candidate_count": 4,
        "user_discovered_candidate_count": 1,
        "other_detector_candidate_count": 0,
        "resolved_training_feedback_count": 4,
        "held_count": 1,
        "true_positive_count": 1,
        "false_positive_count": 2,
        "false_negative_count": 2,
    }
    assert len(snapshot["training_feedback_annotations"]) == 4
    assert snapshot["creates_incident"] is False
    assert snapshot["creates_report_truth"] is False
    assert snapshot["sets_final_severity"] is False
    assert snapshot["starts_training"] is False
    assert snapshot["promotes_model"] is False

    store = ModelIterationQaStore(tmp_path / "iteration.sqlite3")
    assert store.put_survey_model_qa(snapshot) == snapshot
    assert store.put_survey_model_qa(snapshot) == snapshot
    restarted = ModelIterationQaStore(
        tmp_path / "iteration.sqlite3"
    )
    assert restarted.list_survey_model_qa(
        fixture["project_id"]
    ) == [snapshot]


def test_answer_projection_separates_promoted_model_metrics_and_audit_only():
    fixture = _fixtures()
    analysis_job_id = fixture["survey_job_id"]
    pipeline_job_id = UUID(
        fixture["survey_completion"]["pipeline_job_id"]
    )
    specs = [
        (
            "promoted",
            "edit",
            "pothole",
            "pothole",
            True,
        ),
        (
            "other",
            "yes",
            "sign_damage",
            "sign_damage",
            True,
        ),
        (
            "user",
            "yes",
            "road_debris",
            "road_debris",
            True,
        ),
        (
            "promoted",
            "skipped",
            "tunnel_identity",
            None,
            False,
        ),
    ]
    records = []
    rows = []
    candidate_ids = []
    for index, (origin, answer, proposed, final, training) in enumerate(
        specs,
        start=30,
    ):
        candidate_id = _sha(f"projected:{index}")[:20]
        candidate_ids.append(candidate_id)
        membership, graph = _annotation_graph(
            project_id=fixture["project_id"],
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=analysis_job_id,
            candidate_id=candidate_id,
            annotation_index=index,
        )
        if not training:
            membership.update(
                {
                    "partition": "excluded",
                    "include_in_training": False,
                    "split": "excluded",
                }
            )
            graph_base = dict(graph)
            graph_base.pop("provenance_graph_sha256")
            graph_base["answer_authority"] = {
                "answered_by": "local_user",
                "include_in_training": False,
                "user_confirmed": True,
            }
            graph = canonical_annotation_provenance_graph(
                {
                    **graph_base,
                    "provenance_graph_sha256": _sha(graph_base),
                }
            )
        elif answer == "yes":
            membership["partition"] = "approved"
        metadata = {
            "candidate_id": candidate_id,
            "candidate_kind": (
                "user_discovered" if origin == "user" else "detected"
            ),
            "issue_type": proposed,
            "proposal_sources": (
                ["local_user_discovered_false_negative"]
                if origin == "user"
                else (
                    ["promoted_project_model_dense_2fps"]
                    if origin == "promoted"
                    else ["grounding_dino_review_sidecar"]
                )
            ),
        }
        if origin == "promoted":
            metadata["representative_proposal_provenance"] = {
                "model_version_id": fixture["model"][
                    "model_version_id"
                ],
                "dataset_version_id": fixture["model"][
                    "dataset_version_id"
                ],
                "dataset_hash": fixture["model"]["dataset_hash"],
                "fixed_validation_sha256": fixture["model"][
                    "fixed_validation_sha256"
                ],
                "primary_artifact_sha256": fixture["model"][
                    "primary_artifact_sha256"
                ],
                "artifact_manifest_sha256": fixture["model"][
                    "artifact_manifest_sha256"
                ],
                "source_analysis_lineage_sha256": fixture["model"][
                    "source_analysis_lineage_sha256"
                ],
                "dataset_provenance_graph_sha256": fixture["model"][
                    "dataset_provenance_graph_sha256"
                ],
                "survey_analysis_job_id": str(analysis_job_id),
                "independent_survey_verified": True,
            }
        records.append(
            {
                "project_id": str(fixture["project_id"]),
                "analysis_job_id": str(analysis_job_id),
                "candidate_id": candidate_id,
                "question_id": membership["question_id"],
                "record_id": membership["answer_record_id"],
                "record_hash": membership["source_record_hash"],
                "answer_revision": membership["answer_revision"],
                "final_answer": {
                    "answer": answer,
                    "selected_issue_type": final,
                    "include_in_training": training,
                    "answered_by": "local_user",
                },
                "ai_original_candidate": metadata,
                "provenance_graph": graph,
            }
        )
        rows.append((membership, graph))
    next_dataset = _dataset(
        project_id=fixture["project_id"],
        rows=fixture["parent_rows"] + rows,
        name="projected-next",
    )
    outcomes = build_reviewed_survey_outcomes(
        project_id=fixture["project_id"],
        analysis_job_id=analysis_job_id,
        question_candidate_ids=candidate_ids,
        latest_answer_records=records,
        next_dataset_version=next_dataset,
        promoted_model_binding=fixture["model"],
    )
    snapshot = build_survey_model_qa(
        project_id=fixture["project_id"],
        promoted_model_binding=fixture["model"],
        survey_qa_completion=fixture["survey_completion"],
        reviewed_outcomes=outcomes,
        qa_scope_sha256=_sha("projected-scope"),
    )
    assert snapshot["summary"] == {
        "model_candidate_count": 2,
        "user_discovered_candidate_count": 1,
        "other_detector_candidate_count": 1,
        "resolved_training_feedback_count": 3,
        "held_count": 1,
        "true_positive_count": 1,
        "false_positive_count": 0,
        "false_negative_count": 1,
    }
    iteration = build_model_iteration(
        project_id=fixture["project_id"],
        parent_promoted_model=fixture["model"],
        parent_dataset_version=fixture["parent_dataset"],
        survey_model_qa=snapshot,
        next_dataset_version=next_dataset,
    )
    assert len(iteration["feedback_annotations"]) == 3
    assert len(iteration["audit_only_members"]) == 1
    assert iteration["audit_only_members"][0]["training_only"] is False


def test_survey_drift_is_scope_bound_and_never_a_promotion_gate():
    fixture = _fixtures()
    scope = _sha("stable-independent-survey-scope")
    baseline = build_survey_model_qa(
        project_id=fixture["project_id"],
        promoted_model_binding=fixture["model"],
        survey_qa_completion=fixture["survey_completion"],
        reviewed_outcomes=fixture["outcomes"],
        qa_scope_sha256=scope,
    )
    compared = build_survey_model_qa(
        project_id=fixture["project_id"],
        promoted_model_binding=fixture["model"],
        survey_qa_completion=fixture["survey_completion"],
        reviewed_outcomes=fixture["outcomes"],
        qa_scope_sha256=scope,
        baseline_snapshot=baseline,
    )
    assert compared["drift"]["assessed"] is True
    assert compared["drift"]["promotion_gate_authority"] is False
    assert all(
        row["precision_delta"] in {0.0, None}
        and row["recall_delta"] in {0.0, None}
        and row["false_positives_per_1000_frames_delta"] == 0.0
        and row["false_negatives_per_1000_frames_delta"] == 0.0
        for row in compared["drift"]["per_type"]
    )

    with pytest.raises(
        ModelIterationQaError,
        match="different QA scope",
    ):
        build_survey_model_qa(
            project_id=fixture["project_id"],
            promoted_model_binding=fixture["model"],
            survey_qa_completion=fixture["survey_completion"],
            reviewed_outcomes=fixture["outcomes"],
            qa_scope_sha256=_sha("another-scope"),
            baseline_snapshot=baseline,
        )


def test_pipeline_ready_model_binding_is_verified_before_survey_qa():
    fixture = _fixtures()
    ready_binding = {
        **fixture["model"],
        "schema_version": (
            "trafficvisson.promoted-model-job-binding.v1"
        ),
        "state": "ready",
        "output_location_id": "project-output",
        "root_identity_sha256": _sha("project-output-root"),
    }
    ready_binding["binding_sha256"] = _sha(ready_binding)
    snapshot = build_survey_model_qa(
        project_id=fixture["project_id"],
        promoted_model_binding=ready_binding,
        survey_qa_completion=fixture["survey_completion"],
        reviewed_outcomes=fixture["outcomes"],
        qa_scope_sha256=_sha("ready-binding-scope"),
    )
    assert snapshot["promoted_model_binding"][
        "model_promotion_state"
    ] == "promoted"

    changed = copy.deepcopy(ready_binding)
    changed["dataset_hash"] = _sha("another-dataset")
    with pytest.raises(
        ModelIterationQaError,
        match="PipelineJob binding is invalid",
    ):
        build_survey_model_qa(
            project_id=fixture["project_id"],
            promoted_model_binding=changed,
            survey_qa_completion=fixture["survey_completion"],
            reviewed_outcomes=fixture["outcomes"],
            qa_scope_sha256=_sha("ready-binding-scope"),
        )


def test_iteration_binds_exact_feedback_delta_and_preserves_validation(
    tmp_path,
):
    fixture = _fixtures()
    snapshot = build_survey_model_qa(
        project_id=fixture["project_id"],
        promoted_model_binding=fixture["model"],
        survey_qa_completion=fixture["survey_completion"],
        reviewed_outcomes=fixture["outcomes"],
        qa_scope_sha256=_sha("iteration-scope"),
    )
    next_dataset = _dataset(
        project_id=fixture["project_id"],
        rows=fixture["parent_rows"] + fixture["feedback_rows"],
        name="next",
    )
    iteration = build_model_iteration(
        project_id=fixture["project_id"],
        parent_promoted_model=fixture["model"],
        parent_dataset_version=fixture["parent_dataset"],
        survey_model_qa=snapshot,
        next_dataset_version=next_dataset,
    )

    assert iteration["feedback_annotations"] == snapshot[
        "training_feedback_annotations"
    ]
    assert iteration["next_dataset"]["dataset_version_id"] == (
        next_dataset["dataset_version_id"]
    )
    assert iteration[
        "next_training_requires_user_confirmation"
    ] is True
    assert iteration["automatic_training_start"] is False
    assert iteration["automatic_model_promotion"] is False
    assert iteration["incident_authority_created"] is False
    assert iteration["report_truth_created"] is False

    store = ModelIterationQaStore(tmp_path / "iteration.sqlite3")
    store.put_survey_model_qa(snapshot)
    assert store.put_model_iteration(iteration) == iteration
    assert store.put_model_iteration(iteration) == iteration
    restarted = ModelIterationQaStore(
        tmp_path / "iteration.sqlite3"
    )
    assert restarted.list_model_iterations(
        fixture["project_id"]
    ) == [iteration]


def test_iteration_ledger_chains_cumulative_dataset_versions(tmp_path):
    fixture = _fixtures()
    first_snapshot = build_survey_model_qa(
        project_id=fixture["project_id"],
        promoted_model_binding=fixture["model"],
        survey_qa_completion=fixture["survey_completion"],
        reviewed_outcomes=fixture["outcomes"],
        qa_scope_sha256=_sha("iteration-chain-scope"),
    )
    first_dataset = _dataset(
        project_id=fixture["project_id"],
        rows=fixture["parent_rows"] + fixture["feedback_rows"],
        name="chain-first",
    )
    first_iteration = build_model_iteration(
        project_id=fixture["project_id"],
        parent_promoted_model=fixture["model"],
        parent_dataset_version=fixture["parent_dataset"],
        survey_model_qa=first_snapshot,
        next_dataset_version=first_dataset,
    )

    second_pipeline_id = uuid4()
    second_analysis_id = uuid4()
    candidate_id = _sha("second-survey-candidate")[:20]
    second_member, second_graph = _annotation_graph(
        project_id=fixture["project_id"],
        pipeline_job_id=second_pipeline_id,
        analysis_job_id=second_analysis_id,
        candidate_id=candidate_id,
        annotation_index=90,
    )
    completion_base = {
        **fixture["survey_completion"],
        "pipeline_job_id": str(second_pipeline_id),
        "analysis_job_id": str(second_analysis_id),
        "source_sha256": _sha("second-survey-source"),
        "dense_index_sha256": _sha("second-survey-index"),
        "dense_candidate_discovery_revision": _sha(
            "second-survey-discovery"
        ),
        "question_queue_revision": _sha(
            "second-survey-question-queue"
        ),
        "promoted_model_inference_sha256": _sha(
            "second-survey-inference"
        ),
        "final_qa_choice": "no_miss_complete",
        "review_completion_id": "review:second-survey",
        "server_answer_ledger_revision": _sha(
            "second-survey-ledger"
        ),
        "user_discovered_registrations_sha256": _sha([]),
        "user_discovered_candidate_count": 0,
    }
    completion_base.pop("survey_qa_revision")
    second_completion = {
        **completion_base,
        "survey_qa_revision": _sha(completion_base),
    }
    second_outcome = {
        "project_id": str(fixture["project_id"]),
        "analysis_job_id": str(second_analysis_id),
        "review_event_id": second_graph["review_event_id"],
        "review_event_revision": second_graph[
            "review_event_revision"
        ],
        "question_id": second_member["question_id"],
        "candidate_id": candidate_id,
        "candidate_metadata_sha256": second_graph[
            "candidate_metadata_sha256"
        ],
        "evidence_sha256": second_graph["image_sha256"],
        "origin": "promoted_model_candidate",
        "proposed_issue_type": "sign_damage",
        "review_answer": "yes",
        "final_issue_type": "sign_damage",
        "annotation_id": second_member["annotation_id"],
        "annotation_hash": second_member["annotation_hash"],
        "training_authorized": True,
        "answered_by": "local_user",
    }
    revised_model_binding = {
        **fixture["model"],
        "state_revision": 2,
    }
    second_snapshot = build_survey_model_qa(
        project_id=fixture["project_id"],
        promoted_model_binding=revised_model_binding,
        survey_qa_completion=second_completion,
        reviewed_outcomes=[second_outcome],
        qa_scope_sha256=_sha("iteration-chain-scope"),
        baseline_snapshot=first_snapshot,
    )
    second_dataset = _dataset(
        project_id=fixture["project_id"],
        rows=(
            fixture["parent_rows"]
            + fixture["feedback_rows"]
            + [(second_member, second_graph)]
        ),
        name="chain-second",
    )
    second_iteration = build_model_iteration(
        project_id=fixture["project_id"],
        parent_promoted_model=revised_model_binding,
        parent_dataset_version=first_dataset,
        survey_model_qa=second_snapshot,
        next_dataset_version=second_dataset,
        parent_iteration=first_iteration,
    )
    assert second_iteration["parent_iteration_sha256"] == (
        first_iteration["model_iteration_sha256"]
    )
    assert second_iteration["parent_dataset"][
        "dataset_version_id"
    ] == first_dataset["dataset_version_id"]

    store = ModelIterationQaStore(tmp_path / "iteration-chain.sqlite3")
    store.put_survey_model_qa(first_snapshot)
    store.put_model_iteration(first_iteration)
    store.put_survey_model_qa(second_snapshot)
    store.put_model_iteration(second_iteration)
    assert store.list_model_iterations(fixture["project_id"]) == [
        first_iteration,
        second_iteration,
    ]

    orphan_store = ModelIterationQaStore(
        tmp_path / "orphan-iteration.sqlite3"
    )
    with pytest.raises(
        ModelIterationQaError,
        match="parent is not in the project ledger",
    ):
        orphan_store.put_model_iteration(second_iteration)


def test_iteration_rejects_validation_reuse_and_feedback_repartition():
    fixture = _fixtures()
    snapshot = build_survey_model_qa(
        project_id=fixture["project_id"],
        promoted_model_binding=fixture["model"],
        survey_qa_completion=fixture["survey_completion"],
        reviewed_outcomes=fixture["outcomes"],
        qa_scope_sha256=_sha("iteration-rejection-scope"),
    )
    bad_feedback_rows = copy.deepcopy(fixture["feedback_rows"])
    bad_feedback_rows[0][0]["split"] = "validation"
    bad_next = _dataset(
        project_id=fixture["project_id"],
        rows=fixture["parent_rows"] + bad_feedback_rows,
        name="bad-next",
    )
    with pytest.raises(
        ModelIterationQaError,
        match="changed fixed validation|exact TRAIN",
    ):
        build_model_iteration(
            project_id=fixture["project_id"],
            parent_promoted_model=fixture["model"],
            parent_dataset_version=fixture["parent_dataset"],
            survey_model_qa=snapshot,
            next_dataset_version=bad_next,
        )

    reused_completion = copy.deepcopy(
        fixture["survey_completion"]
    )
    reused_completion["analysis_job_id"] = str(
        fixture["training_job_id"]
    )
    base = dict(reused_completion)
    base.pop("survey_qa_revision")
    reused_completion["survey_qa_revision"] = _sha(base)
    with pytest.raises(
        ModelIterationQaError,
        match="reused model training or validation",
    ):
        build_survey_model_qa(
            project_id=fixture["project_id"],
            promoted_model_binding=fixture["model"],
            survey_qa_completion=reused_completion,
            reviewed_outcomes=fixture["outcomes"],
            qa_scope_sha256=_sha("reused-source-scope"),
        )


def test_self_resealed_automatic_authority_is_still_rejected():
    fixture = _fixtures()
    snapshot = build_survey_model_qa(
        project_id=fixture["project_id"],
        promoted_model_binding=fixture["model"],
        survey_qa_completion=fixture["survey_completion"],
        reviewed_outcomes=fixture["outcomes"],
        qa_scope_sha256=_sha("authority-scope"),
    )
    forged_snapshot = copy.deepcopy(snapshot)
    forged_snapshot["promotes_model"] = True
    snapshot_base = dict(forged_snapshot)
    snapshot_base.pop("survey_model_qa_sha256")
    forged_snapshot["survey_model_qa_sha256"] = _sha(snapshot_base)
    with pytest.raises(
        ModelIterationQaError,
        match="authority",
    ):
        canonical_survey_model_qa(forged_snapshot)

    next_dataset = _dataset(
        project_id=fixture["project_id"],
        rows=fixture["parent_rows"] + fixture["feedback_rows"],
        name="authority-next",
    )
    iteration = build_model_iteration(
        project_id=fixture["project_id"],
        parent_promoted_model=fixture["model"],
        parent_dataset_version=fixture["parent_dataset"],
        survey_model_qa=snapshot,
        next_dataset_version=next_dataset,
    )
    forged_iteration = copy.deepcopy(iteration)
    forged_iteration["automatic_model_promotion"] = True
    iteration_base = dict(forged_iteration)
    iteration_base.pop("model_iteration_sha256")
    forged_iteration["model_iteration_sha256"] = _sha(
        iteration_base
    )
    with pytest.raises(
        ModelIterationQaError,
        match="authority",
    ):
        canonical_model_iteration(forged_iteration)
