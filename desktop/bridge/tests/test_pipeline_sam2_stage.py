from __future__ import annotations

from pathlib import Path

from app.pipeline_sam2_stage import (
    PipelineSam2StageError,
    Sam2CandidatePrompt,
    Sam2FrameEvidence,
    run_pipeline_sam2_stage,
)
import pytest


MODEL_SHA = "a" * 64
ARTIFACT_SHA = "b" * 64
CANDIDATE = "1" * 20


class Adapter:
    def status(self):
        return {
            "state": "ready",
            "model_manifest": {
                "pack_id": "trafficvisson-facebook-sam2",
                "pack_version": "0.1.0",
                "manifest_sha256": MODEL_SHA,
                "model_artifact_sha256": ARTIFACT_SHA,
                "provenance_verified": True,
                "review_only": True,
            },
        }

    def refine(self, *, image_path, point):
        return {
            "polygon": [
                [0.1, 0.1],
                [0.5, 0.1],
                [0.5, 0.5],
            ]
        }

    def track(self, *, frame_paths, prompt_frame_index, point):
        return {
            "frames": [
                {
                    "frame_index": index,
                    "polygon": [
                        [0.1 + index * 0.01, 0.1],
                        [0.5, 0.1],
                        [0.5, 0.5],
                    ],
                }
                for index in range(len(frame_paths))
            ]
        }


def _frame(tmp_path: Path, index: int) -> Sam2FrameEvidence:
    path = (tmp_path / f"frame-{index}.jpg").resolve()
    path.write_bytes(b"jpeg")
    return Sam2FrameEvidence(
        evidence_ref=f"rescan:{index}",
        path=path,
        sha256=str(index + 2) * 64,
        timestamp_seconds=float(index),
    )


def _prompt(tmp_path: Path, count: int = 3) -> Sam2CandidatePrompt:
    return Sam2CandidatePrompt(
        candidate_id=CANDIDATE,
        normalized_box={
            "x": 0.2,
            "y": 0.3,
            "width": 0.4,
            "height": 0.2,
        },
        frames=tuple(_frame(tmp_path, index) for index in range(count)),
        prompt_frame_index=0,
    )


def test_tracking_stage_is_review_only_and_creates_no_truth(tmp_path):
    result = run_pipeline_sam2_stage(
        adapter=Adapter(),
        prompts=[_prompt(tmp_path)],
        cancel=lambda: False,
    )
    assert result["status"] == "success"
    checkpoint = result["checkpoint"]
    assert checkpoint["state"] == "completed"
    assert checkpoint["mask_generated"] is True
    assert checkpoint["tracking_performed"] is True
    assert checkpoint["annotation_count"] == 0
    assert checkpoint["incident_count"] == 0
    assert checkpoint["training_membership_allowed"] is False
    proposal = checkpoint["proposals"][0]
    assert proposal["candidate_id"] == CANDIDATE
    assert len(proposal["tracked_frames"]) == 3
    assert proposal["requires_user_confirmation"] is True
    assert result["metrics"]["tracked_frame_count"] == 3


def test_single_frame_runs_mask_without_claiming_tracking(tmp_path):
    result = run_pipeline_sam2_stage(
        adapter=Adapter(),
        prompts=[_prompt(tmp_path, count=1)],
        cancel=lambda: False,
    )
    checkpoint = result["checkpoint"]
    assert checkpoint["mask_generated"] is True
    assert checkpoint["tracking_performed"] is False
    assert len(checkpoint["proposals"][0]["tracked_frames"]) == 1


def test_no_prompt_is_honest_zero_prompt_state():
    result = run_pipeline_sam2_stage(
        adapter=Adapter(),
        prompts=[],
        cancel=lambda: False,
    )
    assert result["status"] == "success"
    assert result["error_code"] == "sam2_no_prompt_boxes"
    assert result["checkpoint"]["state"] == "zero_prompts"
    assert result["checkpoint"]["mask_generated"] is False


def test_cancel_never_returns_a_partial_success(tmp_path):
    result = run_pipeline_sam2_stage(
        adapter=Adapter(),
        prompts=[_prompt(tmp_path)],
        cancel=lambda: True,
    )
    assert result == {
        "status": "cancelled",
        "error_code": "sam2_stage_cancelled",
    }


def test_duplicate_candidate_and_bad_manifest_fail_closed(tmp_path):
    prompt = _prompt(tmp_path)
    with pytest.raises(
        PipelineSam2StageError,
        match="sam2_candidate_prompt_invalid",
    ):
        run_pipeline_sam2_stage(
            adapter=Adapter(),
            prompts=[prompt, prompt],
            cancel=lambda: False,
        )

    class BadAdapter(Adapter):
        def status(self):
            return {"state": "ready", "model_manifest": {}}

    with pytest.raises(
        PipelineSam2StageError,
        match="sam2_model_manifest_invalid",
    ):
        run_pipeline_sam2_stage(
            adapter=BadAdapter(),
            prompts=[prompt],
            cancel=lambda: False,
        )
