from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import json
from uuid import uuid4

from PIL import Image
import pytest

from app.provider_candidate_evidence_resolver import (
    INPUT_SCHEMA_VERSION,
    ProviderCandidateEvidenceResolutionError,
    ResolvedCandidateFrameArtifact,
    resolve_provider_candidate_evidence,
    sam2_evidence_from_checkpoint,
)


PROJECT_ID = uuid4()
ANALYSIS_JOB_ID = uuid4()
PIPELINE_JOB_ID = uuid4()
CANDIDATE_ID = "a" * 20


def _png(seed: int) -> bytes:
    output = io.BytesIO()
    Image.new(
        "RGB",
        (640, 360),
        (30 + seed * 45, 80, 140),
    ).save(output, format="PNG")
    return output.getvalue()


def _polygon(shift: float = 0.0):
    return [
        [0.30 + shift, 0.25],
        [0.65 + shift, 0.25],
        [0.65 + shift, 0.70],
        [0.30 + shift, 0.70],
    ]


def _sam2():
    frames = [
        {
            "evidence_ref": f"rescan:{index:020x}",
            "timestamp_seconds": float(index),
            "polygon": _polygon(index * 0.01),
        }
        for index in (1, 2, 3)
    ]
    return {
        "schema_version": INPUT_SCHEMA_VERSION,
        "candidate_id": CANDIDATE_ID,
        "sam2_revision": "b" * 64,
        "proposal_sha256": "c" * 64,
        "representative_evidence_ref": frames[1][
            "evidence_ref"
        ],
        "representative_polygon": frames[1]["polygon"],
        "tracked_frames": frames,
    }


def _checkpoint():
    proposal = {
        "candidate_id": CANDIDATE_ID,
        "prompt": {
            "kind": "detector_box_center_point",
            "x": 0.48,
            "y": 0.52,
            "prompt_frame_ref": f"rescan:{2:020x}",
        },
        "representative_polygon": _polygon(0.02),
        "normalized_box": {
            "x": 0.32,
            "y": 0.25,
            "width": 0.35,
            "height": 0.45,
        },
        "tracked_frames": _sam2()["tracked_frames"],
        "tracking_performed": True,
        "review_only": True,
        "requires_user_confirmation": True,
        "automatic_annotation_allowed": False,
        "automatic_incident_creation_allowed": False,
        "training_membership_allowed": False,
    }
    proposal["proposal_sha256"] = hashlib.sha256(
        json.dumps(
            proposal,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    checkpoint = {
        "schema_version": (
            "trafficvisson.pipeline-sam2-review.v1"
        ),
        "state": "completed",
        "proposals": [proposal],
        "failures": [],
        "review_only": True,
        "dino_prompt_bindings": [],
        "artifact_root_binding": {
            "output_location_id": "qa-root",
            "registry_revision": 1,
            "root_identity_sha256": "d" * 64,
            "marker_sha256": "e" * 64,
        },
    }
    checkpoint["sam2_revision"] = hashlib.sha256(
        json.dumps(
            {
                key: value
                for key, value in checkpoint.items()
                if key != "artifact_root_binding"
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return checkpoint


def _reseal_checkpoint(checkpoint):
    proposal = checkpoint["proposals"][0]
    proposal["proposal_sha256"] = hashlib.sha256(
        json.dumps(
            {
                key: value
                for key, value in proposal.items()
                if key != "proposal_sha256"
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    checkpoint["sam2_revision"] = hashlib.sha256(
        json.dumps(
            {
                key: value
                for key, value in checkpoint.items()
                if key
                not in {
                    "sam2_revision",
                    "artifact_root_binding",
                }
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return checkpoint


def _artifacts():
    result = {}
    for index in (1, 2, 3):
        evidence_ref = f"rescan:{index:020x}"
        body = _png(index)
        result[evidence_ref] = ResolvedCandidateFrameArtifact(
            project_id=PROJECT_ID,
            analysis_job_id=ANALYSIS_JOB_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            candidate_id=CANDIDATE_ID,
            evidence_ref=evidence_ref,
            timestamp_seconds=float(index),
            body=body,
            media_type="image/png",
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
        )
    return result


def _resolver(artifacts):
    def resolve(**request):
        artifact = artifacts[request["evidence_ref"]]
        assert request["project_id"] == PROJECT_ID
        assert request["analysis_job_id"] == ANALYSIS_JOB_ID
        assert request["pipeline_job_id"] == PIPELINE_JOB_ID
        assert request["candidate_id"] == CANDIDATE_ID
        return artifact

    return resolve


def _resolve(artifacts=None, checkpoint=None):
    artifacts = artifacts or _artifacts()
    return resolve_provider_candidate_evidence(
        project_id=PROJECT_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        candidate_id=CANDIDATE_ID,
        sam2_checkpoint=checkpoint or _checkpoint(),
        artifact_resolver=_resolver(artifacts),
    )


def test_exact_before_representative_after_and_mask_are_hash_bound():
    first = _resolve()
    second = _resolve()

    assert first.montage.body.startswith(b"\x89PNG\r\n\x1a\n")
    assert first.montage.sha256 == second.montage.sha256
    assert first.manifest == second.manifest
    assert [
        row["timestamp_seconds"]
        for row in first.manifest["selected_frames"]
    ] == [1.0, 2.0, 3.0]
    assert [
        row["role"] for row in first.manifest["selected_frames"]
    ] == [
        "before_frame",
        "representative_frame",
        "after_frame",
    ]
    assert first.manifest["mask_overlay"]["sha256"]
    assert first.manifest["montage_sha256"] == first.montage.sha256
    assert first.manifest["privacy_state"] == "unreviewed"
    assert first.manifest["provider_transmission_allowed"] is False
    assert first.manifest["provider_command_invocation_count"] == 0
    assert first.manifest["annotation_allowed"] is False
    assert first.manifest["incident_allowed"] is False
    assert first.manifest["report_truth_allowed"] is False
    assert first.manifest["path_exposed"] is False
    serialized = json.dumps(
        first.manifest, sort_keys=True, separators=(",", ":")
    ).casefold()
    assert r"c:\\" not in serialized
    assert "\\\\" not in serialized
    assert "file://" not in serialized
    assert "/users/" not in serialized
    assert "body" not in serialized


def test_production_checkpoint_adapter_verifies_both_hash_layers():
    checkpoint = _checkpoint()
    adapted = sam2_evidence_from_checkpoint(
        candidate_id=CANDIDATE_ID,
        checkpoint=checkpoint,
    )

    assert adapted["schema_version"] == INPUT_SCHEMA_VERSION
    assert adapted["sam2_revision"] == checkpoint["sam2_revision"]
    assert adapted["proposal_sha256"] == checkpoint["proposals"][0][
        "proposal_sha256"
    ]
    resolved = resolve_provider_candidate_evidence(
        project_id=PROJECT_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        candidate_id=CANDIDATE_ID,
        sam2_checkpoint=checkpoint,
        artifact_resolver=_resolver(_artifacts()),
    )
    assert resolved.manifest["provider_command_invocation_count"] == 0


def test_production_checkpoint_adapter_rejects_checkpoint_tamper():
    checkpoint = _checkpoint()
    checkpoint["proposals"][0]["tracked_frames"][0][
        "polygon"
    ][0][0] = 0.12

    with pytest.raises(
        ProviderCandidateEvidenceResolutionError
    ) as caught:
        sam2_evidence_from_checkpoint(
            candidate_id=CANDIDATE_ID,
            checkpoint=checkpoint,
        )
    assert caught.value.code == "candidate_evidence_checkpoint_changed"


def test_resolver_rehashes_source_bytes_and_rejects_tamper():
    artifacts = _artifacts()
    ref = f"rescan:{2:020x}"
    artifacts[ref] = replace(
        artifacts[ref],
        body=artifacts[ref].body + b"tamper",
        size_bytes=len(artifacts[ref].body) + 6,
    )

    with pytest.raises(
        ProviderCandidateEvidenceResolutionError
    ) as caught:
        _resolve(artifacts)
    assert caught.value.code == "candidate_evidence_artifact_changed"


@pytest.mark.parametrize(
    "field,value,code",
    (
        (
            "project_id",
            uuid4(),
            "candidate_evidence_artifact_scope_mismatch",
        ),
        (
            "analysis_job_id",
            uuid4(),
            "candidate_evidence_artifact_scope_mismatch",
        ),
        (
            "pipeline_job_id",
            uuid4(),
            "candidate_evidence_artifact_scope_mismatch",
        ),
        (
            "candidate_id",
            "d" * 20,
            "candidate_evidence_artifact_scope_mismatch",
        ),
        (
            "timestamp_seconds",
            99.0,
            "candidate_evidence_artifact_scope_mismatch",
        ),
    ),
)
def test_cross_scope_artifacts_are_rejected(field, value, code):
    artifacts = _artifacts()
    ref = f"rescan:{2:020x}"
    artifacts[ref] = replace(artifacts[ref], **{field: value})

    with pytest.raises(
        ProviderCandidateEvidenceResolutionError
    ) as caught:
        _resolve(artifacts)
    assert caught.value.code == code


def test_callback_cannot_return_path_bearing_mapping():
    def unsafe_resolver(**_request):
        return {
            "path": r"C:\raw\frame.png",
            "body": _png(1),
        }

    with pytest.raises(
        ProviderCandidateEvidenceResolutionError
    ) as caught:
        resolve_provider_candidate_evidence(
            project_id=PROJECT_ID,
            analysis_job_id=ANALYSIS_JOB_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            candidate_id=CANDIDATE_ID,
            sam2_checkpoint=_checkpoint(),
            artifact_resolver=unsafe_resolver,
        )
    assert caught.value.code == "candidate_evidence_artifact_invalid"


def test_missing_temporal_side_or_polygon_mismatch_fails_closed():
    checkpoint = _checkpoint()
    checkpoint["proposals"][0]["tracked_frames"][0][
        "timestamp_seconds"
    ] = 2.5
    _reseal_checkpoint(checkpoint)
    with pytest.raises(
        ProviderCandidateEvidenceResolutionError
    ) as caught:
        _resolve(checkpoint=checkpoint)
    assert (
        caught.value.code
        == "candidate_evidence_temporal_context_missing"
    )

    checkpoint = _checkpoint()
    checkpoint["proposals"][0][
        "representative_polygon"
    ] = _polygon(0.08)
    _reseal_checkpoint(checkpoint)
    with pytest.raises(
        ProviderCandidateEvidenceResolutionError
    ) as caught:
        _resolve(checkpoint=checkpoint)
    assert (
        caught.value.code
        == "candidate_evidence_representative_invalid"
    )


def test_path_like_evidence_ref_is_rejected_before_callback():
    checkpoint = _checkpoint()
    checkpoint["proposals"][0]["tracked_frames"][0][
        "evidence_ref"
    ] = (
        r"C:\raw\frame.png"
    )
    _reseal_checkpoint(checkpoint)
    calls = []

    with pytest.raises(ProviderCandidateEvidenceResolutionError):
        resolve_provider_candidate_evidence(
            project_id=PROJECT_ID,
            analysis_job_id=ANALYSIS_JOB_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            candidate_id=CANDIDATE_ID,
            sam2_checkpoint=checkpoint,
            artifact_resolver=lambda **request: calls.append(request),
        )
    assert calls == []
