from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from app.candidate_labels import CandidateLabelStore
from app.main import create_app
from app.review_event_set import (
    ResolvedReviewCandidateEvidence,
    ResolvedReviewCandidateEvidenceSet,
    ReviewEventSetConflict,
    ReviewEventSetIntegrityError,
    ReviewEventSetLimitError,
    ReviewEventSetNotFound,
    ReviewEventSetStore,
)
from app.pipeline_jobs import BridgePipelineRuntime
from test_project_catalog_v2 import _write_completed_analysis


PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
ANALYSIS_ID = UUID("22222222-2222-4222-8222-222222222222")
PIPELINE_ID = UUID("33333333-3333-4333-8333-333333333333")
OVERLAY_ID = f"ceo_{'4' * 32}"
SOURCE_SHA = "5" * 64
OVERLAY_MANIFEST_SHA = "6" * 64
CANDIDATE_SET_METADATA_SHA = "7" * 64
CANDIDATE_A = "a" * 20
CANDIDATE_B = "b" * 20
BASE_OVERLAY_ID = f"ceo_{'1' * 32}"
BASE_OVERLAY_MANIFEST_SHA = "2" * 64
BASE_CANDIDATE_SET_METADATA_SHA = "3" * 64


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def artifact_id(candidate_id: str, kind: str) -> str:
    return f"cea_{sha([candidate_id, kind])[:32]}"


def candidate_metadata(
    candidate_id: str,
    *,
    start: float,
    end: float,
    timestamp: float,
    issue_type: str,
    urgent: bool,
    image_sha: str,
    clip_sha: str,
    representative_proposal_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    image_id = artifact_id(candidate_id, "image")
    clip_id = artifact_id(candidate_id, "clip")
    result = {
        "candidate_id": candidate_id,
        "candidate_kind": "dense_candidate_evidence_overlay",
        "issue_type": issue_type,
        "start_seconds": start,
        "end_seconds": end,
        "representative_timestamp_seconds": timestamp,
        "urgent_review": urgent,
        # The upstream adapter may carry an internal relative name.  The
        # ReviewEventSet output must deliberately omit it.
        "representative_image": {
            "artifact_id": image_id,
            "relative_path": f"{candidate_id}.jpg",
            "media_type": "image/jpeg",
            "size_bytes": 1_024,
            "sha256": image_sha,
            "artifact_ref": f"candidate-evidence:{OVERLAY_ID}:{image_id}",
        },
        "preview_clip": {
            "artifact_id": clip_id,
            "relative_path": f"{candidate_id}.mp4",
            "media_type": "video/mp4",
            "size_bytes": 4_096,
            "sha256": clip_sha,
            "artifact_ref": f"candidate-evidence:{OVERLAY_ID}:{clip_id}",
        },
        "evidence_overlay": {
            "schema_version": "trafficvisson.candidate-evidence-overlay.v1",
            "overlay_id": OVERLAY_ID,
            "pipeline_job_id": str(PIPELINE_ID),
            "manifest_sha256": OVERLAY_MANIFEST_SHA,
            "discovery_revision": "c" * 64,
            "dense_index_sha256": "d" * 64,
            "provider_provenance_sha256": "e" * 64,
            "model_provenance_sha256": "f" * 64,
            "candidate_set_metadata_sha256": CANDIDATE_SET_METADATA_SHA,
            "review_only": True,
        },
    }
    if representative_proposal_provenance is not None:
        result["representative_proposal_provenance"] = (
            representative_proposal_provenance
        )
    return result


def resolved_candidate(
    candidate_id: str,
    *,
    start: float,
    end: float,
    timestamp: float,
    issue_type: str,
    urgent: bool,
    image_sha: str,
    clip_sha: str,
    representative_proposal_provenance: dict[str, Any] | None = None,
) -> ResolvedReviewCandidateEvidence:
    metadata = candidate_metadata(
        candidate_id,
        start=start,
        end=end,
        timestamp=timestamp,
        issue_type=issue_type,
        urgent=urgent,
        image_sha=image_sha,
        clip_sha=clip_sha,
        representative_proposal_provenance=(
            representative_proposal_provenance
        ),
    )
    return ResolvedReviewCandidateEvidence(
        candidate_id=candidate_id,
        candidate_metadata_sha256=sha(metadata),
        metadata=metadata,
    )


def evidence_snapshot(
    *,
    candidates: tuple[ResolvedReviewCandidateEvidence, ...] | None = None,
) -> ResolvedReviewCandidateEvidenceSet:
    if candidates is None:
        candidates = (
            resolved_candidate(
                CANDIDATE_A,
                start=10.0,
                end=10.5,
                timestamp=10.25,
                issue_type="pothole",
                urgent=True,
                image_sha="8" * 64,
                clip_sha="9" * 64,
            ),
            resolved_candidate(
                CANDIDATE_B,
                start=10.4,
                end=11.0,
                timestamp=10.7,
                issue_type="pothole",
                urgent=False,
                image_sha="a" * 64,
                clip_sha="b" * 64,
            ),
        )
    return ResolvedReviewCandidateEvidenceSet(
        project_id=PROJECT_ID,
        analysis_job_id=ANALYSIS_ID,
        pipeline_job_id=PIPELINE_ID,
        overlay_id=OVERLAY_ID,
        source_sha256=SOURCE_SHA,
        overlay_manifest_sha256=OVERLAY_MANIFEST_SHA,
        candidate_set_metadata_sha256=CANDIDATE_SET_METADATA_SHA,
        candidates=candidates,
    )


class MutableResolver:
    def __init__(
        self, snapshot: ResolvedReviewCandidateEvidenceSet
    ) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def __call__(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        overlay_id: str,
        source_sha256: str,
    ) -> ResolvedReviewCandidateEvidenceSet:
        self.calls += 1
        assert project_id == PROJECT_ID
        assert analysis_job_id == ANALYSIS_ID
        assert pipeline_job_id == PIPELINE_ID
        assert overlay_id == OVERLAY_ID
        assert source_sha256 == SOURCE_SHA
        return self.snapshot


def base_identity() -> dict[str, Any]:
    return {
        "analysis_job_id": str(ANALYSIS_ID),
        "pipeline_job_id": str(PIPELINE_ID),
        "overlay_id": OVERLAY_ID,
        "source_sha256": SOURCE_SHA,
        "review_only": True,
        "annotation_count": 0,
        "incident_count": 0,
    }


def sam2_checkpoint(candidate_ids: list[str]) -> dict[str, Any]:
    proposals = []
    for candidate_id in candidate_ids:
        proposal = {
            "candidate_id": candidate_id,
            "prompt": {
                "kind": "detector_box_center_point",
                "x": 0.5,
                "y": 0.5,
                "prompt_frame_ref": (
                    f"candidate-evidence:{candidate_id}:prompt"
                ),
            },
            "representative_polygon": [
                [0.2, 0.2],
                [0.4, 0.2],
                [0.4, 0.4],
            ],
            "normalized_box": {
                "x": 0.2,
                "y": 0.2,
                "width": 0.2,
                "height": 0.2,
            },
            "tracked_frames": [
                {
                    "evidence_ref": (
                        f"candidate-evidence:{candidate_id}:frame-1"
                    ),
                    "timestamp_seconds": 10.7,
                    "polygon": [
                        [0.2, 0.2],
                        [0.4, 0.2],
                        [0.4, 0.4],
                    ],
                }
            ],
            "tracking_performed": False,
            "review_only": True,
            "requires_user_confirmation": True,
            "automatic_annotation_allowed": False,
            "automatic_incident_creation_allowed": False,
            "training_membership_allowed": False,
        }
        proposals.append({**proposal, "proposal_sha256": sha(proposal)})
    body = {
        "schema_version": "trafficvisson.pipeline-sam2-review.v1",
        "state": "completed" if proposals else "zero_prompts",
        "model_manifest": {
            "pack_id": "sam2-pack",
            "pack_version": "1.0",
            "manifest_sha256": "c" * 64,
            "model_artifact_sha256": "d" * 64,
        },
        "proposals": proposals,
        "failures": [],
        "mask_generated": bool(proposals),
        "tracking_performed": False,
        "review_only": True,
        "requires_user_confirmation": True,
        "automatic_annotation_allowed": False,
        "automatic_incident_creation_allowed": False,
        "training_membership_allowed": False,
        "annotation_count": 0,
        "incident_count": 0,
    }
    return {**body, "sam2_revision": sha(body)}


def proposed_judgment(candidate_id: str) -> dict[str, Any]:
    proposal = {
        "schema_version": "trafficvisson.structured-vision-proposal.v1",
        "candidate_id": candidate_id,
        "issue_type": "pothole",
        "maintenance_required": True,
        "action": "포트홀 보수",
        "recommended_severity": "moderate",
        "rationale": "연속 프레임과 승인 사례에서 유사한 노면 손상이 보입니다.",
        "ocr_verification": {
            "status": "not_applicable",
            "normalized_text": None,
            "observation_refs": [],
        },
        "uncertainty": {
            "score": 0.22,
            "reasons": ["사용자 최종 확인 필요"],
        },
        "source_refs": [
            f"candidate-evidence:{candidate_id}:detector",
            "reference:3q:case-001",
        ],
        "comparison_mode": "metadata_comparison_only",
        "review_state": "proposed",
        "review_only": True,
        "automatic_annotation_allowed": False,
        "automatic_incident_allowed": False,
        "automatic_training_membership_allowed": False,
        "automatic_report_truth_allowed": False,
    }
    return {
        "schema_version": "trafficvisson.structured-vision-proposal.v1",
        "status": "proposed",
        "provider": "codex",
        "candidate_id": candidate_id,
        "comparison_mode": "metadata_comparison_only",
        "review_state": "none",
        "review_only": True,
        "automatic_annotation_allowed": False,
        "automatic_incident_allowed": False,
        "automatic_training_membership_allowed": False,
        "automatic_report_truth_allowed": False,
        "llm_bulk_frame_detection_used": False,
        "direct_visual_inspection_claimed": False,
        "proposal": proposal,
        "proposal_sha256": sha(proposal),
        "error_code": None,
    }


def build_checkpoints(
    *,
    question_ids: list[str] | None = None,
    groups: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    question_ids = [CANDIDATE_B] if question_ids is None else question_ids
    if groups is None:
        groups = (
            [
                {
                    "proposal_id": "proposal-001",
                    "representative_candidate_id": CANDIDATE_B,
                    "evidence_candidate_ids": [
                        CANDIDATE_A,
                        CANDIDATE_B,
                    ],
                    "issue_type": "pothole",
                    "start_seconds": 10.0,
                    "end_seconds": 11.0,
                    "detection_count": 2,
                }
            ]
            if question_ids
            else []
        )
    proposal_candidate_ids = (
        [CANDIDATE_A, CANDIDATE_B] if question_ids else []
    )
    ocr_candidate_ids = (
        [CANDIDATE_A, CANDIDATE_B] if question_ids else []
    )
    dino_revision = "f" * 64
    local_inference_revision = "e" * 64
    detected_issue_types = ["pothole"] if question_ids else []
    dino_evidence_source = "candidate_evidence_overlay_2fps"
    high_frequency_evidence_count = (
        len(proposal_candidate_ids) if question_ids else 0
    )
    high_frequency_manifest_sha256 = (
        OVERLAY_MANIFEST_SHA if question_ids else None
    )
    detection_revision = sha(
        {
            "analysis_job_id": str(ANALYSIS_ID),
            "source_sha256": SOURCE_SHA,
            "local_inference_revision": local_inference_revision,
            "dino_revision": dino_revision,
            "dino_evidence_source": dino_evidence_source,
            "high_frequency_evidence_count": (
                high_frequency_evidence_count
            ),
            "high_frequency_manifest_sha256": (
                high_frequency_manifest_sha256
            ),
            "proposal_candidate_ids": proposal_candidate_ids,
            "detected_issue_types": detected_issue_types,
            "ocr_candidate_ids": sorted(ocr_candidate_ids),
        }
    )
    dino = {
        **base_identity(),
        "detection_revision": detection_revision,
        "dino_revision": dino_revision,
        "local_inference_revision": local_inference_revision,
        "dino_evidence_source": dino_evidence_source,
        "high_frequency_evidence_count": high_frequency_evidence_count,
        "high_frequency_manifest_sha256": (
            high_frequency_manifest_sha256
        ),
        "proposal_candidate_ids": proposal_candidate_ids,
        "detected_issue_types": detected_issue_types,
        "ocr_candidate_ids": ocr_candidate_ids,
        "proposal_sources": ["grounding_dino_review_sidecar"],
        "dino_box_proposal_status": (
            "completed" if question_ids else "zero_detections"
        ),
        "dedicated_detector_status": (
            "completed" if question_ids else "zero_detections"
        ),
        "automatic_labeling_allowed": False,
    }
    ocr = {
        **base_identity(),
        "ocr_revision": "1" * 64,
        "ocr_status": "completed" if question_ids else "zero_detections",
        "ocr_candidate_ids": ocr_candidate_ids,
        "ocr_observations_by_candidate": (
            {
                CANDIDATE_A: [
                    {
                        "text": "중부",
                        "confidence": 0.72,
                        "kind": "tunnel_name",
                        "box": {
                            "x": 0.18,
                            "y": 0.1,
                            "width": 0.2,
                            "height": 0.1,
                        },
                    }
                ],
                CANDIDATE_B: [
                    {
                        "text": "중부터널",
                        "confidence": 0.91,
                        "kind": "tunnel_name",
                        "box": {
                            "x": 0.2,
                            "y": 0.1,
                            "width": 0.3,
                            "height": 0.1,
                        },
                    }
                ]
            }
            if question_ids
            else {}
        ),
        "ocr_provenance": {
            "schema_version": (
                "trafficvisson.pipeline-ocr-provenance.v1"
            ),
            "engine_role": "ocr_only",
            "input_revision": "0" * 64,
            "result_revision": "1" * 64,
            "review_only": True,
            "path_exposed": False,
        },
        "automatic_annotation_allowed": False,
    }
    sam2 = sam2_checkpoint(list(question_ids))
    temporal_core = {
        "analysis_job_id": str(ANALYSIS_ID),
        "evaluation_id": "evaluation-001",
        "detection_revision": detection_revision,
        "question_candidate_ids": question_ids,
        "groups": groups,
    }
    temporal = {
        **base_identity(),
        **temporal_core,
        "temporal_merge_revision": sha(temporal_core),
        "automatic_incident_creation_allowed": False,
    }
    matches = (
        {
            candidate_id: [
                {
                    "rank": index,
                    "source_id": "sample-3q",
                    "case_id": f"case-{index:03d}",
                    "quarter": "3Q",
                    "caption": "노면 포트홀",
                    "route": "민자고속도로",
                    "location": "10.7초",
                    "opinion": "포트홀 보수 필요",
                    "document_sha256": "2" * 64,
                    "image_sha256": "3" * 64,
                    "reference_state": "approved_reference",
                    "approved_reference": True,
                    "approval_record_sha256": "4" * 64,
                    "approved_issue_type": "pothole",
                    "approved_action": "포트홀 보수",
                    "combined_similarity_score": 0.88,
                }
                for index in range(1, 4)
            ]
            for candidate_id in question_ids
        }
        if question_ids
        else {}
    )
    visual_index_sha = "5" * 64
    visual_revision = sha(
        {
            "visual_index_manifest_sha256": visual_index_sha,
            "matches_by_candidate": matches,
        }
    )
    similar = {
        **base_identity(),
        "state": "ready",
        "visual_index_manifest_sha256": visual_index_sha,
        "visual_reference_revision": visual_revision,
        "matches_by_candidate": matches,
        "review_only": True,
        "automatic_annotation_allowed": False,
    }
    judgments = {
        candidate_id: proposed_judgment(candidate_id)
        for candidate_id in question_ids
    }
    structured_core = {
        "pipeline_job_id": str(PIPELINE_ID),
        "analysis_job_id": str(ANALYSIS_ID),
        "source_sha256": SOURCE_SHA,
        "detection_revision": detection_revision,
        "temporal_merge_revision": temporal["temporal_merge_revision"],
        "visual_reference_revision": visual_revision,
        "provider": "codex",
        "provider_state": "ready",
        "transmission_consent": True,
        "judgments": judgments,
    }
    structured = {
        **base_identity(),
        "detection_revision": detection_revision,
        "temporal_merge_revision": temporal["temporal_merge_revision"],
        "structured_vision_revision": sha(structured_core),
        "structured_judgments_by_candidate": judgments,
        "selected_provider": "codex",
        "provider_state": "ready",
        "transmission_consent": True,
        "review_only": True,
        "automatic_annotation_allowed": False,
        "automatic_incident_allowed": False,
        "automatic_training_membership_allowed": False,
        "automatic_report_truth_allowed": False,
        "training_membership_count": 0,
        "report_incident_count": 0,
    }
    return {
        "dino_checkpoint": dino,
        "ocr_checkpoint": ocr,
        "sam2_checkpoint": sam2,
        "temporal_checkpoint": temporal,
        "similar_case_checkpoint": similar,
        "structured_proposal_checkpoint": structured,
    }


def dino_seed_temporal_fixture() -> tuple[
    ResolvedReviewCandidateEvidenceSet,
    dict[str, dict[str, Any]],
]:
    parent_candidate_id = CANDIDATE_A
    synthetic_candidate_id = CANDIDATE_B
    prompt_candidate_id = "c" * 20
    evidence_candidate_id = "d" * 20
    dino_proposal_id = f"dino_{'e' * 24}"
    dino_revision = "6" * 64
    local_inference_revision = "7" * 64
    dino_row = {
        "proposal_id": dino_proposal_id,
        "parent_candidate_id": parent_candidate_id,
        "evidence_candidate_id": evidence_candidate_id,
        "evidence_image_sha256": "8" * 64,
        "evidence_timestamp_seconds": 10.7,
        "evidence_source": "candidate_window_rescan_5_10fps",
        "issue_type": "guide_sign_identity",
        "normalized_box": {
            "x": 0.2,
            "y": 0.2,
            "width": 0.4,
            "height": 0.3,
        },
        "score": 0.61,
        "score_semantics": "model_score_not_probability",
        "source_label": "a traffic sign",
        "review_only": True,
        "automatic_labeling_allowed": False,
    }
    dino_rows = {parent_candidate_id: [dino_row]}
    dino_rows_revision = sha(dino_rows)
    detection_payload = {
        "analysis_job_id": str(ANALYSIS_ID),
        "source_sha256": SOURCE_SHA,
        "local_inference_revision": local_inference_revision,
        "dino_revision": dino_revision,
        "dino_evidence_source": (
            "candidate_window_rescan_5_10fps"
        ),
        "high_frequency_evidence_count": 8,
        "high_frequency_manifest_sha256": OVERLAY_MANIFEST_SHA,
        "proposal_candidate_ids": [parent_candidate_id],
        "detected_issue_types": ["guide_sign_identity"],
        "ocr_candidate_ids": [],
        "visual_evidence_quality_state": "usable",
        "visual_evidence_quality_revision": "5" * 64,
        "visual_quality_trusted_original_candidate_ids": [
            parent_candidate_id
        ],
        "visual_quality_blocked_parent_candidate_ids": [],
        "dino_review_proposals_revision": dino_rows_revision,
    }
    detection_revision = sha(detection_payload)
    provenance = {
        "schema_version": (
            "trafficvisson.dino-temporal-evidence.v1"
        ),
        "detection_revision": detection_revision,
        "dino_review_proposals_revision": dino_rows_revision,
        "parent_candidate_ids": [parent_candidate_id],
        "evidence_proposal_ids": [dino_proposal_id],
        "peak_dino_proposal_id": dino_proposal_id,
        "peak_evidence_candidate_id": evidence_candidate_id,
        "peak_evidence_image_sha256": "8" * 64,
        "peak_evidence_timestamp_seconds": 10.7,
        "peak_normalized_box": dino_row["normalized_box"],
        "review_only": True,
    }
    synthetic = resolved_candidate(
        synthetic_candidate_id,
        start=10.7,
        end=10.7,
        timestamp=10.7,
        issue_type="guide_sign_identity",
        urgent=False,
        image_sha="8" * 64,
        clip_sha="9" * 64,
        representative_proposal_provenance=provenance,
    )
    snapshot = evidence_snapshot(candidates=(synthetic,))
    group = {
        "proposal_id": "tip_seed_dino_001",
        "representative_candidate_id": synthetic_candidate_id,
        "evidence_candidate_ids": [synthetic_candidate_id],
        "issue_type": "guide_sign_identity",
        "start_seconds": 10.7,
        "end_seconds": 10.7,
        "detection_count": 1,
    }
    dino_temporal_group = {
        "synthetic_candidate_id": synthetic_candidate_id,
        "temporal_proposal_id": "tip_seed_dino_001",
        "issue_type": "guide_sign_identity",
        "start_seconds": 10.7,
        "end_seconds": 10.7,
        "merged_frame_count": 1,
        "evidence_proposal_ids": [dino_proposal_id],
        "evidence_frame_ids": [evidence_candidate_id],
        "sam2_prompt_candidate_ids": [prompt_candidate_id],
        "peak_evidence": {
            "dino_proposal_id": dino_proposal_id,
            "evidence_candidate_id": evidence_candidate_id,
            "image_sha256": "8" * 64,
            "normalized_box": dino_row["normalized_box"],
            "parent_candidate_id": parent_candidate_id,
            "score": 0.61,
            "timestamp_seconds": 10.7,
        },
        "review_only": True,
        "automatic_annotation_allowed": False,
        "automatic_incident_creation_allowed": False,
    }
    temporal_overlay = {
        "overlay_id": OVERLAY_ID,
        "project_id": str(PROJECT_ID),
        "analysis_job_id": str(ANALYSIS_ID),
        "pipeline_job_id": str(PIPELINE_ID),
        "source_sha256": SOURCE_SHA,
        "candidate_count": 1,
        "review_only": True,
    }
    temporal_core = {
        "analysis_job_id": str(ANALYSIS_ID),
        "evaluation_id": "seed-dino-evaluation",
        "detection_revision": detection_revision,
        "question_candidate_ids": [synthetic_candidate_id],
        "groups": [group],
        "dino_temporal_groups_by_candidate": {
            synthetic_candidate_id: dino_temporal_group
        },
        "dino_candidate_evidence_overlay": temporal_overlay,
    }
    checkpoints = build_checkpoints(
        question_ids=[synthetic_candidate_id],
        groups=[group],
    )
    checkpoints["dino_checkpoint"] = {
        **base_identity(),
        **detection_payload,
        "detection_revision": detection_revision,
        "dino_review_proposals_by_candidate": dino_rows,
        "proposal_sources": ["grounding_dino_review_sidecar"],
        "dino_box_proposal_status": "completed",
        "dedicated_detector_status": "model_unavailable",
        "automatic_labeling_allowed": False,
    }
    checkpoints["ocr_checkpoint"] = {
        **base_identity(),
        "ocr_revision": "1" * 64,
        "ocr_status": "failed",
        "ocr_candidate_ids": [],
        "ocr_observations_by_candidate": {},
        "ocr_provenance": {
            "schema_version": (
                "trafficvisson.pipeline-ocr-provenance.v1"
            ),
            "engine_role": "ocr_only",
            "input_revision": "0" * 64,
            "result_revision": "1" * 64,
            "review_only": True,
            "path_exposed": False,
        },
        "automatic_annotation_allowed": False,
    }
    sam2 = sam2_checkpoint([prompt_candidate_id])
    sam2_proposal_sha = sam2["proposals"][0]["proposal_sha256"]
    sam2["dino_prompt_bindings"] = [
        {
            "dino_proposal_id": dino_proposal_id,
            "parent_candidate_id": parent_candidate_id,
            "prompt_candidate_id": prompt_candidate_id,
            "evidence_candidate_id": evidence_candidate_id,
            "evidence_image_sha256": "8" * 64,
            "evidence_timestamp_seconds": 10.7,
            "issue_type": "guide_sign_identity",
            "normalized_box": dino_row["normalized_box"],
            "score": 0.61,
            "sam2_status": "available",
            "sam2_proposal_sha256": sam2_proposal_sha,
            "sam2_error_code": None,
            "review_only": True,
        }
    ]
    sam2["dino_prompt_count"] = 1
    sam2["dino_mask_count"] = 1
    sam2_payload = dict(sam2)
    sam2_payload.pop("sam2_revision")
    sam2["sam2_revision"] = sha(sam2_payload)
    checkpoints["sam2_checkpoint"] = sam2
    checkpoints["temporal_checkpoint"] = {
        **base_identity(),
        **temporal_core,
        "dino_candidate_evidence_overlay_id": OVERLAY_ID,
        "temporal_merge_revision": sha(temporal_core),
        "automatic_incident_creation_allowed": False,
    }
    structured = dict(
        checkpoints["structured_proposal_checkpoint"]
    )
    structured["detection_revision"] = detection_revision
    structured["temporal_merge_revision"] = sha(temporal_core)
    structured["structured_vision_revision"] = sha(
        {
            "pipeline_job_id": str(PIPELINE_ID),
            "analysis_job_id": str(ANALYSIS_ID),
            "source_sha256": SOURCE_SHA,
            "detection_revision": detection_revision,
            "temporal_merge_revision": sha(temporal_core),
            "visual_reference_revision": checkpoints[
                "similar_case_checkpoint"
            ]["visual_reference_revision"],
            "provider": structured["selected_provider"],
            "provider_state": structured["provider_state"],
            "transmission_consent": structured[
                "transmission_consent"
            ],
            "judgments": structured[
                "structured_judgments_by_candidate"
            ],
        }
    )
    checkpoints["structured_proposal_checkpoint"] = structured
    return snapshot, checkpoints


def provider_visual_preparation(candidate_id=CANDIDATE_B):
    attachments = [
        {
            "order": index,
            "role": (
                "candidate_representative"
                if index == 0
                else "similar_case"
            ),
            "source_ref": (
                f"deid:{candidate_id}:provider-crop"
                if index == 0
                else f"reference:sample-3q:case-{index:03d}"
            ),
            "source_sha256": f"{index + 1:064x}",
            "output_sha256": f"{index + 11:064x}",
            "media_type": "image/png",
            "size_bytes": 1_024 + index,
            "width": 640,
            "height": 360,
            "face_detection_count": 0,
            "plate_detection_count": 1,
        }
        for index in range(4)
    ]
    row = {
        "schema_version": (
            "trafficvisson.provider-visual-evidence-preparation.v1"
        ),
        "project_id": str(PROJECT_ID),
        "pipeline_job_id": str(PIPELINE_ID),
        "analysis_job_id": str(ANALYSIS_ID),
        "candidate_id": candidate_id,
        "preparation_id": "c" * 24,
        "input_revision": "1" * 64,
        "state": "verified_for_provider_transmission",
        "manifest_sha256": "2" * 64,
        "engine_identity_sha256": "3" * 64,
        "attachment_count": len(attachments),
        "attachments": attachments,
        "automatic_redaction_performed": True,
        "automatic_privacy_verification": False,
        "human_verified": True,
        "review_required": False,
        "review_record_sha256": "4" * 64,
        "path_exposed": False,
    }
    row["preparation_revision"] = sha(row)
    return row


def bind_provider_visual_evidence(checkpoints, visual_evidence):
    structured = dict(
        checkpoints["structured_proposal_checkpoint"]
    )
    structured[
        "provider_visual_evidence_by_candidate"
    ] = visual_evidence
    core = {
        "pipeline_job_id": str(PIPELINE_ID),
        "analysis_job_id": str(ANALYSIS_ID),
        "source_sha256": SOURCE_SHA,
        "detection_revision": structured["detection_revision"],
        "temporal_merge_revision": structured[
            "temporal_merge_revision"
        ],
        "visual_reference_revision": checkpoints[
            "similar_case_checkpoint"
        ]["visual_reference_revision"],
        "provider": structured["selected_provider"],
        "provider_state": structured["provider_state"],
        "transmission_consent": structured[
            "transmission_consent"
        ],
        "provider_visual_evidence": visual_evidence,
        "judgments": structured[
            "structured_judgments_by_candidate"
        ],
    }
    structured["structured_vision_revision"] = sha(core)
    checkpoints["structured_proposal_checkpoint"] = structured
    return checkpoints


def make_store(
    tmp_path: Path,
    snapshot: ResolvedReviewCandidateEvidenceSet | None = None,
) -> tuple[ReviewEventSetStore, MutableResolver]:
    resolver = MutableResolver(snapshot or evidence_snapshot())
    store = ReviewEventSetStore(
        (tmp_path / "review-events").resolve(),
        evidence_resolver=resolver,
    )
    return store, resolver


def materialize_completed(
    store: ReviewEventSetStore,
    **checkpoint_overrides: dict[str, Any],
):
    checkpoints = build_checkpoints()
    checkpoints.update(checkpoint_overrides)
    return store.materialize(
        project_id=PROJECT_ID,
        analysis_job_id=ANALYSIS_ID,
        pipeline_job_id=PIPELINE_ID,
        overlay_id=OVERLAY_ID,
        source_sha256=SOURCE_SHA,
        **checkpoints,
    )


def test_materializes_one_path_free_event_per_temporal_group(
    tmp_path: Path,
) -> None:
    store, resolver = make_store(tmp_path)

    result = materialize_completed(store)

    assert result.status == "completed"
    assert result.idempotent_replay is False
    assert result.annotations_created == 0
    assert result.incidents_created == 0
    assert result.training_members_created == 0
    assert result.report_truth_records_created == 0
    assert len(result.events) == 1
    event = result.events[0]
    assert event.representative_candidate_id == CANDIDATE_B
    assert event.evidence_candidate_ids == (
        CANDIDATE_A,
        CANDIDATE_B,
    )
    assert event.merged_frame_count == 2
    assert event.urgent_review is True
    assert event.representative_image.artifact_ref.startswith(
        f"candidate-evidence:{OVERLAY_ID}:cea_"
    )
    assert event.preview_clip.artifact_ref.startswith(
        f"candidate-evidence:{OVERLAY_ID}:cea_"
    )
    assert {
        item["text"]: item["source_candidate_ids"]
        for item in event.ocr_observations
    } == {
        "중부": [CANDIDATE_A],
        "중부터널": [CANDIDATE_B],
    }
    assert event.sam2_summary["status"] == "available"
    assert event.similar_cases[0]["case_id"] == "case-001"
    assert event.structured_proposal["status"] == "proposed"
    public_json = canonical(result.to_checkpoint()).decode("utf-8")
    event_json = canonical(
        {
            "image": event.representative_image.__dict__,
            "clip": event.preview_clip.__dict__,
            "sam2": event.sam2_summary,
            "ocr": event.ocr_observations,
        }
    ).decode("utf-8")
    assert '"path"' not in public_json
    assert "relative_path" not in event_json
    assert "geometry" not in event_json
    assert resolver.calls >= 2  # materialize + persisted reload verification


def test_batch_binding_56_candidates_loads_full_event_set_once(
    tmp_path: Path,
) -> None:
    candidate_ids = [
        CANDIDATE_A,
        CANDIDATE_B,
        *[f"{index + 1:020x}" for index in range(54)],
    ]
    candidates = tuple(
        resolved_candidate(
            candidate_id,
            start=10.0,
            end=11.0,
            timestamp=10.7,
            issue_type="pothole",
            urgent=False,
            image_sha=sha([candidate_id, "image"]),
            clip_sha=sha([candidate_id, "clip"]),
        )
        for candidate_id in candidate_ids
    )
    snapshot = evidence_snapshot(candidates=candidates)
    store, resolver = make_store(tmp_path, snapshot)
    groups = [
        {
            "proposal_id": f"proposal-{index:03d}",
            "representative_candidate_id": candidate_id,
            "evidence_candidate_ids": [candidate_id],
            "issue_type": "pothole",
            "start_seconds": 10.0,
            "end_seconds": 11.0,
            "detection_count": 1,
        }
        for index, candidate_id in enumerate(candidate_ids)
    ]
    checkpoints = build_checkpoints(
        question_ids=candidate_ids,
        groups=groups,
    )
    store.materialize(
        project_id=PROJECT_ID,
        analysis_job_id=ANALYSIS_ID,
        pipeline_job_id=PIPELINE_ID,
        overlay_id=OVERLAY_ID,
        source_sha256=SOURCE_SHA,
        **checkpoints,
    )
    resolver.calls = 0

    bindings = store.resolve_candidate_bindings_batch(
        PROJECT_ID,
        ANALYSIS_ID,
        PIPELINE_ID,
        candidate_ids,
    )

    assert [
        item.event.representative_candidate_id
        for item in bindings
    ] == candidate_ids
    assert resolver.calls == 1
    assert all(
        item.candidate_metadata["candidate_id"]
        == item.event.representative_candidate_id
        for item in bindings
    )


def test_question_catalog_projection_consumes_atomic_review_batch(
    tmp_path: Path,
) -> None:
    store, resolver = make_store(tmp_path)
    event_set = materialize_completed(store)
    event = event_set.events[0]

    def forbidden_item_resolver(*_args):
        raise AssertionError("per-candidate resolver must not run")

    labels = CandidateLabelStore(
        (tmp_path / "labels").resolve(),
        forbidden_item_resolver,
    )
    projected = []

    def project(
        project_id,
        analysis_job_id,
        source_sha256,
        questions,
    ):
        projected.extend(questions)
        return {
            "project_id": str(project_id),
            "analysis_job_id": str(analysis_job_id),
            "source_sha256": source_sha256,
            "question_count": len(questions),
            "question_set_sha256": sha(questions),
        }

    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        candidate_labels=labels,
        question_catalog_projector=project,
        review_event_sets=store,
    )
    resolver.calls = 0

    state, revision = runtime._register_question_catalog(
        {"project_id": str(PROJECT_ID)},
        analysis_job_id=str(ANALYSIS_ID),
        source_sha256=SOURCE_SHA,
        candidate_ids=[event.representative_candidate_id],
        review_event_set=event_set.to_checkpoint(),
        review_event_bindings=[
            {
                "event_id": event.event_id,
                "representative_candidate_id": (
                    event.representative_candidate_id
                ),
                "event_revision": event.event_revision,
            }
        ],
    )

    assert state == "registered"
    assert revision == sha(projected)
    assert resolver.calls == 1
    assert len(projected) == 1
    binding = projected[0]["ai_original_candidate"][
        "review_event_binding"
    ]
    assert binding["review_event_set_id"] == (
        event_set.review_event_set_id
    )
    assert binding["review_event_id"] == event.event_id
    assert binding["review_only"] is True


def test_sam2_revision_ignores_pipeline_artifact_root_envelope(
    tmp_path: Path,
) -> None:
    store, _resolver = make_store(tmp_path)
    checkpoint = sam2_checkpoint([CANDIDATE_A, CANDIDATE_B])
    checkpoint["artifact_root_binding"] = {
        "output_location_id": "project-default",
        "registry_revision": 1,
        "root_identity_sha256": "1" * 64,
        "marker_sha256": "2" * 64,
    }

    result = materialize_completed(
        store,
        sam2_checkpoint=checkpoint,
    )

    assert result.status == "completed"
    assert result.annotations_created == 0
    assert result.incidents_created == 0


def test_structured_revision_roundtrips_empty_provider_visual_evidence(
    tmp_path: Path,
) -> None:
    store, _resolver = make_store(tmp_path)
    checkpoints = bind_provider_visual_evidence(
        build_checkpoints(), {}
    )

    result = store.materialize(
        project_id=PROJECT_ID,
        analysis_job_id=ANALYSIS_ID,
        pipeline_job_id=PIPELINE_ID,
        overlay_id=OVERLAY_ID,
        source_sha256=SOURCE_SHA,
        **checkpoints,
    )

    assert result.status == "completed"
    assert len(result.events) == 1


def test_structured_revision_roundtrips_verified_codex_visual_manifest(
    tmp_path: Path,
) -> None:
    store, _resolver = make_store(tmp_path)
    checkpoints = bind_provider_visual_evidence(
        build_checkpoints(),
        {CANDIDATE_B: provider_visual_preparation()},
    )

    result = store.materialize(
        project_id=PROJECT_ID,
        analysis_job_id=ANALYSIS_ID,
        pipeline_job_id=PIPELINE_ID,
        overlay_id=OVERLAY_ID,
        source_sha256=SOURCE_SHA,
        **checkpoints,
    )

    assert result.status == "completed"
    assert len(result.events) == 1


def test_structured_revision_rejects_tampered_codex_visual_manifest(
    tmp_path: Path,
) -> None:
    store, _resolver = make_store(tmp_path)
    visual = provider_visual_preparation()
    visual["preparation_revision"] = "f" * 64
    checkpoints = bind_provider_visual_evidence(
        build_checkpoints(),
        {CANDIDATE_B: visual},
    )

    with pytest.raises(
        ReviewEventSetIntegrityError,
        match="provider visual evidence",
    ):
        store.materialize(
            project_id=PROJECT_ID,
            analysis_job_id=ANALYSIS_ID,
            pipeline_job_id=PIPELINE_ID,
            overlay_id=OVERLAY_ID,
            source_sha256=SOURCE_SHA,
            **checkpoints,
        )


def test_materialization_is_immutable_and_idempotent(
    tmp_path: Path,
) -> None:
    store, _resolver = make_store(tmp_path)

    first = materialize_completed(store)
    second = materialize_completed(store)

    assert second.review_event_set_id == first.review_event_set_id
    assert second.review_event_set_revision == first.review_event_set_revision
    assert second.manifest_sha256 == first.manifest_sha256
    assert second.created_at == first.created_at
    assert second.idempotent_replay is True
    manifests = list(
        (tmp_path / "review-events").rglob("manifest.json")
    )
    assert len(manifests) == 1


def test_get_list_and_resolve_event_reverify_evidence(
    tmp_path: Path,
) -> None:
    store, resolver = make_store(tmp_path)
    created = materialize_completed(store)

    fetched = store.get(
        PROJECT_ID,
        ANALYSIS_ID,
        PIPELINE_ID,
        OVERLAY_ID,
        created.review_event_set_id,
    )
    listed = store.list(
        PROJECT_ID,
        ANALYSIS_ID,
        PIPELINE_ID,
        OVERLAY_ID,
    )
    resolved = store.resolve_event(
        PROJECT_ID,
        ANALYSIS_ID,
        PIPELINE_ID,
        OVERLAY_ID,
        created.review_event_set_id,
        created.events[0].event_id,
    )

    assert fetched.idempotent_replay is True
    assert [item.review_event_set_id for item in listed] == [
        created.review_event_set_id
    ]
    assert resolved == created.events[0]
    assert resolver.calls >= 5


def test_create_app_resolves_base_candidate_from_composite_review_set(
    tmp_path: Path,
) -> None:
    active_candidate = resolved_candidate(
        CANDIDATE_A,
        start=10.0,
        end=10.5,
        timestamp=10.25,
        issue_type="pothole",
        urgent=True,
        image_sha="8" * 64,
        clip_sha="9" * 64,
    )
    base_metadata = candidate_metadata(
        CANDIDATE_B,
        start=10.4,
        end=11.0,
        timestamp=10.7,
        issue_type="pothole",
        urgent=False,
        image_sha="a" * 64,
        clip_sha="b" * 64,
    )
    base_metadata["evidence_overlay"] = {
        **base_metadata["evidence_overlay"],
        "overlay_id": BASE_OVERLAY_ID,
        "manifest_sha256": BASE_OVERLAY_MANIFEST_SHA,
        "candidate_set_metadata_sha256": (
            BASE_CANDIDATE_SET_METADATA_SHA
        ),
    }
    for key in ("representative_image", "preview_clip"):
        base_metadata[key] = {
            **base_metadata[key],
            "artifact_ref": base_metadata[key]["artifact_ref"].replace(
                OVERLAY_ID,
                BASE_OVERLAY_ID,
                1,
            ),
        }
    base_candidate = ResolvedReviewCandidateEvidence(
        candidate_id=CANDIDATE_B,
        candidate_metadata_sha256=sha(base_metadata),
        metadata=base_metadata,
    )
    members = sorted(
        [
            {
                "overlay_id": OVERLAY_ID,
                "manifest_sha256": OVERLAY_MANIFEST_SHA,
                "candidate_set_metadata_sha256": (
                    CANDIDATE_SET_METADATA_SHA
                ),
                "candidate_count": 1,
            },
            {
                "overlay_id": BASE_OVERLAY_ID,
                "manifest_sha256": BASE_OVERLAY_MANIFEST_SHA,
                "candidate_set_metadata_sha256": (
                    BASE_CANDIDATE_SET_METADATA_SHA
                ),
                "candidate_count": 1,
            },
        ],
        key=lambda item: item["overlay_id"],
    )
    candidates = tuple(
        sorted(
            (active_candidate, base_candidate),
            key=lambda item: item.candidate_id,
        )
    )
    snapshot = ResolvedReviewCandidateEvidenceSet(
        project_id=PROJECT_ID,
        analysis_job_id=ANALYSIS_ID,
        pipeline_job_id=PIPELINE_ID,
        overlay_id=OVERLAY_ID,
        source_sha256=SOURCE_SHA,
        overlay_manifest_sha256=sha(members),
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
        overlay_members=tuple(members),
    )
    store, _resolver = make_store(tmp_path / "store", snapshot)
    checkpoints = build_checkpoints(
        question_ids=[CANDIDATE_A, CANDIDATE_B],
        groups=[
            {
                "proposal_id": "proposal-a",
                "representative_candidate_id": CANDIDATE_A,
                "evidence_candidate_ids": [CANDIDATE_A],
                "issue_type": "pothole",
                "start_seconds": 10.0,
                "end_seconds": 10.5,
                "detection_count": 1,
            },
            {
                "proposal_id": "proposal-b",
                "representative_candidate_id": CANDIDATE_B,
                "evidence_candidate_ids": [CANDIDATE_B],
                "issue_type": "pothole",
                "start_seconds": 10.4,
                "end_seconds": 11.0,
                "detection_count": 1,
            },
        ],
    )
    created = store.materialize(
        project_id=PROJECT_ID,
        analysis_job_id=ANALYSIS_ID,
        pipeline_job_id=PIPELINE_ID,
        overlay_id=OVERLAY_ID,
        source_sha256=SOURCE_SHA,
        **checkpoints,
    )

    found = store.find_candidate_binding(
        PROJECT_ID,
        ANALYSIS_ID,
        PIPELINE_ID,
        CANDIDATE_B,
    )
    assert found is not None
    found_set, found_event = found
    assert found_set.review_event_set_id == created.review_event_set_id
    assert found_event.representative_candidate_id == CANDIDATE_B
    event_by_candidate = {
        event.representative_candidate_id: event
        for event in created.events
    }
    assert set(event_by_candidate) == {CANDIDATE_A, CANDIDATE_B}

    class BaseCandidateEvidence:
        def resolve_candidate(
            self,
            project_id: UUID,
            analysis_job_id: UUID,
            candidate_id: str,
        ) -> dict[str, Any]:
            assert project_id == PROJECT_ID
            assert analysis_job_id == ANALYSIS_ID
            metadata_by_candidate = {
                CANDIDATE_A: dict(active_candidate.metadata),
                CANDIDATE_B: dict(base_metadata),
            }
            assert candidate_id in metadata_by_candidate
            return {
                "status": "completed",
                "project_id": str(PROJECT_ID),
                "analysis_job_id": str(ANALYSIS_ID),
                "source_sha256": SOURCE_SHA,
                "candidate_id": candidate_id,
                "candidate_source": "candidate_evidence_overlay",
                "candidate_metadata_sha256": (
                    sha(metadata_by_candidate[candidate_id])
                ),
                "metadata": metadata_by_candidate[candidate_id],
            }

    app_data = (tmp_path / "app-data").resolve()
    analysis_fixture = _write_completed_analysis(
        app_data,
        project_id=PROJECT_ID,
        job_id=ANALYSIS_ID,
        source_seed="composite-review-set",
        candidate_count=2,
    )
    analysis_fixture["payload"]["source_sha256"] = SOURCE_SHA
    analysis_fixture["payload"]["result"]["provenance"].update(
        {
            "source_fingerprint": SOURCE_SHA,
            "source_sha256": SOURCE_SHA,
        }
    )
    analysis_body = canonical(analysis_fixture["payload"])
    analysis_fixture["record_path"].write_bytes(analysis_body)
    analysis_fixture["digest_path"].write_text(
        hashlib.sha256(analysis_body).hexdigest(),
        encoding="ascii",
    )
    app = create_app(
        "r" * 64,
        fixture_mode=True,
        allow_test_client=True,
        bridge_data_root=str(app_data),
    )
    app.state.candidate_evidence = BaseCandidateEvidence()
    app.state.review_event_sets = store
    questions = [
        app.state.candidate_labels.resolve_question(
            PROJECT_ID,
            ANALYSIS_ID,
            candidate_id,
        )
        for candidate_id in (CANDIDATE_A, CANDIDATE_B)
    ]
    question = questions[1]
    original = question["ai_original_candidate"]
    assert original["evidence_overlay"]["overlay_id"] == BASE_OVERLAY_ID
    assert original["review_event_binding"] == {
        "schema_version": "trafficvisson.review-event-binding.v1",
        "review_event_set_id": created.review_event_set_id,
        "review_event_set_revision": (
            created.review_event_set_revision
        ),
        "review_event_set_manifest_sha256": created.manifest_sha256,
        "review_event_id": event_by_candidate[CANDIDATE_B].event_id,
        "review_event_revision": (
            event_by_candidate[CANDIDATE_B].event_revision
        ),
        "representative_candidate_id": CANDIDATE_B,
        "review_only": True,
    }
    registered = app.state.store.register_label_questions(
        PROJECT_ID,
        ANALYSIS_ID,
        SOURCE_SHA,
        questions,
    )
    assert registered["created_count"] == 2
    assert registered["overlay_binding_count"] == 2
    assert app.state.store.list_current_annotations(PROJECT_ID) == []
    assert app.state.store.list_incidents(
        PROJECT_ID,
        analysis_job_id=ANALYSIS_ID,
    ) == []

    restarted = create_app(
        "r" * 64,
        fixture_mode=True,
        allow_test_client=True,
        bridge_data_root=str(app_data),
    )
    restarted.state.candidate_evidence = BaseCandidateEvidence()
    restarted.state.review_event_sets = store
    replay_questions = [
        restarted.state.candidate_labels.resolve_question(
            PROJECT_ID,
            ANALYSIS_ID,
            candidate_id,
        )
        for candidate_id in (CANDIDATE_A, CANDIDATE_B)
    ]
    replay = restarted.state.store.register_label_questions(
        PROJECT_ID,
        ANALYSIS_ID,
        SOURCE_SHA,
        replay_questions,
    )
    assert replay["created_count"] == 0
    assert (
        replay["evidence_binding_sha256"]
        == registered["evidence_binding_sha256"]
    )
    assert restarted.state.store.list_current_annotations(
        PROJECT_ID
    ) == []
    assert restarted.state.store.list_incidents(
        PROJECT_ID,
        analysis_job_id=ANALYSIS_ID,
    ) == []
    answer = app.state.candidate_labels.record_answer(
        PROJECT_ID,
        ANALYSIS_ID,
        CANDIDATE_B,
        answer="yes",
        selected_issue_type="pothole",
        severity="moderate",
        answered_by="local_user",
    )
    assert answer["ai_original_candidate"]["candidate_id"] == CANDIDATE_B
    assert "metadata" not in answer["ai_original_candidate"]
    assert (
        answer["ai_original_candidate"]["review_event_binding"]
        == original["review_event_binding"]
    )


def test_zero_and_failed_are_distinct_durable_statuses(
    tmp_path: Path,
) -> None:
    empty = evidence_snapshot(candidates=())
    zero_store, _ = make_store(tmp_path / "zero", empty)
    zero_checkpoints = build_checkpoints(question_ids=[], groups=[])

    zero = zero_store.materialize(
        project_id=PROJECT_ID,
        analysis_job_id=ANALYSIS_ID,
        pipeline_job_id=PIPELINE_ID,
        overlay_id=OVERLAY_ID,
        source_sha256=SOURCE_SHA,
        status="zero",
        **zero_checkpoints,
    )
    assert zero.status == "zero"
    assert zero.error_code is None
    assert zero.events == ()

    failed_store, _ = make_store(tmp_path / "failed")
    failed = failed_store.materialize(
        project_id=PROJECT_ID,
        analysis_job_id=ANALYSIS_ID,
        pipeline_job_id=PIPELINE_ID,
        overlay_id=OVERLAY_ID,
        source_sha256=SOURCE_SHA,
        status="failed",
        error_code="temporal_merge_failed",
        **zero_checkpoints,
    )
    assert failed.status == "failed"
    assert failed.error_code == "temporal_merge_failed"
    assert failed.events == ()
    assert failed.review_event_set_id != zero.review_event_set_id


def test_degraded_question_queue_is_partial_not_completed(
    tmp_path: Path,
) -> None:
    store, _resolver = make_store(tmp_path)
    checkpoints = build_checkpoints()
    checkpoints["sam2_checkpoint"] = sam2_checkpoint([])

    result = store.materialize(
        project_id=PROJECT_ID,
        analysis_job_id=ANALYSIS_ID,
        pipeline_job_id=PIPELINE_ID,
        overlay_id=OVERLAY_ID,
        source_sha256=SOURCE_SHA,
        **checkpoints,
    )

    assert result.status == "partial"
    assert result.error_code == "degraded_review_capabilities"
    assert len(result.events) == 1
    assert result.events[0].sam2_summary["status"] == "not_requested"


def test_tampered_manifest_fails_closed(tmp_path: Path) -> None:
    store, _resolver = make_store(tmp_path)
    created = materialize_completed(store)
    manifest = (
        tmp_path
        / "review-events"
        / str(PROJECT_ID)
        / str(ANALYSIS_ID)
        / str(PIPELINE_ID)
        / OVERLAY_ID
        / created.review_event_set_id
        / "manifest.json"
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["status"] = "zero"
    manifest.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ReviewEventSetIntegrityError):
        store.get(
            PROJECT_ID,
            ANALYSIS_ID,
            PIPELINE_ID,
            OVERLAY_ID,
            created.review_event_set_id,
        )


def test_rehashed_malformed_event_summary_still_fails_closed(
    tmp_path: Path,
) -> None:
    store, _resolver = make_store(tmp_path)
    created = materialize_completed(store)
    manifest = (
        tmp_path
        / "review-events"
        / str(PROJECT_ID)
        / str(ANALYSIS_ID)
        / str(PIPELINE_ID)
        / OVERLAY_ID
        / created.review_event_set_id
        / "manifest.json"
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    event = payload["events"][0]
    event["detector_summary"]["proposal_sources"] = ["../renderer-path"]
    event_without_revision = dict(event)
    event_without_revision.pop("event_revision")
    event["event_revision"] = sha(event_without_revision)
    identity_keys = (
        "schema_version",
        "project_id",
        "analysis_job_id",
        "pipeline_job_id",
        "overlay_id",
        "source_sha256",
        "overlay_manifest_sha256",
        "candidate_set_metadata_sha256",
        "candidate_evidence_revision",
        "status",
        "error_code",
        "checkpoint_revisions",
        "checkpoint_record_sha256",
    )
    set_identity = {key: payload[key] for key in identity_keys}
    payload["review_event_set_revision"] = sha(
        {
            **set_identity,
            "events": payload["events"],
            "review_only": True,
            "annotations_created": 0,
            "incidents_created": 0,
            "training_members_created": 0,
            "report_truth_records_created": 0,
        }
    )
    payload_without_record = dict(payload)
    payload_without_record.pop("record_sha256")
    payload["record_sha256"] = sha(payload_without_record)
    manifest.write_bytes(canonical(payload))

    with pytest.raises(
        ReviewEventSetIntegrityError,
        match="detector summary",
    ):
        store.get(
            PROJECT_ID,
            ANALYSIS_ID,
            PIPELINE_ID,
            OVERLAY_ID,
            created.review_event_set_id,
        )


def test_changed_artifact_hash_fails_fresh_get(tmp_path: Path) -> None:
    store, resolver = make_store(tmp_path)
    created = materialize_completed(store)
    old = resolver.snapshot.candidates[1]
    metadata = dict(old.metadata)
    image = dict(metadata["representative_image"])
    image["sha256"] = "0" * 64
    metadata["representative_image"] = image
    changed = replace(
        old,
        candidate_metadata_sha256=sha(metadata),
        metadata=metadata,
    )
    resolver.snapshot = replace(
        resolver.snapshot,
        candidates=(resolver.snapshot.candidates[0], changed),
    )

    with pytest.raises(
        ReviewEventSetIntegrityError,
        match="changed after review-event materialization",
    ):
        store.get(
            PROJECT_ID,
            ANALYSIS_ID,
            PIPELINE_ID,
            OVERLAY_ID,
            created.review_event_set_id,
        )


@pytest.mark.parametrize(
    "checkpoint_name,revision_key",
    [
        ("dino_checkpoint", "detection_revision"),
        ("ocr_checkpoint", "ocr_revision"),
        ("temporal_checkpoint", "temporal_merge_revision"),
        ("sam2_checkpoint", "sam2_revision"),
        (
            "structured_proposal_checkpoint",
            "structured_vision_revision",
        ),
    ],
)
def test_checkpoint_revision_tampering_fails_closed(
    tmp_path: Path,
    checkpoint_name: str,
    revision_key: str,
) -> None:
    store, _resolver = make_store(tmp_path)
    checkpoints = build_checkpoints()
    checkpoint = dict(checkpoints[checkpoint_name])
    checkpoint[revision_key] = "0" * 64
    checkpoints[checkpoint_name] = checkpoint

    with pytest.raises(ReviewEventSetIntegrityError, match="revision"):
        store.materialize(
            project_id=PROJECT_ID,
            analysis_job_id=ANALYSIS_ID,
            pipeline_job_id=PIPELINE_ID,
            overlay_id=OVERLAY_ID,
            source_sha256=SOURCE_SHA,
            **checkpoints,
        )


def test_cross_source_or_candidate_binding_fails_closed(
    tmp_path: Path,
) -> None:
    store, _resolver = make_store(tmp_path)
    checkpoints = build_checkpoints()
    ocr = dict(checkpoints["ocr_checkpoint"])
    ocr["source_sha256"] = "0" * 64
    checkpoints["ocr_checkpoint"] = ocr

    with pytest.raises(ReviewEventSetConflict, match="source_sha256"):
        store.materialize(
            project_id=PROJECT_ID,
            analysis_job_id=ANALYSIS_ID,
            pipeline_job_id=PIPELINE_ID,
            overlay_id=OVERLAY_ID,
            source_sha256=SOURCE_SHA,
            **checkpoints,
        )

    checkpoints = build_checkpoints()
    ocr = dict(checkpoints["ocr_checkpoint"])
    ocr["ocr_candidate_ids"] = ["c" * 20]
    checkpoints["ocr_checkpoint"] = ocr
    with pytest.raises(ReviewEventSetConflict, match="foreign candidate"):
        store.materialize(
            project_id=PROJECT_ID,
            analysis_job_id=ANALYSIS_ID,
            pipeline_job_id=PIPELINE_ID,
            overlay_id=OVERLAY_ID,
            source_sha256=SOURCE_SHA,
            **checkpoints,
        )


def test_seed_only_dino_temporal_lineage_materializes_review_event(
    tmp_path: Path,
) -> None:
    snapshot, checkpoints = dino_seed_temporal_fixture()
    store, _resolver = make_store(tmp_path, snapshot)

    result = store.materialize(
        project_id=PROJECT_ID,
        analysis_job_id=ANALYSIS_ID,
        pipeline_job_id=PIPELINE_ID,
        overlay_id=OVERLAY_ID,
        source_sha256=SOURCE_SHA,
        **checkpoints,
    )

    assert result.status == "partial"
    assert len(result.events) == 1
    assert result.events[0].representative_candidate_id == CANDIDATE_B
    assert result.events[0].detector_summary[
        "candidate_was_proposed"
    ] is True
    assert result.events[0].sam2_summary["status"] == "available"
    assert result.annotations_created == 0
    assert result.incidents_created == 0
    assert result.training_members_created == 0
    assert result.report_truth_records_created == 0

    tampered = dict(checkpoints)
    dino = dict(tampered["dino_checkpoint"])
    dino["proposal_candidate_ids"] = ["f" * 20]
    payload = {
        key: dino.get(key)
        for key in (
            "analysis_job_id",
            "source_sha256",
            "local_inference_revision",
            "dino_revision",
            "dino_evidence_source",
            "high_frequency_evidence_count",
            "high_frequency_manifest_sha256",
            "proposal_candidate_ids",
            "detected_issue_types",
            "ocr_candidate_ids",
            "dino_review_proposals_revision",
        )
    }
    dino["detection_revision"] = sha(payload)
    tampered["dino_checkpoint"] = dino
    with pytest.raises(
        (ReviewEventSetConflict, ReviewEventSetIntegrityError)
    ):
        store.materialize(
            project_id=PROJECT_ID,
            analysis_job_id=ANALYSIS_ID,
            pipeline_job_id=PIPELINE_ID,
            overlay_id=OVERLAY_ID,
            source_sha256=SOURCE_SHA,
            **tampered,
        )


def test_dino_temporal_prompt_maps_when_parent_is_resolved_evidence(
    tmp_path: Path,
) -> None:
    """A sibling overlay may expose both the parent and synthetic event.

    The SAM2 binding still belongs to the uniquely matching synthetic DINO
    temporal group.  Treating a known parent as direct-only would drop that
    binding and fail the final review queue despite an exact prompt match.
    """

    snapshot, checkpoints = dino_seed_temporal_fixture()
    parent = resolved_candidate(
        CANDIDATE_A,
        start=10.0,
        end=10.5,
        timestamp=10.25,
        issue_type="guide_sign_identity",
        urgent=False,
        image_sha="2" * 64,
        clip_sha="3" * 64,
    )
    combined_snapshot = evidence_snapshot(
        candidates=(parent, *snapshot.candidates),
    )
    store, _resolver = make_store(tmp_path, combined_snapshot)

    result = store.materialize(
        project_id=PROJECT_ID,
        analysis_job_id=ANALYSIS_ID,
        pipeline_job_id=PIPELINE_ID,
        overlay_id=OVERLAY_ID,
        source_sha256=SOURCE_SHA,
        **checkpoints,
    )

    assert result.status == "partial"
    assert len(result.events) == 1
    assert result.events[0].representative_candidate_id == CANDIDATE_B
    assert result.events[0].sam2_summary["status"] == "available"
    assert result.annotations_created == 0
    assert result.incidents_created == 0
    assert result.training_members_created == 0
    assert result.report_truth_records_created == 0


def test_seed_dino_visual_quality_binding_is_revision_normalized(
    tmp_path: Path,
) -> None:
    snapshot, checkpoints = dino_seed_temporal_fixture()
    store, _resolver = make_store(tmp_path, snapshot)
    dino = dict(checkpoints["dino_checkpoint"])
    dino["visual_evidence_quality_revision"] = "4" * 64
    checkpoints["dino_checkpoint"] = dino

    with pytest.raises(
        ReviewEventSetIntegrityError,
        match="detector checkpoint revision does not match",
    ):
        store.materialize(
            project_id=PROJECT_ID,
            analysis_job_id=ANALYSIS_ID,
            pipeline_job_id=PIPELINE_ID,
            overlay_id=OVERLAY_ID,
            source_sha256=SOURCE_SHA,
            **checkpoints,
        )


def test_nonrepresentative_temporal_candidate_is_rejected(
    tmp_path: Path,
) -> None:
    store, _resolver = make_store(tmp_path)
    checkpoints = build_checkpoints(
        question_ids=[CANDIDATE_A, CANDIDATE_B]
    )

    with pytest.raises(
        ReviewEventSetIntegrityError,
        match="non-representative",
    ):
        store.materialize(
            project_id=PROJECT_ID,
            analysis_job_id=ANALYSIS_ID,
            pipeline_job_id=PIPELINE_ID,
            overlay_id=OVERLAY_ID,
            source_sha256=SOURCE_SHA,
            **checkpoints,
        )


def test_renderer_path_geometry_and_truth_projection_are_rejected(
    tmp_path: Path,
) -> None:
    store, _resolver = make_store(tmp_path)
    checkpoints = build_checkpoints()
    dino = dict(checkpoints["dino_checkpoint"])
    dino["renderer_path"] = r"C:\user-controlled\frame.jpg"
    checkpoints["dino_checkpoint"] = dino
    with pytest.raises(
        ReviewEventSetIntegrityError,
        match="renderer paths or geometry",
    ):
        store.materialize(
            project_id=PROJECT_ID,
            analysis_job_id=ANALYSIS_ID,
            pipeline_job_id=PIPELINE_ID,
            overlay_id=OVERLAY_ID,
            source_sha256=SOURCE_SHA,
            **checkpoints,
        )

    checkpoints = build_checkpoints()
    structured = dict(checkpoints["structured_proposal_checkpoint"])
    structured["incident_count"] = 1
    checkpoints["structured_proposal_checkpoint"] = structured
    with pytest.raises(ReviewEventSetConflict, match="truth boundary"):
        store.materialize(
            project_id=PROJECT_ID,
            analysis_job_id=ANALYSIS_ID,
            pipeline_job_id=PIPELINE_ID,
            overlay_id=OVERLAY_ID,
            source_sha256=SOURCE_SHA,
            **checkpoints,
        )


def test_similar_case_bound_and_unapproved_filter(
    tmp_path: Path,
) -> None:
    store, _resolver = make_store(tmp_path)
    checkpoints = build_checkpoints()
    similar = dict(checkpoints["similar_case_checkpoint"])
    matches = dict(similar["matches_by_candidate"])
    row = dict(matches[CANDIDATE_B][0])
    matches[CANDIDATE_B] = [
        {**row, "case_id": f"case-{index:03d}"}
        for index in range(6)
    ]
    similar["matches_by_candidate"] = matches
    similar["visual_reference_revision"] = sha(
        {
            "visual_index_manifest_sha256": similar[
                "visual_index_manifest_sha256"
            ],
            "matches_by_candidate": matches,
        }
    )
    checkpoints["similar_case_checkpoint"] = similar
    structured = dict(checkpoints["structured_proposal_checkpoint"])
    core = {
        "pipeline_job_id": str(PIPELINE_ID),
        "analysis_job_id": str(ANALYSIS_ID),
        "source_sha256": SOURCE_SHA,
        "detection_revision": structured["detection_revision"],
        "temporal_merge_revision": structured[
            "temporal_merge_revision"
        ],
        "visual_reference_revision": similar[
            "visual_reference_revision"
        ],
        "provider": structured["selected_provider"],
        "provider_state": structured["provider_state"],
        "transmission_consent": structured["transmission_consent"],
        "judgments": structured[
            "structured_judgments_by_candidate"
        ],
    }
    structured["structured_vision_revision"] = sha(core)
    checkpoints["structured_proposal_checkpoint"] = structured

    with pytest.raises(ReviewEventSetLimitError, match="similar-case"):
        store.materialize(
            project_id=PROJECT_ID,
            analysis_job_id=ANALYSIS_ID,
            pipeline_job_id=PIPELINE_ID,
            overlay_id=OVERLAY_ID,
            source_sha256=SOURCE_SHA,
            **checkpoints,
        )


def test_pipeline_question_queue_binds_immutable_review_event_set(
    tmp_path: Path,
) -> None:
    store, _resolver = make_store(tmp_path)
    checkpoints = build_checkpoints()
    detection = {
        **checkpoints["dino_checkpoint"],
        **checkpoints["ocr_checkpoint"],
        "proposal_candidate_count": 2,
        "ocr_observation_count": 2,
    }
    vision = {
        **checkpoints["structured_proposal_checkpoint"],
        "structured_llm_semantics_status": "completed",
        "structured_proposal_count": 1,
        "structured_not_run_count": 0,
        "structured_failure_count": 0,
        "comparison_mode": "metadata_comparison_only",
    }
    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        review_event_sets=store,
    )
    result = runtime._prepare_question_queue(
        {
            "job_id": str(PIPELINE_ID),
            "project_id": str(PROJECT_ID),
            "profile": {
                "review_mode": "precision",
                "question_policy": "learning_assist",
            },
            "checkpoints": {
                "candidate_rescan_5_10fps": {
                    "analysis_job_id": str(ANALYSIS_ID),
                    "source_sha256": SOURCE_SHA,
                    "high_frequency_rescan_performed": True,
                    "candidate_evidence_overlay": {
                        "schema_version": (
                            "trafficvisson."
                            "candidate-evidence-overlay.v1"
                        ),
                        "overlay_id": OVERLAY_ID,
                        "project_id": str(PROJECT_ID),
                        "analysis_job_id": str(ANALYSIS_ID),
                        "pipeline_job_id": str(PIPELINE_ID),
                        "source_sha256": SOURCE_SHA,
                    },
                },
                "detection_ocr": detection,
                "sam2_segmentation_tracking": checkpoints[
                    "sam2_checkpoint"
                ],
                "temporal_merge": checkpoints[
                    "temporal_checkpoint"
                ],
                "similar_case_search": checkpoints[
                    "similar_case_checkpoint"
                ],
                "vision_proposal": vision,
            },
        }
    )

    review_set = result.checkpoint["review_event_set"]
    assert result.outcome == "success"
    assert result.checkpoint["review_event_set_state"] == "materialized"
    assert result.checkpoint["review_event_count"] == 1
    assert review_set["review_event_set_id"].startswith("res_")
    assert result.checkpoint["review_event_set_id"] == review_set[
        "review_event_set_id"
    ]
    assert result.checkpoint["review_event_set_revision"] == review_set[
        "review_event_set_revision"
    ]
    assert result.checkpoint[
        "review_event_set_manifest_sha256"
    ] == review_set["manifest_sha256"]
    assert result.checkpoint["review_event_bindings"][0][
        "representative_candidate_id"
    ] == CANDIDATE_B
    assert result.checkpoint["annotation_count"] == 0
    assert result.checkpoint["incident_count"] == 0
    assert result.checkpoint["training_membership_count"] == 0
    assert result.checkpoint["report_incident_count"] == 0
    assert len(
        store.list(
            PROJECT_ID,
            ANALYSIS_ID,
            PIPELINE_ID,
            OVERLAY_ID,
        )
    ) == 1


def test_seed_only_dino_event_registers_one_question_catalog_row(
    tmp_path: Path,
) -> None:
    snapshot, checkpoints = dino_seed_temporal_fixture()
    store, _resolver = make_store(tmp_path, snapshot)
    projected_questions: list[list[dict[str, Any]]] = []

    class SyntheticCandidateLabels:
        def resolve_question(
            self,
            project_id: UUID,
            analysis_job_id: UUID,
            candidate_id: str,
        ) -> dict[str, Any]:
            result = store.list(
                project_id,
                analysis_job_id,
                PIPELINE_ID,
                OVERLAY_ID,
            )[0]
            event = result.events[0]
            assert candidate_id == event.representative_candidate_id
            return {
                "candidate_id": candidate_id,
                "ai_original_candidate": {
                    "review_event_binding": {
                        "review_event_set_id": (
                            result.review_event_set_id
                        ),
                        "review_event_set_revision": (
                            result.review_event_set_revision
                        ),
                        "review_event_set_manifest_sha256": (
                            result.manifest_sha256
                        ),
                        "review_event_id": event.event_id,
                        "review_event_revision": (
                            event.event_revision
                        ),
                        "representative_candidate_id": candidate_id,
                        "review_only": True,
                    }
                },
            }

    def project_questions(
        project_id: UUID,
        analysis_job_id: UUID,
        source_sha256: str,
        questions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        projected_questions.append(questions)
        return {
            "project_id": str(project_id),
            "analysis_job_id": str(analysis_job_id),
            "source_sha256": source_sha256,
            "question_count": len(questions),
            "question_set_sha256": sha(questions),
        }

    detection = {
        **checkpoints["dino_checkpoint"],
        **checkpoints["ocr_checkpoint"],
        "proposal_candidate_count": 1,
        "ocr_observation_count": 0,
    }
    vision = {
        **checkpoints["structured_proposal_checkpoint"],
        "structured_llm_semantics_status": "completed",
        "structured_proposal_count": 1,
        "structured_not_run_count": 0,
        "structured_failure_count": 0,
        "comparison_mode": "metadata_comparison_only",
    }
    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        review_event_sets=store,
        candidate_labels=SyntheticCandidateLabels(),
        question_catalog_projector=project_questions,
    )
    result = runtime._prepare_question_queue(
        {
            "job_id": str(PIPELINE_ID),
            "project_id": str(PROJECT_ID),
            "profile": {
                "review_mode": "precision",
                "question_policy": "learning_assist",
            },
            "checkpoints": {
                "candidate_rescan_5_10fps": {
                    "analysis_job_id": str(ANALYSIS_ID),
                    "source_sha256": SOURCE_SHA,
                    "high_frequency_rescan_performed": True,
                    "candidate_evidence_overlay": {
                        "schema_version": (
                            "trafficvisson."
                            "candidate-evidence-overlay.v1"
                        ),
                        "overlay_id": OVERLAY_ID,
                        "project_id": str(PROJECT_ID),
                        "analysis_job_id": str(ANALYSIS_ID),
                        "pipeline_job_id": str(PIPELINE_ID),
                        "source_sha256": SOURCE_SHA,
                    },
                },
                "detection_ocr": detection,
                "sam2_segmentation_tracking": checkpoints[
                    "sam2_checkpoint"
                ],
                "temporal_merge": checkpoints[
                    "temporal_checkpoint"
                ],
                "similar_case_search": checkpoints[
                    "similar_case_checkpoint"
                ],
                "vision_proposal": vision,
            },
        }
    )

    assert result.outcome == "success"
    assert result.checkpoint["question_count"] == 1
    assert result.checkpoint["question_catalog_state"] == "registered"
    assert len(projected_questions) == 1
    assert projected_questions[0][0]["candidate_id"] == CANDIDATE_B
    assert result.checkpoint["annotation_count"] == 0
    assert result.checkpoint["incident_count"] == 0
    assert result.checkpoint["training_membership_count"] == 0
    assert result.checkpoint["report_incident_count"] == 0


def test_unknown_set_or_event_is_not_found(tmp_path: Path) -> None:
    store, _resolver = make_store(tmp_path)
    created = materialize_completed(store)

    with pytest.raises(ReviewEventSetNotFound):
        store.get(
            PROJECT_ID,
            ANALYSIS_ID,
            PIPELINE_ID,
            OVERLAY_ID,
            f"res_{'0' * 32}",
        )
    with pytest.raises(ReviewEventSetNotFound):
        store.resolve_event(
            PROJECT_ID,
            ANALYSIS_ID,
            PIPELINE_ID,
            OVERLAY_ID,
            created.review_event_set_id,
            f"rve_{'0' * 32}",
        )
