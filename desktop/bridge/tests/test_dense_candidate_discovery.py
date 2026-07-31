from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable, Sequence
from uuid import UUID

import pytest

from app.dense_candidate_discovery import (
    ActiveProjectModelDenseProposalProvider,
    ApprovedReferenceDenseProposalProvider,
    DenseCandidateDiscoveryCancelled,
    DenseCandidateDiscoveryEngine,
    DenseCandidateDiscoveryError,
    DenseCandidateDiscoveryIntegrityError,
    DenseDiscoveryLimits,
    DenseFrameEvidence,
    DenseFrameProposal,
    LocalVisionDenseProposalProvider,
    capture_promoted_model_job_binding,
)
from app.project_model_lifecycle import (
    LOCAL_OBJECT_DETECTOR_MODEL_FORMAT,
    ProjectModelLifecycleError,
    ResolvedActiveProjectModel,
    VISUAL_CENTROID_MODEL_FORMAT,
)
from app.weak_case_visual_index import WeakCaseVisualIndexError


PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
TRAINING_ANALYSIS_JOB_ID = UUID(
    "22222222-2222-4222-8222-222222222222"
)
VALIDATION_ANALYSIS_JOB_ID = UUID(
    "33333333-3333-4333-8333-333333333333"
)
SURVEY_ANALYSIS_JOB_ID = UUID(
    "44444444-4444-4444-8444-444444444444"
)
SOURCE_SHA256 = "a" * 64


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _dense_index(
    root: Path,
    *,
    frame_count: int,
) -> tuple[Path, str, list[dict[str, object]]]:
    artifact_root = root / "dense"
    frames_root = artifact_root / "frames"
    frames_root.mkdir(parents=True)
    frames: list[dict[str, object]] = []
    for index in range(frame_count):
        body = b"\xff\xd8" + f"frame-{index}".encode("ascii") + b"\xff\xd9"
        file_name = f"frame-{index:08d}.jpg"
        path = frames_root / file_name
        path.write_bytes(body)
        frames.append(
            {
                "frame_id": hashlib.sha256(
                    (
                        "trafficvisson.dense-frame-index.v1"
                        f"\0{SOURCE_SHA256}\0fps=2\0frame={index}"
                    ).encode("ascii")
                ).hexdigest()[:24],
                "frame_index": index,
                "timestamp_seconds": index / 2,
                "timestamp_numerator": index,
                "timestamp_denominator": 2,
                "relative_path": f"frames/{file_name}",
                "width": 1920,
                "height": 1080,
                "size_bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "media_type": "image/jpeg",
                "evidence_status": "unreviewed_candidate",
            }
        )
    payload = {
        "schema_version": "trafficvisson.dense-frame-index.v1",
        "frame_count": len(frames),
        "frames": frames,
        "provenance": {
            "schema_version": "trafficvisson.dense-frame-index.v1",
            "source_sha256": SOURCE_SHA256,
            "sampling_fps": 2,
            "frame_count": len(frames),
            "timestamp_method": (
                "zero_based_frame_index_divided_by_integer_sampling_fps"
            ),
        },
    }
    index_path = artifact_root / "frame-index.json"
    body = _canonical_json(payload)
    index_path.write_bytes(body)
    return index_path, hashlib.sha256(body).hexdigest(), frames


class _RecordingProvider:
    def __init__(
        self,
        proposal_factory: Callable[
            [Sequence[DenseFrameEvidence]],
            Sequence[DenseFrameProposal],
        ],
        *,
        after_call: Callable[[], None] | None = None,
    ) -> None:
        self.proposal_factory = proposal_factory
        self.after_call = after_call
        self.batches: list[tuple[str, ...]] = []

    def status(self, project_id: UUID) -> dict[str, object]:
        assert project_id == PROJECT_ID
        return {
            "provider": "recording_provider",
            "state": "ready",
            "manifest_sha256": "b" * 64,
        }

    def propose(
        self,
        project_id: UUID,
        frames: Sequence[DenseFrameEvidence],
        *,
        frames_root: Path,
        cancel: Callable[[], bool],
    ) -> Sequence[DenseFrameProposal]:
        assert project_id == PROJECT_ID
        assert frames_root.name == "frames"
        assert not cancel()
        self.batches.append(tuple(frame.frame_id for frame in frames))
        values = self.proposal_factory(frames)
        if self.after_call is not None:
            self.after_call()
        return values


def _engine(
    *providers: object,
    batch_size: int = 3,
    max_proposals_per_frame: int = 4,
    temporal_gap_seconds: float = 1.5,
) -> DenseCandidateDiscoveryEngine:
    return DenseCandidateDiscoveryEngine(
        providers,
        limits=DenseDiscoveryLimits(
            batch_size=batch_size,
            max_frames=100,
            max_proposals_per_frame=max_proposals_per_frame,
            max_candidates=100,
            temporal_gap_seconds=temporal_gap_seconds,
            minimum_score=0.5,
        ),
    )


def _discover(
    engine: DenseCandidateDiscoveryEngine,
    index_path: Path,
    index_sha256: str,
    *,
    cancel: Callable[[], bool] = lambda: False,
    analysis_job_id: UUID = SURVEY_ANALYSIS_JOB_ID,
    expected_promoted_model_binding: object = None,
):
    return engine.discover(
        project_id=PROJECT_ID,
        analysis_job_id=analysis_job_id,
        expected_promoted_model_binding=(
            expected_promoted_model_binding
        ),
        source_sha256=SOURCE_SHA256,
        dense_index_path=index_path,
        dense_index_sha256=index_sha256,
        cancel=cancel,
    )


class _DenseOcrService:
    def status(self):
        return {
            "enabled": True,
            "pack_id": "easyocr-ko",
            "pack_version": "1",
            "manifest_sha256": "f" * 64,
            "capabilities": ["ocr"],
        }

    def infer_candidates(self, candidates, _frames_root, *, cancel):
        assert cancel() is False
        return (
            {
                candidate["candidate_id"]: {
                    "mode": "local_model",
                    "ocr_observations": [
                        {
                            "text": "37루 4532",
                            "confidence": 0.999,
                            "box": {
                                "x": 0.28,
                                "y": 0.66,
                                "width": 0.10,
                                "height": 0.03,
                            },
                        },
                        {
                            "text": "1671",
                            "confidence": 0.95,
                            "box": {
                                "x": 0.78,
                                "y": 0.47,
                                "width": 0.02,
                                "height": 0.02,
                            },
                        },
                        {
                            "text": "진영 자유마을",
                            "confidence": 0.91,
                            "box": {
                                "x": 0.75,
                                "y": 0.10,
                                "width": 0.12,
                                "height": 0.40,
                            },
                        },
                    ],
                }
                for candidate in candidates
            },
            {
                "pack_id": "easyocr-ko",
                "pack_version": "1",
                "manifest_sha256": "f" * 64,
                "mode": "local_model",
            },
        )


class _DenseDetectorService:
    def status(self):
        return {
            "enabled": True,
            "pack_id": "road-detector",
            "pack_version": "1",
            "manifest_sha256": "e" * 64,
            "capabilities": ["defect_detection"],
        }

    def infer_candidates(self, candidates, _frames_root, *, cancel):
        assert cancel() is False
        return (
            {
                candidate["candidate_id"]: {
                    "mode": "local_model",
                    "predictions": [
                        {
                            "issue_type": "pothole",
                            "confidence": 0.81,
                            "box": {
                                "x": 0.4,
                                "y": 0.55,
                                "width": 0.2,
                                "height": 0.2,
                            },
                            "source_label": "pothole",
                            "calibration": "model_score",
                        }
                    ],
                }
                for candidate in candidates
            },
            {
                "pack_id": "road-detector",
                "pack_version": "1",
                "manifest_sha256": "e" * 64,
                "mode": "local_model",
            },
        )


def test_dense_detector_consumes_normalized_vision_confidence_contract():
    frame = DenseFrameEvidence(
        frame_id="a" * 24,
        frame_index=0,
        timestamp_seconds=0.0,
        file_name="frame-00000000.jpg",
        size_bytes=10,
        sha256="b" * 64,
        media_type="image/jpeg",
        path=Path("C:/qa/frame-00000000.jpg"),
    )
    provider = LocalVisionDenseProposalProvider(
        _DenseDetectorService(),
        role="detector",
    )

    proposals = provider.propose(
        PROJECT_ID,
        [frame],
        frames_root=Path("C:/qa"),
        cancel=lambda: False,
    )

    assert len(proposals) == 1
    assert proposals[0].issue_type == "pothole"
    assert proposals[0].score == 0.81
    assert proposals[0].provenance["calibration"] == "model_score"
    assert (
        proposals[0].provenance["score_semantics"]
        == "model_score_not_probability"
    )
    assert proposals[0].provenance["spatial_gate"] == "forward_road_roi_v1"


def test_dense_detector_rejects_road_surface_box_outside_forward_road_roi():
    class EdgeDetector(_DenseDetectorService):
        def infer_candidates(self, candidates, _frames_root, *, cancel):
            results, summary = super().infer_candidates(
                candidates,
                _frames_root,
                cancel=cancel,
            )
            for result in results.values():
                result["predictions"][0]["box"] = {
                    "x": 0.88,
                    "y": 0.54,
                    "width": 0.12,
                    "height": 0.12,
                }
            return results, summary

    frame = DenseFrameEvidence(
        frame_id="a" * 24,
        frame_index=0,
        timestamp_seconds=0.0,
        file_name="frame-00000000.jpg",
        size_bytes=10,
        sha256="b" * 64,
        media_type="image/jpeg",
        path=Path("C:/qa/frame-00000000.jpg"),
    )
    proposals = LocalVisionDenseProposalProvider(
        EdgeDetector(),
        role="detector",
    ).propose(
        PROJECT_ID,
        [frame],
        frames_root=Path("C:/qa"),
        cancel=lambda: False,
    )

    assert proposals == []


def test_dense_ocr_requires_meaningful_korean_sign_text():
    frame = DenseFrameEvidence(
        frame_id="a" * 24,
        frame_index=0,
        timestamp_seconds=0.0,
        file_name="frame-00000000.jpg",
        size_bytes=10,
        sha256="b" * 64,
        media_type="image/jpeg",
        path=Path("C:/qa/frame-00000000.jpg"),
    )
    provider = LocalVisionDenseProposalProvider(
        _DenseOcrService(),
        role="ocr",
    )

    proposals = provider.propose(
        PROJECT_ID,
        [frame],
        frames_root=Path("C:/qa"),
        cancel=lambda: False,
    )

    assert len(proposals) == 1
    assert proposals[0].ocr_text == "진영 자유마을"
    assert proposals[0].issue_type == "guide_sign_identity"


def test_dense_ocr_does_not_turn_warning_or_short_fragment_into_identity():
    class SignOcr(_DenseOcrService):
        def infer_candidates(self, candidates, _frames_root, *, cancel):
            results, summary = super().infer_candidates(
                candidates,
                _frames_root,
                cancel=cancel,
            )
            for result in results.values():
                result["ocr_observations"] = [
                    {
                        "text": "중부",
                        "confidence": 1.0,
                        "box": {
                            "x": 0.7,
                            "y": 0.5,
                            "width": 0.05,
                            "height": 0.05,
                        },
                    },
                    {
                        "text": "안개 지역 위험",
                        "confidence": 0.95,
                        "box": {
                            "x": 0.7,
                            "y": 0.5,
                            "width": 0.15,
                            "height": 0.05,
                        },
                    },
                    {
                        "text": "및 건설기",
                        "confidence": 0.98,
                        "box": {
                            "x": 0.7,
                            "y": 0.5,
                            "width": 0.15,
                            "height": 0.05,
                        },
                    },
                    {
                        "text": "가세요",
                        "confidence": 0.96,
                        "box": {
                            "x": 0.7,
                            "y": 0.5,
                            "width": 0.08,
                            "height": 0.05,
                        },
                    },
                    {
                        "text": "대전",
                        "confidence": 0.97,
                        "box": {
                            "x": 0.7,
                            "y": 0.5,
                            "width": 0.05,
                            "height": 0.05,
                        },
                    },
                    {
                        "text": "정안알밤휴게소",
                        "confidence": 0.62,
                        "box": {
                            "x": 0.6,
                            "y": 0.4,
                            "width": 0.2,
                            "height": 0.08,
                        },
                    },
                ]
            return results, summary

    frame = DenseFrameEvidence(
        frame_id="a" * 24,
        frame_index=0,
        timestamp_seconds=0.0,
        file_name="frame-00000000.jpg",
        size_bytes=10,
        sha256="b" * 64,
        media_type="image/jpeg",
        path=Path("C:/qa/frame-00000000.jpg"),
    )
    proposals = LocalVisionDenseProposalProvider(
        SignOcr(),
        role="ocr",
    ).propose(
        PROJECT_ID,
        [frame],
        frames_root=Path("C:/qa"),
        cancel=lambda: False,
    )

    assert [proposal.ocr_text for proposal in proposals] == [
        "대전",
        "정안알밤휴게소",
    ]


def test_scans_every_real_2fps_frame_in_bounded_batches_and_creates_no_truth(
    tmp_path: Path,
) -> None:
    index_path, index_sha256, frames = _dense_index(
        tmp_path,
        frame_count=7,
    )
    provider = _RecordingProvider(
        lambda batch: [
            DenseFrameProposal(
                frame_id=frame.frame_id,
                issue_type="pothole",
                score=0.8,
                proposal_source="recording_dense_2fps",
                provenance={"manifest_sha256": "b" * 64},
            )
            for frame in batch
        ]
    )

    result = _discover(_engine(provider, batch_size=3), index_path, index_sha256)

    assert result.sampling_fps == 2
    assert result.scanned_frame_count == 7
    assert [len(batch) for batch in provider.batches] == [3, 3, 1]
    assert [item for batch in provider.batches for item in batch] == [
        str(frame["frame_id"]) for frame in frames
    ]
    checkpoint = result.to_checkpoint()
    assert checkpoint["review_only"] is True
    assert checkpoint["automatic_annotation_creation"] is False
    assert checkpoint["automatic_incident_creation"] is False
    assert checkpoint["automatic_training_ingest"] is False
    assert checkpoint["automatic_report_truth"] is False
    assert checkpoint["annotations_created"] == 0
    assert checkpoint["incidents_created"] == 0
    assert checkpoint["training_members_created"] == 0
    assert checkpoint["report_truth_records_created"] == 0


def test_enforces_per_frame_and_current_batch_proposal_bounds(
    tmp_path: Path,
) -> None:
    index_path, index_sha256, frames = _dense_index(
        tmp_path,
        frame_count=3,
    )
    overflow = _RecordingProvider(
        lambda batch: [
            DenseFrameProposal(
                frame_id=batch[0].frame_id,
                issue_type="pothole",
                score=0.9,
                proposal_source=f"overflow-{index}",
            )
            for index in range(3)
        ]
    )
    with pytest.raises(
        DenseCandidateDiscoveryError,
        match="proposal bound",
    ):
        _discover(
            _engine(
                overflow,
                batch_size=2,
                max_proposals_per_frame=2,
            ),
            index_path,
            index_sha256,
        )

    outside_batch = _RecordingProvider(
        lambda _batch: [
            DenseFrameProposal(
                frame_id=str(frames[-1]["frame_id"]),
                issue_type="pothole",
                score=0.9,
                proposal_source="outside_current_batch",
            )
        ]
    )
    with pytest.raises(
        DenseCandidateDiscoveryError,
        match="invalid",
    ):
        _discover(
            _engine(outside_batch, batch_size=1),
            index_path,
            index_sha256,
        )


def _resolved_model(
    root: Path,
    *,
    suffix: str,
    revision: int,
    model_format: str = VISUAL_CENTROID_MODEL_FORMAT,
) -> ResolvedActiveProjectModel:
    source_lineage = {
        "training_analysis_job_ids": [
            str(TRAINING_ANALYSIS_JOB_ID)
        ],
        "fixed_validation_analysis_job_ids": [
            str(VALIDATION_ANALYSIS_JOB_ID)
        ],
    }
    return ResolvedActiveProjectModel(
        project_id=str(PROJECT_ID),
        state_revision=revision,
        model_version_id=(
            f"{suffix * 8}-{suffix * 4}-4{suffix * 3}-"
            f"8{suffix * 3}-{suffix * 12}"
        ),
        model_version=f"project-model-{suffix}@1.0",
        model_format=model_format,
        dataset_version_id=(
            f"{suffix * 8}-{suffix * 4}-5{suffix * 3}-"
            f"8{suffix * 3}-{suffix * 12}"
        ),
        dataset_hash=suffix * 64,
        fixed_validation_sha256=("c" if suffix != "c" else "d") * 64,
        primary_artifact_path=root / f"{suffix}.json",
        primary_artifact_sha256=("d" if suffix != "d" else "e") * 64,
        artifact_manifest_path=root / f"{suffix}-manifest.json",
        artifact_manifest_sha256=("e" if suffix != "e" else "f") * 64,
        training_analysis_job_ids=(
            str(TRAINING_ANALYSIS_JOB_ID),
        ),
        fixed_validation_analysis_job_ids=(
            str(VALIDATION_ANALYSIS_JOB_ID),
        ),
        source_analysis_lineage_sha256=hashlib.sha256(
            _canonical_json(source_lineage)
        ).hexdigest(),
        dataset_provenance_graph_sha256=hashlib.sha256(
            f"dataset-provenance:{suffix}".encode("utf-8")
        ).hexdigest(),
    )


class _FakeProjectInference:
    def __init__(self) -> None:
        self.models: list[ResolvedActiveProjectModel] = []

    def classify(
        self,
        model: ResolvedActiveProjectModel,
        *,
        body: bytes,
        media_type: str,
        expected_sha256: str,
    ) -> dict[str, object]:
        assert hashlib.sha256(body).hexdigest() == expected_sha256
        assert media_type == "image/jpeg"
        self.models.append(model)
        return {
            "model_version_id": model.model_version_id,
            "model_version": model.model_version,
            "dataset_version_id": model.dataset_version_id,
            "predictions": [
                {"issue_type": "pothole", "score": 0.91}
            ],
        }


class _FakePromotedDetectorInference:
    def __init__(
        self,
        *,
        state: str = "ready",
        predictions: Sequence[dict[str, object]] = (),
    ) -> None:
        self.state = state
        self.predictions = tuple(predictions)
        self.calls: list[tuple[ResolvedActiveProjectModel, str]] = []

    def status(self) -> dict[str, object]:
        return {
            "provider": "promoted_local_object_detector",
            "state": self.state,
            "reason": (
                None
                if self.state == "ready"
                else f"fake_detector_{self.state}"
            ),
            "backend_id": "fake-detector",
            "backend_version": "1.0",
        }

    def classify(
        self,
        model: ResolvedActiveProjectModel,
        *,
        survey_analysis_job_id: str,
        body: bytes,
        media_type: str,
        expected_sha256: str,
    ) -> dict[str, object]:
        assert hashlib.sha256(body).hexdigest() == expected_sha256
        assert media_type == "image/jpeg"
        self.calls.append((model, survey_analysis_job_id))
        return {
            "model_version_id": model.model_version_id,
            "model_version": model.model_version,
            "dataset_version_id": model.dataset_version_id,
            "candidate_image_sha256": expected_sha256,
            "predictions": list(self.predictions),
        }


def test_freezes_promoted_project_model_and_binds_candidate_provenance(
    tmp_path: Path,
) -> None:
    index_path, index_sha256, _frames = _dense_index(
        tmp_path,
        frame_count=3,
    )
    first = _resolved_model(tmp_path, suffix="1", revision=4)
    second = _resolved_model(tmp_path, suffix="2", revision=5)
    resolved: list[ResolvedActiveProjectModel] = []

    def resolver(_project_id: UUID) -> ResolvedActiveProjectModel:
        model = first if not resolved else second
        resolved.append(model)
        return model

    inference = _FakeProjectInference()
    provider = ActiveProjectModelDenseProposalProvider(
        resolver,
        inference,
    )
    result = _discover(
        _engine(provider, batch_size=2),
        index_path,
        index_sha256,
    )

    assert resolved == [first]
    assert inference.models == [first, first, first]
    state = result.provider_states[0]
    assert state["model_version_id"] == first.model_version_id
    assert state["dataset_hash"] == first.dataset_hash
    assert state["fixed_validation_sha256"] == (
        first.fixed_validation_sha256
    )
    assert state["primary_artifact_sha256"] == (
        first.primary_artifact_sha256
    )
    assert result.candidates[0].representative_proposal_provenance == {
        "artifact_manifest_sha256": first.artifact_manifest_sha256,
        "dataset_hash": first.dataset_hash,
        "dataset_version_id": first.dataset_version_id,
        "fixed_validation_sha256": first.fixed_validation_sha256,
        "model_version": first.model_version,
        "model_version_id": first.model_version_id,
        "primary_artifact_sha256": first.primary_artifact_sha256,
        "state_revision": first.state_revision,
        "survey_analysis_job_id": str(SURVEY_ANALYSIS_JOB_ID),
        "independent_survey_verified": True,
        "source_analysis_lineage_sha256": (
            first.source_analysis_lineage_sha256
        ),
        "dataset_provenance_graph_sha256": (
            first.dataset_provenance_graph_sha256
        ),
    }
    assert len(result.candidates[0].proposal_provenance_sha256) == 64


def test_rejects_promoted_model_changed_after_pipeline_binding_capture(
    tmp_path: Path,
) -> None:
    index_path, index_sha256, _frames = _dense_index(
        tmp_path,
        frame_count=3,
    )
    first = _resolved_model(tmp_path, suffix="1", revision=4)
    second = _resolved_model(tmp_path, suffix="2", revision=5)
    expected_binding = capture_promoted_model_job_binding(
        PROJECT_ID,
        lambda _project_id: first,
    )
    inference = _FakeProjectInference()
    provider = ActiveProjectModelDenseProposalProvider(
        lambda _project_id: second,
        inference,
    )

    with pytest.raises(
        DenseCandidateDiscoveryIntegrityError,
        match="promoted_project_model_binding_conflict",
    ):
        _discover(
            _engine(provider),
            index_path,
            index_sha256,
            expected_promoted_model_binding=expected_binding,
        )

    assert inference.models == []


def test_rejects_resealed_noncanonical_promoted_model_binding(
    tmp_path: Path,
) -> None:
    index_path, index_sha256, _frames = _dense_index(
        tmp_path,
        frame_count=1,
    )
    model = _resolved_model(tmp_path, suffix="1", revision=4)
    binding = capture_promoted_model_job_binding(
        PROJECT_ID,
        lambda _project_id: model,
    )
    binding["state_revision"] = 4.0
    binding.pop("binding_sha256")
    binding["binding_sha256"] = hashlib.sha256(
        _canonical_json(binding)
    ).hexdigest()
    provider = ActiveProjectModelDenseProposalProvider(
        lambda _project_id: model,
        _FakeProjectInference(),
    )

    with pytest.raises(
        DenseCandidateDiscoveryError,
        match="expected promoted model job binding is invalid",
    ):
        _discover(
            _engine(provider),
            index_path,
            index_sha256,
            expected_promoted_model_binding=binding,
        )


def test_preserves_stable_promoted_model_resolution_failure_as_failed_state(
    tmp_path: Path,
) -> None:
    index_path, index_sha256, _frames = _dense_index(
        tmp_path,
        frame_count=1,
    )

    def failed_resolver(
        _project_id: UUID,
    ) -> ResolvedActiveProjectModel:
        raise ProjectModelLifecycleError(
            "artifact_integrity_failed",
            "test artifact failure",
        )

    binding = capture_promoted_model_job_binding(
        PROJECT_ID,
        failed_resolver,
    )
    provider = ActiveProjectModelDenseProposalProvider(
        failed_resolver,
        _FakeProjectInference(),
    )

    result = _discover(
        _engine(provider),
        index_path,
        index_sha256,
        expected_promoted_model_binding=binding,
    )

    assert result.candidates == ()
    assert result.provider_states == (
        {
            "provider": "promoted_project_model",
            "state": "failed",
            "reason": "artifact_integrity_failed",
        },
    )


def test_routes_promoted_detector_and_preserves_box_and_survey_lineage(
    tmp_path: Path,
) -> None:
    index_path, index_sha256, _frames = _dense_index(
        tmp_path,
        frame_count=1,
    )
    model = _resolved_model(
        tmp_path,
        suffix="1",
        revision=4,
        model_format=LOCAL_OBJECT_DETECTOR_MODEL_FORMAT,
    )
    centroid = _FakeProjectInference()
    detector = _FakePromotedDetectorInference(
        predictions=(
            {
                "issue_type": "pothole",
                "score": 0.93,
                "box": {
                    "x": 0.1,
                    "y": 0.2,
                    "width": 0.3,
                    "height": 0.4,
                },
            },
        )
    )
    provider = ActiveProjectModelDenseProposalProvider(
        lambda _project_id: model,
        centroid,
        detector_inference=detector,
    )

    result = _discover(
        _engine(provider),
        index_path,
        index_sha256,
    )

    assert centroid.models == []
    assert detector.calls == [(model, str(SURVEY_ANALYSIS_JOB_ID))]
    assert result.provider_states[0]["state"] == "ready"
    assert result.provider_states[0]["model_format"] == (
        LOCAL_OBJECT_DETECTOR_MODEL_FORMAT
    )
    assert len(result.candidates) == 1
    assert result.candidates[0].normalized_box == {
        "x": 0.1,
        "y": 0.2,
        "width": 0.3,
        "height": 0.4,
    }
    provenance = result.candidates[
        0
    ].representative_proposal_provenance
    assert provenance is not None
    assert provenance["model_format"] == (
        LOCAL_OBJECT_DETECTOR_MODEL_FORMAT
    )
    assert provenance["survey_analysis_job_id"] == str(
        SURVEY_ANALYSIS_JOB_ID
    )


@pytest.mark.parametrize("runtime_state", ["unavailable", "failed"])
def test_promoted_detector_runtime_state_is_not_zero_detection(
    tmp_path: Path,
    runtime_state: str,
) -> None:
    index_path, index_sha256, _frames = _dense_index(
        tmp_path,
        frame_count=1,
    )
    model = _resolved_model(
        tmp_path,
        suffix="1",
        revision=4,
        model_format=LOCAL_OBJECT_DETECTOR_MODEL_FORMAT,
    )
    detector = _FakePromotedDetectorInference(
        state=runtime_state,
    )
    provider = ActiveProjectModelDenseProposalProvider(
        lambda _project_id: model,
        detector_inference=detector,
    )

    result = _discover(
        _engine(provider),
        index_path,
        index_sha256,
    )

    assert result.candidates == ()
    assert result.provider_states[0]["state"] == runtime_state
    assert result.provider_states[0]["reason"] == (
        f"fake_detector_{runtime_state}"
    )
    assert detector.calls == []


def test_ready_promoted_detector_with_no_boxes_is_zero_detection(
    tmp_path: Path,
) -> None:
    index_path, index_sha256, _frames = _dense_index(
        tmp_path,
        frame_count=1,
    )
    model = _resolved_model(
        tmp_path,
        suffix="1",
        revision=4,
        model_format=LOCAL_OBJECT_DETECTOR_MODEL_FORMAT,
    )
    detector = _FakePromotedDetectorInference()
    provider = ActiveProjectModelDenseProposalProvider(
        lambda _project_id: model,
        detector_inference=detector,
    )

    result = _discover(
        _engine(provider),
        index_path,
        index_sha256,
    )

    assert result.provider_states[0]["state"] == "ready"
    assert result.candidates == ()
    assert detector.calls == [(model, str(SURVEY_ANALYSIS_JOB_ID))]


def test_promoted_project_model_cannot_resurvey_training_or_validation_job(
    tmp_path: Path,
) -> None:
    index_path, index_sha256, _frames = _dense_index(
        tmp_path,
        frame_count=1,
    )
    model = _resolved_model(tmp_path, suffix="1", revision=4)
    inference = _FakeProjectInference()
    provider = ActiveProjectModelDenseProposalProvider(
        lambda _project_id: model,
        inference,
    )

    for reused_job_id in (
        TRAINING_ANALYSIS_JOB_ID,
        VALIDATION_ANALYSIS_JOB_ID,
    ):
        result = _discover(
            _engine(provider),
            index_path,
            index_sha256,
            analysis_job_id=reused_job_id,
        )
        assert result.candidates == ()
        assert result.provider_states[0] == {
            "provider": "promoted_project_model",
            "state": "unavailable",
            "reason": "survey_analysis_reuses_model_source",
            "survey_analysis_job_id": str(reused_job_id),
            "source_analysis_lineage_sha256": (
                model.source_analysis_lineage_sha256
            ),
        }
    assert inference.models == []


class _ReferenceService:
    def __init__(
        self,
        cases: Sequence[dict[str, object]],
        *,
        query_error: WeakCaseVisualIndexError | None = None,
        approved_reference_count: int = 1,
    ) -> None:
        self.cases = list(cases)
        self.query_error = query_error
        self.approved_reference_count = approved_reference_count
        self.queries = 0

    def build(self, project_id: UUID) -> dict[str, object]:
        assert project_id == PROJECT_ID
        return {
            "index_manifest_sha256": "6" * 64,
            "source_snapshot_sha256": "7" * 64,
            "approved_reference_count": self.approved_reference_count,
        }

    def query(self, project_id: UUID, evidence, **kwargs):
        assert project_id == PROJECT_ID
        assert evidence.project_id == PROJECT_ID
        assert kwargs == {
            "issue_type": None,
            "query_text": None,
            "limit": 5,
        }
        self.queries += 1
        if self.query_error is not None:
            raise self.query_error
        return {
            "index_manifest_sha256": "6" * 64,
            "source_snapshot_sha256": "7" * 64,
            "cases": self.cases,
        }


def _reference_case(
    *,
    case_id: str,
    approved: bool,
    issue_type: str,
    score: float,
) -> dict[str, object]:
    return {
        "case_id": case_id,
        "source_id": "sample-hwpx-3q-24ffb4bc62cd86a6",
        "approved_reference": approved,
        "approved_issue_type": issue_type,
        "combined_similarity_score": score,
        "approval_record_sha256": "8" * 64,
    }


def test_reference_provider_uses_only_approved_3q_4q_cases(
    tmp_path: Path,
) -> None:
    index_path, index_sha256, _frames = _dense_index(
        tmp_path,
        frame_count=1,
    )
    service = _ReferenceService(
        [
            _reference_case(
                case_id="unapproved-high",
                approved=False,
                issue_type="road_debris",
                score=0.99,
            ),
            _reference_case(
                case_id="approved-lower",
                approved=True,
                issue_type="roadside_mowing_required",
                score=0.85,
            ),
        ]
    )
    result = _discover(
        _engine(
            ApprovedReferenceDenseProposalProvider(
                service,
                minimum_similarity=0.78,
            )
        ),
        index_path,
        index_sha256,
    )

    assert service.queries == 1
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.issue_type == "roadside_mowing_required"
    assert candidate.representative_proposal_provenance[
        "reference_case_id"
    ] == "approved-lower"
    assert "unapproved-high" not in json.dumps(
        candidate.representative_proposal_provenance
    )


def test_reference_provider_skips_dense_embedding_without_approvals(
    tmp_path: Path,
) -> None:
    index_path, index_sha256, _frames = _dense_index(
        tmp_path,
        frame_count=3,
    )
    service = _ReferenceService(
        [
            _reference_case(
                case_id="weak-only",
                approved=False,
                issue_type="roadside_mowing_required",
                score=0.99,
            )
        ],
        approved_reference_count=0,
    )

    result = _discover(
        _engine(ApprovedReferenceDenseProposalProvider(service)),
        index_path,
        index_sha256,
    )

    assert service.queries == 0
    assert result.candidates == ()


def test_reference_index_integrity_errors_fail_closed(
    tmp_path: Path,
) -> None:
    index_path, index_sha256, _frames = _dense_index(
        tmp_path,
        frame_count=1,
    )
    service = _ReferenceService(
        [],
        query_error=WeakCaseVisualIndexError(
            "visual_index_invalid",
            "tampered approved-reference index",
        ),
    )
    provider = ApprovedReferenceDenseProposalProvider(service)
    with pytest.raises(
        DenseCandidateDiscoveryIntegrityError,
        match="approved_reference_query_failed",
    ):
        _discover(_engine(provider), index_path, index_sha256)


def test_temporal_merge_keeps_incident_types_and_gaps_separate(
    tmp_path: Path,
) -> None:
    index_path, index_sha256, _frames = _dense_index(
        tmp_path,
        frame_count=8,
    )

    def proposals(
        batch: Sequence[DenseFrameEvidence],
    ) -> list[DenseFrameProposal]:
        values: list[DenseFrameProposal] = []
        for frame in batch:
            if frame.frame_index in {0, 1, 2, 7}:
                values.append(
                    DenseFrameProposal(
                        frame_id=frame.frame_id,
                        issue_type="pothole",
                        score=0.8 + frame.frame_index / 100,
                        proposal_source="temporal-test",
                    )
                )
            if frame.frame_index == 1:
                values.append(
                    DenseFrameProposal(
                        frame_id=frame.frame_id,
                        issue_type="road_debris",
                        score=0.95,
                        proposal_source="temporal-test",
                        urgent_review=True,
                    )
                )
        return values

    result = _discover(
        _engine(
            _RecordingProvider(proposals),
            batch_size=3,
            temporal_gap_seconds=1.0,
        ),
        index_path,
        index_sha256,
    )

    assert [
        (candidate.issue_type, len(candidate.supporting_frame_ids))
        for candidate in result.candidates
    ] == [
        ("road_debris", 1),
        ("pothole", 3),
        ("pothole", 1),
    ]


def test_cancel_after_provider_call_stops_before_candidate_commit(
    tmp_path: Path,
) -> None:
    index_path, index_sha256, _frames = _dense_index(
        tmp_path,
        frame_count=2,
    )
    state = {"cancelled": False}
    provider = _RecordingProvider(
        lambda _batch: [],
        after_call=lambda: state.__setitem__("cancelled", True),
    )
    with pytest.raises(DenseCandidateDiscoveryCancelled):
        _discover(
            _engine(provider),
            index_path,
            index_sha256,
            cancel=lambda: state["cancelled"],
        )


def test_frame_and_index_hash_changes_fail_closed_even_without_ready_provider(
    tmp_path: Path,
) -> None:
    index_path, index_sha256, _frames = _dense_index(
        tmp_path,
        frame_count=2,
    )
    (index_path.parent / "frames" / "frame-00000001.jpg").write_bytes(
        b"tampered-frame"
    )
    with pytest.raises(
        DenseCandidateDiscoveryIntegrityError,
        match="dense_frame_hash_mismatch",
    ):
        _discover(_engine(), index_path, index_sha256)

    fresh_index, fresh_sha256, _ = _dense_index(
        tmp_path / "fresh",
        frame_count=1,
    )
    with pytest.raises(
        DenseCandidateDiscoveryIntegrityError,
        match="dense_index_hash_mismatch",
    ):
        _discover(_engine(), fresh_index, "f" * 64)


def test_provider_provenance_rejects_path_or_secret_leakage(
    tmp_path: Path,
) -> None:
    index_path, index_sha256, _frames = _dense_index(
        tmp_path,
        frame_count=1,
    )

    class UnsafeProvider(_RecordingProvider):
        def status(self, project_id: UUID) -> dict[str, object]:
            del project_id
            return {
                "provider": "unsafe",
                "state": "ready",
                "artifact_path": r"C:\secret\model.bin",
            }

    with pytest.raises(
        DenseCandidateDiscoveryError,
        match="provider state is invalid",
    ):
        _discover(
            _engine(UnsafeProvider(lambda _batch: [])),
            index_path,
            index_sha256,
        )
