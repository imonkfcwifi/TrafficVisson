from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Optional

import pytest

from app.review_event_set import (
    ResolvedReviewCandidateEvidence,
    ResolvedReviewCandidateEvidenceSet,
)
from app.user_discovered_candidate import (
    SAM2_USER_DISCOVERY_REQUEST_SCHEMA,
    UNCLASSIFIED_REVIEW_ISSUE_TYPE,
    USER_DISCOVERED_CANDIDATE_SCHEMA,
    USER_DISCOVERED_PROPOSAL_SOURCE,
)
from app.user_discovered_review_queue import (
    USER_DISCOVERED_REVIEW_QUEUE_SCHEMA,
    UserDiscoveredReviewQueueError,
    UserDiscoveredReviewQueueService,
)
from test_review_event_set import (
    ANALYSIS_ID,
    CANDIDATE_A,
    CANDIDATE_B,
    CANDIDATE_SET_METADATA_SHA,
    OVERLAY_ID,
    OVERLAY_MANIFEST_SHA,
    PIPELINE_ID,
    PROJECT_ID,
    SOURCE_SHA,
    artifact_id,
    build_checkpoints,
    evidence_snapshot,
    make_store,
    materialize_completed,
    sha,
)


NEW_CANDIDATE = "c" * 20
NEW_OVERLAY_ID = f"ceo_{'8' * 32}"
NEW_OVERLAY_MANIFEST_SHA = "9" * 64
NEW_OVERLAY_CANDIDATE_SET_SHA = "0" * 64


def _new_candidate_metadata(
    *,
    issue_type_proposal: Optional[str],
) -> dict[str, Any]:
    review_issue_type = (
        issue_type_proposal or UNCLASSIFIED_REVIEW_ISSUE_TYPE
    )
    image_id = artifact_id(NEW_CANDIDATE, "image")
    clip_id = artifact_id(NEW_CANDIDATE, "clip")
    return {
        "candidate_id": NEW_CANDIDATE,
        "candidate_kind": "user_discovered",
        "user_issue_type_proposal": issue_type_proposal,
        "issue_type": review_issue_type,
        "start_seconds": 12.0,
        "end_seconds": 12.0,
        "representative_timestamp_seconds": 12.0,
        "urgent_review": False,
        "representative_image": {
            "artifact_id": image_id,
            "media_type": "image/jpeg",
            "size_bytes": 2_048,
            "sha256": "d" * 64,
            "artifact_ref": (
                f"candidate-evidence:{NEW_OVERLAY_ID}:{image_id}"
            ),
        },
        "preview_clip": {
            "artifact_id": clip_id,
            "media_type": "video/mp4",
            "size_bytes": 8_192,
            "sha256": "e" * 64,
            "artifact_ref": (
                f"candidate-evidence:{NEW_OVERLAY_ID}:{clip_id}"
            ),
        },
        "evidence_overlay": {
            "schema_version": (
                "trafficvisson.candidate-evidence-overlay.v1"
            ),
            "overlay_id": NEW_OVERLAY_ID,
            "pipeline_job_id": str(PIPELINE_ID),
            "manifest_sha256": NEW_OVERLAY_MANIFEST_SHA,
            "discovery_revision": "1" * 64,
            "dense_index_sha256": "2" * 64,
            "provider_provenance_sha256": "3" * 64,
            "model_provenance_sha256": "4" * 64,
            "candidate_set_metadata_sha256": (
                NEW_OVERLAY_CANDIDATE_SET_SHA
            ),
            "review_only": True,
        },
    }


def _composite_snapshot(
    *,
    issue_type_proposal: Optional[str] = None,
) -> tuple[
    ResolvedReviewCandidateEvidenceSet,
    ResolvedReviewCandidateEvidence,
]:
    old_candidates = evidence_snapshot().candidates
    metadata = _new_candidate_metadata(
        issue_type_proposal=issue_type_proposal
    )
    new_candidate = ResolvedReviewCandidateEvidence(
        candidate_id=NEW_CANDIDATE,
        candidate_metadata_sha256=sha(metadata),
        metadata=metadata,
    )
    candidates = tuple(
        sorted(
            (*old_candidates, new_candidate),
            key=lambda item: item.candidate_id,
        )
    )
    members = tuple(
        sorted(
            (
                {
                    "overlay_id": OVERLAY_ID,
                    "manifest_sha256": OVERLAY_MANIFEST_SHA,
                    "candidate_set_metadata_sha256": (
                        CANDIDATE_SET_METADATA_SHA
                    ),
                    "candidate_count": len(old_candidates),
                },
                {
                    "overlay_id": NEW_OVERLAY_ID,
                    "manifest_sha256": NEW_OVERLAY_MANIFEST_SHA,
                    "candidate_set_metadata_sha256": (
                        NEW_OVERLAY_CANDIDATE_SET_SHA
                    ),
                    "candidate_count": 1,
                },
            ),
            key=lambda item: item["overlay_id"],
        )
    )
    return (
        ResolvedReviewCandidateEvidenceSet(
            project_id=PROJECT_ID,
            analysis_job_id=ANALYSIS_ID,
            pipeline_job_id=PIPELINE_ID,
            overlay_id=OVERLAY_ID,
            source_sha256=SOURCE_SHA,
            overlay_manifest_sha256=sha(list(members)),
            candidate_set_metadata_sha256=sha(
                [
                    {
                        "candidate_id": item.candidate_id,
                        "candidate_metadata_sha256": (
                            item.candidate_metadata_sha256
                        ),
                    }
                    for item in candidates
                ]
            ),
            candidates=candidates,
            overlay_members=members,
        ),
        new_candidate,
    )


def _stage_checkpoints(
    baseline_review_event_set,
) -> dict[str, dict[str, Any]]:
    checkpoints = build_checkpoints()
    detection_ocr = {
        **checkpoints["dino_checkpoint"],
        **checkpoints["ocr_checkpoint"],
    }
    prior_binding = {
        "event_id": baseline_review_event_set.events[0].event_id,
        "representative_candidate_id": CANDIDATE_B,
        "event_revision": (
            baseline_review_event_set.events[0].event_revision
        ),
    }
    queue_revision = sha(
        {
            "question_candidate_ids": [CANDIDATE_B],
            "review_event_set_revision": (
                baseline_review_event_set.review_event_set_revision
            ),
            "review_event_bindings": [prior_binding],
        }
    )
    question_queue = {
        "analysis_job_id": str(ANALYSIS_ID),
        "source_sha256": SOURCE_SHA,
        "question_count": 1,
        "question_candidate_ids": [CANDIDATE_B],
        "human_question_count": 1,
        "human_question_candidate_ids": [CANDIDATE_B],
        "structured_training_label_count": 0,
        "structured_training_label_candidate_ids": [],
        "mandatory_human_candidate_ids": [],
        "question_queue_revision": queue_revision,
        "review_event_set_state": "materialized",
        "review_event_set": baseline_review_event_set.to_checkpoint(),
        "review_event_set_id": (
            baseline_review_event_set.review_event_set_id
        ),
        "review_event_set_revision": (
            baseline_review_event_set.review_event_set_revision
        ),
        "review_event_set_manifest_sha256": (
            baseline_review_event_set.manifest_sha256
        ),
        "candidate_evidence_revision": (
            baseline_review_event_set.candidate_evidence_revision
        ),
        "review_event_count": 1,
        "review_event_ids": [
            baseline_review_event_set.events[0].event_id
        ],
        "review_event_bindings": [prior_binding],
        "question_catalog_state": "registered",
        "question_catalog_revision": "a" * 64,
        "review_only": True,
        "annotation_count": 0,
        "incident_count": 0,
        "training_membership_count": 0,
        "report_incident_count": 0,
    }
    return {
        "detection_ocr": detection_ocr,
        "sam2_segmentation_tracking": checkpoints[
            "sam2_checkpoint"
        ],
        "temporal_merge": checkpoints["temporal_checkpoint"],
        "similar_case_search": checkpoints[
            "similar_case_checkpoint"
        ],
        "vision_proposal": checkpoints[
            "structured_proposal_checkpoint"
        ],
        "question_queue": question_queue,
    }


def _sam2_request() -> dict[str, Any]:
    request = {
        "schema_version": SAM2_USER_DISCOVERY_REQUEST_SCHEMA,
        "project_id": str(PROJECT_ID),
        "analysis_job_id": str(ANALYSIS_ID),
        "pipeline_job_id": str(PIPELINE_ID),
        "request_sha256": "5" * 64,
        "dense_index_sha256": "6" * 64,
        "frame_id": "frame-000024",
        "frame_sha256": "7" * 64,
        "selection": {"kind": "point", "x": 0.5, "y": 0.5},
        "point_prompt": {"x": 0.5, "y": 0.5},
        "status": "requested_pending_runtime_binding",
        "runtime_provenance": None,
        "review_only": True,
        "automatic_annotation_creation": False,
        "automatic_incident_creation": False,
        "automatic_training_ingest": False,
    }
    return {**request, "request_revision": sha(request)}


def _user_evidence(
    *,
    issue_type_proposal: Optional[str] = None,
    include_sam2: bool = True,
) -> dict[str, Any]:
    return {
        "schema_version": USER_DISCOVERED_CANDIDATE_SCHEMA,
        "candidate_kind": "user_discovered",
        "project_id": str(PROJECT_ID),
        "analysis_job_id": str(ANALYSIS_ID),
        "pipeline_job_id": str(PIPELINE_ID),
        "source_sha256": SOURCE_SHA,
        "overlay_id": NEW_OVERLAY_ID,
        "overlay_manifest_sha256": NEW_OVERLAY_MANIFEST_SHA,
        "candidate_id": NEW_CANDIDATE,
        "selected_frame_timestamp_seconds": 12.0,
        "issue_type_proposal": issue_type_proposal,
        "review_issue_type": (
            issue_type_proposal or UNCLASSIFIED_REVIEW_ISSUE_TYPE
        ),
        "sam2_refinement_request": (
            _sam2_request() if include_sam2 else None
        ),
        "review_only": True,
        "annotations_created": 0,
        "incidents_created": 0,
        "training_members_created": 0,
        "report_truth_records_created": 0,
    }


def test_augments_queue_with_one_delta_event_without_rebinding_old_event(
    tmp_path: Path,
) -> None:
    store, resolver = make_store(tmp_path)
    baseline = materialize_completed(store)
    original_set_id = baseline.review_event_set_id
    original_event = baseline.events[0]
    original_manifest_path = (
        tmp_path
        / "review-events"
        / str(PROJECT_ID)
        / str(ANALYSIS_ID)
        / str(PIPELINE_ID)
        / OVERLAY_ID
        / original_set_id
        / "manifest.json"
    )
    original_manifest_bytes = original_manifest_path.read_bytes()
    composite, new_evidence = _composite_snapshot()
    resolver.snapshot = composite
    stages = _stage_checkpoints(baseline)
    original_stages = deepcopy(stages)
    service = UserDiscoveredReviewQueueService(store)

    result = service.augment(
        project_id=PROJECT_ID,
        analysis_job_id=ANALYSIS_ID,
        pipeline_job_id=PIPELINE_ID,
        composite_overlay_id=OVERLAY_ID,
        source_sha256=SOURCE_SHA,
        stage_checkpoints=stages,
        user_discovered_evidence=_user_evidence(),
    )

    assert result.schema_version == USER_DISCOVERED_REVIEW_QUEUE_SCHEMA
    assert result.prior_question_candidate_ids == (CANDIDATE_B,)
    assert result.question_candidate_ids == (
        CANDIDATE_B,
        NEW_CANDIDATE,
    )
    assert result.delta_question_candidate_ids == (NEW_CANDIDATE,)
    assert result.user_issue_type_proposal is None
    assert result.review_issue_type == UNCLASSIFIED_REVIEW_ISSUE_TYPE
    assert result.idempotent_replay is False
    assert stages == original_stages
    assert "question_queue" not in result.delta_stage_checkpoints

    delta_set = result.delta_review_event_set
    assert delta_set["review_event_set_id"] != original_set_id
    assert delta_set["status"] == "partial"
    assert delta_set["error_code"] == "degraded_review_capabilities"
    assert delta_set["event_count"] == 1
    assert result.delta_event_binding[
        "representative_candidate_id"
    ] == NEW_CANDIDATE
    assert result.delta_event_binding["review_event_set_id"] == (
        delta_set["review_event_set_id"]
    )
    assert result.delta_event_binding["review_only"] is True

    queue = result.question_queue_checkpoint
    assert queue["question_candidate_ids"] == [
        CANDIDATE_B,
        NEW_CANDIDATE,
    ]
    assert queue["human_question_candidate_ids"] == [
        CANDIDATE_B,
        NEW_CANDIDATE,
    ]
    assert queue["mandatory_human_candidate_ids"] == [NEW_CANDIDATE]
    assert queue["structured_training_label_candidate_ids"] == []
    assert queue["review_event_bindings"][0] == (
        original_stages["question_queue"]["review_event_bindings"][0]
    )
    assert queue["review_event_bindings"][1] == (
        result.delta_event_binding
    )
    assert queue["review_event_set_id"] == original_set_id
    assert queue["review_event_set_revision"] == (
        baseline.review_event_set_revision
    )
    assert queue["review_event_set_manifest_sha256"] == (
        baseline.manifest_sha256
    )
    assert queue["review_event_collection_refresh_required"] is True
    assert queue["question_catalog_state"] == "sidecar_only"
    assert queue["question_catalog_revision"] is None
    assert queue["question_catalog_refresh_required"] is True
    assert queue["user_discovered_annotation_count"] == 0
    assert queue["user_discovered_incident_count"] == 0
    assert queue["user_discovered_training_membership_count"] == 0
    assert queue["user_discovered_report_incident_count"] == 0

    detection = result.delta_stage_checkpoints["detection_ocr"]
    assert detection["proposal_candidate_ids"] == [NEW_CANDIDATE]
    assert detection["proposal_sources"] == [
        USER_DISCOVERED_PROPOSAL_SOURCE
    ]
    assert detection["ocr_candidate_ids"] == []
    assert detection["ocr_observations_by_candidate"] == {}
    detection_core = {
        "analysis_job_id": str(ANALYSIS_ID),
        "source_sha256": SOURCE_SHA,
        "local_inference_revision": detection[
            "local_inference_revision"
        ],
        "dino_revision": detection["dino_revision"],
        "dino_evidence_source": detection["dino_evidence_source"],
        "high_frequency_evidence_count": 0,
        "high_frequency_manifest_sha256": None,
        "proposal_candidate_ids": [NEW_CANDIDATE],
        "detected_issue_types": [UNCLASSIFIED_REVIEW_ISSUE_TYPE],
        "ocr_candidate_ids": [],
    }
    assert detection["detection_revision"] == sha(detection_core)
    assert detection["ocr_provenance"]["result_revision"] == (
        detection["ocr_revision"]
    )
    assert detection["ocr_provenance"]["review_only"] is True

    sam2 = result.delta_stage_checkpoints[
        "sam2_segmentation_tracking"
    ]
    sam2_core = dict(sam2)
    sam2_core.pop("sam2_revision")
    assert sam2["sam2_revision"] == sha(sam2_core)
    assert sam2["proposals"] == []
    assert sam2["failures"] == []
    assert sam2[
        "user_discovered_pending_requests_by_candidate"
    ] == {NEW_CANDIDATE: _sam2_request()}
    assert sam2["user_discovered_sam2_status_by_candidate"] == {
        NEW_CANDIDATE: "pending"
    }

    temporal = result.delta_stage_checkpoints["temporal_merge"]
    assert temporal["question_candidate_ids"] == [NEW_CANDIDATE]
    assert len(temporal["groups"]) == 1
    assert temporal["groups"][0]["evidence_candidate_ids"] == [
        NEW_CANDIDATE
    ]
    temporal_core = {
        "analysis_job_id": str(ANALYSIS_ID),
        "evaluation_id": temporal["evaluation_id"],
        "detection_revision": detection["detection_revision"],
        "question_candidate_ids": [NEW_CANDIDATE],
        "groups": temporal["groups"],
    }
    assert temporal["temporal_merge_revision"] == sha(temporal_core)

    similar = result.delta_stage_checkpoints[
        "similar_case_search"
    ]
    assert similar["matches_by_candidate"] == {NEW_CANDIDATE: []}
    assert similar["visual_reference_revision"] == sha(
        {
            "visual_index_manifest_sha256": similar[
                "visual_index_manifest_sha256"
            ],
            "matches_by_candidate": {NEW_CANDIDATE: []},
        }
    )

    structured = result.delta_stage_checkpoints["vision_proposal"]
    judgment = structured[
        "structured_judgments_by_candidate"
    ][NEW_CANDIDATE]
    assert set(structured["structured_judgments_by_candidate"]) == {
        NEW_CANDIDATE
    }
    assert judgment["status"] == "not_run"
    assert judgment["error_code"] == "user_discovered_pending_review"
    assert judgment["review_only"] is True
    assert judgment["proposal"] is None
    structured_core = {
        "pipeline_job_id": str(PIPELINE_ID),
        "analysis_job_id": str(ANALYSIS_ID),
        "source_sha256": SOURCE_SHA,
        "detection_revision": detection["detection_revision"],
        "temporal_merge_revision": temporal[
            "temporal_merge_revision"
        ],
        "visual_reference_revision": similar[
            "visual_reference_revision"
        ],
        "provider": structured["selected_provider"],
        "provider_state": structured["provider_state"],
        "transmission_consent": structured["transmission_consent"],
        "judgments": {NEW_CANDIDATE: judgment},
    }
    assert structured["structured_vision_revision"] == sha(
        structured_core
    )

    found_set = store.get(
        PROJECT_ID,
        ANALYSIS_ID,
        PIPELINE_ID,
        OVERLAY_ID,
        delta_set["review_event_set_id"],
    )
    assert found_set.review_event_set_id == delta_set[
        "review_event_set_id"
    ]
    assert len(found_set.events) == 1
    found_event = found_set.events[0]
    assert found_event.representative_candidate_id == NEW_CANDIDATE
    assert found_event.issue_type == UNCLASSIFIED_REVIEW_ISSUE_TYPE
    assert found_event.sam2_summary["status"] == "not_requested"
    assert found_event.similar_cases == ()
    assert found_event.structured_proposal["status"] == "not_run"
    assert found_event.structured_proposal["error_code"] == (
        "user_discovered_pending_review"
    )
    assert found_event.annotations_created == 0
    assert found_event.incidents_created == 0
    assert found_event.training_members_created == 0
    assert found_event.report_truth_records_created == 0
    assert new_evidence.metadata["user_issue_type_proposal"] is None
    assert new_evidence.metadata["issue_type"] == (
        UNCLASSIFIED_REVIEW_ISSUE_TYPE
    )

    assert original_manifest_path.read_bytes() == original_manifest_bytes
    assert queue["review_event_bindings"][0] == {
        "event_id": original_event.event_id,
        "representative_candidate_id": CANDIDATE_B,
        "event_revision": original_event.event_revision,
    }

    replay = service.augment(
        project_id=PROJECT_ID,
        analysis_job_id=ANALYSIS_ID,
        pipeline_job_id=PIPELINE_ID,
        composite_overlay_id=OVERLAY_ID,
        source_sha256=SOURCE_SHA,
        stage_checkpoints=stages,
        user_discovered_evidence=_user_evidence(),
    )
    assert replay.idempotent_replay is True
    assert replay.delta_review_event_set[
        "review_event_set_revision"
    ] == delta_set["review_event_set_revision"]
    assert replay.question_queue_checkpoint[
        "question_queue_revision"
    ] == queue["question_queue_revision"]
    assert stages == original_stages


def test_preserves_explicit_issue_proposal_and_rejects_cross_project_sam2(
    tmp_path: Path,
) -> None:
    store, resolver = make_store(tmp_path)
    baseline = materialize_completed(store)
    composite, _candidate = _composite_snapshot(
        issue_type_proposal="pothole"
    )
    resolver.snapshot = composite
    stages = _stage_checkpoints(baseline)
    service = UserDiscoveredReviewQueueService(store)
    evidence = _user_evidence(issue_type_proposal="pothole")

    bad_evidence = deepcopy(evidence)
    bad_request = dict(bad_evidence["sam2_refinement_request"])
    bad_request["project_id"] = str(ANALYSIS_ID)
    bad_request.pop("request_revision")
    bad_request["request_revision"] = sha(bad_request)
    bad_evidence["sam2_refinement_request"] = bad_request
    with pytest.raises(
        UserDiscoveredReviewQueueError,
        match="pending SAM2 request is invalid",
    ) as captured:
        service.augment(
            project_id=PROJECT_ID,
            analysis_job_id=ANALYSIS_ID,
            pipeline_job_id=PIPELINE_ID,
            composite_overlay_id=OVERLAY_ID,
            source_sha256=SOURCE_SHA,
            stage_checkpoints=stages,
            user_discovered_evidence=bad_evidence,
        )
    assert captured.value.code == "user_discovered_review_queue_invalid"

    result = service.augment(
        project_id=PROJECT_ID,
        analysis_job_id=ANALYSIS_ID,
        pipeline_job_id=PIPELINE_ID,
        composite_overlay_id=OVERLAY_ID,
        source_sha256=SOURCE_SHA,
        stage_checkpoints=stages,
        user_discovered_evidence=evidence,
    )
    assert result.user_issue_type_proposal == "pothole"
    assert result.review_issue_type == "pothole"
    found = store.get(
        PROJECT_ID,
        ANALYSIS_ID,
        PIPELINE_ID,
        OVERLAY_ID,
        result.delta_review_event_set["review_event_set_id"],
    )
    assert len(found.events) == 1
    assert found.events[0].issue_type == "pothole"
