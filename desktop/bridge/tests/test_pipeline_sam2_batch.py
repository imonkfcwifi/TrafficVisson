from __future__ import annotations

from pathlib import Path

from app.pipeline_sam2_batch import (
    run_pipeline_sam2_batches,
    select_balanced_sam2_frames,
)
from app.pipeline_sam2_stage import (
    MAX_CANDIDATES,
    Sam2CandidatePrompt,
    Sam2FrameEvidence,
)


def _frame(index: int) -> Sam2FrameEvidence:
    return Sam2FrameEvidence(
        evidence_ref=f"rescan:{index:020x}",
        path=Path(f"C:/qa/frame-{index:03d}.jpg"),
        sha256=f"{index + 1:064x}",
        timestamp_seconds=float(index),
    )


def _box():
    return {"x": 0.2, "y": 0.2, "width": 0.4, "height": 0.4}


class FakeSam2:
    def __init__(self):
        self.track_calls = []

    def status(self):
        return {
            "state": "ready",
            "model_manifest": {
                "pack_id": "sam2",
                "pack_version": "1",
                "manifest_sha256": "a" * 64,
                "model_artifact_sha256": "b" * 64,
                "provenance_verified": True,
                "review_only": True,
            },
        }

    def track(self, *, frame_paths, prompt_frame_index, point):
        self.track_calls.append(
            (tuple(frame_paths), prompt_frame_index, point)
        )
        return {
            "frames": [
                {
                    "frame_index": index,
                    "polygon": [
                        [0.2, 0.2],
                        [0.6, 0.2],
                        [0.6, 0.6],
                        [0.2, 0.6],
                    ],
                }
                for index in range(len(frame_paths))
            ]
        }

    def refine(self, *, image_path, point):
        return {
            "polygon": [
                [0.2, 0.2],
                [0.6, 0.2],
                [0.6, 0.6],
                [0.2, 0.6],
            ]
        }


def test_balanced_selection_keeps_nearest_before_and_after():
    selected, prompt_index, context = select_balanced_sam2_frames(
        tuple(_frame(index) for index in range(10)),
        target_timestamp_seconds=5.0,
        required_evidence_ref=f"rescan:{5:020x}",
        maximum_frames=4,
    )

    timestamps = [item.timestamp_seconds for item in selected]
    assert 4.0 in timestamps
    assert 5.0 in timestamps
    assert 6.0 in timestamps
    assert selected[prompt_index].timestamp_seconds == 5.0
    assert context["before_context_included"] is True
    assert context["after_context_included"] is True
    assert context["selected_frame_count"] == 4
    assert len(context["selection_sha256"]) == 64


def test_missing_side_is_reported_without_inventing_a_frame():
    selected, prompt_index, context = select_balanced_sam2_frames(
        tuple(_frame(index) for index in range(3)),
        target_timestamp_seconds=0.0,
        required_evidence_ref=f"rescan:{0:020x}",
    )

    assert selected[prompt_index].timestamp_seconds == 0.0
    assert context["before_context_included"] is False
    assert context["after_context_included"] is True


def test_more_than_one_safe_batch_keeps_every_prompt():
    adapter = FakeSam2()
    prompts = [
        Sam2CandidatePrompt(
            candidate_id=f"{index + 1:020x}",
            normalized_box=_box(),
            frames=(_frame(index * 3), _frame(index * 3 + 1), _frame(index * 3 + 2)),
            prompt_frame_index=1,
        )
        for index in range(MAX_CANDIDATES + 5)
    ]

    result = run_pipeline_sam2_batches(
        adapter=adapter,
        prompts=prompts,
        cancel=lambda: False,
    )

    assert result["status"] == "success"
    assert result["metrics"]["batch_count"] == 2
    assert result["metrics"]["prompt_count"] == len(prompts)
    assert result["metrics"]["mask_count"] == len(prompts)
    assert len(result["checkpoint"]["proposals"]) == len(prompts)
    assert len(adapter.track_calls) == len(prompts)
    assert result["checkpoint"]["annotation_count"] == 0
    assert result["checkpoint"]["incident_count"] == 0
    assert len(result["checkpoint"]["sam2_revision"]) == 64


def test_cancel_between_batches_returns_cancelled_without_false_success():
    adapter = FakeSam2()
    prompts = [
        Sam2CandidatePrompt(
            candidate_id=f"{index + 1:020x}",
            normalized_box=_box(),
            frames=(_frame(index * 3), _frame(index * 3 + 1), _frame(index * 3 + 2)),
            prompt_frame_index=1,
        )
        for index in range(MAX_CANDIDATES + 1)
    ]
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls > MAX_CANDIDATES + 1

    result = run_pipeline_sam2_batches(
        adapter=adapter,
        prompts=prompts,
        cancel=cancelled,
    )

    assert result == {
        "status": "cancelled",
        "error_code": "sam2_stage_cancelled",
    }
