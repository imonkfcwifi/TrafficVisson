import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from app.dense_frame_index import (
    DenseFrameIndexCancelled,
    DenseFrameIndexEngine,
    DenseFrameIndexError,
    DenseFrameIndexLimits,
    DenseFrameIndexOutputLimit,
    DenseFrameIndexSourceChanged,
    MediaCommand,
    MediaCommandOutput,
    _run_bounded_command,
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
    assert str(source) in extraction.argv
    assert extraction.argv[extraction.argv.index("-frames:v") + 1] == "16"
    assert (
        extraction.argv[extraction.argv.index("-vf") + 1]
        == result.provenance.sampling_filter
    )
    pattern = Path(extraction.argv[-1])
    assert pattern.parent.parent == Path(result.output_directory)
    assert extraction.cwd == Path(result.output_directory)
    assert extraction.watch_directory == pattern.parent


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
