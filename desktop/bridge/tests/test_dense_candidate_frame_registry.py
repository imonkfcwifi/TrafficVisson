from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

import pytest

from app.dense_candidate_discovery import (
    DenseCandidateDiscoveryIntegrityError,
    resolve_dense_candidate_frame,
)
from app.dense_candidate_frame_registry import (
    DenseCandidateFrameRegistry,
    DenseCandidateFrameRegistryError,
)
from app.project_artifact_roots import (
    ProjectArtifactRootRegistry,
)


PROJECT_A = UUID("11111111-1111-4111-8111-111111111111")
PROJECT_B = UUID("22222222-2222-4222-8222-222222222222")
PIPELINE_JOB_A = UUID("33333333-3333-4333-8333-333333333333")
PIPELINE_JOB_B = UUID("44444444-4444-4444-8444-444444444444")
SOURCE_SHA256 = "a" * 64
_DENSE_INDEX_SCHEMA = "trafficvisson.dense-frame-index.v1"


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _frame_id(source_sha256: str, frame_index: int) -> str:
    material = (
        f"{_DENSE_INDEX_SCHEMA}\0{source_sha256}\0"
        f"fps=2\0frame={frame_index}"
    )
    return hashlib.sha256(material.encode("ascii")).hexdigest()[:24]


def _write_dense_index(
    bridge_data_root: Path,
    *,
    project_id: UUID = PROJECT_A,
    pipeline_job_id: UUID = PIPELINE_JOB_A,
    project_artifact_layout: bool = False,
    source_sha256: str = SOURCE_SHA256,
    frame_count: int = 2,
) -> tuple[Path, str, tuple[dict[str, object], ...]]:
    if project_artifact_layout:
        index_root = (
            bridge_data_root
            / "pipeline-jobs"
            / str(pipeline_job_id)
            / "dense"
            / "dense-frame-index-run-0001"
        )
    else:
        index_root = (
            bridge_data_root
            / "projects"
            / str(project_id)
            / "pipeline"
            / "dense"
            / str(pipeline_job_id)
            / "dense-frame-index-run-0001"
        )
    frames_root = index_root / "frames"
    frames_root.mkdir(parents=True)
    frames: list[dict[str, object]] = []
    for frame_index in range(frame_count):
        file_name = f"frame-{frame_index:08d}.jpg"
        body = (
            b"\xff\xd8\xff\xe0"
            + f"dense-frame-{frame_index}".encode("ascii")
            + b"\xff\xd9"
        )
        (frames_root / file_name).write_bytes(body)
        frames.append(
            {
                "frame_id": _frame_id(source_sha256, frame_index),
                "frame_index": frame_index,
                "timestamp_seconds": frame_index / 2,
                "timestamp_numerator": frame_index,
                "timestamp_denominator": 2,
                "relative_path": f"frames/{file_name}",
                "width": 1280,
                "height": 720,
                "size_bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "media_type": "image/jpeg",
                "evidence_status": "unreviewed_candidate",
            }
        )
    payload = {
        "schema_version": _DENSE_INDEX_SCHEMA,
        "frame_count": len(frames),
        "frames": frames,
        "provenance": {
            "schema_version": _DENSE_INDEX_SCHEMA,
            "source_sha256": source_sha256,
            "sampling_fps": 2,
            "frame_count": len(frames),
            "timestamp_method": (
                "zero_based_frame_index_divided_by_integer_sampling_fps"
            ),
        },
    }
    index_path = index_root / "frame-index.json"
    body = _canonical_json(payload)
    index_path.write_bytes(body)
    return index_path, hashlib.sha256(body).hexdigest(), tuple(frames)


def test_register_resolve_and_explicit_reregister_after_restart(
    tmp_path: Path,
) -> None:
    bridge_data_root = (tmp_path / "bridge-data").resolve()
    index_path, index_sha256, frames = _write_dense_index(
        bridge_data_root
    )

    registry = DenseCandidateFrameRegistry(bridge_data_root)
    registry.register(
        PROJECT_A,
        SOURCE_SHA256,
        index_sha256,
        index_path,
    )
    resolved = registry.resolve(
        PROJECT_A,
        index_sha256,
        str(frames[1]["frame_id"]),
    )

    assert resolved.project_id == PROJECT_A
    assert resolved.dense_index_sha256 == index_sha256
    assert resolved.frame_id == frames[1]["frame_id"]
    assert resolved.frames_root == index_path.parent / "frames"
    assert resolved.path == resolved.frames_root / "frame-00000001.jpg"
    assert resolved.media_type == "image/jpeg"
    assert resolved.size_bytes == frames[1]["size_bytes"]
    assert resolved.sha256 == frames[1]["sha256"]

    restarted = DenseCandidateFrameRegistry(bridge_data_root)
    with pytest.raises(
        DenseCandidateFrameRegistryError,
        match="not registered",
    ):
        restarted.resolve(
            PROJECT_A,
            index_sha256,
            str(frames[1]["frame_id"]),
        )

    restarted.register(
        PROJECT_A,
        SOURCE_SHA256,
        index_sha256,
        index_path,
    )
    assert (
        restarted.resolve(
            PROJECT_A,
            index_sha256,
            str(frames[1]["frame_id"]),
        ).sha256
        == frames[1]["sha256"]
    )


@pytest.mark.parametrize("target", ["index", "frame"])
def test_resolve_rehashes_index_and_selected_frame_and_rejects_tamper(
    tmp_path: Path,
    target: str,
) -> None:
    bridge_data_root = (tmp_path / "bridge-data").resolve()
    index_path, index_sha256, frames = _write_dense_index(
        bridge_data_root
    )
    registry = DenseCandidateFrameRegistry(bridge_data_root)
    registry.register(
        PROJECT_A,
        SOURCE_SHA256,
        index_sha256,
        index_path,
    )

    if target == "index":
        index_path.write_bytes(index_path.read_bytes() + b"\n")
        expected = "dense_index_hash_mismatch"
    else:
        frame_path = index_path.parent / str(frames[0]["relative_path"])
        body = bytearray(frame_path.read_bytes())
        body[-3] ^= 0x01
        frame_path.write_bytes(bytes(body))
        expected = "dense_frame_hash_mismatch"

    with pytest.raises(
        DenseCandidateFrameRegistryError,
        match=expected,
    ):
        registry.resolve(
            PROJECT_A,
            index_sha256,
            str(frames[0]["frame_id"]),
        )


def test_register_rejects_path_escape_and_cross_project_binding(
    tmp_path: Path,
) -> None:
    bridge_data_root = (tmp_path / "bridge-data").resolve()
    index_path, index_sha256, frames = _write_dense_index(
        bridge_data_root
    )
    outside_path, outside_sha256, _outside_frames = _write_dense_index(
        (tmp_path / "outside").resolve()
    )
    registry = DenseCandidateFrameRegistry(bridge_data_root)

    with pytest.raises(
        DenseCandidateFrameRegistryError,
        match="outside the bridge data root",
    ):
        registry.register(
            PROJECT_A,
            SOURCE_SHA256,
            outside_sha256,
            outside_path,
        )

    with pytest.raises(
        DenseCandidateFrameRegistryError,
        match="path is invalid",
    ):
        registry.register(
            PROJECT_B,
            SOURCE_SHA256,
            index_sha256,
            index_path,
        )

    registry.register(
        PROJECT_A,
        SOURCE_SHA256,
        index_sha256,
        index_path,
    )
    with pytest.raises(
        DenseCandidateFrameRegistryError,
        match="not registered",
    ):
        registry.resolve(
            PROJECT_B,
            index_sha256,
            str(frames[0]["frame_id"]),
        )


def test_public_resolver_rejects_wrong_source_and_index_sha(
    tmp_path: Path,
) -> None:
    bridge_data_root = (tmp_path / "bridge-data").resolve()
    index_path, index_sha256, frames = _write_dense_index(
        bridge_data_root
    )
    frame_id = str(frames[0]["frame_id"])

    with pytest.raises(
        DenseCandidateDiscoveryIntegrityError,
        match="dense_index_invalid",
    ):
        resolve_dense_candidate_frame(
            dense_index_path=index_path,
            source_sha256="b" * 64,
            dense_index_sha256=index_sha256,
            frame_id=frame_id,
        )

    with pytest.raises(
        DenseCandidateDiscoveryIntegrityError,
        match="dense_index_hash_mismatch",
    ):
        resolve_dense_candidate_frame(
            dense_index_path=index_path,
            source_sha256=SOURCE_SHA256,
            dense_index_sha256="c" * 64,
            frame_id=frame_id,
        )


def test_registry_rejects_wrong_source_and_digest_at_resolution(
    tmp_path: Path,
) -> None:
    bridge_data_root = (tmp_path / "bridge-data").resolve()
    index_path, index_sha256, frames = _write_dense_index(
        bridge_data_root
    )

    wrong_source_registry = DenseCandidateFrameRegistry(
        bridge_data_root
    )
    wrong_source_registry.register(
        PROJECT_A,
        "b" * 64,
        index_sha256,
        index_path,
    )
    with pytest.raises(
        DenseCandidateFrameRegistryError,
        match="dense_index_invalid",
    ):
        wrong_source_registry.resolve(
            PROJECT_A,
            index_sha256,
            str(frames[0]["frame_id"]),
        )

    wrong_index_registry = DenseCandidateFrameRegistry(
        bridge_data_root
    )
    wrong_index_registry.register(
        PROJECT_A,
        SOURCE_SHA256,
        "c" * 64,
        index_path,
    )
    with pytest.raises(
        DenseCandidateFrameRegistryError,
        match="dense_index_hash_mismatch",
    ):
        wrong_index_registry.resolve(
            PROJECT_A,
            "c" * 64,
            str(frames[0]["frame_id"]),
        )


def test_registry_rejects_digest_rebinding(
    tmp_path: Path,
) -> None:
    bridge_data_root = (tmp_path / "bridge-data").resolve()
    index_path, index_sha256, _frames = _write_dense_index(
        bridge_data_root
    )
    registry = DenseCandidateFrameRegistry(bridge_data_root)
    registry.register(
        PROJECT_A,
        SOURCE_SHA256,
        index_sha256,
        index_path,
    )

    with pytest.raises(
        DenseCandidateFrameRegistryError,
        match="digest was rebound",
    ):
        registry.register(
            PROJECT_A,
            "b" * 64,
            index_sha256,
            index_path,
        )


def _capture_bound_root(
    tmp_path: Path,
    *,
    project_id: UUID = PROJECT_A,
    custom: bool = False,
) -> tuple[
    ProjectArtifactRootRegistry,
    Path,
    dict[str, object],
]:
    bridge_root = (tmp_path / "bridge-data").resolve()
    artifact_roots = ProjectArtifactRootRegistry(
        data_root=bridge_root
    )
    if custom:
        selected = (tmp_path / "custom-output").resolve()
        selected.mkdir()
        location = artifact_roots.register(
            project_id,
            selected_directory=selected,
        )
        location_id = str(location["output_location_id"])
    else:
        location_id = str(
            artifact_roots.list(project_id)[0][
                "output_location_id"
            ]
        )
    artifact_roots.capture_profile_binding(
        project_id,
        profile_revision=1,
        output_location_id=location_id,
    )
    captured = artifact_roots.capture_job_binding(
        project_id,
        profile_revision=1,
        output_location_id=location_id,
    )
    return artifact_roots, captured.root, captured.binding


def test_project_artifact_root_registration_is_job_scoped_and_supports_custom_root(
    tmp_path: Path,
) -> None:
    (
        artifact_roots,
        project_root,
        binding,
    ) = _capture_bound_root(tmp_path, custom=True)
    index_a, digest_a, frames_a = _write_dense_index(
        project_root,
        pipeline_job_id=PIPELINE_JOB_A,
        project_artifact_layout=True,
    )
    index_b, digest_b, frames_b = _write_dense_index(
        project_root,
        pipeline_job_id=PIPELINE_JOB_B,
        project_artifact_layout=True,
    )
    assert digest_a == digest_b
    registry = DenseCandidateFrameRegistry(
        (tmp_path / "bridge-data").resolve(),
        artifact_root_registry=artifact_roots,
    )
    registry.register(
        PROJECT_A,
        SOURCE_SHA256,
        digest_a,
        index_a,
        PIPELINE_JOB_A,
        binding,
    )

    with pytest.raises(
        DenseCandidateFrameRegistryError,
        match="not registered",
    ):
        registry.resolve(
            PROJECT_A,
            PIPELINE_JOB_B,
            digest_b,
            str(frames_b[0]["frame_id"]),
        )

    registry.register(
        PROJECT_A,
        SOURCE_SHA256,
        digest_b,
        index_b,
        PIPELINE_JOB_B,
        binding,
    )
    resolved_a = registry.resolve(
        PROJECT_A,
        PIPELINE_JOB_A,
        digest_a,
        str(frames_a[0]["frame_id"]),
    )
    resolved_b = registry.resolve(
        PROJECT_A,
        PIPELINE_JOB_B,
        digest_b,
        str(frames_b[0]["frame_id"]),
    )
    assert resolved_a.path.is_relative_to(
        project_root
        / "pipeline-jobs"
        / str(PIPELINE_JOB_A)
    )
    assert resolved_b.path.is_relative_to(
        project_root
        / "pipeline-jobs"
        / str(PIPELINE_JOB_B)
    )

    frame_b = index_b.parent / str(frames_b[0]["relative_path"])
    frame_b.write_bytes(frame_b.read_bytes() + b"tampered")
    assert (
        registry.resolve(
            PROJECT_A,
            PIPELINE_JOB_A,
            digest_a,
            str(frames_a[0]["frame_id"]),
        ).sha256
        == frames_a[0]["sha256"]
    )
    with pytest.raises(
        DenseCandidateFrameRegistryError,
        match="dense_frame_",
    ):
        registry.resolve(
            PROJECT_A,
            PIPELINE_JOB_B,
            digest_b,
            str(frames_b[0]["frame_id"]),
        )


def test_project_artifact_registration_rejects_wrong_scope_root_and_binding(
    tmp_path: Path,
) -> None:
    (
        artifact_roots,
        project_root,
        binding,
    ) = _capture_bound_root(tmp_path)
    index_path, digest, _frames = _write_dense_index(
        project_root,
        pipeline_job_id=PIPELINE_JOB_A,
        project_artifact_layout=True,
    )
    outside_index, outside_digest, _outside_frames = (
        _write_dense_index(
            (tmp_path / "outside-artifact-root").resolve(),
            pipeline_job_id=PIPELINE_JOB_A,
            project_artifact_layout=True,
        )
    )
    registry = DenseCandidateFrameRegistry(
        (tmp_path / "bridge-data").resolve(),
        artifact_root_registry=artifact_roots,
    )

    with pytest.raises(
        DenseCandidateFrameRegistryError,
        match="artifact-root binding is invalid",
    ):
        registry.register(
            PROJECT_B,
            SOURCE_SHA256,
            digest,
            index_path,
            PIPELINE_JOB_A,
            binding,
        )
    with pytest.raises(
        DenseCandidateFrameRegistryError,
        match="path is invalid",
    ):
        registry.register(
            PROJECT_A,
            SOURCE_SHA256,
            digest,
            index_path,
            PIPELINE_JOB_B,
            binding,
        )
    with pytest.raises(
        DenseCandidateFrameRegistryError,
        match="outside the project artifact root",
    ):
        registry.register(
            PROJECT_A,
            SOURCE_SHA256,
            outside_digest,
            outside_index,
            PIPELINE_JOB_A,
            binding,
        )
    changed_binding = dict(binding)
    changed_binding["marker_sha256"] = "f" * 64
    with pytest.raises(
        DenseCandidateFrameRegistryError,
        match="artifact-root binding is invalid",
    ):
        registry.register(
            PROJECT_A,
            SOURCE_SHA256,
            digest,
            index_path,
            PIPELINE_JOB_A,
            changed_binding,
        )


def test_project_artifact_binding_is_revalidated_after_registration(
    tmp_path: Path,
) -> None:
    (
        artifact_roots,
        project_root,
        binding,
    ) = _capture_bound_root(tmp_path)
    index_path, digest, frames = _write_dense_index(
        project_root,
        pipeline_job_id=PIPELINE_JOB_A,
        project_artifact_layout=True,
    )
    registry = DenseCandidateFrameRegistry(
        (tmp_path / "bridge-data").resolve(),
        artifact_root_registry=artifact_roots,
    )
    registry.register(
        PROJECT_A,
        SOURCE_SHA256,
        digest,
        index_path,
        PIPELINE_JOB_A,
        binding,
    )
    marker = project_root / ".trafficsupervision-root.json"
    marker.write_bytes(marker.read_bytes() + b"tampered")

    with pytest.raises(
        DenseCandidateFrameRegistryError,
        match="artifact-root binding changed",
    ):
        registry.resolve(
            PROJECT_A,
            PIPELINE_JOB_A,
            digest,
            str(frames[0]["frame_id"]),
        )


def test_project_artifact_registration_rejects_symlinked_run(
    tmp_path: Path,
) -> None:
    (
        artifact_roots,
        project_root,
        binding,
    ) = _capture_bound_root(tmp_path)
    real_index, digest, _frames = _write_dense_index(
        project_root,
        pipeline_job_id=PIPELINE_JOB_A,
        project_artifact_layout=True,
    )
    linked_run = real_index.parent.with_name(
        "dense-frame-index-linked-run"
    )
    try:
        os.symlink(
            real_index.parent,
            linked_run,
            target_is_directory=True,
        )
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable")
    linked_index = linked_run / "frame-index.json"
    registry = DenseCandidateFrameRegistry(
        (tmp_path / "bridge-data").resolve(),
        artifact_root_registry=artifact_roots,
    )
    with pytest.raises(
        DenseCandidateFrameRegistryError,
        match="path is invalid",
    ):
        registry.register(
            PROJECT_A,
            SOURCE_SHA256,
            digest,
            linked_index,
            PIPELINE_JOB_A,
            binding,
        )


def test_project_artifact_registration_rejects_reparse_chain(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (
        artifact_roots,
        project_root,
        binding,
    ) = _capture_bound_root(tmp_path)
    index_path, digest, _frames = _write_dense_index(
        project_root,
        pipeline_job_id=PIPELINE_JOB_A,
        project_artifact_layout=True,
    )
    reparse_directory = index_path.parent
    from app import dense_candidate_frame_registry as registry_module

    monkeypatch.setattr(
        registry_module,
        "_is_reparse_point",
        lambda path: Path(path) == reparse_directory,
    )
    registry = DenseCandidateFrameRegistry(
        (tmp_path / "bridge-data").resolve(),
        artifact_root_registry=artifact_roots,
    )
    with pytest.raises(
        DenseCandidateFrameRegistryError,
        match="path is invalid",
    ):
        registry.register(
            PROJECT_A,
            SOURCE_SHA256,
            digest,
            index_path,
            PIPELINE_JOB_A,
            binding,
        )
