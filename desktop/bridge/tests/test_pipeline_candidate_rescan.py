import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.analysis_jobs import ResolvedAnalysisArtifact
from app.candidate_window_rescan import (
    CandidateWindow,
    CandidateWindowFrame,
    CandidateWindowRescanResult,
)
from app.dense_candidate_discovery import (
    DenseCandidateDiscoveryResult,
    DenseCandidateSeed,
)
from app.media_intake import CompletedMediaGrant
from app.pipeline_jobs import (
    BridgePipelineRuntime,
    PipelineStageFailure,
)
from app.project_artifact_roots import ProjectArtifactRootRegistry


def _profile():
    return {
        "detector_pack_id": "road-pack",
        "ocr_pack_id": "road-pack",
        "sam2_pack_id": "sam2",
        "report_provider": "codex",
        "candidate_rescan_fps": 8,
        "review_mode": "precision",
        "question_policy": "learning_assist",
    }


def _context(
    *,
    project_id,
    snapshot_id,
    pipeline_job_id,
    hash_job_id,
    source_sha256,
    capabilities,
    dense=True,
):
    checkpoints = {
        "sha256": {
            "source_sha256": source_sha256,
            "hash_job_id": str(hash_job_id),
            "total_bytes": len(b"source"),
        }
    }
    if dense:
        checkpoints["dense_index_2fps"] = {
            "dense_run_id": "dense-frame-index-test000001",
            "source_sha256": source_sha256,
            "sampling_fps": 2,
        }
    return {
        "job_id": str(pipeline_job_id),
        "project_id": str(project_id),
        "snapshot_id": str(snapshot_id),
        "file_name": "drive.mp4",
        "profile": _profile(),
        "capabilities": capabilities,
        "checkpoints": checkpoints,
        "review_completion_id": None,
    }


class FakeAnalysisJobs:
    def __init__(self, row):
        self.row = row

    def list(self, project_id, *, limit):
        assert project_id == self.row["project_id"]
        assert limit == 128
        return [self.row]

    def get(self, project_id, job_id):
        assert project_id == self.row["project_id"]
        assert job_id == self.row["job_id"]
        return self.row


class FakeMediaIntake:
    def __init__(self, media_path: Path):
        self.media_path = media_path

    def resolve_completed_media(
        self,
        project_id,
        snapshot_id,
        file_name,
        hash_job_id,
        source_sha256,
    ):
        source_stat = self.media_path.stat()
        return CompletedMediaGrant(
            project_id=project_id,
            snapshot_id=snapshot_id,
            file_name=file_name,
            hash_job_id=hash_job_id,
            sha256_lower=source_sha256,
            media_path=self.media_path,
            size_bytes=source_stat.st_size,
            modified_time_ns=source_stat.st_mtime_ns,
        )


def test_dense_and_rescan_artifacts_write_only_to_pinned_custom_root(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "app.pipeline_jobs.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=100 * 1024**3),
    )
    project_id = uuid4()
    snapshot_id = uuid4()
    analysis_job_id = uuid4()
    pipeline_job_id = uuid4()
    hash_job_id = uuid4()
    source = (tmp_path / "drive.mp4").resolve()
    source.write_bytes(b"source")
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    bridge_root = (tmp_path / "bridge-data").resolve()
    custom_base = (tmp_path / "large-artifacts").resolve()
    custom_base.mkdir()
    registry = ProjectArtifactRootRegistry(data_root=bridge_root)
    location = registry.register(
        project_id, selected_directory=custom_base
    )
    registry.capture_profile_binding(
        project_id,
        profile_revision=1,
        output_location_id=location["output_location_id"],
    )
    job_binding = registry.capture_job_binding(
        project_id,
        profile_revision=1,
        output_location_id=location["output_location_id"],
    )
    candidate = {
        "candidate_id": "a" * 20,
        "start_seconds": 1.0,
        "end_seconds": 2.0,
        "representative_timestamp_seconds": 1.5,
    }
    analysis_row = {
        "job_id": analysis_job_id,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "hash_job_id": hash_job_id,
        "file_name": "drive.mp4",
        "source_sha256": source_sha256,
        "status": "completed",
        "result": {
            "candidates": [candidate],
            "provenance": {"effective_cadence_seconds": 15.0},
        },
    }
    captured = {}

    class WritingDenseEngine:
        def __init__(self, **_kwargs):
            pass

        def build(self, _media_path, **kwargs):
            project_root = Path(kwargs["project_root"])
            output = Path(kwargs["output_directory"])
            captured["dense_project_root"] = project_root
            captured["dense_output"] = output
            run = output / "dense-frame-index-test000001"
            run.mkdir()
            (run / "frame-index.json").write_text(
                '{"schema":"fixture"}', encoding="utf-8"
            )
            (run / "dense-proof.bin").write_bytes(b"dense")
            return SimpleNamespace(
                output_directory=str(run),
                index_relative_path="frame-index.json",
                frames=(SimpleNamespace(frame_index=0),),
                provenance=SimpleNamespace(
                    indexed_duration_seconds=2.0,
                    truncated=False,
                ),
            )

    class WritingRescanEngine:
        def __init__(self, *, ffmpeg_path, limits):
            captured["rescan_fps"] = limits.sampling_fps

        def build(self, _media_path, **kwargs):
            project_root = Path(kwargs["project_root"])
            output = Path(kwargs["output_directory"])
            captured["rescan_project_root"] = project_root
            captured["rescan_output"] = output
            run = output / (
                "candidate-window-rescan-" + "e" * 24
            )
            run.mkdir()
            (run / "candidate-window-rescan.json").write_text(
                '{"schema":"fixture"}', encoding="utf-8"
            )
            (run / "rescan-proof.bin").write_bytes(b"rescan")
            frame = CandidateWindowFrame(
                evidence_id="b" * 20,
                window_id="c" * 20,
                frame_index=0,
                timestamp_seconds=1.5,
                timestamp_numerator=12,
                timestamp_denominator=8,
                source_candidate_ids=("a" * 20,),
                primary_candidate_id="a" * 20,
                relative_path=(
                    f"windows/{'c' * 20}/frame-000000.jpg"
                ),
                width=320,
                height=180,
                size_bytes=10,
                sha256="d" * 64,
            )
            return CandidateWindowRescanResult(
                output_directory=str(run),
                manifest_relative_path=(
                    "candidate-window-rescan.json"
                ),
                manifest_sha256="f" * 64,
                input_revision="e" * 64,
                windows=(
                    CandidateWindow(
                        window_id="c" * 20,
                        start_seconds=1.0,
                        end_seconds=2.0,
                        duration_seconds=1.0,
                        source_candidate_ids=("a" * 20,),
                        planned_frame_limit=9,
                    ),
                ),
                frames=(frame,),
                downstream_evidence_ids=(frame.evidence_id,),
                cached=False,
            )

    monkeypatch.setattr(
        "app.pipeline_jobs.DenseFrameIndexEngine",
        WritingDenseEngine,
    )
    monkeypatch.setattr(
        "app.pipeline_jobs.CandidateWindowRescanEngine",
        WritingRescanEngine,
    )
    runtime = BridgePipelineRuntime(
        FakeMediaIntake(source),
        data_root=bridge_root,
        ffmpeg_path="ffmpeg",
        vision_status=lambda: {
            "enabled": True,
            "pack_id": "road-pack",
            "pack_version": "1",
            "manifest_sha256": "1" * 64,
            "capabilities": ["defect_detection", "ocr"],
            "warning_code": None,
        },
        analysis_jobs=FakeAnalysisJobs(analysis_row),
        artifact_root_registry=registry,
    )
    context = _context(
        project_id=project_id,
        snapshot_id=snapshot_id,
        pipeline_job_id=pipeline_job_id,
        hash_job_id=hash_job_id,
        source_sha256=source_sha256,
        capabilities=runtime.capability_snapshot(_profile()),
        dense=False,
    )
    context["artifact_root"] = job_binding.binding
    dense_result = runtime.execute(
        "dense_index_2fps", context, lambda: False
    )
    context["checkpoints"]["dense_index_2fps"] = (
        dense_result.checkpoint
    )
    rescan_result = runtime.execute(
        "candidate_rescan_5_10fps", context, lambda: False
    )

    expected_job_root = (
        job_binding.root / "pipeline-jobs" / str(pipeline_job_id)
    )
    assert dense_result.outcome == "success"
    assert rescan_result.outcome == "success"
    assert captured["dense_project_root"] == job_binding.root
    assert captured["dense_output"] == expected_job_root / "dense"
    assert captured["rescan_project_root"] == job_binding.root
    assert captured["rescan_output"] == (
        expected_job_root / "candidate-window-rescans"
    )
    assert (
        expected_job_root
        / "dense"
        / "dense-frame-index-test000001"
        / "dense-proof.bin"
    ).read_bytes() == b"dense"
    assert (
        expected_job_root
        / "candidate-window-rescans"
        / ("candidate-window-rescan-" + "e" * 24)
        / "rescan-proof.bin"
    ).read_bytes() == b"rescan"
    legacy_pipeline_root = (
        bridge_root
        / "projects"
        / str(project_id)
        / "pipeline"
    )
    assert not (legacy_pipeline_root / "dense").exists()
    assert not (
        legacy_pipeline_root / "candidate-window-rescans"
    ).exists()


def test_pipeline_stage_sets_true_only_after_verified_rescan_result(
    tmp_path, monkeypatch
):
    project_id = uuid4()
    snapshot_id = uuid4()
    analysis_job_id = uuid4()
    pipeline_job_id = uuid4()
    hash_job_id = uuid4()
    source_sha256 = hashlib.sha256(b"source").hexdigest()
    source = tmp_path / "drive.mp4"
    source.write_bytes(b"source")
    candidate = {
        "candidate_id": "a" * 20,
        "start_seconds": 1.0,
        "end_seconds": 2.0,
        "representative_timestamp_seconds": 1.5,
    }
    row = {
        "job_id": analysis_job_id,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "hash_job_id": hash_job_id,
        "file_name": "drive.mp4",
        "source_sha256": source_sha256,
        "status": "completed",
        "result": {
            "candidates": [candidate],
            "provenance": {"effective_cadence_seconds": 15.0},
        },
    }
    captured = {}

    class FakeEngine:
        def __init__(self, *, ffmpeg_path, limits):
            captured["ffmpeg_path"] = ffmpeg_path
            captured["fps"] = limits.sampling_fps

        def build(self, media_path, **kwargs):
            captured["media_path"] = media_path
            captured.update(kwargs)
            frame = CandidateWindowFrame(
                evidence_id="b" * 20,
                window_id="c" * 20,
                frame_index=0,
                timestamp_seconds=1.5,
                timestamp_numerator=12,
                timestamp_denominator=8,
                source_candidate_ids=("a" * 20,),
                primary_candidate_id="a" * 20,
                relative_path=(
                    f"windows/{'c' * 20}/frame-000000.jpg"
                ),
                width=320,
                height=180,
                size_bytes=10,
                sha256="d" * 64,
            )
            window = CandidateWindow(
                window_id="c" * 20,
                start_seconds=1.0,
                end_seconds=2.0,
                duration_seconds=1.0,
                source_candidate_ids=("a" * 20,),
                planned_frame_limit=9,
            )
            output = Path(kwargs["output_directory"])
            run = output / (
                "candidate-window-rescan-" + "e" * 24
            )
            return CandidateWindowRescanResult(
                output_directory=str(run),
                manifest_relative_path=(
                    "candidate-window-rescan.json"
                ),
                manifest_sha256="f" * 64,
                input_revision="e" * 64,
                windows=(window,),
                frames=(frame,),
                downstream_evidence_ids=(frame.evidence_id,),
                cached=False,
            )

    monkeypatch.setattr(
        "app.pipeline_jobs.CandidateWindowRescanEngine", FakeEngine
    )
    runtime = BridgePipelineRuntime(
        FakeMediaIntake(source),
        data_root=tmp_path.resolve(),
        ffmpeg_path="ffmpeg",
        vision_status=lambda: {
            "enabled": True,
            "pack_id": "road-pack",
            "pack_version": "1",
            "manifest_sha256": "1" * 64,
            "capabilities": ["defect_detection", "ocr"],
            "warning_code": None,
        },
        analysis_jobs=FakeAnalysisJobs(row),
    )
    capabilities = runtime.capability_snapshot(_profile())
    context = _context(
        project_id=project_id,
        snapshot_id=snapshot_id,
        pipeline_job_id=pipeline_job_id,
        hash_job_id=hash_job_id,
        source_sha256=source_sha256,
        capabilities=capabilities,
    )
    result = runtime.execute(
        "candidate_rescan_5_10fps", context, lambda: False
    )

    assert result.outcome == "success"
    assert result.error_code is None
    assert result.checkpoint[
        "high_frequency_rescan_performed"
    ] is True
    assert result.checkpoint["actual_rescan_fps"] == 8
    assert result.checkpoint[
        "whole_video_high_frequency_scan"
    ] is False
    assert result.checkpoint["rescan_frame_count"] == 1
    assert result.checkpoint["rescan_manifest_sha256"] == "f" * 64
    assert captured["fps"] == 8
    assert captured["candidates"] == [candidate]
    assert (
        captured["dense_index_path"].name
        == "frame-index.json"
    )


def _dense_rescan_fixture(
    tmp_path,
    monkeypatch,
    *,
    seed_candidates,
    dense_candidates,
):
    project_id = uuid4()
    snapshot_id = uuid4()
    analysis_job_id = uuid4()
    pipeline_job_id = uuid4()
    hash_job_id = uuid4()
    source = (tmp_path / "drive.mp4").resolve()
    source.write_bytes(b"source")
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    dense_index_sha256 = hashlib.sha256(b"dense-index").hexdigest()
    dense_run_id = "dense-frame-index-test000001"
    dense_root = (
        tmp_path.resolve()
        / "projects"
        / str(project_id)
        / "pipeline"
        / "dense"
        / str(pipeline_job_id)
        / dense_run_id
    )
    dense_root.mkdir(parents=True)
    dense_index_path = dense_root / "frame-index.json"
    dense_index_path.write_bytes(b"dense-index")
    row = {
        "job_id": analysis_job_id,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "hash_job_id": hash_job_id,
        "file_name": "drive.mp4",
        "source_sha256": source_sha256,
        "status": "completed",
        "result": {
            "candidates": list(seed_candidates),
            "provenance": {
                "source_sha256": source_sha256,
                "effective_cadence_seconds": 15.0,
            },
        },
    }
    discovery = DenseCandidateDiscoveryResult(
        schema_version="trafficvisson.dense-candidate-discovery.v1",
        project_id=str(project_id),
        source_sha256=source_sha256,
        dense_index_sha256=dense_index_sha256,
        sampling_fps=2,
        scanned_frame_count=44,
        candidates=tuple(dense_candidates),
        provider_states=(
            {
                "provider": "local_detector",
                "state": "unavailable",
                "role": "detector",
            },
            {
                "provider": "local_ocr",
                "state": "ready",
                "role": "ocr",
            },
            {
                "provider": "approved_3q_4q_reference_search",
                "state": "ready",
                "approved_reference_count": 0,
            },
        ),
        discovery_revision="9" * 64,
    )
    captured = {"overlay_calls": 0}

    class DenseDiscovery:
        def discover(self, **_kwargs):
            return discovery

    class Overlay:
        def __init__(self):
            self.candidates = tuple(dense_candidates)
            self.overlay_id = "ceo_" + "a" * 32
            self.manifest_sha256 = "b" * 64
            self.candidate_metadata_sha256 = "c" * 64
            self.idempotent_replay = False

        def to_checkpoint(self):
            return {
                "overlay_id": self.overlay_id,
                "project_id": str(project_id),
                "analysis_job_id": str(analysis_job_id),
                "pipeline_job_id": str(pipeline_job_id),
                "source_sha256": source_sha256,
                "candidate_count": len(self.candidates),
            }

    class Overlays:
        def materialize(self, **_kwargs):
            captured["overlay_calls"] += 1
            return Overlay()

    class RescanEngine:
        def __init__(self, *, ffmpeg_path, limits):
            captured["sampling_fps"] = limits.sampling_fps

        def build(self, _media_path, **kwargs):
            candidates = list(kwargs["candidates"])
            captured.setdefault("candidate_batches", []).append(
                candidates
            )
            source_ids = tuple(
                sorted(
                    candidate["candidate_id"]
                    for candidate in candidates
                )
            )
            window_id = "d" * 20
            frame = CandidateWindowFrame(
                evidence_id="e" * 20,
                window_id=window_id,
                frame_index=0,
                timestamp_seconds=1.0,
                timestamp_numerator=8,
                timestamp_denominator=8,
                source_candidate_ids=source_ids,
                primary_candidate_id=source_ids[0],
                relative_path=(
                    f"windows/{window_id}/frame-000000.jpg"
                ),
                width=320,
                height=180,
                size_bytes=10,
                sha256="f" * 64,
            )
            output = (
                Path(kwargs["output_directory"])
                / ("candidate-window-rescan-" + "1" * 24)
            )
            return CandidateWindowRescanResult(
                output_directory=str(output),
                manifest_relative_path=(
                    "candidate-window-rescan.json"
                ),
                manifest_sha256="2" * 64,
                input_revision=hashlib.sha256(
                    "\n".join(source_ids).encode("ascii")
                ).hexdigest(),
                windows=(
                    CandidateWindow(
                        window_id=window_id,
                        start_seconds=0.0,
                        end_seconds=2.0,
                        duration_seconds=2.0,
                        source_candidate_ids=source_ids,
                        planned_frame_limit=17,
                    ),
                ),
                frames=(frame,),
                downstream_evidence_ids=(frame.evidence_id,),
                cached=False,
            )

    monkeypatch.setattr(
        "app.pipeline_jobs.CandidateWindowRescanEngine",
        RescanEngine,
    )

    class UsableQuality:
        def __init__(self, result):
            self.state = "usable"
            self.quality_revision = "3" * 64
            self.frame_observations = (
                SimpleNamespace(
                    evidence_id=result.frames[0].evidence_id,
                    strong_unusable=False,
                ),
            )
            self.window_decisions = (SimpleNamespace(),)
            self.selected_evidence_ids = (
                result.frames[0].evidence_id,
            )
            self.eligible_parent_candidate_ids = tuple(
                result.frames[0].source_candidate_ids
            )
            self.blocked_parent_candidate_ids = ()

        def to_checkpoint(self):
            return {
                "schema_version": (
                    "trafficvisson.visual-evidence-quality.v1"
                ),
                "state": "usable",
                "source_sha256": source_sha256,
                "rescan_manifest_sha256": "2" * 64,
                "selected_evidence_ids": list(
                    self.selected_evidence_ids
                ),
                "eligible_parent_candidate_ids": list(
                    self.eligible_parent_candidate_ids
                ),
                "blocked_parent_candidate_ids": [],
                "quality_revision": self.quality_revision,
                "automatic_annotation_creation": False,
                "automatic_incident_creation": False,
                "automatic_training_membership": False,
                "automatic_report_truth": False,
            }

    monkeypatch.setattr(
        "app.pipeline_jobs.evaluate_candidate_window_visual_quality",
        lambda result, **_kwargs: UsableQuality(result),
    )
    runtime = BridgePipelineRuntime(
        FakeMediaIntake(source),
        data_root=tmp_path.resolve(),
        ffmpeg_path="ffmpeg",
        analysis_jobs=FakeAnalysisJobs(row),
        dense_candidate_discovery=DenseDiscovery(),
        candidate_evidence_overlays=Overlays(),
    )
    context = _context(
        project_id=project_id,
        snapshot_id=snapshot_id,
        pipeline_job_id=pipeline_job_id,
        hash_job_id=hash_job_id,
        source_sha256=source_sha256,
        capabilities={
            "detector": "model_unavailable",
            "ocr": "ready",
            "dino_review_only_boxes": "ready",
        },
    )
    context["checkpoints"]["dense_index_2fps"].update(
        {
            "dense_run_id": dense_run_id,
            "dense_index_sha256": dense_index_sha256,
        }
    )
    return runtime, context, captured


def test_dense_zero_keeps_nonclassifying_seed_for_rescan(
    tmp_path, monkeypatch
):
    seed = {
        "candidate_id": "1" * 20,
        "start_seconds": 0.5,
        "end_seconds": 1.5,
        "representative_timestamp_seconds": 1.0,
        "scene_score": 0.5,
        "sharpness_score": 0.7,
        "triggers": ["scene_change"],
    }
    runtime, context, captured = _dense_rescan_fixture(
        tmp_path,
        monkeypatch,
        seed_candidates=[seed],
        dense_candidates=[],
    )

    first = runtime.execute(
        "candidate_rescan_5_10fps", context, lambda: False
    )
    second = runtime.execute(
        "candidate_rescan_5_10fps", context, lambda: False
    )

    assert first.outcome == "success"
    assert first.error_code is None
    assert first.checkpoint["candidate_count"] == 1
    assert first.checkpoint["analysis_seed_candidate_count"] == 1
    assert first.checkpoint["dense_candidate_count"] == 0
    assert first.checkpoint["high_frequency_rescan_performed"] is True
    assert first.checkpoint["annotation_count"] == 0
    assert first.checkpoint["incident_count"] == 0
    assert captured["overlay_calls"] == 0
    assert captured["candidate_batches"] == [[seed], [seed]]
    assert (
        first.checkpoint["candidate_rescan_input_origins"]
        == second.checkpoint["candidate_rescan_input_origins"]
    )
    assert (
        first.checkpoint["rescan_input_revision"]
        == second.checkpoint["rescan_input_revision"]
    )
    assert first.checkpoint["candidate_rescan_input_origins"][
        "analysis_seed"
    ]["non_classifying"] is True
    context["checkpoints"]["candidate_rescan_5_10fps"] = dict(
        first.checkpoint
    )
    _analysis_job_id, _row, downstream_candidates = (
        runtime._analysis_job_for_context(
            UUID(context["project_id"]),
            context,
        )
    )
    assert downstream_candidates == [seed]
    tampered = copy.deepcopy(context)
    tampered["checkpoints"]["candidate_rescan_5_10fps"][
        "candidate_rescan_input_origins"
    ]["origin_revision"] = "0" * 64
    with pytest.raises(PipelineStageFailure) as invalid_origin:
        runtime._analysis_job_for_context(
            UUID(context["project_id"]),
            tampered,
        )
    assert invalid_origin.value.code == (
        "candidate_rescan_origin_provenance_invalid"
    )


def test_dense_ocr_candidate_is_unioned_with_scene_seed(
    tmp_path, monkeypatch
):
    seed = {
        "candidate_id": "1" * 20,
        "start_seconds": 0.5,
        "end_seconds": 1.5,
        "representative_timestamp_seconds": 1.0,
        "scene_score": 0.5,
        "sharpness_score": 0.7,
        "triggers": ["cadence"],
    }
    dense = DenseCandidateSeed(
        candidate_id="2" * 20,
        issue_type="guide_sign_identity",
        start_seconds=1.25,
        end_seconds=1.75,
        representative_timestamp_seconds=1.5,
        representative_frame_id="3" * 24,
        representative_frame_sha256="4" * 64,
        score=0.91,
        normalized_box=None,
        ocr_text="논산 방향",
        urgent_review=False,
        supporting_frame_ids=("3" * 24,),
        proposal_sources=("korean_ocr_dense_2fps",),
        representative_proposal_provenance={
            "pack_id": "ocr"
        },
        proposal_provenance_sha256="5" * 64,
    )
    runtime, context, captured = _dense_rescan_fixture(
        tmp_path,
        monkeypatch,
        seed_candidates=[seed],
        dense_candidates=[dense],
    )

    result = runtime.execute(
        "candidate_rescan_5_10fps", context, lambda: False
    )

    assert result.outcome == "success"
    assert result.checkpoint["candidate_count"] == 2
    assert result.checkpoint["analysis_seed_candidate_count"] == 1
    assert result.checkpoint["dense_candidate_count"] == 1
    assert captured["overlay_calls"] == 1
    assert {
        candidate["candidate_id"]
        for candidate in captured["candidate_batches"][0]
    } == {"1" * 20, "2" * 20}
    origins = result.checkpoint[
        "candidate_rescan_input_origins"
    ]
    assert origins["analysis_seed"]["candidate_ids"] == ["1" * 20]
    assert origins["dense_provider"]["candidate_ids"] == ["2" * 20]
    assert origins["dense_provider"][
        "dense_discovery_revision"
    ] == "9" * 64
    assert origins["combined_candidate_ids"] == [
        "1" * 20,
        "2" * 20,
    ]
    assert result.checkpoint["annotation_count"] == 0
    assert result.checkpoint["incident_count"] == 0


def test_candidate_rescan_origin_union_is_order_independent_and_rejects_collision():
    seeds = [
        {
            "candidate_id": "2" * 20,
            "start_seconds": 2.0,
            "end_seconds": 2.0,
            "representative_timestamp_seconds": 2.0,
            "triggers": ["cadence"],
        },
        {
            "candidate_id": "1" * 20,
            "start_seconds": 1.0,
            "end_seconds": 1.0,
            "representative_timestamp_seconds": 1.0,
            "triggers": ["scene_change"],
        },
    ]
    dense = [
        {"candidate_id": "4" * 20},
        {"candidate_id": "3" * 20},
    ]

    first = BridgePipelineRuntime._candidate_rescan_input_origins(
        seed_candidates=seeds,
        dense_candidates=dense,
        dense_discovery_revision="9" * 64,
    )
    second = BridgePipelineRuntime._candidate_rescan_input_origins(
        seed_candidates=list(reversed(seeds)),
        dense_candidates=list(reversed(dense)),
        dense_discovery_revision="9" * 64,
    )

    assert first == second
    assert first["combined_candidate_ids"] == [
        "1" * 20,
        "2" * 20,
        "3" * 20,
        "4" * 20,
    ]
    with pytest.raises(PipelineStageFailure) as collision:
        BridgePipelineRuntime._candidate_rescan_input_origins(
            seed_candidates=seeds,
            dense_candidates=[{"candidate_id": "1" * 20}],
            dense_discovery_revision="9" * 64,
        )
    assert collision.value.code == (
        "candidate_rescan_identity_collision"
    )


def test_pipeline_distinguishes_model_unavailable_and_zero_candidates(
    tmp_path,
):
    project_id = uuid4()
    snapshot_id = uuid4()
    analysis_job_id = uuid4()
    pipeline_job_id = uuid4()
    hash_job_id = uuid4()
    source_sha256 = "a" * 64
    source = tmp_path / "drive.mp4"
    source.write_bytes(b"source")
    row = {
        "job_id": analysis_job_id,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "hash_job_id": hash_job_id,
        "file_name": "drive.mp4",
        "source_sha256": source_sha256,
        "status": "completed",
        "result": {
            "candidates": [],
            "provenance": {"effective_cadence_seconds": 15.0},
        },
    }
    unavailable_runtime = BridgePipelineRuntime(
        FakeMediaIntake(source),
        data_root=tmp_path.resolve(),
        analysis_jobs=FakeAnalysisJobs(row),
    )
    unavailable_capabilities = (
        unavailable_runtime.capability_snapshot(_profile())
    )
    context = _context(
        project_id=project_id,
        snapshot_id=snapshot_id,
        pipeline_job_id=pipeline_job_id,
        hash_job_id=hash_job_id,
        source_sha256=source_sha256,
        capabilities=unavailable_capabilities,
    )
    unavailable = unavailable_runtime.execute(
        "candidate_rescan_5_10fps", context, lambda: False
    )
    assert unavailable.outcome == "model_unavailable"
    assert unavailable.error_code == (
        "candidate_rescan_model_unavailable"
    )

    zero_runtime = BridgePipelineRuntime(
        FakeMediaIntake(source),
        data_root=tmp_path.resolve(),
        vision_status=lambda: {
            "enabled": True,
            "pack_id": "road-pack",
            "pack_version": "1",
            "manifest_sha256": "1" * 64,
            "capabilities": ["defect_detection", "ocr"],
            "warning_code": None,
        },
        analysis_jobs=FakeAnalysisJobs(row),
    )
    context["capabilities"] = zero_runtime.capability_snapshot(
        _profile()
    )
    zero = zero_runtime.execute(
        "candidate_rescan_5_10fps", context, lambda: False
    )
    assert zero.outcome == "zero_detections"
    assert zero.checkpoint["candidate_count"] == 0
    assert zero.checkpoint[
        "high_frequency_rescan_performed"
    ] is False


def test_dino_consumes_high_frequency_frames_and_projects_to_seed_ids(
    tmp_path, monkeypatch
):
    project_id = uuid4()
    analysis_job_id = uuid4()
    snapshot_id = uuid4()
    hash_job_id = uuid4()
    pipeline_job_id = uuid4()
    source_sha256 = "a" * 64
    base_candidates = [
        {
            "candidate_id": "1" * 20,
            "representative_timestamp_seconds": 1.0,
        },
        {
            "candidate_id": "2" * 20,
            "representative_timestamp_seconds": 2.0,
        },
    ]
    row = {
        "job_id": analysis_job_id,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "hash_job_id": hash_job_id,
        "file_name": "drive.mp4",
        "source_sha256": source_sha256,
        "status": "completed",
        "result": {"candidates": base_candidates},
    }
    observed_batches = []

    class FakeGenerator:
        def status(self):
            return {
                "state": "ready",
                "review_only": True,
                "bulk_frame_detection_provider": "local_model",
            }

        def generate(self, *, command_plan, candidates, **_kwargs):
            observed_batches.append(
                [item["candidate_id"] for item in candidates]
            )
            proposals = []
            if command_plan["issue_type"] == "road_debris":
                proposals = [
                    {
                        "candidate_id": item["candidate_id"],
                        "predictions": [
                                {
                                    "issue_type": "road_debris",
                                    "source_label": "a fallen object",
                                    "model_score": 0.8,
                                "box": {
                                    "x": 0.2,
                                    "y": 0.2,
                                    "width": 0.2,
                                    "height": 0.2,
                                },
                            }
                        ],
                    }
                    for item in candidates
                ]
            return SimpleNamespace(
                available=True,
                generated=True,
                model={
                    "provider": "dino",
                    "pack_id": "dino",
                    "pack_version": "1",
                    "manifest_sha256": "b" * 64,
                },
                evaluation={
                    "evaluation_id": "source",
                    "producer": "test",
                    "producer_version": "1",
                },
                proposals=proposals,
            )

    class FakeStore:
        def __init__(self):
            self.rows = {}
            self.evaluation_id = None

        def latest_for_job(
            self, *_args, evaluation_id=None, **_kwargs
        ):
            if evaluation_id != self.evaluation_id:
                return {}
            return self.rows

        def record_run(
            self,
            _project_id,
            _analysis_job_id,
            *,
            evaluation,
            proposals,
            **_kwargs,
        ):
            self.evaluation_id = evaluation["evaluation_id"]
            self.rows = {
                row["candidate_id"]: {
                    "candidate_id": row["candidate_id"],
                    "predictions": row["predictions"],
                }
                for row in proposals
            }
            return list(self.rows.values())

    store = FakeStore()
    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        vision_status=lambda: {
            "enabled": False,
            "warning_code": "model_pack_not_configured",
        },
        analysis_jobs=FakeAnalysisJobs(row),
        command_proposal_generator=FakeGenerator(),
        experimental_proposals=store,
    )
    evidence_candidates = [
        {
            "candidate_id": "3" * 20,
            "representative_timestamp_seconds": 1.1,
        },
        {
            "candidate_id": "4" * 20,
            "representative_timestamp_seconds": 2.1,
        },
    ]

    def fake_high_frequency(*_args):
        def resolve(_project_id, _analysis_job_id, _candidate_id):
            return ResolvedAnalysisArtifact(
                path=(tmp_path / "unused.jpg").resolve(),
                media_type="image/jpeg",
                size_bytes=1,
                sha256="c" * 64,
            )

        return (
            evidence_candidates,
            {"3" * 20: "1" * 20, "4" * 20: "2" * 20},
            resolve,
                {
                    "source": "candidate_window_rescan_5_10fps",
                    "manifest_sha256": "d" * 64,
                    "input_revision": "e" * 64,
                    "selected_frame_count": 2,
                    "sampling_fps": 8,
                    "visual_evidence_quality_state": "usable",
                    "visual_evidence_quality_revision": "f" * 64,
                    "eligible_parent_candidate_ids": [
                        "1" * 20,
                        "2" * 20,
                    ],
                    "blocked_parent_candidate_ids": [],
                    "frame_quality_observations": [
                        {
                            "evidence_id": "3" * 20,
                            "strong_unusable": False,
                        },
                        {
                            "evidence_id": "4" * 20,
                            "strong_unusable": False,
                        },
                    ],
                    "frame_parent_scope": {
                        "3" * 20: {
                            "timestamp_seconds": 1.1,
                            "source_candidate_ids": ["1" * 20],
                            "primary_candidate_id": "1" * 20,
                        },
                        "4" * 20: {
                            "timestamp_seconds": 2.1,
                            "source_candidate_ids": ["2" * 20],
                            "primary_candidate_id": "2" * 20,
                        },
                    },
                },
            )

    monkeypatch.setattr(
        runtime,
        "_high_frequency_evidence_for_context",
        fake_high_frequency,
    )
    context = {
        "job_id": str(pipeline_job_id),
        "project_id": str(project_id),
        "snapshot_id": str(snapshot_id),
        "file_name": "drive.mp4",
        "profile": _profile(),
        "capabilities": {},
        "checkpoints": {
            "candidate_rescan_5_10fps": {
                "analysis_job_id": str(analysis_job_id),
                "source_sha256": source_sha256,
            }
        },
    }
    result = runtime.execute(
        "detection_ocr", context, lambda: False
    )

    assert observed_batches
    assert all(
        batch == ["3" * 20, "4" * 20]
        for batch in observed_batches
    )
    assert set(store.rows) == {"1" * 20, "2" * 20}
    assert result.checkpoint["dino_evidence_source"] == (
        "candidate_window_rescan_5_10fps"
    )
    assert result.checkpoint[
        "high_frequency_evidence_count"
    ] == 2
    assert result.checkpoint[
        "high_frequency_manifest_sha256"
    ] == "d" * 64
    assert result.checkpoint["annotation_count"] == 0
    assert result.checkpoint["incident_count"] == 0


def test_sam2_stage_tracks_verified_high_frequency_candidate_frames(
    tmp_path, monkeypatch
):
    project_id = uuid4()
    analysis_job_id = uuid4()
    candidate_id = "1" * 20
    row = {
        "job_id": analysis_job_id,
        "project_id": project_id,
        "source_sha256": "a" * 64,
        "status": "completed",
        "result": {
            "candidates": [
                {
                    "candidate_id": candidate_id,
                    "representative_timestamp_seconds": 1.0,
                }
            ]
        },
    }

    class Sam2Adapter:
        def status(self):
            return {
                "state": "ready",
                "model_manifest": {
                    "pack_id": "sam2",
                    "pack_version": "1",
                    "manifest_sha256": "b" * 64,
                    "model_artifact_sha256": "c" * 64,
                    "provenance_verified": True,
                    "review_only": True,
                },
            }

        def refine(self, **_kwargs):
            raise AssertionError("tracking frames should be used")

        def track(self, *, frame_paths, **_kwargs):
            assert len(frame_paths) == 2
            return {
                "frames": [
                    {
                        "frame_index": index,
                        "polygon": [
                            [0.1, 0.1],
                            [0.5, 0.1],
                            [0.5, 0.5],
                        ],
                    }
                    for index in range(2)
                ]
            }

    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        analysis_jobs=FakeAnalysisJobs(row),
        sam2_status=lambda: {
            "state": "ready",
            "model_pack": {
                "pack_id": "sam2",
                "manifest_sha256": "b" * 64,
            },
        },
        sam2_adapter=Sam2Adapter(),
    )
    monkeypatch.setattr(
        runtime,
        "_summarize_embedded_local_vision",
        lambda _candidates, _profile: {
            "detections_by_candidate": {
                candidate_id: {
                    "issue_type": "road_debris",
                    "score": 0.8,
                    "normalized_box": {
                        "x": 0.2,
                        "y": 0.2,
                        "width": 0.2,
                        "height": 0.2,
                    },
                }
            }
        },
    )
    frame_paths = []
    for index in range(2):
        path = (tmp_path / f"frame-{index}.jpg").resolve()
        path.write_bytes(b"x")
        frame_paths.append(path)

    def high_frequency(*_args):
        frames = [
            {
                "candidate_id": str(index + 2) * 20,
                "representative_timestamp_seconds": 1.0 + index * 0.1,
            }
            for index in range(2)
        ]
        artifacts = {
            frame["candidate_id"]: ResolvedAnalysisArtifact(
                path=frame_paths[index],
                media_type="image/jpeg",
                size_bytes=1,
                sha256=str(index + 3) * 64,
            )
            for index, frame in enumerate(frames)
        }
        return (
            frames,
            {
                frame["candidate_id"]: candidate_id
                for frame in frames
            },
            (
                lambda _project, _analysis, evidence_id: (
                    artifacts[evidence_id]
                )
            ),
            {
                "source": "candidate_window_rescan_5_10fps",
                "manifest_sha256": "d" * 64,
                "input_revision": "e" * 64,
                "selected_frame_count": 2,
                "sampling_fps": 8,
            },
        )

    monkeypatch.setattr(
        runtime,
        "_high_frequency_evidence_for_context",
        high_frequency,
    )
    profile = _profile()
    capabilities = runtime.capability_snapshot(profile)
    context = {
        "job_id": str(uuid4()),
        "project_id": str(project_id),
        "snapshot_id": str(uuid4()),
        "file_name": "drive.mp4",
        "profile": profile,
        "capabilities": capabilities,
        "checkpoints": {
            "candidate_rescan_5_10fps": {
                "analysis_job_id": str(analysis_job_id),
                "source_sha256": "a" * 64,
            },
            "detection_ocr": {},
        },
    }
    result = runtime.execute(
        "sam2_segmentation_tracking",
        context,
        lambda: False,
    )

    assert result.outcome == "success"
    assert result.error_code is None
    assert result.checkpoint["mask_generated"] is True
    assert result.checkpoint["tracking_performed"] is True
    assert result.metrics["tracked_frame_count"] == 2
    temporal_context = result.checkpoint[
        "prompt_temporal_context_by_candidate"
    ][candidate_id]
    assert temporal_context["before_context_included"] is False
    assert temporal_context["after_context_included"] is True
    assert temporal_context["selected_frame_count"] == 2
    assert (
        result.metrics["full_temporal_context_prompt_count"]
        == 0
    )
    assert (
        result.metrics["missing_temporal_context_prompt_count"]
        == 1
    )
    assert result.checkpoint["annotation_count"] == 0
    assert result.checkpoint["incident_count"] == 0
