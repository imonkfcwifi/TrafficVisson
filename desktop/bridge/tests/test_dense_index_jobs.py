import hashlib
import json
import threading
import time
from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.dense_frame_index import (
    DenseFrameIndexCancelled,
    DenseFrameIndexLimits,
    DenseFrameIndexOutputLimit,
    DenseFrameIndexProvenance,
    DenseFrameIndexResult,
    DenseFrameRecord,
    FRAME_INDEX_SCHEMA,
    ReviewOnlyPolicy,
    _frame_id,
)
from app.dense_index_jobs import (
    DenseIndexJobNotFound,
    DenseIndexJobService,
    DenseIndexJobUnavailable,
    ResolvedDenseIndexSource,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _limits(**overrides) -> DenseFrameIndexLimits:
    values = {
        "sampling_fps": 2,
        "max_frames": 16,
        "max_media_seconds": 20.0,
        "max_source_duration_seconds": 60.0,
        "max_source_bytes": 1024 * 1024,
        "max_frame_width": 320,
        "max_frame_pixels": 320 * 320,
        "max_frame_bytes": 1024,
        "max_total_output_bytes": 128 * 1024,
        "min_free_disk_bytes": 0,
        "max_index_bytes": 64 * 1024,
        "max_probe_output_bytes": 4096,
        "max_process_output_bytes": 4096,
        "probe_timeout_seconds": 2.0,
        "extraction_timeout_seconds": 2.0,
        "max_total_seconds": 10.0,
    }
    values.update(overrides)
    return DenseFrameIndexLimits(**values)


class MappingResolver:
    def __init__(self, project_id: UUID, sources: dict[str, Path]):
        self.project_id = project_id
        self.sources = sources
        self.calls: list[tuple[UUID, str, str]] = []
        self.second_path: Path | None = None

    def __call__(self, project_id, identity, source_sha256):
        self.calls.append((project_id, identity, source_sha256))
        path = self.sources[identity]
        if self.second_path is not None and len(self.calls) >= 2:
            path = self.second_path
        metadata = path.stat()
        return ResolvedDenseIndexSource(
            project_id=project_id,
            opaque_source_identity=identity,
            source_sha256=source_sha256,
            media_path=path,
            size_bytes=metadata.st_size,
            modified_time_ns=metadata.st_mtime_ns,
        )


class EngineController:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = False
        self.raise_output_limit = False
        self.calls = 0
        self.active = 0
        self.maximum_active = 0
        self.lock = threading.Lock()

    def factory(self, limits):
        controller = self

        class FakeEngine:
            def build(
                self,
                media_path,
                *,
                source_sha256,
                project_root,
                output_directory,
                cancel,
            ):
                with controller.lock:
                    controller.calls += 1
                    controller.active += 1
                    controller.maximum_active = max(
                        controller.maximum_active, controller.active
                    )
                controller.started.set()
                try:
                    if controller.raise_output_limit:
                        raise DenseFrameIndexOutputLimit("bounded failure")
                    while controller.block and not controller.release.wait(0.01):
                        if cancel():
                            raise DenseFrameIndexCancelled("cancelled")
                    if cancel():
                        raise DenseFrameIndexCancelled("cancelled")
                    return _fake_result(
                        Path(media_path),
                        source_sha256,
                        Path(output_directory),
                        limits,
                    )
                finally:
                    with controller.lock:
                        controller.active -= 1

        return FakeEngine()


def _fake_result(
    source: Path,
    source_sha256: str,
    output_root: Path,
    limits: DenseFrameIndexLimits,
) -> DenseFrameIndexResult:
    run = output_root / f"dense-frame-index-test-{uuid4().hex[:12]}"
    frames_root = run / "frames"
    frames_root.mkdir(parents=True)
    records = []
    for index in range(2):
        frame_path = frames_root / f"frame-{index:08d}.jpg"
        frame_path.write_bytes(
            b"\xff\xd8\xff" + f"review-frame-{index}".encode()
        )
        body = frame_path.read_bytes()
        records.append(
            DenseFrameRecord(
                frame_id=_frame_id(
                    source_sha256, limits.sampling_fps, index
                ),
                frame_index=index,
                timestamp_seconds=round(
                    index / limits.sampling_fps, 6
                ),
                timestamp_numerator=index,
                timestamp_denominator=limits.sampling_fps,
                relative_path=f"frames/{frame_path.name}",
                width=320,
                height=180,
                size_bytes=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
            )
        )
    metadata = source.stat()
    provenance = DenseFrameIndexProvenance(
        schema_version=FRAME_INDEX_SCHEMA,
        source_name=source.name,
        source_size_bytes=metadata.st_size,
        source_modified_time_ns=metadata.st_mtime_ns,
        source_sha256=source_sha256,
        source_access="read_only_sha256_and_identity_verified",
        duration_seconds=2.0,
        indexed_duration_seconds=2.0,
        sampling_fps=limits.sampling_fps,
        sampling_filter=f"fake-fps={limits.sampling_fps}",
        timestamp_method=(
            "zero_based_frame_index_divided_by_integer_sampling_fps"
        ),
        frame_id_method=(
            "sha256(schema,source_sha256,fps,zero_based_frame_index)"
            "_first_24_hex"
        ),
        frame_count=len(records),
        truncated=False,
        ffmpeg_version="ffmpeg version test",
        ffprobe_version="ffprobe version test",
        output_scope=(
            "new_run_directory_under_authorized_project_subfolder"
        ),
        limits=asdict(limits),
        review_policy=ReviewOnlyPolicy(),
    )
    index_payload = {
        "schema_version": FRAME_INDEX_SCHEMA,
        "frame_count": len(records),
        "frames": [asdict(row) for row in records],
        "provenance": asdict(provenance),
    }
    (run / "frame-index.json").write_text(
        json.dumps(
            index_payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return DenseFrameIndexResult(
        output_directory=str(run),
        index_relative_path="frame-index.json",
        frames=tuple(records),
        provenance=provenance,
    )


def _wait_for(service, project_id, job_id, statuses, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = service.get(project_id, job_id)
        if row["status"] in statuses:
            return row
        time.sleep(0.01)
    raise AssertionError(f"job did not reach {statuses}")


def _fixture(tmp_path):
    project_id = uuid4()
    source = tmp_path / "road.mp4"
    source.write_bytes(b"registered road source")
    resolver = MappingResolver(project_id, {"source-a": source})
    controller = EngineController()
    service = DenseIndexJobService(
        resolver,
        data_root=tmp_path / "data",
        dense_limits=_limits(),
        engine_factory=controller.factory,
    )
    return project_id, source, resolver, controller, service


@pytest.mark.parametrize("fps", [1, 2, 3, 4, 5])
def test_service_accepts_only_integer_1_to_5_fps(tmp_path, fps):
    project_id, source, resolver, controller, service = _fixture(tmp_path)
    try:
        row = service.start(
            project_id,
            "source-a",
            _sha256(source),
            sampling_fps=fps,
        )
        assert row["sampling_fps"] == fps
        assert _wait_for(
            service, project_id, row["job_id"], {"completed"}
        )["status"] == "completed"
    finally:
        service.shutdown()


@pytest.mark.parametrize("fps", [0, 6, 1.5, True])
def test_service_rejects_fps_outside_contract(tmp_path, fps):
    project_id, source, resolver, controller, service = _fixture(tmp_path)
    try:
        with pytest.raises(ValueError):
            service.start(
                project_id,
                "source-a",
                _sha256(source),
                sampling_fps=fps,
            )
        assert resolver.calls == []
        assert controller.calls == 0
    finally:
        service.shutdown()


def test_completed_job_is_hashed_persisted_and_review_only(tmp_path):
    project_id, source, resolver, controller, service = _fixture(tmp_path)
    try:
        queued = service.start(
            project_id,
            "source-a",
            _sha256(source),
            sampling_fps=5,
        )
        assert queued["status"] == "queued"
        assert queued["progress_percent"] == 0
        completed = _wait_for(
            service, project_id, queued["job_id"], {"completed"}
        )
        assert completed["progress_percent"] == 100
        assert completed["progress_basis"] == (
            "stage_only_no_fabricated_frame_progress"
        )
        assert completed["record_sha256"]
        assert len(resolver.calls) == 2
        assert completed["result"]["frame_count"] == 2
        assert completed["result"]["safety"]["review_only"] is True
        for field in (
            "automatic_incident_creation",
            "automatic_annotation_creation",
            "automatic_training_ingest",
            "automatic_maintenance_decision",
            "automatic_model_promotion",
        ):
            assert completed["result"]["safety"][field] is False
        assert "output_directory" not in completed["result"]
        serialized = json.dumps(completed["result"])
        assert str(source.parent) not in serialized
        assert "prediction" not in serialized
        assert "model_inference" not in serialized
        assert "provider" not in serialized

        records = (
            tmp_path
            / "data"
            / "projects"
            / str(project_id)
            / "dense-index-job-records"
        )
        record_path = records / f"{queued['job_id']}.json"
        digest_path = records / f"{queued['job_id']}.sha256"
        raw = record_path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest_path.read_text()
        assert completed["record_sha256"] == digest_path.read_text()
    finally:
        service.shutdown()


def test_completed_artifact_resolution_is_project_scoped_and_reverified(
    tmp_path,
):
    project_id, source, resolver, controller, service = _fixture(tmp_path)
    try:
        queued = service.start(
            project_id,
            "source-a",
            _sha256(source),
            sampling_fps=2,
        )
        completed = _wait_for(
            service, project_id, queued["job_id"], {"completed"}
        )
        frame = completed["result"]["frames"][0]
        resolved = service.resolve_artifact(
            project_id,
            queued["job_id"],
            frame["frame_id"],
        )
        assert resolved.media_type == "image/jpeg"
        assert resolved.sha256 == frame["sha256"]
        assert resolved.path.name == "frame-00000000.jpg"
        assert str(resolved.path) not in json.dumps(
            completed, default=str
        )

        index = service.resolve_artifact(
            project_id,
            queued["job_id"],
            "frame-index",
        )
        assert index.media_type == "application/json"
        assert index.sha256 == completed["result"]["index"]["sha256"]

        with pytest.raises(DenseIndexJobNotFound):
            service.resolve_artifact(
                uuid4(),
                queued["job_id"],
                frame["frame_id"],
            )
        with pytest.raises(DenseIndexJobNotFound):
            service.resolve_artifact(
                project_id,
                queued["job_id"],
                "not-a-frame",
            )

        resolved.path.write_bytes(resolved.path.read_bytes() + b"tamper")
        with pytest.raises(DenseIndexJobUnavailable):
            service.resolve_artifact(
                project_id,
                queued["job_id"],
                frame["frame_id"],
            )
    finally:
        service.shutdown()


def test_single_worker_keeps_second_job_queued_and_cancel_is_honored(tmp_path):
    project_id = uuid4()
    source_a = tmp_path / "a.mp4"
    source_b = tmp_path / "b.mp4"
    source_a.write_bytes(b"source a")
    source_b.write_bytes(b"source b")
    resolver = MappingResolver(
        project_id, {"source-a": source_a, "source-b": source_b}
    )
    controller = EngineController()
    controller.block = True
    service = DenseIndexJobService(
        resolver,
        data_root=tmp_path / "data",
        dense_limits=_limits(),
        engine_factory=controller.factory,
    )
    try:
        first = service.start(
            project_id,
            "source-a",
            _sha256(source_a),
            sampling_fps=2,
        )
        assert controller.started.wait(1.0)
        running = _wait_for(
            service, project_id, first["job_id"], {"running"}
        )
        assert running["stage"] == "indexing_frames"
        assert running["progress_percent"] is None
        assert running["progress_state"] == "indeterminate"

        second = service.start(
            project_id,
            "source-b",
            _sha256(source_b),
            sampling_fps=2,
        )
        assert service.get(
            project_id, second["job_id"]
        )["status"] == "queued"
        cancelled = service.cancel(project_id, second["job_id"])
        assert cancelled["status"] == "cancelled"
        controller.release.set()
        assert _wait_for(
            service, project_id, first["job_id"], {"completed"}
        )["status"] == "completed"
        time.sleep(0.05)
        assert controller.calls == 1
        assert controller.maximum_active == 1
    finally:
        controller.release.set()
        service.shutdown()


def test_running_cancel_transitions_to_cancelled_without_persistence(tmp_path):
    project_id, source, resolver, controller, service = _fixture(tmp_path)
    controller.block = True
    try:
        row = service.start(
            project_id,
            "source-a",
            _sha256(source),
            sampling_fps=2,
        )
        assert controller.started.wait(1.0)
        cancelling = service.cancel(project_id, row["job_id"])
        assert cancelling["status"] == "cancelling"
        terminal = _wait_for(
            service, project_id, row["job_id"], {"cancelled"}
        )
        assert terminal["result"] is None
        records = (
            tmp_path
            / "data"
            / "projects"
            / str(project_id)
            / "dense-index-job-records"
        )
        assert list(records.glob("*.json")) == []
    finally:
        controller.release.set()
        service.shutdown()


def test_resolver_is_rechecked_and_changed_opaque_grant_fails(tmp_path):
    project_id, source, resolver, controller, service = _fixture(tmp_path)
    replacement = tmp_path / "replacement.mp4"
    replacement.write_bytes(source.read_bytes())
    resolver.second_path = replacement
    try:
        row = service.start(
            project_id,
            "source-a",
            _sha256(source),
            sampling_fps=2,
        )
        terminal = _wait_for(
            service, project_id, row["job_id"], {"failed"}
        )
        assert terminal["error_code"] == "source_changed"
        assert controller.calls == 0
        assert len(resolver.calls) == 2
    finally:
        service.shutdown()


def test_bounded_engine_failure_maps_without_result(tmp_path):
    project_id, source, resolver, controller, service = _fixture(tmp_path)
    controller.raise_output_limit = True
    try:
        row = service.start(
            project_id,
            "source-a",
            _sha256(source),
            sampling_fps=2,
        )
        terminal = _wait_for(
            service, project_id, row["job_id"], {"failed"}
        )
        assert terminal["error_code"] == "output_limit"
        assert terminal["result"] is None
    finally:
        service.shutdown()


def test_completed_record_recovers_after_restart(tmp_path):
    project_id, source, resolver, controller, service = _fixture(tmp_path)
    row = service.start(
        project_id,
        "source-a",
        _sha256(source),
        sampling_fps=3,
    )
    completed = _wait_for(
        service, project_id, row["job_id"], {"completed"}
    )
    service.shutdown()

    restarted = DenseIndexJobService(
        resolver,
        data_root=tmp_path / "data",
        dense_limits=_limits(),
        engine_factory=controller.factory,
    )
    try:
        recovered = restarted.get(project_id, row["job_id"])
        assert recovered["status"] == "completed"
        assert recovered["result"] == completed["result"]
        assert recovered["record_sha256"] == completed["record_sha256"]
        assert restarted.recovery_summary() == {
            "scanned_records": 2,
            "loaded_records": 1,
            "rejected_records": 0,
        }
        assert len(resolver.calls) == 2
    finally:
        restarted.shutdown()


@pytest.mark.parametrize("target", ["record", "digest", "index"])
def test_restart_refuses_tampered_persisted_state(tmp_path, target):
    project_id, source, resolver, controller, service = _fixture(tmp_path)
    row = service.start(
        project_id,
        "source-a",
        _sha256(source),
        sampling_fps=2,
    )
    completed = _wait_for(
        service, project_id, row["job_id"], {"completed"}
    )
    service.shutdown()
    project = tmp_path / "data" / "projects" / str(project_id)
    record = (
        project / "dense-index-job-records" / f"{row['job_id']}.json"
    )
    digest = record.with_suffix(".sha256")
    if target == "record":
        record.write_bytes(record.read_bytes() + b" ")
    elif target == "digest":
        digest.write_text("0" * 64, encoding="ascii")
    else:
        run_name = json.loads(record.read_text())["artifact_directory_name"]
        index = (
            project
            / "dense-frame-indexes"
            / run_name
            / completed["result"]["index"]["relative_path"]
        )
        index.write_bytes(index.read_bytes() + b" ")

    restarted = DenseIndexJobService(
        resolver,
        data_root=tmp_path / "data",
        dense_limits=_limits(),
        engine_factory=controller.factory,
    )
    try:
        with pytest.raises(DenseIndexJobNotFound):
            restarted.get(project_id, row["job_id"])
        summary = restarted.recovery_summary()
        assert summary["loaded_records"] == 0
        assert summary["rejected_records"] == 1
    finally:
        restarted.shutdown()


def test_project_scoping_hides_other_project_jobs(tmp_path):
    project_id, source, resolver, controller, service = _fixture(tmp_path)
    other_project = uuid4()
    try:
        row = service.start(
            project_id,
            "source-a",
            _sha256(source),
            sampling_fps=2,
        )
        _wait_for(service, project_id, row["job_id"], {"completed"})
        assert service.list(other_project) == []
        with pytest.raises(DenseIndexJobNotFound):
            service.get(other_project, row["job_id"])
    finally:
        service.shutdown()
