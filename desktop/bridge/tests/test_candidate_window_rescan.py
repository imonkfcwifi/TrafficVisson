import hashlib
import json
from pathlib import Path

import pytest

from app.candidate_window_rescan import (
    CandidateWindow,
    CandidateWindowRescanCancelled,
    CandidateWindowRescanEngine,
    CandidateWindowRescanLimits,
    CandidateWindowRescanManifestInvalid,
    CandidateWindowRescanOutputLimit,
    CandidateWindowRescanSourceChanged,
    _planned_output_bytes_upper_bound,
    load_candidate_window_rescan,
    resolve_candidate_window_frame,
)
from app.dense_frame_index import MediaCommand, MediaCommandOutput


def _jpeg(width: int = 320, height: int = 180) -> bytes:
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


def _limits(**overrides) -> CandidateWindowRescanLimits:
    values = {
        "sampling_fps": 8,
        "padding_before_seconds": 1.0,
        "padding_after_seconds": 1.0,
        "fallback_half_width_seconds": 0.75,
        "merge_gap_seconds": 0.25,
        "minimum_window_seconds": 0.5,
        "max_candidates": 16,
        "max_windows": 8,
        "max_total_window_seconds": 30.0,
        "max_frames": 128,
        "max_downstream_frames": 16,
        "max_source_bytes": 1024 * 1024,
        "max_frame_width": 320,
        "max_frame_pixels": 320 * 320,
        "max_frame_bytes": 1024,
        "max_total_output_bytes": 256 * 1024,
        "min_free_disk_bytes": 0,
        "max_manifest_bytes": 128 * 1024,
        "max_process_output_bytes": 4096,
        "probe_timeout_seconds": 2.0,
        "extraction_timeout_seconds": 2.0,
        "max_total_seconds": 10.0,
    }
    values.update(overrides)
    return CandidateWindowRescanLimits(**values)


def _fixture(tmp_path: Path, *, duration: float = 12.0):
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"mock ffmpeg executable")
    source = tmp_path / "road.mp4"
    source.write_bytes(b"immutable registered road video")
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    project_root = tmp_path / "project"
    project_root.mkdir()
    dense_run = project_root / "dense" / "dense-frame-index-test000001"
    dense_run.mkdir(parents=True)
    dense_index = dense_run / "frame-index.json"
    dense_index.write_text(
        json.dumps(
            {
                "schema_version": "trafficvisson.dense-frame-index.v1",
                "frame_count": 1,
                "frames": [{"frame_id": "a" * 24}],
                "provenance": {
                    "schema_version": (
                        "trafficvisson.dense-frame-index.v1"
                    ),
                    "source_sha256": source_sha256,
                    "sampling_fps": 2,
                    "indexed_duration_seconds": duration,
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    output = project_root / "candidate-window-rescans"
    output.mkdir()
    return (
        ffmpeg,
        source,
        source_sha256,
        project_root,
        dense_index,
        output,
    )


class FakeRescanRunner:
    def __init__(
        self,
        source: Path,
        *,
        frames_per_window: int = 3,
        mutate_source: bool = False,
    ) -> None:
        self.source = source
        self.frames_per_window = frames_per_window
        self.mutate_source = mutate_source
        self.calls: list[MediaCommand] = []

    def __call__(self, command, _cancel):
        self.calls.append(command)
        if command.argv[1:] == ("-version",):
            return MediaCommandOutput(
                0, b"ffmpeg version test-4.4.1\n", b""
            )
        output_pattern = Path(command.argv[-1])
        limit = int(
            command.argv[command.argv.index("-frames:v") + 1]
        )
        for index in range(min(self.frames_per_window, limit)):
            (
                output_pattern.parent
                / f"frame-{index:06d}.jpg"
            ).write_bytes(_jpeg())
        if self.mutate_source:
            self.source.write_bytes(
                self.source.read_bytes() + b"changed"
            )
        return MediaCommandOutput(0, b"", b"")


def _candidates():
    return [
        {
            "candidate_id": "a" * 20,
            "start_seconds": 1.0,
            "end_seconds": 2.0,
            "representative_timestamp_seconds": 1.5,
        },
        {
            "candidate_id": "b" * 20,
            "start_seconds": 2.1,
            "end_seconds": 3.0,
            "representative_timestamp_seconds": 2.5,
        },
        {
            "candidate_id": "c" * 20,
            "start_seconds": 8.0,
            "end_seconds": 8.5,
            "representative_timestamp_seconds": 8.2,
        },
    ]


def test_preflight_storage_bound_tracks_planned_frames_not_global_cap():
    limits = CandidateWindowRescanLimits()
    windows = (
        CandidateWindow(
            window_id="a" * 20,
            start_seconds=0.0,
            end_seconds=3.25,
            duration_seconds=3.25,
            source_candidate_ids=("b" * 20,),
            planned_frame_limit=27,
        ),
    )

    expected = (
        27 * limits.max_frame_bytes
        + limits.max_manifest_bytes
    )
    assert expected < limits.max_total_output_bytes
    assert (
        _planned_output_bytes_upper_bound(windows, limits)
        == expected
    )

    capped_limits = _limits(
        max_frame_bytes=64 * 1024,
        max_total_output_bytes=256 * 1024,
        max_manifest_bytes=128 * 1024,
    )
    assert (
        _planned_output_bytes_upper_bound(
            windows,
            capped_limits,
        )
        == capped_limits.max_total_output_bytes
    )


def test_candidate_windows_only_are_merged_extracted_and_hashed(tmp_path):
    (
        ffmpeg,
        source,
        source_sha256,
        project_root,
        dense_index,
        output,
    ) = _fixture(tmp_path)
    runner = FakeRescanRunner(source)
    result = CandidateWindowRescanEngine(
        ffmpeg_path=ffmpeg,
        limits=_limits(),
        command_runner=runner,
    ).build(
        source,
        source_sha256=source_sha256,
        dense_index_path=dense_index,
        candidates=_candidates(),
        project_root=project_root,
        output_directory=output,
    )

    assert result.cached is False
    assert len(result.windows) == 2
    assert len(result.frames) == 6
    assert len(result.downstream_evidence_ids) == 6
    assert result.windows[0].source_candidate_ids == (
        "a" * 20,
        "b" * 20,
    )
    assert all(frame.sha256 for frame in result.frames)
    assert all(
        frame.primary_candidate_id in frame.source_candidate_ids
        for frame in result.frames
    )
    extraction_calls = [
        command
        for command in runner.calls
        if command.argv[1:] != ("-version",)
    ]
    assert len(extraction_calls) == 2
    assert all("-ss" in command.argv for command in extraction_calls)
    assert all("-t" in command.argv for command in extraction_calls)
    assert all("-n" in command.argv for command in extraction_calls)
    assert all("-y" not in command.argv for command in extraction_calls)
    assert all(
        float(command.argv[command.argv.index("-t") + 1]) < 6.0
        for command in extraction_calls
    )
    assert all(command.cwd.parent == project_root for command in runner.calls)
    assert all(command.cwd.name.startswith(".cwr-") for command in runner.calls)
    manifest = json.loads(
        (
            Path(result.output_directory)
            / result.manifest_relative_path
        ).read_text(encoding="utf-8")
    )
    assert manifest["provenance"][
        "whole_video_high_frequency_scan"
    ] is False
    assert manifest["provenance"]["review_policy"][
        "automatic_annotation_creation"
    ] is False
    assert (
        hashlib.sha256(
            (
                Path(result.output_directory)
                / result.manifest_relative_path
            ).read_bytes()
        ).hexdigest()
        == result.manifest_sha256
    )


def test_same_source_dense_index_candidates_and_fps_reuse_manifest(tmp_path):
    (
        ffmpeg,
        source,
        source_sha256,
        project_root,
        dense_index,
        output,
    ) = _fixture(tmp_path)
    runner = FakeRescanRunner(source)
    engine = CandidateWindowRescanEngine(
        ffmpeg_path=ffmpeg,
        limits=_limits(),
        command_runner=runner,
    )
    first = engine.build(
        source,
        source_sha256=source_sha256,
        dense_index_path=dense_index,
        candidates=_candidates(),
        project_root=project_root,
        output_directory=output,
    )
    call_count = len(runner.calls)
    second = engine.build(
        source,
        source_sha256=source_sha256,
        dense_index_path=dense_index,
        candidates=_candidates(),
        project_root=project_root,
        output_directory=output,
    )

    assert second.cached is True
    assert second.output_directory == first.output_directory
    assert second.input_revision == first.input_revision
    assert second.manifest_sha256 == first.manifest_sha256
    assert len(runner.calls) == call_count
    assert len(list(output.iterdir())) == 1


def test_cancel_limit_source_change_and_zero_are_not_empty_success(tmp_path):
    (
        ffmpeg,
        source,
        source_sha256,
        project_root,
        dense_index,
        output,
    ) = _fixture(tmp_path)
    engine = CandidateWindowRescanEngine(
        ffmpeg_path=ffmpeg,
        limits=_limits(),
        command_runner=FakeRescanRunner(source),
    )
    with pytest.raises(CandidateWindowRescanCancelled):
        engine.build(
            source,
            source_sha256=source_sha256,
            dense_index_path=dense_index,
            candidates=_candidates(),
            project_root=project_root,
            output_directory=output,
            cancel=lambda: True,
        )
    with pytest.raises(CandidateWindowRescanOutputLimit):
        CandidateWindowRescanEngine(
            ffmpeg_path=ffmpeg,
            limits=_limits(max_total_window_seconds=1.0),
            command_runner=FakeRescanRunner(source),
        ).build(
            source,
            source_sha256=source_sha256,
            dense_index_path=dense_index,
            candidates=_candidates(),
            project_root=project_root,
            output_directory=output,
        )
    with pytest.raises(CandidateWindowRescanSourceChanged):
        CandidateWindowRescanEngine(
            ffmpeg_path=ffmpeg,
            limits=_limits(),
            command_runner=FakeRescanRunner(
                source, mutate_source=True
            ),
        ).build(
            source,
            source_sha256=source_sha256,
            dense_index_path=dense_index,
            candidates=_candidates(),
            project_root=project_root,
            output_directory=output,
        )
    assert not list(project_root.glob(".cwr-*"))


def test_manifest_and_frame_tampering_fail_closed(tmp_path):
    (
        ffmpeg,
        source,
        source_sha256,
        project_root,
        dense_index,
        output,
    ) = _fixture(tmp_path)
    result = CandidateWindowRescanEngine(
        ffmpeg_path=ffmpeg,
        limits=_limits(),
        command_runner=FakeRescanRunner(source),
    ).build(
        source,
        source_sha256=source_sha256,
        dense_index_path=dense_index,
        candidates=_candidates(),
        project_root=project_root,
        output_directory=output,
    )
    first = result.frames[0]
    (Path(result.output_directory) / first.relative_path).write_bytes(
        _jpeg() + b"tampered"
    )
    with pytest.raises(CandidateWindowRescanManifestInvalid):
        load_candidate_window_rescan(
            result.output_directory,
            expected_source_sha256=source_sha256,
        )


def test_downstream_resolver_returns_verified_internal_handle(tmp_path):
    (
        ffmpeg,
        source,
        source_sha256,
        project_root,
        dense_index,
        output,
    ) = _fixture(tmp_path)
    result = CandidateWindowRescanEngine(
        ffmpeg_path=ffmpeg,
        limits=_limits(max_downstream_frames=2),
        command_runner=FakeRescanRunner(source),
    ).build(
        source,
        source_sha256=source_sha256,
        dense_index_path=dense_index,
        candidates=_candidates(),
        project_root=project_root,
        output_directory=output,
    )
    assert len(result.downstream_evidence_ids) == 2
    resolved = resolve_candidate_window_frame(
        result, result.downstream_evidence_ids[0]
    )
    assert resolved.path.is_absolute()
    assert resolved.media_type == "image/jpeg"
    assert resolved.sha256 == hashlib.sha256(
        resolved.path.read_bytes()
    ).hexdigest()


@pytest.mark.parametrize("fps", [5, 6, 7, 8, 9, 10])
def test_only_project_profile_5_to_10_fps_is_supported(fps):
    assert _limits(sampling_fps=fps).sampling_fps == fps


@pytest.mark.parametrize("fps", [4, 11, 7.5, True])
def test_out_of_contract_rescan_fps_is_rejected(fps):
    with pytest.raises(ValueError):
        _limits(sampling_fps=fps)
