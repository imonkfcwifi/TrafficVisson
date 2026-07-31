"""Balanced SAM2 prompt selection and bounded multi-batch execution.

One ``run_pipeline_sam2_stage`` call intentionally accepts at most sixteen
prompts.  A road-video PipelineJob may create more than sixteen review
candidates, so silently slicing the list loses masks and temporal context.
This module keeps the per-call safety bound while executing every prompt in
deterministic batches and merging the review-only checkpoint.

It also selects prompt frames around the representative timestamp.  When both
sides exist, the nearest frame before and after the representative are always
included before the remaining nearest frames are filled.  This does not invent
temporal evidence: a missing side remains explicitly unavailable and the later
Provider montage resolver fails closed.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Callable, Mapping, Optional, Sequence

from .pipeline_sam2_stage import (
    MAX_CANDIDATES,
    MAX_FRAMES_PER_CANDIDATE,
    PIPELINE_SAM2_SCHEMA,
    PipelineSam2Adapter,
    PipelineSam2StageError,
    Sam2CandidatePrompt,
    Sam2FrameEvidence,
    run_pipeline_sam2_stage,
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def select_balanced_sam2_frames(
    frames: Sequence[Sam2FrameEvidence],
    *,
    target_timestamp_seconds: float,
    required_evidence_ref: Optional[str] = None,
    maximum_frames: int = MAX_FRAMES_PER_CANDIDATE,
) -> tuple[tuple[Sam2FrameEvidence, ...], int, Mapping[str, Any]]:
    """Select a deterministic prompt window with both temporal sides.

    ``required_evidence_ref`` is used for a DINO proposal whose exact source
    frame must remain the prompt frame.  Without it, the frame nearest the
    target timestamp becomes representative.
    """

    if (
        isinstance(frames, (str, bytes, bytearray))
        or not frames
        or len(frames) > 4_096
        or isinstance(target_timestamp_seconds, bool)
        or not isinstance(target_timestamp_seconds, (int, float))
        or not math.isfinite(float(target_timestamp_seconds))
        or float(target_timestamp_seconds) < 0
        or isinstance(maximum_frames, bool)
        or not isinstance(maximum_frames, int)
        or not 1 <= maximum_frames <= MAX_FRAMES_PER_CANDIDATE
    ):
        raise PipelineSam2StageError("sam2_candidate_evidence_invalid")

    by_ref: dict[str, Sam2FrameEvidence] = {}
    timestamps: set[float] = set()
    for frame in frames:
        if (
            not isinstance(frame, Sam2FrameEvidence)
            or frame.evidence_ref in by_ref
            or not math.isfinite(float(frame.timestamp_seconds))
            or float(frame.timestamp_seconds) < 0
            or float(frame.timestamp_seconds) in timestamps
        ):
            raise PipelineSam2StageError(
                "sam2_candidate_evidence_invalid"
            )
        by_ref[frame.evidence_ref] = frame
        timestamps.add(float(frame.timestamp_seconds))

    ordered = sorted(
        frames,
        key=lambda frame: (
            float(frame.timestamp_seconds),
            frame.evidence_ref,
        ),
    )
    if required_evidence_ref is not None:
        representative = by_ref.get(required_evidence_ref)
        if representative is None:
            raise PipelineSam2StageError(
                "sam2_required_evidence_unavailable"
            )
    else:
        representative = min(
            ordered,
            key=lambda frame: (
                abs(
                    float(frame.timestamp_seconds)
                    - float(target_timestamp_seconds)
                ),
                float(frame.timestamp_seconds),
                frame.evidence_ref,
            ),
        )

    representative_time = float(
        representative.timestamp_seconds
    )
    before = [
        frame
        for frame in ordered
        if float(frame.timestamp_seconds) < representative_time
    ]
    after = [
        frame
        for frame in ordered
        if float(frame.timestamp_seconds) > representative_time
    ]
    selected: dict[str, Sam2FrameEvidence] = {
        representative.evidence_ref: representative
    }
    if maximum_frames >= 2 and before:
        nearest_before = max(
            before,
            key=lambda frame: float(frame.timestamp_seconds),
        )
        selected[nearest_before.evidence_ref] = nearest_before
    if maximum_frames >= 3 and after:
        nearest_after = min(
            after,
            key=lambda frame: float(frame.timestamp_seconds),
        )
        selected[nearest_after.evidence_ref] = nearest_after

    remaining = sorted(
        (
            frame
            for frame in ordered
            if frame.evidence_ref not in selected
        ),
        key=lambda frame: (
            abs(
                float(frame.timestamp_seconds)
                - representative_time
            ),
            float(frame.timestamp_seconds),
            frame.evidence_ref,
        ),
    )
    for frame in remaining:
        if len(selected) >= maximum_frames:
            break
        selected[frame.evidence_ref] = frame

    result = tuple(
        sorted(
            selected.values(),
            key=lambda frame: (
                float(frame.timestamp_seconds),
                frame.evidence_ref,
            ),
        )
    )
    prompt_index = next(
        index
        for index, frame in enumerate(result)
        if frame.evidence_ref == representative.evidence_ref
    )
    context = {
        "representative_evidence_ref": representative.evidence_ref,
        "representative_timestamp_seconds": round(
            representative_time, 6
        ),
        "before_context_included": any(
            float(frame.timestamp_seconds) < representative_time
            for frame in result
        ),
        "after_context_included": any(
            float(frame.timestamp_seconds) > representative_time
            for frame in result
        ),
        "selected_frame_count": len(result),
        "source_frame_count": len(ordered),
    }
    context["selection_sha256"] = _sha(context)
    return result, prompt_index, context


def _merge_batch_results(
    results: Sequence[Mapping[str, Any]],
    *,
    prompt_count: int,
) -> Mapping[str, Any]:
    proposals: list[Mapping[str, Any]] = []
    failures: list[Mapping[str, Any]] = []
    model_manifest: Optional[Mapping[str, Any]] = None
    seen: set[str] = set()
    for result in results:
        checkpoint = result.get("checkpoint")
        if not isinstance(checkpoint, Mapping):
            raise PipelineSam2StageError(
                "sam2_batch_checkpoint_invalid"
            )
        current_manifest = checkpoint.get("model_manifest")
        if not isinstance(current_manifest, Mapping):
            raise PipelineSam2StageError(
                "sam2_batch_checkpoint_invalid"
            )
        if model_manifest is None:
            model_manifest = dict(current_manifest)
        elif dict(current_manifest) != dict(model_manifest):
            raise PipelineSam2StageError("sam2_model_changed")
        for key, sink in (
            ("proposals", proposals),
            ("failures", failures),
        ):
            rows = checkpoint.get(key)
            if not isinstance(rows, list):
                raise PipelineSam2StageError(
                    "sam2_batch_checkpoint_invalid"
                )
            for row in rows:
                if (
                    not isinstance(row, Mapping)
                    or not isinstance(
                        row.get("candidate_id"), str
                    )
                    or row["candidate_id"] in seen
                ):
                    raise PipelineSam2StageError(
                        "sam2_batch_checkpoint_invalid"
                    )
                seen.add(row["candidate_id"])
                sink.append(dict(row))
    if len(seen) != prompt_count:
        raise PipelineSam2StageError(
            "sam2_batch_checkpoint_invalid"
        )
    checkpoint_without_hash = {
        "schema_version": PIPELINE_SAM2_SCHEMA,
        "state": (
            "completed"
            if proposals and not failures
            else "partial"
            if proposals
            else "failed"
            if failures
            else "zero_prompts"
        ),
        "model_manifest": dict(model_manifest or {}),
        "proposals": proposals,
        "failures": failures,
        "mask_generated": bool(proposals),
        "tracking_performed": any(
            row.get("tracking_performed") is True
            for row in proposals
        ),
        "review_only": True,
        "requires_user_confirmation": True,
        "automatic_annotation_allowed": False,
        "automatic_incident_creation_allowed": False,
        "training_membership_allowed": False,
        "annotation_count": 0,
        "incident_count": 0,
    }
    metrics = {
        "prompt_count": prompt_count,
        "mask_count": len(proposals),
        "tracked_candidate_count": sum(
            row.get("tracking_performed") is True
            for row in proposals
        ),
        "tracked_frame_count": sum(
            len(row.get("tracked_frames", ()))
            for row in proposals
        ),
        "failure_count": len(failures),
        "batch_count": len(results),
        "annotation_count": 0,
        "incident_count": 0,
    }
    return {
        "status": (
            "success"
            if proposals or not failures
            else "failed"
        ),
        "error_code": (
            "sam2_partial_failure"
            if proposals and failures
            else "sam2_inference_failed"
            if failures
            else "sam2_no_prompt_boxes"
            if not proposals
            else None
        ),
        "checkpoint": {
            **checkpoint_without_hash,
            "sam2_revision": _sha(checkpoint_without_hash),
        },
        "metrics": metrics,
    }


def run_pipeline_sam2_batches(
    *,
    adapter: PipelineSam2Adapter,
    prompts: Sequence[Sam2CandidatePrompt],
    cancel: Callable[[], bool],
) -> Mapping[str, Any]:
    """Run every SAM2 prompt in safe deterministic chunks."""

    if (
        isinstance(prompts, (str, bytes, bytearray))
        or len(prompts) > 1_024
        or not callable(cancel)
    ):
        raise PipelineSam2StageError("sam2_stage_input_invalid")
    if not prompts:
        return run_pipeline_sam2_stage(
            adapter=adapter,
            prompts=(),
            cancel=cancel,
        )
    candidate_ids = [
        prompt.candidate_id for prompt in prompts
    ]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise PipelineSam2StageError(
            "sam2_candidate_prompt_invalid"
        )
    results = []
    for offset in range(0, len(prompts), MAX_CANDIDATES):
        if cancel():
            return {
                "status": "cancelled",
                "error_code": "sam2_stage_cancelled",
            }
        result = run_pipeline_sam2_stage(
            adapter=adapter,
            prompts=prompts[offset : offset + MAX_CANDIDATES],
            cancel=cancel,
        )
        if result.get("status") == "cancelled":
            return result
        results.append(result)
    return _merge_batch_results(
        results,
        prompt_count=len(prompts),
    )


__all__ = [
    "run_pipeline_sam2_batches",
    "select_balanced_sam2_frames",
]
