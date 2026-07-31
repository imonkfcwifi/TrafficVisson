from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
from uuid import uuid4

from PIL import Image
import pytest

from app.provider_candidate_evidence_resolver import (
    ALIAS_MAPPING_SCHEMA_VERSION,
    ProviderCandidateEvidenceResolutionError,
    ResolvedAliasedCandidateFrameArtifact,
    bind_review_candidate_to_sam2_proposal,
    resolve_aliased_provider_candidate_evidence,
)


PROJECT_ID = uuid4()
ANALYSIS_JOB_ID = uuid4()
PIPELINE_JOB_ID = uuid4()
REVIEW_CANDIDATE_ID = "d" * 20
SAM2_PROPOSAL_CANDIDATE_ID = "a" * 20
PARENT_CANDIDATE_ID = "e" * 20
EVIDENCE_CANDIDATE_ID = "f" * 20
BEFORE_CANDIDATE_ID = "b" * 20
AFTER_CANDIDATE_ID = "c" * 20
DINO_PROPOSAL_ID = f"dino_{'1' * 24}"
EVIDENCE_IMAGE_SHA256 = "2" * 64


def _canonical_json(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _box():
    return {
        "x": 0.25,
        "y": 0.30,
        "width": 0.35,
        "height": 0.40,
    }


def _polygon(shift: float = 0.0):
    return [
        [0.25 + shift, 0.30],
        [0.60 + shift, 0.30],
        [0.60 + shift, 0.70],
        [0.25 + shift, 0.70],
    ]


def _png(seed: int) -> bytes:
    output = io.BytesIO()
    Image.new(
        "RGB",
        (640, 360),
        (30 + seed * 40, 75, 130),
    ).save(output, format="PNG")
    return output.getvalue()


def _sam2_checkpoint():
    tracked_frames = [
        {
            "evidence_ref": f"rescan:{BEFORE_CANDIDATE_ID}",
            "timestamp_seconds": 1.0,
            "polygon": _polygon(-0.01),
        },
        {
            "evidence_ref": f"rescan:{EVIDENCE_CANDIDATE_ID}",
            "timestamp_seconds": 2.0,
            "polygon": _polygon(),
        },
        {
            "evidence_ref": f"rescan:{AFTER_CANDIDATE_ID}",
            "timestamp_seconds": 3.0,
            "polygon": _polygon(0.01),
        },
    ]
    proposal = {
        "candidate_id": SAM2_PROPOSAL_CANDIDATE_ID,
        "prompt": {
            "kind": "detector_box_center_point",
            "x": 0.425,
            "y": 0.50,
            "prompt_frame_ref": (
                f"rescan:{EVIDENCE_CANDIDATE_ID}"
            ),
        },
        "representative_polygon": _polygon(),
        "normalized_box": _box(),
        "tracked_frames": tracked_frames,
        "tracking_performed": True,
        "review_only": True,
        "requires_user_confirmation": True,
        "automatic_annotation_allowed": False,
        "automatic_incident_creation_allowed": False,
        "training_membership_allowed": False,
    }
    proposal["proposal_sha256"] = _sha256(proposal)
    binding = {
        "prompt_candidate_id": SAM2_PROPOSAL_CANDIDATE_ID,
        "dino_proposal_id": DINO_PROPOSAL_ID,
        "parent_candidate_id": PARENT_CANDIDATE_ID,
        "evidence_candidate_id": EVIDENCE_CANDIDATE_ID,
        "evidence_timestamp_seconds": 2.0,
        "evidence_image_sha256": EVIDENCE_IMAGE_SHA256,
        "issue_type": "road_debris",
        "score": 0.88,
        "normalized_box": _box(),
        "prompt_evidence_ref": (
            f"rescan:{EVIDENCE_CANDIDATE_ID}"
        ),
        "review_only": True,
        "sam2_status": "available",
        "sam2_proposal_sha256": proposal["proposal_sha256"],
        "sam2_error_code": None,
    }
    checkpoint = {
        "schema_version": "trafficvisson.pipeline-sam2-review.v1",
        "state": "completed",
        "proposals": [proposal],
        "failures": [],
        "review_only": True,
        "dino_prompt_bindings": [binding],
        "artifact_root_binding": {
            "root_identity_sha256": "3" * 64,
        },
    }
    _reseal_sam2(checkpoint)
    return checkpoint


def _reseal_sam2(checkpoint):
    for proposal in checkpoint["proposals"]:
        proposal["proposal_sha256"] = _sha256(
            {
                key: value
                for key, value in proposal.items()
                if key != "proposal_sha256"
            }
        )
    by_id = {
        proposal["candidate_id"]: proposal["proposal_sha256"]
        for proposal in checkpoint["proposals"]
    }
    for binding in checkpoint["dino_prompt_bindings"]:
        if binding.get("sam2_status") == "available":
            binding["sam2_proposal_sha256"] = by_id[
                binding["prompt_candidate_id"]
            ]
    checkpoint["sam2_revision"] = _sha256(
        {
            key: value
            for key, value in checkpoint.items()
            if key not in {
                "sam2_revision",
                "artifact_root_binding",
            }
        }
    )
    return checkpoint


def _temporal_checkpoint():
    detail = {
        "temporal_proposal_id": "tip_" + "4" * 24,
        "synthetic_candidate_id": REVIEW_CANDIDATE_ID,
        "issue_type": "road_debris",
        "start_seconds": 1.5,
        "end_seconds": 2.5,
        "merged_frame_count": 1,
        "evidence_proposal_ids": [DINO_PROPOSAL_ID],
        "evidence_frame_ids": [EVIDENCE_CANDIDATE_ID],
        "sam2_prompt_candidate_ids": [
            SAM2_PROPOSAL_CANDIDATE_ID
        ],
        "peak_evidence": {
            "dino_proposal_id": DINO_PROPOSAL_ID,
            "parent_candidate_id": PARENT_CANDIDATE_ID,
            "evidence_candidate_id": EVIDENCE_CANDIDATE_ID,
            "timestamp_seconds": 2.0,
            "image_sha256": EVIDENCE_IMAGE_SHA256,
            "score": 0.88,
            "normalized_box": _box(),
        },
        "review_only": True,
        "automatic_annotation_allowed": False,
        "automatic_incident_creation_allowed": False,
    }
    summary = {
        "proposal_id": detail["temporal_proposal_id"],
        "representative_candidate_id": REVIEW_CANDIDATE_ID,
        "evidence_candidate_ids": [REVIEW_CANDIDATE_ID],
        "issue_type": "road_debris",
        "start_seconds": 1.5,
        "end_seconds": 2.5,
        "detection_count": 1,
    }
    overlay = {
        "project_id": str(PROJECT_ID),
        "analysis_job_id": str(ANALYSIS_JOB_ID),
        "pipeline_job_id": str(PIPELINE_JOB_ID),
        "review_only": True,
        "annotations_created": 0,
        "incidents_created": 0,
        "training_members_created": 0,
        "report_truth_records_created": 0,
    }
    checkpoint = {
        "analysis_job_id": str(ANALYSIS_JOB_ID),
        "evaluation_id": "evaluation-v1",
        "detection_revision": "5" * 64,
        "question_candidate_ids": [REVIEW_CANDIDATE_ID],
        "groups": [summary],
        "dino_temporal_groups_by_candidate": {
            REVIEW_CANDIDATE_ID: detail
        },
        "dino_candidate_evidence_overlay": overlay,
        "review_only": True,
        "automatic_annotation_allowed": False,
        "automatic_incident_creation_allowed": False,
        "artifact_root_binding": {
            "root_identity_sha256": "6" * 64,
        },
    }
    _reseal_temporal(checkpoint)
    return checkpoint


def _reseal_temporal(checkpoint):
    payload = {
        key: checkpoint[key]
        for key in (
            "analysis_job_id",
            "evaluation_id",
            "detection_revision",
            "question_candidate_ids",
            "groups",
        )
    }
    if checkpoint.get("dino_temporal_groups_by_candidate"):
        payload["dino_temporal_groups_by_candidate"] = (
            checkpoint["dino_temporal_groups_by_candidate"]
        )
        payload["dino_candidate_evidence_overlay"] = checkpoint[
            "dino_candidate_evidence_overlay"
        ]
    checkpoint["temporal_merge_revision"] = _sha256(payload)
    return checkpoint


def _artifacts():
    result = {}
    rows = (
        (BEFORE_CANDIDATE_ID, 1.0, 1),
        (EVIDENCE_CANDIDATE_ID, 2.0, 2),
        (AFTER_CANDIDATE_ID, 3.0, 3),
    )
    for candidate_id, timestamp, seed in rows:
        body = _png(seed)
        evidence_ref = f"rescan:{candidate_id}"
        result[evidence_ref] = ResolvedAliasedCandidateFrameArtifact(
            project_id=PROJECT_ID,
            analysis_job_id=ANALYSIS_JOB_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            review_candidate_id=REVIEW_CANDIDATE_ID,
            sam2_proposal_candidate_id=(
                SAM2_PROPOSAL_CANDIDATE_ID
            ),
            evidence_ref=evidence_ref,
            timestamp_seconds=timestamp,
            body=body,
            media_type="image/png",
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
        )
    return result


def _resolver(artifacts, calls=None):
    calls = calls if calls is not None else []

    def resolve(**request):
        calls.append(request)
        assert request["review_candidate_id"] == (
            REVIEW_CANDIDATE_ID
        )
        assert request["sam2_proposal_candidate_id"] == (
            SAM2_PROPOSAL_CANDIDATE_ID
        )
        return artifacts[request["evidence_ref"]]

    return resolve


def _bind(temporal=None, sam2=None):
    return bind_review_candidate_to_sam2_proposal(
        project_id=PROJECT_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        review_candidate_id=REVIEW_CANDIDATE_ID,
        sam2_proposal_candidate_id=(
            SAM2_PROPOSAL_CANDIDATE_ID
        ),
        temporal_checkpoint=temporal or _temporal_checkpoint(),
        sam2_checkpoint=sam2 or _sam2_checkpoint(),
    )


def test_alias_binding_verifies_both_revisions_and_peak_identity():
    binding = _bind()

    assert binding.review_candidate_id == REVIEW_CANDIDATE_ID
    assert binding.sam2_proposal_candidate_id == (
        SAM2_PROPOSAL_CANDIDATE_ID
    )
    assert binding.manifest["schema_version"] == (
        ALIAS_MAPPING_SCHEMA_VERSION
    )
    assert binding.manifest["mapping_sha256"] == _sha256(
        {
            key: value
            for key, value in binding.manifest.items()
            if key != "mapping_sha256"
        }
    )
    assert binding.manifest["provider_transmission_allowed"] is False
    assert binding.manifest["provider_command_invocation_count"] == 0
    assert binding.manifest["annotation_allowed"] is False
    assert binding.manifest["incident_allowed"] is False


def test_alias_resolution_keeps_review_and_sam2_ids_separate():
    result = resolve_aliased_provider_candidate_evidence(
        project_id=PROJECT_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        review_candidate_id=REVIEW_CANDIDATE_ID,
        sam2_proposal_candidate_id=(
            SAM2_PROPOSAL_CANDIDATE_ID
        ),
        temporal_checkpoint=_temporal_checkpoint(),
        sam2_checkpoint=_sam2_checkpoint(),
        artifact_resolver=_resolver(_artifacts()),
    )

    assert result.manifest["candidate_id"] == REVIEW_CANDIDATE_ID
    assert result.manifest["review_candidate_id"] == (
        REVIEW_CANDIDATE_ID
    )
    assert result.manifest["sam2_proposal_candidate_id"] == (
        SAM2_PROPOSAL_CANDIDATE_ID
    )
    assert result.manifest["mapping_sha256"]
    assert result.montage.manifest["candidate_id"] == (
        REVIEW_CANDIDATE_ID
    )
    assert result.manifest["provider_transmission_allowed"] is False
    assert result.manifest["provider_command_invocation_count"] == 0
    assert result.manifest["annotation_allowed"] is False
    assert result.manifest["incident_allowed"] is False
    assert result.manifest["training_start_allowed"] is False
    assert result.manifest["report_truth_allowed"] is False
    assert result.manifest["hwp_apply_allowed"] is False


def test_temporal_and_sam2_revision_tamper_fail_closed():
    temporal = _temporal_checkpoint()
    temporal["dino_temporal_groups_by_candidate"][
        REVIEW_CANDIDATE_ID
    ]["issue_type"] = "pothole"
    with pytest.raises(
        ProviderCandidateEvidenceResolutionError
    ) as caught:
        _bind(temporal=temporal)
    assert caught.value.code == (
        "candidate_alias_temporal_checkpoint_changed"
    )

    sam2 = _sam2_checkpoint()
    sam2["proposals"][0]["prompt"]["x"] = 0.99
    with pytest.raises(
        ProviderCandidateEvidenceResolutionError
    ) as caught:
        _bind(sam2=sam2)
    assert caught.value.code == "candidate_evidence_checkpoint_changed"


def test_missing_multiple_and_cross_scope_prompt_fail_closed():
    temporal = _temporal_checkpoint()
    temporal["dino_temporal_groups_by_candidate"][
        REVIEW_CANDIDATE_ID
    ]["sam2_prompt_candidate_ids"] = []
    _reseal_temporal(temporal)
    with pytest.raises(
        ProviderCandidateEvidenceResolutionError
    ) as caught:
        _bind(temporal=temporal)
    assert caught.value.code == "candidate_alias_prompt_missing"

    sam2 = _sam2_checkpoint()
    sam2["dino_prompt_bindings"].append(
        deepcopy(sam2["dino_prompt_bindings"][0])
    )
    _reseal_sam2(sam2)
    with pytest.raises(
        ProviderCandidateEvidenceResolutionError
    ) as caught:
        _bind(sam2=sam2)
    assert caught.value.code == "candidate_alias_prompt_ambiguous"

    sam2 = _sam2_checkpoint()
    sam2["dino_prompt_bindings"][0][
        "parent_candidate_id"
    ] = "7" * 20
    _reseal_sam2(sam2)
    with pytest.raises(
        ProviderCandidateEvidenceResolutionError
    ) as caught:
        _bind(sam2=sam2)
    assert caught.value.code == "candidate_alias_scope_mismatch"


def test_mapping_can_be_valid_while_temporal_context_is_not_usable():
    sam2 = _sam2_checkpoint()
    sam2["proposals"][0]["tracking_performed"] = False
    sam2["proposals"][0]["tracked_frames"] = [
        sam2["proposals"][0]["tracked_frames"][1]
    ]
    _reseal_sam2(sam2)
    binding = _bind(sam2=sam2)
    assert binding.manifest["mapping_sha256"]
    calls = []

    with pytest.raises(
        ProviderCandidateEvidenceResolutionError
    ) as caught:
        resolve_aliased_provider_candidate_evidence(
            project_id=PROJECT_ID,
            analysis_job_id=ANALYSIS_JOB_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            review_candidate_id=REVIEW_CANDIDATE_ID,
            sam2_proposal_candidate_id=(
                SAM2_PROPOSAL_CANDIDATE_ID
            ),
            temporal_checkpoint=_temporal_checkpoint(),
            sam2_checkpoint=sam2,
            artifact_resolver=_resolver(_artifacts(), calls),
        )
    assert caught.value.code == "candidate_evidence_proposal_invalid"
    assert calls == []


def test_aliased_artifact_cross_candidate_scope_is_rejected():
    artifacts = _artifacts()
    representative_ref = f"rescan:{EVIDENCE_CANDIDATE_ID}"
    artifact = artifacts[representative_ref]
    artifacts[representative_ref] = (
        ResolvedAliasedCandidateFrameArtifact(
            **{
                **artifact.__dict__,
                "sam2_proposal_candidate_id": "8" * 20,
            }
        )
    )

    with pytest.raises(
        ProviderCandidateEvidenceResolutionError
    ) as caught:
        resolve_aliased_provider_candidate_evidence(
            project_id=PROJECT_ID,
            analysis_job_id=ANALYSIS_JOB_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            review_candidate_id=REVIEW_CANDIDATE_ID,
            sam2_proposal_candidate_id=(
                SAM2_PROPOSAL_CANDIDATE_ID
            ),
            temporal_checkpoint=_temporal_checkpoint(),
            sam2_checkpoint=_sam2_checkpoint(),
            artifact_resolver=_resolver(artifacts),
        )
    assert caught.value.code == (
        "candidate_alias_artifact_scope_mismatch"
    )
