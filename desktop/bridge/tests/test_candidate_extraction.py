import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.candidate_extraction import (
    CandidateExtractionCancelled,
    CandidateExtractionEngine,
    CandidateExtractionError,
    CandidateExtractionOutputLimit,
    ExtractionLimits,
    _CommandOutput,
    _parse_source_location,
    _run_bounded_command,
)


def _fake_tools(tmp_path):
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    ffmpeg.write_bytes(b"test executable; process execution is mocked")
    ffprobe.write_bytes(b"test executable; process execution is mocked")
    return ffmpeg, ffprobe


def _test_limits(**overrides):
    values = {
        "cadence_seconds": 5.0,
        "scene_threshold": 0.3,
        "group_gap_seconds": 2.0,
        "preview_duration_seconds": 3.0,
        "max_sample_frames": 8,
        "max_candidate_groups": 8,
        "max_frame_width": 320,
        "max_frame_bytes": 1024,
        "max_clip_bytes": 1024,
        "max_output_bytes": 2048,
        "min_free_disk_bytes": 0,
        "max_probe_output_bytes": 4096,
        "max_scan_output_bytes": 4096,
        "probe_timeout_seconds": 2.0,
        "scan_timeout_seconds": 2.0,
        "artifact_timeout_seconds": 2.0,
        "max_total_seconds": 10.0,
        "max_media_duration_seconds": 3600.0,
    }
    values.update(overrides)
    return ExtractionLimits(**values)


class FakeMediaRunner:
    def __init__(self, source_path, discovery=None, oversized_image=False):
        self.source_path = source_path
        self.calls = []
        self.oversized_image = oversized_image
        self.discovery = discovery or (
            "frame:0 pts:0 pts_time:0.000000\n"
            "lavfi.scene_score=0.000000\n"
            "lavfi.signalstats.YAVG=25.500000\n"
            "frame:1 pts:10 pts_time:1.000000\n"
            "lavfi.scene_score=0.800000\n"
            "lavfi.signalstats.YAVG=51.000000\n"
            "frame:2 pts:20 pts_time:2.000000\n"
            "lavfi.scene_score=0.700000\n"
            "lavfi.signalstats.YAVG=76.500000\n"
            "frame:3 pts:100 pts_time:10.000000\n"
            "lavfi.scene_score=0.100000\n"
            "lavfi.signalstats.YAVG=12.750000\n"
        ).encode("utf-8")

    def __call__(self, argv, timeout, output_limit, cancel):
        self.calls.append((list(argv), timeout, output_limit, cancel))
        executable = Path(argv[0]).name.casefold()
        if argv[1:] == ["-version"]:
            return _CommandOutput(
                0, f"{executable} version test-1.0\n".encode("utf-8"), b""
            )
        if executable == "ffprobe.exe":
            payload = {
                "format": {
                    "duration": "12.000000",
                    "size": str(self.source_path.stat().st_size),
                    "format_name": "mov,mp4",
                    "tags": {"location": "+37.0545+126.8899/"},
                },
                "streams": [
                    {
                        "index": 0,
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1920,
                        "height": 1080,
                        "avg_frame_rate": "30/1",
                    }
                ],
            }
            return _CommandOutput(0, json.dumps(payload).encode("utf-8"), b"")
        if argv[-1] == "-":
            return _CommandOutput(0, self.discovery, b"")

        output_path = Path(argv[-1])
        if output_path.suffix == ".jpg":
            body = b"x" * (1100 if self.oversized_image else 16)
            output_path.write_bytes(b"\xff\xd8\xff" + body + b"\xff\xd9")
        elif output_path.suffix == ".mp4":
            output_path.write_bytes(
                b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomiso2"
            )
        else:
            raise AssertionError(f"unexpected output path: {output_path}")
        return _CommandOutput(0, b"", b"")


def test_default_runner_uses_argv_shell_false_and_bounded_capture(
    tmp_path, monkeypatch
):
    ffmpeg, _ = _fake_tools(tmp_path)
    captured = {}

    class FakeProcess:
        def __init__(self):
            self.stdout = io.BytesIO(b"stdout")
            self.stderr = io.BytesIO(b"stderr")
            self.returncode = 0

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(
        "app.candidate_extraction.subprocess.Popen", fake_popen
    )
    result = _run_bounded_command(
        [str(ffmpeg), "-version"], 1.0, 32, None
    )

    assert captured["argv"] == [str(ffmpeg), "-version"]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL
    assert result.stdout == b"stdout"
    assert result.stderr == b"stderr"


def test_default_runner_rejects_overflow_and_pre_start_cancellation(
    tmp_path, monkeypatch
):
    ffmpeg, _ = _fake_tools(tmp_path)

    class OverflowProcess:
        def __init__(self):
            self.stdout = io.BytesIO(b"x" * 64)
            self.stderr = io.BytesIO(b"")
            self.returncode = 0

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(
        "app.candidate_extraction.subprocess.Popen",
        lambda argv, **kwargs: OverflowProcess(),
    )
    with pytest.raises(CandidateExtractionOutputLimit):
        _run_bounded_command([str(ffmpeg), "-version"], 1.0, 8, None)

    def must_not_start(*args, **kwargs):
        raise AssertionError("cancelled work must not start a process")

    monkeypatch.setattr(
        "app.candidate_extraction.subprocess.Popen", must_not_start
    )
    with pytest.raises(CandidateExtractionCancelled):
        _run_bounded_command(
            [str(ffmpeg), "-version"], 1.0, 8, lambda: True
        )


def test_default_runner_kills_an_in_flight_process_on_cancellation(
    tmp_path, monkeypatch
):
    ffmpeg, _ = _fake_tools(tmp_path)
    process_holder = {}

    class RunningProcess:
        def __init__(self):
            self.stdout = io.BytesIO(b"")
            self.stderr = io.BytesIO(b"")
            self.returncode = None
            self.killed = False

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    def fake_popen(argv, **kwargs):
        process_holder["process"] = RunningProcess()
        return process_holder["process"]

    checks = {"count": 0}

    def cancel_after_start():
        checks["count"] += 1
        return checks["count"] >= 2

    monkeypatch.setattr(
        "app.candidate_extraction.subprocess.Popen", fake_popen
    )
    with pytest.raises(CandidateExtractionCancelled):
        _run_bounded_command(
            [str(ffmpeg), "-version"], 1.0, 32, cancel_after_start
        )

    assert process_holder["process"].killed is True


def test_engine_rejects_arbitrary_executable_names(tmp_path):
    malicious = tmp_path / "road-analyzer.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    malicious.write_bytes(b"x")
    ffprobe.write_bytes(b"x")

    with pytest.raises(CandidateExtractionError):
        CandidateExtractionEngine(
            ffmpeg_path=malicious,
            ffprobe_path=ffprobe,
        )


def test_extraction_is_deterministic_grouped_and_path_safe(tmp_path):
    ffmpeg, ffprobe = _fake_tools(tmp_path)
    source = tmp_path / "road;$(not-a-command).mp4"
    source.write_bytes(b"validated local video fixture")
    output_root = tmp_path / "project-output"
    output_root.mkdir()
    runner = FakeMediaRunner(source)
    engine = CandidateExtractionEngine(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        limits=_test_limits(),
        command_runner=runner,
    )

    first = engine.extract(
        source,
        output_root,
        source_sha256="a" * 64,
    )
    second = engine.extract(
        source,
        output_root,
        source_sha256="a" * 64,
    )

    assert [item.candidate_id for item in first.candidates] == [
        item.candidate_id for item in second.candidates
    ]
    assert len(first.candidates) == 2
    assert first.candidates[0].start_seconds == 0.0
    assert first.candidates[0].end_seconds == 2.0
    assert first.candidates[0].representative_timestamp_seconds == 1.0
    assert first.candidates[0].scene_score == 0.8
    assert first.candidates[0].sharpness_score == 0.2
    assert "scene_change" in first.candidates[0].triggers
    assert first.provenance.source_name == source.name
    assert first.provenance.source_sha256 == "a" * 64
    assert first.provenance.sharpness_score_method.startswith("edge-map")
    assert first.provenance.source_location is not None
    assert first.provenance.source_location.latitude == 37.0545
    assert first.provenance.source_location.longitude == 126.8899
    assert first.provenance.source_location.scope == "capture_start_only"
    assert Path(first.output_directory).parent == output_root
    assert Path(second.output_directory).parent == output_root
    assert first.output_directory != second.output_directory
    assert all(
        Path(item.representative_image.relative_path).name
        == item.representative_image.relative_path
        for item in first.candidates
    )
    assert all(
        (Path(first.output_directory) / item.preview_clip.relative_path).is_file()
        for item in first.candidates
    )

    source_arguments = [
        argument
        for argv, _, _, _ in runner.calls
        for argument in argv
        if argument == str(source)
    ]
    assert source_arguments
    assert all(isinstance(argv, list) for argv, _, _, _ in runner.calls)


def test_source_location_parser_accepts_real_iso6709_and_rejects_zero_or_noise():
    parsed = _parse_source_location(
        {
            "tags": {
                "com.apple.quicktime.location.ISO6709": (
                    "+37.0545+126.8899+123.5/"
                )
            }
        }
    )
    assert parsed is not None
    assert parsed.latitude == 37.0545
    assert parsed.longitude == 126.8899
    assert parsed.altitude_meters == 123.5
    assert parsed.scope == "capture_start_only"

    assert (
        _parse_source_location({"tags": {"location": "+00.0000+000.0000/"}})
        is None
    )
    assert (
        _parse_source_location({"tags": {"location": "not-a-coordinate"}})
        is None
    )
    assert (
        _parse_source_location({"tags": {"location": "+91.0+126.0/"}})
        is None
    )


def test_oversized_artifact_aborts_and_removes_only_its_run_directory(
    tmp_path,
):
    ffmpeg, ffprobe = _fake_tools(tmp_path)
    source = tmp_path / "road.mp4"
    source.write_bytes(b"validated local video fixture")
    output_root = tmp_path / "project-output"
    output_root.mkdir()
    keep = output_root / "user-owned.txt"
    keep.write_text("keep", encoding="utf-8")
    runner = FakeMediaRunner(source, oversized_image=True)
    engine = CandidateExtractionEngine(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        limits=_test_limits(),
        command_runner=runner,
    )

    with pytest.raises(CandidateExtractionOutputLimit):
        engine.extract(source, output_root)

    assert keep.read_text(encoding="utf-8") == "keep"
    assert list(output_root.iterdir()) == [keep]


def test_engine_checks_cancellation_before_creating_output(tmp_path):
    ffmpeg, ffprobe = _fake_tools(tmp_path)
    source = tmp_path / "road.mp4"
    source.write_bytes(b"validated local video fixture")
    output_root = tmp_path / "project-output"
    output_root.mkdir()
    runner = FakeMediaRunner(source)
    engine = CandidateExtractionEngine(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        limits=_test_limits(),
        command_runner=runner,
    )

    with pytest.raises(CandidateExtractionCancelled):
        engine.extract(source, output_root, cancel=lambda: True)

    assert list(output_root.iterdir()) == []
    assert runner.calls == []


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required for the integration test",
)
def test_tiny_generated_video_integration(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    source = tmp_path / "tiny-road.mp4"
    generation = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=10:duration=4",
            "-c:v",
            "mpeg4",
            "-q:v",
            "5",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(source),
        ],
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
        check=False,
    )
    if generation.returncode != 0:
        pytest.skip("the installed ffmpeg cannot generate the integration fixture")
    output_root = tmp_path / "project-output"
    output_root.mkdir()
    limits = _test_limits(
        cadence_seconds=1.5,
        scene_threshold=0.2,
        group_gap_seconds=0.5,
        preview_duration_seconds=2.0,
        max_sample_frames=4,
        max_candidate_groups=4,
        max_frame_width=160,
        max_frame_bytes=2 * 1024 * 1024,
        max_clip_bytes=16 * 1024 * 1024,
        max_output_bytes=18 * 1024 * 1024,
        probe_timeout_seconds=10.0,
        scan_timeout_seconds=30.0,
        artifact_timeout_seconds=30.0,
        max_total_seconds=60.0,
    )

    result = CandidateExtractionEngine(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        limits=limits,
    ).extract(source, output_root)

    assert 1 <= len(result.candidates) <= 4
    assert result.provenance.duration_seconds == pytest.approx(4.0, abs=0.2)
    for candidate in result.candidates:
        image = Path(result.output_directory) / (
            candidate.representative_image.relative_path
        )
        clip = Path(result.output_directory) / candidate.preview_clip.relative_path
        assert image.read_bytes().startswith(b"\xff\xd8\xff")
        assert b"ftyp" in clip.read_bytes()[:32]
        clip_probe = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "json",
                str(clip),
            ],
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert json.loads(clip_probe.stdout)["streams"][0]["codec_name"] == "h264"
        assert 0.0 <= candidate.scene_score <= 1.0
        assert 0.0 <= candidate.sharpness_score <= 1.0
