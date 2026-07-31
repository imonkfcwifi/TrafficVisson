import json
import subprocess
import threading
import time
import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.folder_pipeline_batches import (
    CAPACITY_MODEL_VERSION,
    FolderPipelineBatchService,
    build_pipeline_storage_plan,
)
from app.main import create_app
from app.media_intake import CompletedMediaGrant, MediaIntakeService
from app.pipeline_jobs import PipelineJobService, PipelineStageResult
from app.project_artifact_roots import (
    ProjectArtifactRootRegistry,
    ProjectArtifactRootUnavailable,
)
from app.stable_media_snapshots import StableMediaSnapshotStore
from app.store import DEMO_PROJECT_ID
from conftest import TEST_TOKEN


def test_practical_capacity_plan_rescans_only_candidate_windows():
    plan = build_pipeline_storage_plan(
        duration_seconds=82 * 60,
        candidate_rescan_fps=8,
    )

    stages = {
        row["stage"]: row for row in plan["stage_estimates"]
    }
    candidate_stage = stages["candidate_rescan_5_10fps"]
    assert plan["schema_version"] == (
        "trafficvisson.pipeline-capacity-plan.v2"
    )
    assert plan["dense_frame_count"] == 82 * 60 * 2
    assert plan["planned_candidate_count"] == 82
    assert plan["planned_candidate_window_seconds"] == 738.0
    assert plan["candidate_rescan_frame_count"] == 738 * 8
    assert plan["candidate_rescan_frame_count"] < 82 * 60 * 8
    assert candidate_stage["planned_frame_count"] == 738 * 8
    assert candidate_stage["estimated_bytes"] < (
        candidate_stage["hard_ceiling_bytes"]
    )
    assert plan["estimated_artifact_bytes"] < sum(
        int(row["hard_ceiling_bytes"] or row["estimated_bytes"])
        for row in plan["stage_estimates"]
    )


def test_actual_51_video_inventory_capacity_snapshot_is_fixed():
    # Path-private ffprobe durations from the user's stable 51-video NAS
    # snapshot. Names and source paths are intentionally not persisted here.
    durations = (
        825.8164,
        1272.1047,
        1281.3074,
        2285.2094,
        814.3173,
        1346.7183,
        2346.0907,
        1167.5443,
        794.2959,
        1044.7451,
        283.5672,
        2748.5628,
        1166.8406,
        2163.5563,
        872.2897,
        1353.7861,
        2135.0091,
        886.4279,
        961.385,
        1125.7521,
        2.8095,
        1470.853,
        996.9258,
        13.783,
        519.2656,
        1886.915,
        329.3118,
        4959.7074,
        321.2814,
        671.8564,
        1279.4198,
        370.9775,
        81.1675,
        631.5421,
        588.998,
        777.1898,
        935.9353,
        910.8452,
        1569.4875,
        390.5935,
        850.5023,
        4356.2156,
        916.7208,
        1033.7799,
        188.1971,
        233.486,
        848.5573,
        644.168,
        330.026,
        450.723,
        583.448,
    )
    plans = [
        build_pipeline_storage_plan(
            duration_seconds=duration,
            candidate_rescan_fps=8,
        )
        for duration in durations
    ]
    estimated = sum(
        int(plan["estimated_artifact_bytes"]) for plan in plans
    )
    shared_safety = max(
        int(plan["safety_reserve_bytes"]) for plan in plans
    )

    assert len(plans) == 51
    assert round(sum(durations), 4) == 56020.0154
    assert sum(plan["dense_frame_count"] for plan in plans) == 112065
    assert sum(
        plan["planned_candidate_count"] for plan in plans
    ) == 970
    assert sum(
        plan["candidate_rescan_frame_count"] for plan in plans
    ) == 67255
    assert round(
        sum(
            plan["planned_candidate_window_seconds"]
            for plan in plans
        ),
        6,
    ) == 8403.080885
    assert estimated == 144792092672
    assert shared_safety == 4 * 1024**3
    assert estimated + shared_safety == 149087059968


def _valid_probe_output(size: int) -> dict:
    return {
        "format": {
            "duration": "12.5",
            "size": str(size),
            "format_name": "mov,mp4",
        },
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "30000/1001",
            }
        ],
    }


def _stable_folder(
    tmp_path: Path, project_id: UUID
) -> tuple[MediaIntakeService, UUID, Path]:
    directory = tmp_path / "stable NAS fixture"
    directory.mkdir()
    (directory / "z-last.MP4").write_bytes(b"z" * 13)
    (directory / "A-first.mov").write_bytes(b"a" * 11)
    (directory / "b-middle.mp4").write_bytes(b"b" * 12)
    (directory / "notes.txt").write_text("ignored", encoding="utf-8")
    nested = directory / "nested"
    nested.mkdir()
    (nested / "not-top-level.mp4").write_bytes(b"nested")

    fake_ffprobe = tmp_path / "ffprobe.exe"
    fake_ffprobe.write_bytes(b"fixture")
    media = MediaIntakeService(
        ffprobe_path=str(fake_ffprobe),
        minimum_interval_seconds=0,
    )
    snapshots = [
        media.capture_snapshot(project_id, str(directory)) for _ in range(3)
    ]
    stable = media.compare_snapshots(
        project_id,
        [UUID(str(item["snapshot_id"])) for item in snapshots],
    )
    assert stable["stable"] is True
    return media, UUID(str(stable["stable_snapshot_id"])), directory


class FakePipelineJobs:
    def __init__(
        self,
        output_location_id="project-default",
        revision=1,
    ) -> None:
        self.jobs = {}
        self.started_names = []
        self.resumed = []
        self.output_location_id = output_location_id
        self.revision = revision
        self.staging_status = {
            "state": "ready",
            "source": "fixture",
            "available_free_bytes": 64 * 1024**3,
            "recovered_stale_children": 0,
            "error_code": None,
            "path_exposed": False,
        }

    def get_profile(self, project_id):
        return {
            "project_id": str(project_id),
            "revision": self.revision,
            "review_mode": "quick",
            "output_location_id": self.output_location_id,
        }

    def dense_staging_status(self):
        return dict(self.staging_status)

    def start(
        self,
        project_id,
        *,
        snapshot_id,
        file_name,
        rights_confirmed,
        idempotency_key,
    ):
        assert rights_confirmed is True
        for job in self.jobs.values():
            if job["idempotency_key"] == idempotency_key:
                return dict(job), True
        job_id = uuid4()
        job = {
            "job_id": job_id,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "file_name": file_name,
            "idempotency_key": idempotency_key,
            "status": "queued",
        }
        self.jobs[job_id] = job
        self.started_names.append(file_name)
        return dict(job), False

    def get(self, project_id, job_id):
        job = self.jobs[job_id]
        assert job["project_id"] == project_id
        return dict(job)

    def list(self, project_id, *, limit=100):
        assert 1 <= limit <= 128
        return [
            dict(job)
            for job in reversed(tuple(self.jobs.values()))
            if job["project_id"] == project_id
        ][:limit]

    def resume(self, project_id, job_id):
        job = self.jobs[job_id]
        assert job["project_id"] == project_id
        job["status"] = "queued"
        self.resumed.append(job_id)
        return dict(job)


def _wait_for_batch(service, project_id, batch_id, timeout=3):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        latest = service.get(project_id, batch_id)
        if latest["enqueue_completed"]:
            return latest
        time.sleep(0.01)
    raise AssertionError(f"batch did not finish enqueueing: {latest}")


def test_stable_top_level_folder_is_deterministic_and_partial_probe_failure_retries(
    tmp_path, monkeypatch
):
    project_id = uuid4()
    media, snapshot_id, _ = _stable_folder(tmp_path, project_id)
    failed_once = {"b-middle.mp4": True}

    def fake_run(argv, **kwargs):
        file_name = Path(argv[-1]).name
        if failed_once.get(file_name):
            failed_once[file_name] = False
            return subprocess.CompletedProcess(argv, 1, "", "bad stream")
        size = Path(argv[-1]).stat().st_size
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(_valid_probe_output(size)), ""
        )

    monkeypatch.setattr("app.media_intake.subprocess.run", fake_run)
    pipeline = FakePipelineJobs()
    service = FolderPipelineBatchService(
        media,
        pipeline,
        data_root=tmp_path.resolve(),
        probe_timeout_seconds=5,
        disk_usage=lambda _path: SimpleNamespace(
            free=100 * 1024**3
        ),
    )
    started, replayed = service.start(
        project_id,
        snapshot_id=snapshot_id,
        rights_confirmed=True,
        idempotency_key=f"folder-start-{snapshot_id}",
    )
    assert replayed is False
    completed = _wait_for_batch(
        service, project_id, UUID(started["batch_id"])
    )
    assert [row["file_name"] for row in completed["files"]] == [
        "A-first.mov",
        "b-middle.mp4",
        "z-last.MP4",
    ]
    assert pipeline.started_names == ["A-first.mov", "z-last.MP4"]
    assert completed["status"] == "partial_failed"
    assert completed["counts"]["validation_failed"] == 1
    assert completed["files"][1]["retry_available"] is True

    retried = service.retry(
        project_id,
        UUID(started["batch_id"]),
        user_confirmed=True,
    )
    assert retried["retry_count"] == 1
    completed = _wait_for_batch(
        service, project_id, UUID(started["batch_id"])
    )
    assert pipeline.started_names == [
        "A-first.mov",
        "z-last.MP4",
        "b-middle.mp4",
    ]
    assert completed["counts"]["validation_failed"] == 0
    assert completed["counts"]["queued_or_running"] == 3
    assert completed["status"] == "active"
    service.shutdown()
    media.shutdown()


def test_pending_batch_and_private_stable_snapshot_resume_after_restart(
    tmp_path, monkeypatch
):
    project_id = uuid4()
    media, snapshot_id, _ = _stable_folder(tmp_path, project_id)
    stable_store = StableMediaSnapshotStore(tmp_path.resolve())
    stable_store.persist(media, project_id, snapshot_id)

    first_pipeline = FakePipelineJobs()
    first = FolderPipelineBatchService(
        media,
        first_pipeline,
        data_root=tmp_path.resolve(),
        probe_timeout_seconds=5,
        disk_usage=lambda _path: SimpleNamespace(
            free=100 * 1024**3
        ),
    )
    monkeypatch.setattr(first, "_ensure_worker_locked", lambda: None)
    created, _ = first.start(
        project_id,
        snapshot_id=snapshot_id,
        rights_confirmed=True,
        idempotency_key=f"folder-restart-{snapshot_id}",
    )
    batch_id = UUID(created["batch_id"])
    assert created["counts"]["pending"] == 3
    first.shutdown()
    media.shutdown()

    restored_media = MediaIntakeService(
        ffprobe_path=str(tmp_path / "ffprobe.exe"),
        minimum_interval_seconds=0,
    )
    assert stable_store.restore_all(restored_media) == {
        "loaded": 1,
        "rejected": 0,
    }

    def fake_run(argv, **kwargs):
        size = Path(argv[-1]).stat().st_size
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(_valid_probe_output(size)), ""
        )

    monkeypatch.setattr("app.media_intake.subprocess.run", fake_run)
    restarted_pipeline = FakePipelineJobs()
    restarted = FolderPipelineBatchService(
        restored_media,
        restarted_pipeline,
        data_root=tmp_path.resolve(),
        probe_timeout_seconds=5,
        disk_usage=lambda _path: SimpleNamespace(
            free=100 * 1024**3
        ),
    )
    completed = _wait_for_batch(
        restarted, project_id, batch_id
    )
    assert restarted.recovery_summary() == {
        "batches_loaded": 1,
        "batches_requeued": 1,
        "records_rejected": 0,
    }
    assert completed["recovered_after_restart"] is True
    assert completed["resume_count"] == 1
    assert all(
        row["recovered_after_restart"] for row in completed["files"]
    )
    assert restarted_pipeline.started_names == [
        "A-first.mov",
        "b-middle.mp4",
        "z-last.MP4",
    ]
    restarted.shutdown()
    restored_media.shutdown()


def test_aggregate_output_preflight_blocks_all_jobs_when_project_disk_is_short(
    tmp_path, monkeypatch
):
    project_id = uuid4()
    media, snapshot_id, _ = _stable_folder(tmp_path, project_id)

    def fake_run(argv, **kwargs):
        size = Path(argv[-1]).stat().st_size
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(_valid_probe_output(size)), ""
        )

    monkeypatch.setattr("app.media_intake.subprocess.run", fake_run)
    pipeline = FakePipelineJobs()
    service = FolderPipelineBatchService(
        media,
        pipeline,
        data_root=tmp_path.resolve(),
        probe_timeout_seconds=5,
        disk_usage=lambda _path: SimpleNamespace(free=1024**3),
    )
    started, _ = service.start(
        project_id,
        snapshot_id=snapshot_id,
        rights_confirmed=True,
        idempotency_key=f"folder-storage-{snapshot_id}",
    )
    completed = _wait_for_batch(
        service, project_id, UUID(started["batch_id"])
    )
    assert completed["status"] == "blocked_storage"
    assert completed["counts"]["storage_blocked"] == 3
    assert completed["counts"]["queued_or_running"] == 0
    assert pipeline.started_names == []
    assert all(
        item["pipeline_job_id"] is None
        and item["last_error_code"] == "insufficient_project_storage"
        for item in completed["files"]
    )
    preflight = completed["output_preflight"]
    assert preflight["state"] == "blocked"
    assert (
        preflight["capacity_model_version"]
        == CAPACITY_MODEL_VERSION
    )
    assert preflight["dense_sampling_fps"] == 2
    assert preflight["candidate_rescan_fps"] == 8
    assert preflight["estimated_jpeg_bytes_per_frame"] == 112 * 1024
    assert preflight["derived_output_overhead_ratio"] == 1.0
    assert preflight["dense_frame_count"] == 75
    per_video = build_pipeline_storage_plan(
        duration_seconds=12.5,
        candidate_rescan_fps=8,
    )
    assert preflight["candidate_rescan_frame_count"] == (
        3 * per_video["candidate_rescan_frame_count"]
    )
    assert preflight["estimated_artifact_bytes"] == (
        3 * per_video["estimated_artifact_bytes"]
    )
    assert [row["stage"] for row in preflight["stage_estimates"]] == [
        "intake_validation",
        "sha256",
        "metadata_gps_preflight",
        "dense_index_2fps",
        "candidate_rescan_5_10fps",
        "detection_ocr",
        "sam2_segmentation_tracking",
        "temporal_merge",
        "similar_case_search",
        "vision_proposal",
        "question_queue",
        "derived_outputs",
    ]
    assert preflight["separate_capacity_preflights"] == [
        "project_model_training",
        "hwp_com_application_and_48_page_render",
    ]
    assert preflight["required_free_bytes"] > preflight[
        "available_free_bytes"
    ]
    service.shutdown()
    media.shutdown()


def test_unrouted_project_output_location_blocks_before_any_pipeline_start(
    tmp_path, monkeypatch
):
    project_id = uuid4()
    media, snapshot_id, _ = _stable_folder(tmp_path, project_id)

    def fake_run(argv, **kwargs):
        size = Path(argv[-1]).stat().st_size
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(_valid_probe_output(size)), ""
        )

    monkeypatch.setattr("app.media_intake.subprocess.run", fake_run)
    pipeline = FakePipelineJobs("project-output-not-routed")
    service = FolderPipelineBatchService(
        media,
        pipeline,
        data_root=tmp_path.resolve(),
        probe_timeout_seconds=5,
        disk_usage=lambda _path: SimpleNamespace(free=100 * 1024**3),
    )
    started, _ = service.start(
        project_id,
        snapshot_id=snapshot_id,
        rights_confirmed=True,
        idempotency_key=f"folder-unrouted-{snapshot_id}",
    )
    completed = _wait_for_batch(
        service, project_id, UUID(started["batch_id"])
    )
    assert completed["status"] == "blocked_storage"
    assert completed["output_preflight"]["error_code"] == (
        "project_output_location_unavailable"
    )
    assert completed["counts"]["storage_blocked"] == 3
    assert pipeline.started_names == []
    assert all(
        item["pipeline_job_id"] is None
        and item["last_error_code"]
        == "project_output_location_unavailable"
        for item in completed["files"]
    )
    service.shutdown()
    media.shutdown()


def test_registered_custom_output_with_insufficient_capacity_creates_zero_jobs(
    tmp_path, monkeypatch
):
    project_id = uuid4()
    media, snapshot_id, _ = _stable_folder(tmp_path, project_id)

    def fake_run(argv, **kwargs):
        size = Path(argv[-1]).stat().st_size
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(_valid_probe_output(size)), ""
        )

    monkeypatch.setattr("app.media_intake.subprocess.run", fake_run)
    custom_base = (tmp_path / "small custom output").resolve()
    custom_base.mkdir()
    registry = ProjectArtifactRootRegistry(
        data_root=(tmp_path / "bridge-small").resolve(),
        disk_usage=lambda _path: SimpleNamespace(free=1024**3),
    )
    location = registry.register(
        project_id, selected_directory=custom_base
    )
    registry.capture_profile_binding(
        project_id,
        profile_revision=1,
        output_location_id=location["output_location_id"],
    )
    pipeline = FakePipelineJobs(location["output_location_id"])
    service = FolderPipelineBatchService(
        media,
        pipeline,
        data_root=(tmp_path / "bridge-small").resolve(),
        artifact_root_registry=registry,
        probe_timeout_seconds=5,
    )

    started, _ = service.start(
        project_id,
        snapshot_id=snapshot_id,
        rights_confirmed=True,
        idempotency_key=f"folder-custom-small-{snapshot_id}",
    )
    completed = _wait_for_batch(
        service, project_id, UUID(started["batch_id"])
    )

    assert completed["status"] == "blocked_storage"
    assert completed["output_preflight"]["error_code"] == (
        "insufficient_project_storage"
    )
    assert completed["counts"]["storage_blocked"] == 3
    assert pipeline.started_names == []
    assert all(
        item["pipeline_job_id"] is None
        for item in completed["files"]
    )
    service.shutdown()
    media.shutdown()


def test_registered_custom_output_with_capacity_enqueues_every_file(
    tmp_path, monkeypatch
):
    project_id = uuid4()
    media, snapshot_id, _ = _stable_folder(tmp_path, project_id)

    def fake_run(argv, **kwargs):
        size = Path(argv[-1]).stat().st_size
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(_valid_probe_output(size)), ""
        )

    monkeypatch.setattr("app.media_intake.subprocess.run", fake_run)
    custom_base = (tmp_path / "large custom output").resolve()
    custom_base.mkdir()
    registry = ProjectArtifactRootRegistry(
        data_root=(tmp_path / "bridge-large").resolve(),
        disk_usage=lambda _path: SimpleNamespace(free=100 * 1024**3),
    )
    location = registry.register(
        project_id, selected_directory=custom_base
    )
    registry.capture_profile_binding(
        project_id,
        profile_revision=1,
        output_location_id=location["output_location_id"],
    )
    pipeline = FakePipelineJobs(location["output_location_id"])
    service = FolderPipelineBatchService(
        media,
        pipeline,
        data_root=(tmp_path / "bridge-large").resolve(),
        artifact_root_registry=registry,
        probe_timeout_seconds=5,
    )

    started, _ = service.start(
        project_id,
        snapshot_id=snapshot_id,
        rights_confirmed=True,
        idempotency_key=f"folder-custom-large-{snapshot_id}",
    )
    completed = _wait_for_batch(
        service, project_id, UUID(started["batch_id"])
    )

    assert completed["output_preflight"]["state"] == "ready"
    assert completed["counts"]["queued_or_running"] == 3
    assert pipeline.started_names == [
        "A-first.mov",
        "b-middle.mp4",
        "z-last.MP4",
    ]
    service.shutdown()
    media.shutdown()


def test_long_folder_output_blocks_on_staging_and_releases_reservation(
    tmp_path,
    monkeypatch,
):
    gib = 1024**3
    project_id = uuid4()
    media, snapshot_id, _ = _stable_folder(tmp_path, project_id)

    def fake_run(argv, **kwargs):
        size = Path(argv[-1]).stat().st_size
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(_valid_probe_output(size)), ""
        )

    monkeypatch.setattr("app.media_intake.subprocess.run", fake_run)
    monkeypatch.setattr(
        "app.folder_pipeline_batches."
        "dense_frame_index_requires_short_staging",
        lambda _path: True,
    )
    custom_base = (tmp_path / "long-custom-output").resolve()
    custom_base.mkdir()
    registry = ProjectArtifactRootRegistry(
        data_root=(tmp_path / "bridge-staging-low").resolve(),
        disk_usage=lambda _path: SimpleNamespace(free=100 * gib),
    )
    location = registry.register(
        project_id,
        selected_directory=custom_base,
    )
    binding = registry.capture_profile_binding(
        project_id,
        profile_revision=1,
        output_location_id=location["output_location_id"],
    ).binding
    pipeline = FakePipelineJobs(location["output_location_id"])
    pipeline.staging_status = {
        "state": "ready",
        "source": "configured",
        "available_free_bytes": 1,
        "recovered_stale_children": 0,
        "error_code": None,
        "path_exposed": False,
    }
    service = FolderPipelineBatchService(
        media,
        pipeline,
        data_root=(tmp_path / "bridge-staging-low").resolve(),
        artifact_root_registry=registry,
        probe_timeout_seconds=5,
    )

    started, _ = service.start(
        project_id,
        snapshot_id=snapshot_id,
        rights_confirmed=True,
        idempotency_key=f"folder-staging-low-{snapshot_id}",
    )
    completed = _wait_for_batch(
        service,
        project_id,
        UUID(started["batch_id"]),
    )

    single_plan = build_pipeline_storage_plan(
        duration_seconds=12.5,
        candidate_rescan_fps=8,
    )
    assert completed["status"] == "blocked_storage"
    assert completed["output_preflight"]["error_code"] == (
        "dense_staging_storage_blocked"
    )
    assert completed["output_preflight"]["safety_reserve_bytes"] == (
        single_plan["safety_reserve_bytes"]
    )
    assert pipeline.started_names == []
    assert all(
        item["pipeline_job_id"] is None
        for item in completed["files"]
    )

    # The folder reservation is released when the independent staging volume
    # blocks, so another large reservation on the same output volume can fit.
    followup_reservation_id = f"folder-batch:{uuid4()}"
    observation = registry.adjust_capacity_reservation(
        project_id,
        binding,
        reservation_id=followup_reservation_id,
        reserved_bytes=80 * gib,
        safety_reserve_bytes=1 * gib,
    )
    assert observation["other_reserved_bytes"] == 0
    registry.release_capacity_reservation(
        project_id,
        binding,
        reservation_id=followup_reservation_id,
    )
    service.shutdown()
    media.shutdown()


def test_restart_reconciles_started_jobs_before_batch_reservation(
    tmp_path,
    monkeypatch,
):
    project_id = uuid4()
    media, snapshot_id, _ = _stable_folder(tmp_path, project_id)
    probe_calls = 0

    def fake_run(argv, **kwargs):
        nonlocal probe_calls
        probe_calls += 1
        size = Path(argv[-1]).stat().st_size
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(_valid_probe_output(size)), ""
        )

    monkeypatch.setattr("app.media_intake.subprocess.run", fake_run)
    single_plan = build_pipeline_storage_plan(
        duration_seconds=12.5,
        candidate_rescan_fps=8,
    )
    estimated = 3 * single_plan["estimated_artifact_bytes"]
    safety = int(single_plan["safety_reserve_bytes"])
    free_bytes = estimated + safety + 1024**2
    data_root = (tmp_path / "bridge-crash-window").resolve()
    custom_base = (tmp_path / "crash-window-output").resolve()
    custom_base.mkdir()
    first_registry = ProjectArtifactRootRegistry(
        data_root=data_root,
        disk_usage=lambda _path: SimpleNamespace(free=free_bytes),
    )
    location = first_registry.register(
        project_id,
        selected_directory=custom_base,
    )
    first_registry.capture_profile_binding(
        project_id,
        profile_revision=1,
        output_location_id=location["output_location_id"],
    )
    pipelines = FakePipelineJobs(location["output_location_id"])
    first = FolderPipelineBatchService(
        media,
        pipelines,
        data_root=data_root,
        artifact_root_registry=first_registry,
        probe_timeout_seconds=5,
    )
    started, _ = first.start(
        project_id,
        snapshot_id=snapshot_id,
        rights_confirmed=True,
        idempotency_key=f"folder-crash-window-{snapshot_id}",
    )
    batch_id = UUID(started["batch_id"])
    completed = _wait_for_batch(
        first,
        project_id,
        batch_id,
    )
    assert completed["counts"]["queued_or_running"] == 3
    probes_before_restart = probe_calls
    with first._lock:
        durable = first._records[batch_id]
        binding = dict(durable["artifact_root_binding"])
        entry_snapshots = []
        for entry in durable["entries"]:
            job = next(
                row
                for row in pipelines.jobs.values()
                if row["idempotency_key"]
                == entry["pipeline_idempotency_key"]
            )
            entry_snapshots.append(
                (
                    UUID(str(job["job_id"])),
                    int(entry["storage_plan"][
                        "estimated_artifact_bytes"
                    ]),
                )
            )
            entry["state"] = "enqueueing"
            entry["pipeline_job_id"] = None
            entry["idempotent_replay"] = False
        durable["enqueue_completed"] = False
        first._persist_locked(durable)
    first.shutdown()
    surviving_job_id, surviving_reserved_bytes = entry_snapshots[0]
    pipelines.jobs = {
        surviving_job_id: pipelines.jobs[surviving_job_id]
    }
    pipelines.started_names = []

    restarted_registry = ProjectArtifactRootRegistry(
        data_root=data_root,
        disk_usage=lambda _path: SimpleNamespace(free=free_bytes),
    )
    restarted_registry.adjust_capacity_reservation(
        project_id,
        binding,
        reservation_id=f"pipeline-job:{surviving_job_id}",
        reserved_bytes=surviving_reserved_bytes,
        safety_reserve_bytes=safety,
    )
    restarted = FolderPipelineBatchService(
        media,
        pipelines,
        data_root=data_root,
        artifact_root_registry=restarted_registry,
        probe_timeout_seconds=5,
    )
    reconciled = _wait_for_batch(
        restarted,
        project_id,
        batch_id,
    )

    assert reconciled["output_preflight"]["state"] == "ready"
    assert (
        reconciled["output_preflight"]["estimated_artifact_bytes"]
        == estimated
    )
    assert (
        reconciled["output_preflight"]["required_free_bytes"]
        == estimated + safety
    )
    assert reconciled["counts"]["queued_or_running"] == 3
    assert probe_calls == probes_before_restart + 2
    replay_states = {
        item["file_name"]: item["idempotent_replay"]
        for item in reconciled["files"]
    }
    surviving_name = next(
        item["file_name"]
        for item in reconciled["files"]
        if item["pipeline_job_id"] == str(surviving_job_id)
    )
    assert replay_states[surviving_name] is True
    assert sum(replay_states.values()) == 1
    assert len(pipelines.started_names) == 2
    assert surviving_name not in pipelines.started_names
    with pytest.raises(ProjectArtifactRootUnavailable):
        restarted_registry.adjust_capacity_reservation(
            project_id,
            binding,
            reservation_id=f"folder-batch:{batch_id}",
            reserved_bytes=estimated,
            safety_reserve_bytes=safety,
        )
    restarted.shutdown()

    second_restart_registry = ProjectArtifactRootRegistry(
        data_root=data_root,
        disk_usage=lambda _path: SimpleNamespace(free=free_bytes),
    )
    for item in reconciled["files"]:
        second_restart_registry.adjust_capacity_reservation(
            project_id,
            binding,
            reservation_id=(
                f"pipeline-job:{UUID(item['pipeline_job_id'])}"
            ),
            reserved_bytes=int(item["estimated_output_bytes"]),
            safety_reserve_bytes=safety,
        )
    second_restart = FolderPipelineBatchService(
        media,
        pipelines,
        data_root=data_root,
        artifact_root_registry=second_restart_registry,
        probe_timeout_seconds=5,
    )
    loaded_again = second_restart.get(project_id, batch_id)
    assert second_restart.recovery_summary()["records_rejected"] == 0
    assert loaded_again["output_preflight"]["state"] == "ready"
    assert (
        loaded_again["output_preflight"]["estimated_artifact_bytes"]
        == estimated
    )
    assert loaded_again["counts"]["queued_or_running"] == 3
    second_restart.shutdown()
    media.shutdown()


def test_active_batch_reservation_prevents_same_root_overcommit(
    tmp_path, monkeypatch
):
    project_id = uuid4()
    media, first_snapshot_id, directory = _stable_folder(
        tmp_path, project_id
    )
    snapshots = [
        media.capture_snapshot(project_id, str(directory))
        for _ in range(3)
    ]
    second_stability = media.compare_snapshots(
        project_id,
        [UUID(str(item["snapshot_id"])) for item in snapshots],
    )
    second_snapshot_id = UUID(
        str(second_stability["stable_snapshot_id"])
    )

    def fake_run(argv, **kwargs):
        size = Path(argv[-1]).stat().st_size
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(_valid_probe_output(size)), ""
        )

    monkeypatch.setattr("app.media_intake.subprocess.run", fake_run)
    single_plan = build_pipeline_storage_plan(
        duration_seconds=12.5,
        candidate_rescan_fps=8,
    )
    estimated = 3 * single_plan["estimated_artifact_bytes"]
    required = estimated + single_plan["safety_reserve_bytes"]
    free_bytes = required + estimated // 2
    custom_base = (tmp_path / "reserved output").resolve()
    custom_base.mkdir()
    registry = ProjectArtifactRootRegistry(
        data_root=(tmp_path / "bridge-reserved").resolve(),
        disk_usage=lambda _path: SimpleNamespace(free=free_bytes),
    )
    location = registry.register(
        project_id, selected_directory=custom_base
    )
    registry.capture_profile_binding(
        project_id,
        profile_revision=1,
        output_location_id=location["output_location_id"],
    )
    pipeline = FakePipelineJobs(location["output_location_id"])
    service = FolderPipelineBatchService(
        media,
        pipeline,
        data_root=(tmp_path / "bridge-reserved").resolve(),
        artifact_root_registry=registry,
        probe_timeout_seconds=5,
    )
    first, _ = service.start(
        project_id,
        snapshot_id=first_snapshot_id,
        rights_confirmed=True,
        idempotency_key=f"folder-reserve-first-{first_snapshot_id}",
    )
    first_completed = _wait_for_batch(
        service, project_id, UUID(first["batch_id"])
    )
    assert first_completed["output_preflight"]["state"] == "ready"
    assert len(pipeline.started_names) == 3

    second, _ = service.start(
        project_id,
        snapshot_id=second_snapshot_id,
        rights_confirmed=True,
        idempotency_key=f"folder-reserve-second-{second_snapshot_id}",
    )
    second_completed = _wait_for_batch(
        service, project_id, UUID(second["batch_id"])
    )
    assert second_completed["status"] == "blocked_storage"
    assert second_completed["output_preflight"]["error_code"] == (
        "insufficient_project_storage"
    )
    assert len(pipeline.started_names) == 3
    service.shutdown()
    media.shutdown()


def test_active_batch_reservation_spans_custom_roots_on_same_volume(
    tmp_path, monkeypatch
):
    project_id = uuid4()
    media, first_snapshot_id, directory = _stable_folder(
        tmp_path, project_id
    )
    snapshots = [
        media.capture_snapshot(project_id, str(directory))
        for _ in range(3)
    ]
    second_snapshot_id = UUID(
        str(
            media.compare_snapshots(
                project_id,
                [UUID(str(item["snapshot_id"])) for item in snapshots],
            )["stable_snapshot_id"]
        )
    )

    def fake_run(argv, **kwargs):
        size = Path(argv[-1]).stat().st_size
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(_valid_probe_output(size)), ""
        )

    monkeypatch.setattr("app.media_intake.subprocess.run", fake_run)
    single_plan = build_pipeline_storage_plan(
        duration_seconds=12.5,
        candidate_rescan_fps=8,
    )
    estimated = 3 * single_plan["estimated_artifact_bytes"]
    required = estimated + single_plan["safety_reserve_bytes"]
    free_bytes = required + estimated // 2
    first_base = (tmp_path / "volume root one").resolve()
    second_base = (tmp_path / "volume root two").resolve()
    first_base.mkdir()
    second_base.mkdir()
    registry = ProjectArtifactRootRegistry(
        data_root=(tmp_path / "bridge-volume").resolve(),
        disk_usage=lambda _path: SimpleNamespace(free=free_bytes),
    )
    first_location = registry.register(
        project_id, selected_directory=first_base
    )
    second_location = registry.register(
        project_id, selected_directory=second_base
    )
    registry.capture_profile_binding(
        project_id,
        profile_revision=1,
        output_location_id=first_location["output_location_id"],
    )
    pipeline = FakePipelineJobs(
        first_location["output_location_id"],
        revision=1,
    )
    service = FolderPipelineBatchService(
        media,
        pipeline,
        data_root=(tmp_path / "bridge-volume").resolve(),
        artifact_root_registry=registry,
        probe_timeout_seconds=5,
    )
    first, _ = service.start(
        project_id,
        snapshot_id=first_snapshot_id,
        rights_confirmed=True,
        idempotency_key=f"folder-volume-first-{first_snapshot_id}",
    )
    assert _wait_for_batch(
        service, project_id, UUID(first["batch_id"])
    )["output_preflight"]["state"] == "ready"
    assert len(pipeline.started_names) == 3

    registry.capture_profile_binding(
        project_id,
        profile_revision=2,
        output_location_id=second_location["output_location_id"],
    )
    pipeline.output_location_id = second_location[
        "output_location_id"
    ]
    pipeline.revision = 2
    second, _ = service.start(
        project_id,
        snapshot_id=second_snapshot_id,
        rights_confirmed=True,
        idempotency_key=f"folder-volume-second-{second_snapshot_id}",
    )
    second_completed = _wait_for_batch(
        service, project_id, UUID(second["batch_id"])
    )
    assert second_completed["status"] == "blocked_storage"
    assert len(pipeline.started_names) == 3
    service.shutdown()
    media.shutdown()


def test_unexpected_preflight_failure_is_durable_and_worker_continues(
    tmp_path, monkeypatch
):
    project_id = uuid4()
    media, first_snapshot_id, directory = _stable_folder(
        tmp_path, project_id
    )
    snapshots = [
        media.capture_snapshot(project_id, str(directory))
        for _ in range(3)
    ]
    second_snapshot_id = UUID(
        str(
            media.compare_snapshots(
                project_id,
                [UUID(str(item["snapshot_id"])) for item in snapshots],
            )["stable_snapshot_id"]
        )
    )

    def fake_run(argv, **kwargs):
        size = Path(argv[-1]).stat().st_size
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(_valid_probe_output(size)), ""
        )

    monkeypatch.setattr("app.media_intake.subprocess.run", fake_run)
    disk_calls = 0

    def flaky_disk_usage(_path):
        nonlocal disk_calls
        disk_calls += 1
        if disk_calls == 1:
            raise RuntimeError("simulated disconnect race")
        return SimpleNamespace(free=100 * 1024**3)

    pipeline = FakePipelineJobs()
    service = FolderPipelineBatchService(
        media,
        pipeline,
        data_root=tmp_path.resolve(),
        probe_timeout_seconds=5,
        disk_usage=flaky_disk_usage,
    )
    first, _ = service.start(
        project_id,
        snapshot_id=first_snapshot_id,
        rights_confirmed=True,
        idempotency_key=f"folder-worker-fail-{first_snapshot_id}",
    )
    first_completed = _wait_for_batch(
        service, project_id, UUID(first["batch_id"])
    )
    assert first_completed["output_preflight"]["error_code"] == (
        "batch_processing_failed"
    )
    assert first_completed["counts"]["enqueue_failed"] == 3
    assert pipeline.started_names == []

    second, _ = service.start(
        project_id,
        snapshot_id=second_snapshot_id,
        rights_confirmed=True,
        idempotency_key=f"folder-worker-next-{second_snapshot_id}",
    )
    second_completed = _wait_for_batch(
        service, project_id, UUID(second["batch_id"])
    )
    assert second_completed["output_preflight"]["state"] == "ready"
    assert len(pipeline.started_names) == 3
    service.shutdown()
    media.shutdown()


def test_legacy_completed_grants_merge_multiple_files_under_one_snapshot(
    tmp_path,
):
    project_id = uuid4()
    snapshot_id = uuid4()
    directory = tmp_path / "legacy aggregate grants"
    directory.mkdir()
    paths = []
    for name, data in (("A.mov", b"first"), ("b.mp4", b"second")):
        path = directory / name
        path.write_bytes(data)
        paths.append(path)
    media = MediaIntakeService(minimum_interval_seconds=0)
    for path in paths:
        stat = path.stat()
        media.restore_completed_media_grant(
            CompletedMediaGrant(
                project_id=project_id,
                snapshot_id=snapshot_id,
                hash_job_id=uuid4(),
                file_name=path.name,
                media_path=path.resolve(),
                size_bytes=stat.st_size,
                modified_time_ns=stat.st_mtime_ns,
                created_time_ns=stat.st_ctime_ns,
                sha256_lower=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        )
    grant = media.export_stable_snapshot_grant(
        project_id, snapshot_id
    )
    assert [item.name for item in grant.files] == ["A.mov", "b.mp4"]
    media.shutdown()


class BatchApiRuntime:
    def capability_snapshot(self, profile):
        return {
            "local_models": "ready",
            "selected_report_provider": profile["report_provider"],
            "bulk_frame_provider": "local_models_only",
        }

    def execute(self, stage, context, cancel):
        if stage == "metadata_gps_preflight":
            return PipelineStageResult(
                "blocked_gpx_required",
                checkpoint={
                    "timed_track_status": "missing",
                    "capture_start_coordinate_used_as_route": False,
                },
                metrics={"route_point_count": 0},
                error_code="gpx_required",
                non_blocking=True,
            )
        if stage == "question_queue":
            return PipelineStageResult(
                "success",
                checkpoint={"question_queue_revision": "folder-api-q1"},
                metrics={"question_count": 1},
            )
        return PipelineStageResult(
            "success",
            checkpoint={f"{stage}_id": f"{stage}-folder-api"},
            metrics={"completed": 1},
        )

    def cancel(self, context):
        return None

    def validate_review_completion(self, context, review_completion_id):
        return {
            "review_completion_id": review_completion_id,
            "all_required_questions_answered": True,
            "answered_question_count": 1,
            "server_answer_ledger_revision": "folder-api-ledger",
        }


def test_profile_change_during_batch_preflight_rejects_all_jobs(
    tmp_path, monkeypatch
):
    project_id = uuid4()
    media, snapshot_id, _ = _stable_folder(tmp_path, project_id)
    bridge_root = (tmp_path / "bridge-pinned-batch").resolve()
    first_base = (tmp_path / "pinned root one").resolve()
    second_base = (tmp_path / "pinned root two").resolve()
    first_base.mkdir()
    second_base.mkdir()
    registry = ProjectArtifactRootRegistry(
        data_root=bridge_root,
        disk_usage=lambda _path: SimpleNamespace(free=100 * 1024**3),
    )
    first_location = registry.register(
        project_id, selected_directory=first_base
    )
    second_location = registry.register(
        project_id, selected_directory=second_base
    )
    pipelines = PipelineJobService(
        BatchApiRuntime(),
        data_root=bridge_root,
        artifact_root_registry=registry,
    )

    def configure(location_id):
        return pipelines.configure_profile(
            project_id,
            question_policy="learning_assist",
            review_mode="quick",
            label_confirmation_authority="human_review",
            default_template_id="sample-q3",
            default_template_sha256=hashlib.sha256(
                b"template"
            ).hexdigest(),
            detector_pack_id="detector-v1",
            ocr_pack_id="ocr-v1",
            sam2_pack_id="sam2-v1",
            report_provider="none",
            output_location_id=location_id,
            candidate_rescan_fps=8,
        )

    configure(first_location["output_location_id"])
    probe_entered = threading.Event()
    release_probe = threading.Event()

    def fake_run(argv, **kwargs):
        probe_entered.set()
        assert release_probe.wait(timeout=3)
        size = Path(argv[-1]).stat().st_size
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(_valid_probe_output(size)), ""
        )

    monkeypatch.setattr("app.media_intake.subprocess.run", fake_run)
    monkeypatch.setattr(
        "app.pipeline_jobs.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=100 * 1024**3),
    )
    monkeypatch.setattr(
        "app.folder_pipeline_batches."
        "dense_frame_index_requires_short_staging",
        lambda _path: False,
    )
    service = FolderPipelineBatchService(
        media,
        pipelines,
        data_root=bridge_root,
        artifact_root_registry=registry,
        probe_timeout_seconds=5,
    )
    started, _ = service.start(
        project_id,
        snapshot_id=snapshot_id,
        rights_confirmed=True,
        idempotency_key=f"folder-profile-race-{snapshot_id}",
    )
    assert probe_entered.wait(timeout=3)
    configure(second_location["output_location_id"])
    release_probe.set()

    completed = _wait_for_batch(
        service, project_id, UUID(started["batch_id"])
    )
    assert completed["counts"]["enqueue_failed"] == 3
    assert all(
        item["pipeline_job_id"] is None
        and item["last_error_code"]
        == "pipeline_profile_binding_changed"
        for item in completed["files"]
    )
    assert pipelines.list(project_id) == []
    service.shutdown()
    pipelines.shutdown()
    media.shutdown()


def _configure_api_profile(app):
    app.state.pipeline_jobs.configure_profile(
        DEMO_PROJECT_ID,
        question_policy="learning_assist",
        review_mode="quick",
        label_confirmation_authority="human_review",
        default_template_id="sample-q3",
        default_template_sha256=hashlib.sha256(b"template").hexdigest(),
        detector_pack_id="detector-v1",
        ocr_pack_id="ocr-v1",
        sam2_pack_id="sam2-v1",
        report_provider="none",
        output_location_id="project-default",
        candidate_rescan_fps=8,
    )


def test_folder_batch_api_is_path_private_idempotent_and_restart_visible(
    tmp_path, monkeypatch
):
    directory = tmp_path / "api stable folder"
    directory.mkdir()
    for name, data in (
        ("03.mp4", b"333"),
        ("01.mov", b"1"),
        ("02.MP4", b"22"),
    ):
        (directory / name).write_bytes(data)
    fake_ffprobe = tmp_path / "ffprobe.exe"
    fake_ffprobe.write_bytes(b"fixture")

    def fake_run(argv, **kwargs):
        size = Path(argv[-1]).stat().st_size
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(_valid_probe_output(size)), ""
        )

    monkeypatch.setattr("app.media_intake.subprocess.run", fake_run)
    monkeypatch.setattr(
        "app.pipeline_jobs.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=100 * 1024**3),
    )
    monkeypatch.setattr(
        "app.folder_pipeline_batches."
        "dense_frame_index_requires_short_staging",
        lambda _path: False,
    )
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
    root = (tmp_path / "bridge-data").resolve()
    batch_id = None
    snapshot_id = None
    with TestClient(
        create_app(
            TEST_TOKEN,
            fixture_mode=True,
            allow_test_client=True,
            bridge_data_root=str(root),
            ffprobe_path=str(fake_ffprobe),
            pipeline_runtime=BatchApiRuntime(),
        )
    ) as client:
        client.app.state.project_artifact_roots._disk_usage = (
            lambda _path: SimpleNamespace(
                free=100 * 1024**3
            )
        )
        _configure_api_profile(client.app)
        snapshots = []
        for _ in range(3):
            response = client.post(
                f"/projects/{DEMO_PROJECT_ID}/media/snapshots",
                headers=headers,
                json={
                    "project_id": str(DEMO_PROJECT_ID),
                    "directory": str(directory),
                },
            )
            assert response.status_code == 201, response.text
            snapshots.append(response.json())
        stability = client.post(
            f"/projects/{DEMO_PROJECT_ID}/media/stability",
            headers=headers,
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "snapshot_ids": [
                    item["snapshot_id"] for item in snapshots
                ],
            },
        )
        assert stability.status_code == 200, stability.text
        snapshot_id = stability.json()["stable_snapshot_id"]
        request = {
            "project_id": str(DEMO_PROJECT_ID),
            "snapshot_id": snapshot_id,
            "rights_confirmed": True,
            "idempotency_key": f"folder-api-{snapshot_id}",
        }
        started = client.post(
            f"/projects/{DEMO_PROJECT_ID}/pipeline/folder-batches",
            headers=headers,
            json=request,
        )
        assert started.status_code == 202, started.text
        assert started.json()["idempotent_replay"] is False
        batch_id = started.json()["batch"]["batch_id"]
        replay = client.post(
            f"/projects/{DEMO_PROJECT_ID}/pipeline/folder-batches",
            headers=headers,
            json=request,
        )
        assert replay.status_code == 202, replay.text
        assert replay.json()["idempotent_replay"] is True
        endpoint = (
            f"/projects/{DEMO_PROJECT_ID}/pipeline/folder-batches/"
            f"{batch_id}"
        )
        deadline = time.monotonic() + 3
        current = None
        while time.monotonic() < deadline:
            current = client.get(endpoint, headers=headers)
            assert current.status_code == 200, current.text
            if current.json()["enqueue_completed"]:
                break
            time.sleep(0.01)
        payload = current.json()
        assert [item["file_name"] for item in payload["files"]] == [
            "01.mov",
            "02.MP4",
            "03.mp4",
        ]
        assert payload["counts"]["total"] == 3
        serialized = json.dumps(payload, ensure_ascii=False)
        assert str(directory) not in serialized
        assert "directory" not in serialized

    with TestClient(
        create_app(
            TEST_TOKEN,
            fixture_mode=True,
            allow_test_client=True,
            bridge_data_root=str(root),
            ffprobe_path=str(fake_ffprobe),
            pipeline_runtime=BatchApiRuntime(),
        )
    ) as restarted:
        response = restarted.get(
            f"/projects/{DEMO_PROJECT_ID}/pipeline/folder-batches",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        restored = response.json()["batches"][0]
        assert restored["batch_id"] == batch_id
        assert restored["snapshot_id"] == snapshot_id
        assert restored["recovered_after_restart"] is True
        assert restored["resume_count"] >= 1
        assert all(
            item["recovered_after_restart"]
            for item in restored["files"]
        )
