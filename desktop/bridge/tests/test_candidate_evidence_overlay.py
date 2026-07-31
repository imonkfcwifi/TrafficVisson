from __future__ import annotations

import hashlib
import io
import json
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable
from uuid import UUID, uuid4

import pytest

from app.candidate_evidence_overlay import (
    CANDIDATE_EVIDENCE_OVERLAY_SCHEMA,
    CandidateEvidenceMediaCommand,
    CandidateEvidenceMediaCommandOutput,
    CandidateEvidenceOverlayCancelled,
    CandidateEvidenceOverlayConflict,
    CandidateEvidenceOverlayIntegrityError,
    CandidateEvidenceOverlayLimits,
    CandidateEvidenceOverlayNotFound,
    CandidateEvidenceOverlayOutputLimit,
    CandidateEvidenceOverlayStore,
    ResolvedCandidateEvidenceBinding,
    ResolvedDenseCandidateFrame,
    run_candidate_evidence_command,
)
from app.dense_candidate_discovery import (
    DENSE_CANDIDATE_DISCOVERY_SCHEMA,
    DenseCandidateDiscoveryResult,
    DenseCandidateSeed,
)
from app.media_intake import CompletedMediaGrant


PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
ANALYSIS_JOB_ID = UUID("22222222-2222-4222-8222-222222222222")
PIPELINE_JOB_ID = UUID("33333333-3333-4333-8333-333333333333")
DENSE_INDEX_SHA256 = "d" * 64
FRAME_ID = "a" * 24
JPEG_BODY = b"\xff\xd8\xff\xe0trafficvisson-frame\xff\xd9"
MP4_BODY = b"\x00\x00\x00\x18ftypisom" + (b"\x00" * 116)
SOURCE_BODY = b"\x00\x00\x00\x18ftypisom" + (b"\x01" * 4_096)


def test_ffmpeg_runner_uses_windows_long_path_for_process_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deep_root = tmp_path
    for index in range(7):
        deep_root = deep_root / (f"evidence-{index}-" + ("x" * 32))
    deep_root.mkdir(parents=True)
    executable = tmp_path / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    executable.write_bytes(b"test executable")
    output = deep_root / "candidate.mp4"
    captured: dict[str, object] = {}

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            self.returncode = 0

        def poll(self) -> int:
            return 0

    def fake_popen(*args, **kwargs):
        captured["args"] = args
        captured["cwd"] = kwargs["cwd"]
        return FakeProcess()

    monkeypatch.setattr(
        "app.candidate_evidence_overlay.subprocess.Popen",
        fake_popen,
    )
    result = run_candidate_evidence_command(
        CandidateEvidenceMediaCommand(
            argv=(str(executable), "-version"),
            cwd=deep_root,
            timeout_seconds=10.0,
            capture_limit_bytes=1_024,
            output_path=output,
        ),
        None,
    )
    assert result.returncode == 0
    if os.name == "nt" and len(str(deep_root)) >= 248:
        assert str(captured["cwd"]).startswith("\\\\?\\")
    else:
        assert captured["cwd"] == str(deep_root)


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _candidate(
    *,
    candidate_id: str = "b" * 20,
    frame_id: str = FRAME_ID,
    frame_sha256: str = _sha256(JPEG_BODY),
    timestamp_seconds: float = 8.0,
) -> DenseCandidateSeed:
    return DenseCandidateSeed(
        candidate_id=candidate_id,
        issue_type="pothole",
        start_seconds=round(timestamp_seconds - 0.25, 6),
        end_seconds=round(timestamp_seconds + 0.25, 6),
        representative_timestamp_seconds=timestamp_seconds,
        representative_frame_id=frame_id,
        representative_frame_sha256=frame_sha256,
        score=0.91,
        normalized_box={
            "x": 0.1,
            "y": 0.2,
            "width": 0.3,
            "height": 0.4,
        },
        ocr_text=None,
        urgent_review=False,
        supporting_frame_ids=(frame_id,),
        proposal_sources=("promoted_project_model",),
        representative_proposal_provenance={
            "model_version_id": "model-version-1",
            "dataset_hash": "e" * 64,
        },
        proposal_provenance_sha256="f" * 64,
    )


def _discovery(
    source_sha256: str,
    *,
    project_id: UUID = PROJECT_ID,
    candidates: tuple[DenseCandidateSeed, ...] | None = None,
) -> DenseCandidateDiscoveryResult:
    candidate_values = (
        (_candidate(),) if candidates is None else candidates
    )
    provider_states = (
        {
            "provider": "promoted_project_model",
            "state": "ready",
            "model_version_id": "model-version-1",
            "artifact_manifest_sha256": "c" * 64,
        },
    )
    revision_payload = {
        "schema_version": DENSE_CANDIDATE_DISCOVERY_SCHEMA,
        "project_id": str(project_id),
        "source_sha256": source_sha256,
        "dense_index_sha256": DENSE_INDEX_SHA256,
        "sampling_fps": 2,
        "scanned_frame_count": 20,
        "provider_states": provider_states,
        "candidates": [
            asdict(candidate) for candidate in candidate_values
        ],
        "review_only": True,
        "automatic_annotation_creation": False,
        "automatic_incident_creation": False,
        "automatic_training_ingest": False,
        "automatic_report_truth": False,
    }
    return DenseCandidateDiscoveryResult(
        schema_version=DENSE_CANDIDATE_DISCOVERY_SCHEMA,
        project_id=str(project_id),
        source_sha256=source_sha256,
        dense_index_sha256=DENSE_INDEX_SHA256,
        sampling_fps=2,
        scanned_frame_count=20,
        candidates=candidate_values,
        provider_states=provider_states,
        discovery_revision=_sha256(_canonical_json(revision_payload)),
    )


@dataclass
class _Harness:
    root: Path
    source_path: Path
    source_body: bytes
    frame_root: Path
    frame_path: Path
    ffmpeg_path: Path
    grant: CompletedMediaGrant
    binding: ResolvedCandidateEvidenceBinding
    frame: ResolvedDenseCandidateFrame
    commands: list[CandidateEvidenceMediaCommand]
    runner: Callable[
        [CandidateEvidenceMediaCommand, Callable[[], bool] | None],
        CandidateEvidenceMediaCommandOutput,
    ]

    def store(
        self,
        *,
        binding: ResolvedCandidateEvidenceBinding | None = None,
        frame: ResolvedDenseCandidateFrame | None = None,
        runner: Callable[
            [
                CandidateEvidenceMediaCommand,
                Callable[[], bool] | None,
            ],
            CandidateEvidenceMediaCommandOutput,
        ]
        | None = None,
        limits: CandidateEvidenceOverlayLimits | None = None,
        artifact_root_resolver: Callable[
            [UUID, UUID, UUID], Path
        ]
        | None = None,
    ) -> CandidateEvidenceOverlayStore:
        resolved_binding = binding or self.binding
        resolved_frame = frame or self.frame

        def binding_resolver(
            project_id: UUID,
            analysis_job_id: UUID,
            pipeline_job_id: UUID,
            source_sha256: str,
        ) -> ResolvedCandidateEvidenceBinding:
            assert project_id == PROJECT_ID
            assert analysis_job_id == ANALYSIS_JOB_ID
            assert pipeline_job_id == PIPELINE_JOB_ID
            assert source_sha256 == self.grant.sha256_lower
            return resolved_binding

        def frame_resolver(
            project_id: UUID,
            pipeline_job_id: UUID,
            dense_index_sha256: str,
            frame_id: str,
        ) -> ResolvedDenseCandidateFrame:
            assert project_id == PROJECT_ID
            assert pipeline_job_id == PIPELINE_JOB_ID
            assert dense_index_sha256 == DENSE_INDEX_SHA256
            assert frame_id == FRAME_ID
            return resolved_frame

        return CandidateEvidenceOverlayStore(
            self.root,
            binding_resolver=binding_resolver,
            frame_resolver=frame_resolver,
            artifact_root_resolver=artifact_root_resolver,
            ffmpeg_path=self.ffmpeg_path,
            limits=limits,
            command_runner=runner or self.runner,
        )


@pytest.fixture
def harness(tmp_path: Path) -> _Harness:
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(SOURCE_BODY)
    source_stat = source_path.stat()
    frame_root = tmp_path / "dense-frames"
    frame_root.mkdir()
    frame_path = frame_root / "frame-00000001.jpg"
    frame_path.write_bytes(JPEG_BODY)
    ffmpeg_path = tmp_path / "ffmpeg.exe"
    ffmpeg_path.write_bytes(b"test-only-ffmpeg")
    grant = CompletedMediaGrant(
        project_id=PROJECT_ID,
        snapshot_id=uuid4(),
        hash_job_id=uuid4(),
        file_name=source_path.name,
        media_path=source_path,
        size_bytes=source_stat.st_size,
        modified_time_ns=source_stat.st_mtime_ns,
        sha256_lower=_sha256(SOURCE_BODY),
    )
    binding = ResolvedCandidateEvidenceBinding(
        project_id=PROJECT_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_status="completed",
        grant=grant,
    )
    frame = ResolvedDenseCandidateFrame(
        project_id=PROJECT_ID,
        dense_index_sha256=DENSE_INDEX_SHA256,
        frame_id=FRAME_ID,
        frames_root=frame_root,
        path=frame_path,
        media_type="image/jpeg",
        size_bytes=len(JPEG_BODY),
        sha256=_sha256(JPEG_BODY),
    )
    commands: list[CandidateEvidenceMediaCommand] = []

    def runner(
        command: CandidateEvidenceMediaCommand,
        _cancel: Callable[[], bool] | None,
    ) -> CandidateEvidenceMediaCommandOutput:
        commands.append(command)
        command.output_path.write_bytes(MP4_BODY)
        return CandidateEvidenceMediaCommandOutput(0, b"", b"")

    return _Harness(
        root=tmp_path / "overlays",
        source_path=source_path,
        source_body=SOURCE_BODY,
        frame_root=frame_root,
        frame_path=frame_path,
        ffmpeg_path=ffmpeg_path,
        grant=grant,
        binding=binding,
        frame=frame,
        commands=commands,
        runner=runner,
    )


def _materialize(
    store: CandidateEvidenceOverlayStore,
    discovery: DenseCandidateDiscoveryResult,
    *,
    cancel: Callable[[], bool] | None = None,
):
    return store.materialize(
        project_id=PROJECT_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        discovery=discovery,
        cancel=cancel,
    )


def _overlay_directory(harness: _Harness, overlay_id: str) -> Path:
    return (
        harness.root
        / str(PROJECT_ID)
        / str(ANALYSIS_JOB_ID)
        / str(PIPELINE_JOB_ID)
        / overlay_id
    )


def test_materializes_immutable_evidence_without_truth_side_effects_or_path_leaks(
    harness: _Harness,
) -> None:
    analysis_record = harness.source_path.parent / "analysis-record.json"
    analysis_record.write_text('{"status":"completed"}', encoding="utf-8")
    analysis_before = analysis_record.read_bytes()
    discovery = _discovery(harness.grant.sha256_lower)

    result = _materialize(harness.store(), discovery)

    assert result.schema_version == CANDIDATE_EVIDENCE_OVERLAY_SCHEMA
    assert result.idempotent_replay is False
    assert len(result.candidates) == 1
    assert result.to_checkpoint()["annotations_created"] == 0
    assert result.to_checkpoint()["incidents_created"] == 0
    assert result.to_checkpoint()["training_members_created"] == 0
    assert result.to_checkpoint()["report_truth_records_created"] == 0
    assert analysis_record.read_bytes() == analysis_before
    assert harness.source_path.read_bytes() == harness.source_body

    command = harness.commands[0]
    assert command.argv[0] == str(harness.ffmpeg_path.resolve())
    assert "-t" in command.argv
    assert command.argv[command.argv.index("-t") + 1] == "3.000000"
    assert "-an" in command.argv
    assert "libx264" in command.argv
    assert command.output_path.parent == command.cwd
    assert command.cwd.parent.parent == harness.root.resolve()
    assert command.cwd.parent.name.startswith(".ceo-")

    candidate = harness.store().resolve_candidate(
        PROJECT_ID,
        ANALYSIS_JOB_ID,
        PIPELINE_JOB_ID,
        result.overlay_id,
        result.candidates[0].candidate_id,
    )
    image = harness.store().resolve_artifact(
        PROJECT_ID,
        ANALYSIS_JOB_ID,
        PIPELINE_JOB_ID,
        result.overlay_id,
        candidate.candidate_id,
        "image",
    )
    clip = harness.store().resolve_artifact(
        PROJECT_ID,
        ANALYSIS_JOB_ID,
        PIPELINE_JOB_ID,
        result.overlay_id,
        candidate.candidate_id,
        "clip",
    )
    assert image.path.read_bytes() == JPEG_BODY
    assert clip.path.read_bytes() == MP4_BODY

    overlay_dir = _overlay_directory(harness, result.overlay_id)
    manifest_text = (overlay_dir / "manifest.json").read_text(
        encoding="utf-8"
    )
    safe_payload = json.dumps(
        {
            "checkpoint": result.to_checkpoint(),
            "candidate": asdict(candidate),
        },
        ensure_ascii=False,
    )
    assert str(harness.root) not in manifest_text
    assert str(harness.root) not in safe_payload
    assert str(harness.source_path) not in manifest_text
    assert str(harness.source_path) not in safe_payload


def test_same_revision_is_idempotent_and_recovers_after_restart(
    harness: _Harness,
) -> None:
    discovery = _discovery(harness.grant.sha256_lower)
    first = _materialize(harness.store(), discovery)

    def unexpected_runner(
        _command: CandidateEvidenceMediaCommand,
        _cancel: Callable[[], bool] | None,
    ) -> CandidateEvidenceMediaCommandOutput:
        raise AssertionError("idempotent recovery must not rerun ffmpeg")

    restarted = harness.store(runner=unexpected_runner)
    replay = _materialize(restarted, discovery)
    recovered = restarted.get(
        PROJECT_ID,
        ANALYSIS_JOB_ID,
        PIPELINE_JOB_ID,
        first.overlay_id,
    )

    assert replay.overlay_id == first.overlay_id
    assert replay.manifest_sha256 == first.manifest_sha256
    assert replay.idempotent_replay is True
    assert recovered.overlay_id == first.overlay_id
    assert recovered.candidates == first.candidates


def test_external_job_root_keeps_heavy_media_outside_bridge_catalog_and_replays(
    harness: _Harness,
) -> None:
    discovery = _discovery(harness.grant.sha256_lower)
    external_root = harness.root.parent / "job-candidate-evidence"
    external_root.mkdir()
    store = harness.store()

    first = store.materialize(
        project_id=PROJECT_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        discovery=discovery,
        artifact_root=external_root,
    )

    external_overlay = external_root / first.overlay_id
    catalog_overlay = _overlay_directory(harness, first.overlay_id)
    assert (external_overlay / "manifest.json").is_file()
    assert (external_overlay / "artifacts").is_dir()
    assert sorted(item.name for item in catalog_overlay.iterdir()) == [
        "artifact-locator.json",
        "artifact-locator.json.sha256",
    ]
    locator_text = (
        catalog_overlay / "artifact-locator.json"
    ).read_text(encoding="utf-8")
    assert str(external_root) not in locator_text
    assert first.annotations_created == 0
    assert first.incidents_created == 0
    assert first.training_members_created == 0
    assert first.report_truth_records_created == 0

    def unexpected_runner(
        _command: CandidateEvidenceMediaCommand,
        _cancel: Callable[[], bool] | None,
    ) -> CandidateEvidenceMediaCommandOutput:
        raise AssertionError("external replay must not rerun ffmpeg")

    restarted = harness.store(runner=unexpected_runner)
    with pytest.raises(CandidateEvidenceOverlayNotFound):
        restarted.get(
            PROJECT_ID,
            ANALYSIS_JOB_ID,
            PIPELINE_JOB_ID,
            first.overlay_id,
        )
    resolver_calls: list[tuple[UUID, UUID, UUID]] = []

    def resolve_artifact_root(
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
    ) -> Path:
        resolver_calls.append(
            (project_id, pipeline_job_id, analysis_job_id)
        )
        return external_root

    restarted_with_resolver = harness.store(
        runner=unexpected_runner,
        artifact_root_resolver=resolve_artifact_root,
    )
    recovered = restarted_with_resolver.get(
        PROJECT_ID,
        ANALYSIS_JOB_ID,
        PIPELINE_JOB_ID,
        first.overlay_id,
    )
    assert recovered.overlay_id == first.overlay_id
    assert resolver_calls == [
        (PROJECT_ID, PIPELINE_JOB_ID, ANALYSIS_JOB_ID)
    ]

    replay = restarted_with_resolver.materialize(
        project_id=PROJECT_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        discovery=discovery,
        artifact_root=external_root,
    )
    assert replay.idempotent_replay is True
    image = restarted_with_resolver.resolve_artifact(
        PROJECT_ID,
        ANALYSIS_JOB_ID,
        PIPELINE_JOB_ID,
        replay.overlay_id,
        replay.candidates[0].candidate_id,
        "image",
    )
    assert image.path.is_relative_to(external_root.resolve())
    assert image.path.read_bytes() == JPEG_BODY


def test_external_job_root_change_and_locator_tamper_fail_closed(
    harness: _Harness,
) -> None:
    discovery = _discovery(harness.grant.sha256_lower)
    first_root = harness.root.parent / "candidate-root-a"
    second_root = harness.root.parent / "candidate-root-b"
    first_root.mkdir()
    second_root.mkdir()
    store = harness.store()
    result = store.materialize(
        project_id=PROJECT_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        discovery=discovery,
        artifact_root=first_root,
    )

    with pytest.raises(CandidateEvidenceOverlayConflict):
        store.materialize(
            project_id=PROJECT_ID,
            analysis_job_id=ANALYSIS_JOB_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            discovery=discovery,
            artifact_root=second_root,
        )

    restarted_with_wrong_root = harness.store(
        artifact_root_resolver=(
            lambda _project, _pipeline, _analysis: second_root
        )
    )
    with pytest.raises(CandidateEvidenceOverlayIntegrityError):
        restarted_with_wrong_root.get(
            PROJECT_ID,
            ANALYSIS_JOB_ID,
            PIPELINE_JOB_ID,
            result.overlay_id,
        )

    locator = (
        _overlay_directory(harness, result.overlay_id)
        / "artifact-locator.json"
    )
    locator.write_text('{"tampered":true}', encoding="utf-8")
    with pytest.raises(CandidateEvidenceOverlayIntegrityError):
        store.get(
            PROJECT_ID,
            ANALYSIS_JOB_ID,
            PIPELINE_JOB_ID,
            result.overlay_id,
        )


def test_rejects_cross_project_or_changed_analysis_source_binding(
    harness: _Harness,
) -> None:
    discovery = _discovery(harness.grant.sha256_lower)
    with pytest.raises(
        CandidateEvidenceOverlayIntegrityError,
        match="dense discovery result is invalid",
    ):
        _materialize(
            harness.store(),
            replace(discovery, project_id=str(uuid4())),
        )

    wrong_binding = replace(
        harness.binding,
        analysis_job_id=uuid4(),
    )
    with pytest.raises(
        CandidateEvidenceOverlayConflict,
        match="binding does not match",
    ):
        _materialize(
            harness.store(binding=wrong_binding),
            discovery,
        )

    first = _materialize(harness.store(), discovery)
    changed_grant = replace(
        harness.grant,
        snapshot_id=uuid4(),
    )
    changed_binding = replace(
        harness.binding,
        grant=changed_grant,
    )
    with pytest.raises(
        CandidateEvidenceOverlayConflict,
        match="source binding changed",
    ):
        _materialize(
            harness.store(binding=changed_binding),
            discovery,
        )
    assert first.overlay_id


def test_restart_recovery_rejects_artifact_tamper_and_hardlinks(
    harness: _Harness,
) -> None:
    discovery = _discovery(harness.grant.sha256_lower)
    first = _materialize(harness.store(), discovery)
    overlay_dir = _overlay_directory(harness, first.overlay_id)
    clip_path = (
        overlay_dir
        / "artifacts"
        / f"{first.candidates[0].candidate_id}.mp4"
    )
    clip_path.write_bytes(b"\x00" * len(MP4_BODY))

    with pytest.raises(CandidateEvidenceOverlayIntegrityError):
        harness.store().get(
            PROJECT_ID,
            ANALYSIS_JOB_ID,
            PIPELINE_JOB_ID,
            first.overlay_id,
        )

    # Restore the content through a second hardlink.  The byte hash is valid,
    # but immutable overlay artifacts must have exactly one filesystem link.
    clip_path.unlink()
    hardlink_source = harness.source_path.parent / "valid-clip.mp4"
    hardlink_source.write_bytes(MP4_BODY)
    os.link(hardlink_source, clip_path)
    with pytest.raises(CandidateEvidenceOverlayIntegrityError):
        harness.store().get(
            PROJECT_ID,
            ANALYSIS_JOB_ID,
            PIPELINE_JOB_ID,
            first.overlay_id,
        )


def test_source_change_during_ffmpeg_fails_closed_and_cleans_staging(
    harness: _Harness,
) -> None:
    discovery = _discovery(harness.grant.sha256_lower)

    def changing_runner(
        command: CandidateEvidenceMediaCommand,
        _cancel: Callable[[], bool] | None,
    ) -> CandidateEvidenceMediaCommandOutput:
        command.output_path.write_bytes(MP4_BODY)
        changed = bytearray(harness.source_body)
        changed[-1] ^= 0x01
        harness.source_path.write_bytes(changed)
        old = harness.grant.modified_time_ns
        os.utime(
            harness.source_path,
            ns=(old + 10_000_000, old + 10_000_000),
        )
        return CandidateEvidenceMediaCommandOutput(0, b"", b"")

    with pytest.raises(
        CandidateEvidenceOverlayIntegrityError,
        match="source changed",
    ):
        _materialize(
            harness.store(runner=changing_runner),
            discovery,
        )
    assert not list(harness.root.rglob(".ceo-*"))
    assert not list(harness.root.rglob("manifest.json"))


def test_rejects_bad_frame_grant_and_invalid_clip_output(
    harness: _Harness,
) -> None:
    discovery = _discovery(harness.grant.sha256_lower)
    bad_frame = replace(harness.frame, sha256="0" * 64)
    with pytest.raises(
        CandidateEvidenceOverlayIntegrityError,
        match="frame grant does not match",
    ):
        _materialize(
            harness.store(frame=bad_frame),
            discovery,
        )

    def bad_clip_runner(
        command: CandidateEvidenceMediaCommand,
        _cancel: Callable[[], bool] | None,
    ) -> CandidateEvidenceMediaCommandOutput:
        command.output_path.write_bytes(b"not-an-mp4")
        return CandidateEvidenceMediaCommandOutput(0, b"", b"")

    with pytest.raises(
        CandidateEvidenceOverlayIntegrityError,
        match="invalid MP4 magic",
    ):
        _materialize(
            harness.store(runner=bad_clip_runner),
            discovery,
        )


def test_cancellation_before_and_after_clip_has_no_final_overlay(
    harness: _Harness,
) -> None:
    discovery = _discovery(harness.grant.sha256_lower)
    with pytest.raises(CandidateEvidenceOverlayCancelled):
        _materialize(
            harness.store(),
            discovery,
            cancel=lambda: True,
        )
    assert harness.commands == []

    state = {"cancelled": False}

    def cancelling_runner(
        command: CandidateEvidenceMediaCommand,
        _cancel: Callable[[], bool] | None,
    ) -> CandidateEvidenceMediaCommandOutput:
        command.output_path.write_bytes(MP4_BODY)
        state["cancelled"] = True
        return CandidateEvidenceMediaCommandOutput(0, b"", b"")

    with pytest.raises(CandidateEvidenceOverlayCancelled):
        _materialize(
            harness.store(runner=cancelling_runner),
            discovery,
            cancel=lambda: state["cancelled"],
        )
    assert not list(harness.root.rglob(".ceo-*"))
    assert not list(harness.root.rglob("manifest.json"))


def test_zero_candidates_is_honest_success_but_more_than_512_is_rejected(
    harness: _Harness,
) -> None:
    empty = _discovery(
        harness.grant.sha256_lower,
        candidates=(),
    )
    result = _materialize(harness.store(), empty)
    assert result.candidates == ()
    assert result.to_checkpoint()["candidate_count"] == 0
    assert harness.commands == []

    too_many = tuple(
        _candidate(candidate_id=f"{index:020x}")
        for index in range(513)
    )
    oversized = _discovery(
        harness.grant.sha256_lower,
        candidates=too_many,
    )
    with pytest.raises(
        CandidateEvidenceOverlayIntegrityError,
        match="dense discovery result is invalid",
    ):
        _materialize(harness.store(), oversized)


def test_total_output_budget_is_enforced_and_partial_files_are_removed(
    harness: _Harness,
) -> None:
    candidates = (
        _candidate(candidate_id="1" * 20, timestamp_seconds=8.0),
        _candidate(candidate_id="2" * 20, timestamp_seconds=9.0),
    )
    discovery = _discovery(
        harness.grant.sha256_lower,
        candidates=candidates,
    )
    limits = CandidateEvidenceOverlayLimits(
        max_image_bytes=len(JPEG_BODY),
        max_clip_bytes=len(MP4_BODY),
        max_total_output_bytes=len(JPEG_BODY) + len(MP4_BODY),
        minimum_free_disk_bytes=0,
    )

    with pytest.raises(
        CandidateEvidenceOverlayOutputLimit,
        match="output budget",
    ):
        _materialize(
            harness.store(limits=limits),
            discovery,
        )
    assert not list(harness.root.rglob(".ceo-*"))
    assert not list(harness.root.rglob("manifest.json"))


def test_manifest_field_tamper_and_unsafe_frame_containment_fail_closed(
    harness: _Harness,
) -> None:
    discovery = _discovery(harness.grant.sha256_lower)
    first = _materialize(harness.store(), discovery)
    manifest_path = (
        _overlay_directory(harness, first.overlay_id)
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["candidate_count"] = 9
    manifest_path.write_text(
        json.dumps(manifest, separators=(",", ":")),
        encoding="utf-8",
    )
    with pytest.raises(CandidateEvidenceOverlayIntegrityError):
        harness.store().get(
            PROJECT_ID,
            ANALYSIS_JOB_ID,
            PIPELINE_JOB_ID,
            first.overlay_id,
        )

    clean_root = harness.source_path.parent / "separate-overlays"
    outside_frame = harness.source_path.parent / "outside.jpg"
    outside_frame.write_bytes(JPEG_BODY)
    unsafe_frame = replace(harness.frame, path=outside_frame)

    def binding_resolver(
        _project_id: UUID,
        _analysis_job_id: UUID,
        _pipeline_job_id: UUID,
        _source_sha256: str,
    ) -> ResolvedCandidateEvidenceBinding:
        return harness.binding

    def frame_resolver(
        _project_id: UUID,
        _pipeline_job_id: UUID,
        _dense_index_sha256: str,
        _frame_id: str,
    ) -> ResolvedDenseCandidateFrame:
        return unsafe_frame

    store = CandidateEvidenceOverlayStore(
        clean_root,
        binding_resolver=binding_resolver,
        frame_resolver=frame_resolver,
        ffmpeg_path=harness.ffmpeg_path,
        command_runner=harness.runner,
        limits=CandidateEvidenceOverlayLimits(
            minimum_free_disk_bytes=0
        ),
    )
    with pytest.raises(
        CandidateEvidenceOverlayIntegrityError,
        match="containment failed",
    ):
        _materialize(store, discovery)
