import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import app.dense_frame_index as dense_module
from app.dense_frame_index import (
    DenseFrameIndexCancelled,
    DenseFrameIndexEngine,
    DenseFrameIndexError,
    DenseFrameIndexLimits,
    DenseFrameIndexOutputLimit,
    DenseFrameIndexSourceChanged,
    DenseFrameIndexStagingUnavailable,
    MediaCommand,
    MediaCommandOutput,
    _run_bounded_command,
    build_dense_frame_capacity_plan,
    initialize_dense_staging_root,
    recover_dense_staging_root,
)


def _fake_tools(tmp_path: Path) -> tuple[Path, Path]:
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    ffmpeg.write_bytes(b"mock ffmpeg executable")
    ffprobe.write_bytes(b"mock ffprobe executable")
    return ffmpeg, ffprobe


def _source_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jpeg(width: int = 320, height: int = 180) -> bytes:
    # Minimal SOF0 structure sufficient for the bounded artifact validator.
    component_rows = b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    sof_payload = (
        b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03"
        + component_rows
    )
    return (
        b"\xff\xd8"
        + b"\xff\xc0"
        + (len(sof_payload) + 2).to_bytes(2, "big")
        + sof_payload
        + b"\xff\xd9"
    )


def _test_limits(**overrides) -> DenseFrameIndexLimits:
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


def test_dense_capacity_plan_matches_hard_writer_cap_and_ceil_boundary():
    digest = hashlib.sha256(b"dense-capacity").hexdigest()
    defaults = DenseFrameIndexLimits(sampling_fps=2)

    long_plan = build_dense_frame_capacity_plan(
        source_sha256=digest,
        duration_seconds=82 * 60,
        limits=defaults,
    )
    boundary_plan = build_dense_frame_capacity_plan(
        source_sha256=digest,
        duration_seconds=1.00000049,
        limits=defaults,
    )

    assert long_plan.planned_frame_count == 82 * 60 * 2 + 1
    assert long_plan.planned_output_bytes_upper_bound == 16 * 1024**3
    assert (
        long_plan.planned_output_bytes_upper_bound
        == long_plan.hard_ceiling_bytes
    )
    assert boundary_plan.source_duration_seconds == 1.0
    assert boundary_plan.planned_frame_count == 4


def test_frame_count_bounds_preserve_ffmpeg_tolerance_near_writer_cap():
    assert dense_module._frame_count_bounds(330.026, 2, 661) == (659, 661)


def test_frame_count_bounds_require_exact_cap_only_after_true_truncation():
    assert dense_module._frame_count_bounds(400.0, 2, 661) == (661, 661)


class FakeDenseRunner:
    def __init__(
        self,
        source: Path,
        *,
        duration: float = 2.0,
        frame_count: int = 4,
        mode: str = "valid",
    ) -> None:
        self.source = source
        self.duration = duration
        self.frame_count = frame_count
        self.mode = mode
        self.calls: list[MediaCommand] = []

    def __call__(self, command, cancel):
        self.calls.append(command)
        executable = Path(command.argv[0]).name.casefold()
        if command.argv[1:] == ("-version",):
            return MediaCommandOutput(
                0,
                f"{executable.removesuffix('.exe')} version test-1.0\n".encode(),
                b"",
            )
        if executable == "ffprobe.exe":
            payload = {
                "format": {
                    "duration": f"{self.duration:.6f}",
                    "size": str(self.source.stat().st_size),
                    "format_name": "mov,mp4",
                },
                "streams": [
                    {
                        "index": 0,
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1920,
                        "height": 1080,
                    }
                ],
            }
            if self.mode == "bad_probe_size":
                payload["format"]["size"] = "999"
            return MediaCommandOutput(
                0, json.dumps(payload).encode("utf-8"), b""
            )

        output_pattern = Path(command.argv[-1])
        if not output_pattern.is_absolute():
            output_pattern = command.cwd / output_pattern
        frames_root = output_pattern.parent
        for index in range(self.frame_count):
            name_index = index
            if self.mode == "gap" and index == self.frame_count - 1:
                name_index += 1
            (frames_root / f"frame-{name_index:08d}.jpg").write_bytes(_jpeg())
        if self.mode == "unexpected":
            (frames_root / "notes.txt").write_text("unexpected", encoding="utf-8")
        if self.mode == "oversized":
            (frames_root / "frame-00000000.jpg").write_bytes(b"x" * 2048)
        if self.mode == "mutate_source":
            self.source.write_bytes(self.source.read_bytes() + b"changed")
        if self.mode == "oversized_capture":
            return MediaCommandOutput(0, b"x" * 5000, b"")
        return MediaCommandOutput(0, b"", b"")


def _fixture_paths(tmp_path: Path):
    ffmpeg, ffprobe = _fake_tools(tmp_path)
    source = tmp_path / "road;not-a-command.mp4"
    source.write_bytes(b"registered immutable source video")
    project_root = tmp_path / "project"
    project_root.mkdir()
    output = project_root / "frame-indexes"
    output.mkdir()
    return ffmpeg, ffprobe, source, project_root, output


@pytest.mark.parametrize("fps", [1, 2, 3, 4, 5])
def test_integer_1_to_5_fps_contract_is_supported(fps):
    limits = _test_limits(sampling_fps=fps)
    assert limits.sampling_fps == fps


@pytest.mark.parametrize("fps", [0, 6, 1.5, True])
def test_sampling_outside_integer_1_to_5_fps_is_rejected(fps):
    with pytest.raises(ValueError):
        _test_limits(sampling_fps=fps)


def test_build_is_deterministic_bounded_and_review_only(tmp_path):
    ffmpeg, ffprobe, source, project_root, output = _fixture_paths(tmp_path)
    runner = FakeDenseRunner(source)
    engine = DenseFrameIndexEngine(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        limits=_test_limits(),
        command_runner=runner,
    )

    first = engine.build(
        source,
        source_sha256=_source_sha256(source),
        project_root=project_root,
        output_directory=output,
    )
    second = engine.build(
        source,
        source_sha256=_source_sha256(source),
        project_root=project_root,
        output_directory=output,
    )

    assert Path(first.output_directory).parent == output
    assert Path(second.output_directory).parent == output
    assert first.output_directory != second.output_directory
    assert [row.frame_id for row in first.frames] == [
        row.frame_id for row in second.frames
    ]
    assert [row.timestamp_seconds for row in first.frames] == [
        0.0,
        0.5,
        1.0,
        1.5,
    ]
    assert all(row.timestamp_denominator == 2 for row in first.frames)
    assert all(row.evidence_status == "unreviewed_candidate" for row in first.frames)
    assert first.provenance.source_access == (
        "read_only_sha256_and_identity_verified"
    )
    policy = first.provenance.review_policy
    assert policy.review_only is True
    assert policy.automatic_incident_creation is False
    assert policy.automatic_annotation_creation is False
    assert policy.automatic_training_ingest is False
    assert policy.automatic_maintenance_decision is False
    assert policy.automatic_model_promotion is False

    index_path = Path(first.output_directory) / first.index_relative_path
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "trafficvisson.dense-frame-index.v1"
    assert payload["frame_count"] == 4
    assert payload["provenance"]["review_policy"]["review_only"] is True
    assert payload["frames"][0]["relative_path"] == (
        "frames/frame-00000000.jpg"
    )


def test_ffmpeg_argv_is_non_shell_data_and_output_is_confined(tmp_path):
    ffmpeg, ffprobe, source, project_root, output = _fixture_paths(tmp_path)
    runner = FakeDenseRunner(source)
    result = DenseFrameIndexEngine(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        limits=_test_limits(),
        command_runner=runner,
    ).build(
        source,
        source_sha256=_source_sha256(source),
        project_root=project_root,
        output_directory=output,
    )

    extraction = next(
        command
        for command in runner.calls
        if Path(command.argv[0]).name.casefold() == "ffmpeg.exe"
        and command.argv[1:] != ("-version",)
    )
    assert isinstance(extraction.argv, tuple)
    assert "-n" in extraction.argv
    assert "-y" not in extraction.argv
    assert "-fps_mode" not in extraction.argv
    assert extraction.argv[extraction.argv.index("-vsync") + 1] == "0"
    assert str(source) in extraction.argv
    assert extraction.argv[extraction.argv.index("-frames:v") + 1] == "5"
    assert (
        extraction.argv[extraction.argv.index("-vf") + 1]
        == result.provenance.sampling_filter
    )
    pattern = Path(extraction.argv[-1])
    assert pattern == Path("frames") / "frame-%08d.jpg"
    assert (
        extraction.cwd / pattern.parent
        == Path(result.output_directory) / "frames"
    )
    assert extraction.cwd == Path(result.output_directory)
    assert extraction.watch_directory == (
        extraction.cwd / pattern.parent
    )


@pytest.mark.skipif(
    os.name != "nt",
    reason="ffmpeg 4.4 MAX_PATH staging is Windows-specific",
)
def test_long_windows_output_uses_short_staging_then_verified_publish(
    tmp_path,
):
    ffmpeg, ffprobe = _fake_tools(tmp_path)
    source = tmp_path / "short-source.mp4"
    source.write_bytes(b"registered immutable source video")
    project_root = (
        tmp_path
        / ("long-project-root-" + "a" * 90)
        / ("nested-output-" + "b" * 90)
    )
    output = project_root / ("job-" + "c" * 48)
    output.mkdir(parents=True)
    runner = FakeDenseRunner(source)
    staging_root = initialize_dense_staging_root(
        tmp_path / "dense-stage"
    )
    owner_id = uuid4()

    result = DenseFrameIndexEngine(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        limits=_test_limits(),
        staging_root=staging_root,
        staging_owner_id=str(owner_id),
        command_runner=runner,
    ).build(
        source,
        source_sha256=_source_sha256(source),
        project_root=project_root,
        output_directory=output,
    )

    extraction = next(
        command
        for command in runner.calls
        if Path(command.argv[0]).name.casefold() == "ffmpeg.exe"
        and command.argv[1:] != ("-version",)
    )
    published = Path(result.output_directory)
    assert extraction.cwd != published
    assert len(str(published / result.frames[0].relative_path)) >= 240
    assert published.parent == output
    assert (published / result.index_relative_path).is_file()
    assert result.provenance.output_scope == (
        "private_short_staging_then_verified_project_publish"
    )


def test_default_runner_uses_shell_false_and_explicit_cwd(
    tmp_path, monkeypatch
):
    ffmpeg, _ = _fake_tools(tmp_path)
    working = tmp_path / "working"
    working.mkdir()
    captured = {}

    class FinishedProcess:
        returncode = 0

        def poll(self):
            return 0

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FinishedProcess()

    monkeypatch.setattr(
        "app.dense_frame_index.subprocess.Popen", fake_popen
    )
    result = _run_bounded_command(
        MediaCommand(
            argv=(str(ffmpeg), "-version"),
            cwd=working,
            timeout_seconds=1.0,
            capture_limit_bytes=1024,
        ),
        None,
    )

    assert result.returncode == 0
    assert captured["argv"] == [str(ffmpeg), "-version"]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL
    assert captured["kwargs"]["cwd"] == str(working)


def test_registered_sha256_is_verified_before_any_output(tmp_path):
    ffmpeg, ffprobe, source, project_root, output = _fixture_paths(tmp_path)
    runner = FakeDenseRunner(source)
    engine = DenseFrameIndexEngine(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        limits=_test_limits(),
        command_runner=runner,
    )

    with pytest.raises(DenseFrameIndexSourceChanged):
        engine.build(
            source,
            source_sha256="0" * 64,
            project_root=project_root,
            output_directory=output,
        )

    assert runner.calls == []
    assert list(output.iterdir()) == []


def test_source_mutation_aborts_and_removes_only_owned_run(tmp_path):
    ffmpeg, ffprobe, source, project_root, output = _fixture_paths(tmp_path)
    keep = output / "keep.txt"
    keep.write_text("caller-owned", encoding="utf-8")
    runner = FakeDenseRunner(source, mode="mutate_source")
    engine = DenseFrameIndexEngine(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        limits=_test_limits(),
        command_runner=runner,
    )

    with pytest.raises(DenseFrameIndexSourceChanged):
        engine.build(
            source,
            source_sha256=_source_sha256(source),
            project_root=project_root,
            output_directory=output,
        )

    assert keep.read_text(encoding="utf-8") == "caller-owned"
    assert list(output.iterdir()) == [keep]


@pytest.mark.parametrize("mode", ["gap", "unexpected"])
def test_invalid_ffmpeg_frame_sequence_is_rejected_and_cleaned(tmp_path, mode):
    ffmpeg, ffprobe, source, project_root, output = _fixture_paths(tmp_path)
    runner = FakeDenseRunner(source, mode=mode)
    engine = DenseFrameIndexEngine(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        limits=_test_limits(),
        command_runner=runner,
    )

    with pytest.raises(DenseFrameIndexError):
        engine.build(
            source,
            source_sha256=_source_sha256(source),
            project_root=project_root,
            output_directory=output,
        )
    assert list(output.iterdir()) == []


def test_ffmpeg_artifact_and_capture_bounds_are_enforced(tmp_path):
    for mode in ("oversized", "oversized_capture"):
        case_root = tmp_path / mode
        case_root.mkdir()
        ffmpeg, ffprobe, source, project_root, output = _fixture_paths(case_root)
        runner = FakeDenseRunner(source, mode=mode)
        engine = DenseFrameIndexEngine(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            limits=_test_limits(),
            command_runner=runner,
        )
        with pytest.raises(DenseFrameIndexOutputLimit):
            engine.build(
                source,
                source_sha256=_source_sha256(source),
                project_root=project_root,
                output_directory=output,
            )
        assert list(output.iterdir()) == []


def test_short_media_uses_duration_bounded_disk_reservation(
    tmp_path,
    monkeypatch,
):
    ffmpeg, ffprobe, source, project_root, output = _fixture_paths(
        tmp_path
    )
    runner = FakeDenseRunner(
        source,
        duration=2.0,
        frame_count=4,
    )
    limits = _test_limits()
    # Less than the global 128 KiB run cap, but more than the bounded
    # 5-frame plus index budget for this two-second source.
    monkeypatch.setattr(
        "app.dense_frame_index.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=70 * 1024),
    )
    engine = DenseFrameIndexEngine(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        limits=limits,
        command_runner=runner,
    )

    result = engine.build(
        source,
        source_sha256=_source_sha256(source),
        project_root=project_root,
        output_directory=output,
    )

    assert len(result.frames) == 4
    extraction = next(
        command
        for command in runner.calls
        if "-frames:v" in command.argv
    )
    assert extraction.argv[
        extraction.argv.index("-frames:v") + 1
    ] == "5"
    assert extraction.watch_limit_bytes == 5 * 1024


def test_bad_ffprobe_result_is_rejected(tmp_path):
    ffmpeg, ffprobe, source, project_root, output = _fixture_paths(tmp_path)
    runner = FakeDenseRunner(source, mode="bad_probe_size")
    engine = DenseFrameIndexEngine(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        limits=_test_limits(),
        command_runner=runner,
    )

    with pytest.raises(DenseFrameIndexError):
        engine.build(
            source,
            source_sha256=_source_sha256(source),
            project_root=project_root,
            output_directory=output,
        )
    assert list(output.iterdir()) == []


def test_output_must_be_existing_strict_project_subfolder(tmp_path):
    ffmpeg, ffprobe, source, project_root, output = _fixture_paths(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    engine = DenseFrameIndexEngine(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        limits=_test_limits(),
        command_runner=FakeDenseRunner(source),
    )
    digest = _source_sha256(source)

    with pytest.raises(ValueError):
        engine.build(
            source,
            source_sha256=digest,
            project_root=project_root,
            output_directory=project_root,
        )
    with pytest.raises(ValueError):
        engine.build(
            source,
            source_sha256=digest,
            project_root=project_root,
            output_directory=outside,
        )

    assert list(output.iterdir()) == []
    assert list(outside.iterdir()) == []


def test_pre_start_cancellation_creates_no_output(tmp_path):
    ffmpeg, ffprobe, source, project_root, output = _fixture_paths(tmp_path)
    runner = FakeDenseRunner(source)
    engine = DenseFrameIndexEngine(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        limits=_test_limits(),
        command_runner=runner,
    )

    with pytest.raises(DenseFrameIndexCancelled):
        engine.build(
            source,
            source_sha256=_source_sha256(source),
            project_root=project_root,
            output_directory=output,
            cancel=lambda: True,
        )

    assert runner.calls == []
    assert list(output.iterdir()) == []


def test_output_symlink_is_rejected_when_platform_allows_it(tmp_path):
    ffmpeg, ffprobe, source, project_root, output = _fixture_paths(tmp_path)
    linked = project_root / "linked-output"
    try:
        os.symlink(output, linked, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable on this platform")
    engine = DenseFrameIndexEngine(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        limits=_test_limits(),
        command_runner=FakeDenseRunner(source),
    )
    with pytest.raises(ValueError):
        engine.build(
            source,
            source_sha256=_source_sha256(source),
            project_root=project_root,
            output_directory=linked,
        )


def test_long_windows_path_staging_blocks_before_media_or_output_write(
    tmp_path,
    monkeypatch,
):
    ffmpeg, ffprobe = _fake_tools(tmp_path)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    project_root = tmp_path
    output = project_root
    for index in range(4):
        output = output / (f"dense-{index}-" + "x" * 48)
        output.mkdir()
    staging = initialize_dense_staging_root(tmp_path / "stage")
    runner = FakeDenseRunner(source)
    monkeypatch.setattr(
        "app.dense_frame_index.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=1),
    )
    engine = DenseFrameIndexEngine(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        limits=_test_limits(),
        staging_root=staging,
        staging_owner_id=str(uuid4()),
        command_runner=runner,
    )

    with pytest.raises(DenseFrameIndexStagingUnavailable):
        engine.build(
            source,
            source_sha256=_source_sha256(source),
            project_root=project_root,
            output_directory=output,
        )

    assert runner.calls == []
    assert list(output.iterdir()) == []


def test_dense_staging_recovery_removes_only_verified_owned_child(tmp_path):
    root = initialize_dense_staging_root(tmp_path / "stage")
    owner_id = uuid4()
    child = root / f"job-{owner_id}"
    child.mkdir()
    payload = {
        "schema_version": "trafficvisson.dense-staging-child.v1",
        "owner_id": str(owner_id),
        "path_exposed": False,
    }
    wrapped = {
        **payload,
        "integrity_sha256": hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
    }
    raw = json.dumps(
        wrapped,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    marker = child / "ownership.json"
    marker.write_bytes(raw)

    assert recover_dense_staging_root(root) == 1
    assert not child.exists()

    unowned = root / "do-not-delete"
    unowned.mkdir()
    with pytest.raises(DenseFrameIndexStagingUnavailable):
        recover_dense_staging_root(root)
    assert unowned.is_dir()


def test_dense_staging_root_marker_rejects_symlink_and_oversize(
    tmp_path,
):
    root = initialize_dense_staging_root(tmp_path / "stage")
    marker = root / ".trafficvisson-dense-staging.json"
    sentinel = tmp_path / "sentinel.json"
    sentinel.write_bytes(b'{"sentinel":true}')
    marker.unlink()
    try:
        os.symlink(sentinel, marker)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks are unavailable on this platform")

    with pytest.raises(DenseFrameIndexStagingUnavailable):
        initialize_dense_staging_root(root)
    assert sentinel.read_bytes() == b'{"sentinel":true}'

    marker.unlink()
    marker.write_bytes(b"x" * (16 * 1024 + 1))
    with pytest.raises(DenseFrameIndexStagingUnavailable):
        initialize_dense_staging_root(root)
    assert marker.stat().st_size == 16 * 1024 + 1


def test_dense_staging_child_marker_symlink_preserves_sentinel(
    tmp_path,
):
    root = initialize_dense_staging_root(tmp_path / "stage")
    owner_id = uuid4()
    child = root / f"job-{owner_id}"
    child.mkdir()
    sentinel = tmp_path / "sentinel.json"
    sentinel.write_bytes(b'{"sentinel":true}')
    try:
        os.symlink(sentinel, child / "ownership.json")
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks are unavailable on this platform")

    with pytest.raises(DenseFrameIndexStagingUnavailable):
        recover_dense_staging_root(root)
    assert sentinel.read_bytes() == b'{"sentinel":true}'
    assert child.is_dir()


def test_dense_publish_incoming_marker_symlink_preserves_sentinel(
    tmp_path,
):
    output = tmp_path / "output"
    output.mkdir()
    owner_id = str(uuid4())
    incoming = output / f".dense-incoming-{owner_id}"
    incoming.mkdir()
    sentinel = tmp_path / "sentinel.json"
    sentinel.write_bytes(b'{"sentinel":true}')
    try:
        os.symlink(sentinel, incoming / "ownership.json")
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks are unavailable on this platform")

    with pytest.raises(DenseFrameIndexStagingUnavailable):
        dense_module._prepare_dense_publish_incoming(
            output,
            owner_id=owner_id,
            source_sha256=hashlib.sha256(b"source").hexdigest(),
            final_name="dense-frame-index-" + "a" * 24,
        )
    assert sentinel.read_bytes() == b'{"sentinel":true}'
    assert incoming.is_dir()


def test_dense_staging_recovery_fails_closed_on_sharing_violation(
    tmp_path,
    monkeypatch,
):
    root = initialize_dense_staging_root(tmp_path / "stage")
    owner_id = str(uuid4())
    child = dense_module._create_owned_staging_child(root, owner_id)

    def busy_rmtree(_path):
        raise PermissionError("simulated Windows sharing violation")

    monkeypatch.setattr(
        "app.dense_frame_index.shutil.rmtree",
        busy_rmtree,
    )
    with pytest.raises(DenseFrameIndexStagingUnavailable):
        recover_dense_staging_root(root)
    assert child.is_dir()


def test_dense_atomic_publish_adopts_renamed_run_after_restart(
    tmp_path,
    monkeypatch,
):
    ffmpeg, ffprobe, source, project_root, output = _fixture_paths(
        tmp_path
    )
    staging = initialize_dense_staging_root(tmp_path / "stage")
    owner_id = str(uuid4())
    first_runner = FakeDenseRunner(source)
    real_cleanup = dense_module._cleanup_dense_publish_incoming
    monkeypatch.setattr(
        "app.dense_frame_index.dense_frame_index_requires_short_staging",
        lambda _path: True,
    )
    monkeypatch.setattr(
        "app.dense_frame_index._cleanup_dense_publish_incoming",
        lambda *_args, **_kwargs: None,
    )
    first = DenseFrameIndexEngine(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        limits=_test_limits(),
        staging_root=staging,
        staging_owner_id=owner_id,
        command_runner=first_runner,
    ).build(
        source,
        source_sha256=_source_sha256(source),
        project_root=project_root,
        output_directory=output,
    )
    incoming = output / f".dense-incoming-{owner_id}"
    assert Path(first.output_directory).is_dir()
    assert incoming.is_dir()

    monkeypatch.setattr(
        "app.dense_frame_index._cleanup_dense_publish_incoming",
        real_cleanup,
    )
    second_runner = FakeDenseRunner(source)
    second = DenseFrameIndexEngine(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        limits=_test_limits(),
        staging_root=staging,
        staging_owner_id=owner_id,
        command_runner=second_runner,
    ).build(
        source,
        source_sha256=_source_sha256(source),
        project_root=project_root,
        output_directory=output,
    )

    assert second.output_directory == first.output_directory
    assert second_runner.calls == []
    assert not incoming.exists()
    assert len(
        [
            child
            for child in output.iterdir()
            if child.name.startswith("dense-frame-index-")
        ]
    ) == 1


def test_dense_atomic_publish_copy_failure_leaves_no_partial_final(
    tmp_path,
    monkeypatch,
):
    ffmpeg, ffprobe, source, project_root, output = _fixture_paths(
        tmp_path
    )
    staging = initialize_dense_staging_root(tmp_path / "stage")
    owner_id = str(uuid4())
    monkeypatch.setattr(
        "app.dense_frame_index.dense_frame_index_requires_short_staging",
        lambda _path: True,
    )

    def interrupted_copytree(_source, destination, **_kwargs):
        destination.mkdir()
        (destination / "partial.tmp").write_bytes(b"partial")
        raise OSError("simulated interrupted cross-volume copy")

    monkeypatch.setattr(
        "app.dense_frame_index.shutil.copytree",
        interrupted_copytree,
    )
    with pytest.raises(DenseFrameIndexError):
        DenseFrameIndexEngine(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            limits=_test_limits(),
            staging_root=staging,
            staging_owner_id=owner_id,
            command_runner=FakeDenseRunner(source),
        ).build(
            source,
            source_sha256=_source_sha256(source),
            project_root=project_root,
            output_directory=output,
        )

    assert not (
        output / f".dense-incoming-{owner_id}"
    ).exists()
    assert not any(
        child.name.startswith("dense-frame-index-")
        for child in output.iterdir()
    )


def test_same_volume_staging_reserves_run_and_atomic_copy_peak(
    tmp_path,
    monkeypatch,
):
    ffmpeg, ffprobe, source, project_root, output = _fixture_paths(
        tmp_path
    )
    staging = initialize_dense_staging_root(tmp_path / "stage")
    limits = _test_limits()
    runner = FakeDenseRunner(source)
    monkeypatch.setattr(
        "app.dense_frame_index.dense_frame_index_requires_short_staging",
        lambda _path: True,
    )
    # This fits the generic writer cap used by staging validation but cannot
    # hold both the completed staging run and the same-volume incoming copy.
    monkeypatch.setattr(
        "app.dense_frame_index.shutil.disk_usage",
        lambda _path: SimpleNamespace(
            free=limits.max_total_output_bytes
        ),
    )

    with pytest.raises(DenseFrameIndexOutputLimit):
        DenseFrameIndexEngine(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            limits=limits,
            staging_root=staging,
            staging_owner_id=str(uuid4()),
            command_runner=runner,
        ).build(
            source,
            source_sha256=_source_sha256(source),
            project_root=project_root,
            output_directory=output,
        )

    assert not any(
        Path(command.argv[0]).name.casefold() == "ffmpeg.exe"
        and command.argv[1:] != ("-version",)
        for command in runner.calls
    )
    assert list(output.iterdir()) == []
