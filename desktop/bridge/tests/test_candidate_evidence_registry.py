from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.analysis_jobs import (
    ResolvedAnalysisArtifact,
    ResolvedAnalysisCandidateImage,
    ResolvedAnalysisCandidateImageBatch,
)
from app.candidate_evidence_overlay import (
    CANDIDATE_EVIDENCE_OVERLAY_SCHEMA,
    CandidateEvidenceArtifact,
    CandidateEvidenceOverlayCandidate,
    CandidateEvidenceOverlayIntegrityError,
    CandidateEvidenceOverlayNotFound,
    CandidateEvidenceOverlayResult,
    ResolvedCandidateEvidenceArtifact,
)
from app.candidate_evidence_registry import (
    CandidateEvidenceRegistry,
    CandidateEvidenceRegistryLimits,
)
from app.composite_candidate_evidence import (
    CandidateEvidenceConflict,
    CandidateEvidenceIntegrityError,
)


SOURCE_SHA256 = "1" * 64
BASE_CANDIDATE_ID = "2" * 20
OVERLAY_CANDIDATE_ID = "3" * 20
SECOND_OVERLAY_CANDIDATE_ID = "4" * 20


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_media(root: Path, candidate_id: str):
    root.mkdir(parents=True, exist_ok=True)
    image = root / f"{candidate_id}.jpg"
    clip = root / f"{candidate_id}.mp4"
    image.write_bytes(b"\xff\xd8\xffregistry-jpeg-" + candidate_id.encode())
    clip.write_bytes(
        b"\x00\x00\x00\x18ftypisomregistry-mp4-"
        + candidate_id.encode()
    )
    return image, clip


def _artifact(path: Path, media_type: str) -> ResolvedAnalysisArtifact:
    return ResolvedAnalysisArtifact(
        path=path.resolve(),
        media_type=media_type,
        size_bytes=path.stat().st_size,
        sha256=_sha256(path),
    )


def _base_metadata(
    candidate_id: str,
    image: Path,
    clip: Path,
) -> dict:
    return {
        "candidate_id": candidate_id,
        "issue_type": "pothole",
        "start_seconds": 1.0,
        "end_seconds": 2.0,
        "representative_timestamp_seconds": 1.5,
        "score": 0.8,
        "representative_image": {
            "artifact_id": f"ai_{candidate_id}",
            "relative_path": f"{candidate_id}.jpg",
            "size_bytes": image.stat().st_size,
            "sha256": _sha256(image),
            "media_type": "image/jpeg",
            "artifact_ref": f"analysis:job:ai_{candidate_id}",
        },
        "preview_clip": {
            "artifact_id": f"ac_{candidate_id}",
            "relative_path": f"{candidate_id}.mp4",
            "size_bytes": clip.stat().st_size,
            "sha256": _sha256(clip),
            "media_type": "video/mp4",
            "artifact_ref": f"analysis:job:ac_{candidate_id}",
        },
    }


class FakeBase:
    def __init__(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        *,
        candidates=(),
        artifacts=None,
    ):
        self.project_id = project_id
        self.analysis_job_id = analysis_job_id
        self.row = {
            "status": "completed",
            "project_id": str(project_id),
            "job_id": str(analysis_job_id),
            "source_sha256": SOURCE_SHA256,
            "result": {
                "provenance": {"source_sha256": SOURCE_SHA256},
                "candidates": deepcopy(list(candidates)),
            },
        }
        self.artifacts = dict(artifacts or {})
        self.errors = {}

    def get(self, project_id, job_id):
        if project_id != self.project_id or job_id != self.analysis_job_id:
            raise HTTPException(status_code=404, detail="analysis not found")
        return deepcopy(self.row)

    def resolve_artifact(
        self, project_id, job_id, candidate_id, kind
    ):
        error = self.errors.get((candidate_id, kind))
        if error is not None:
            raise error
        artifact = self.artifacts.get((candidate_id, kind))
        if artifact is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        return artifact

    def resolve_candidate_image_batch(
        self, project_id, job_id, candidate_ids
    ):
        images = []
        run_directory = None
        for candidate_id in candidate_ids:
            artifact = self.resolve_artifact(
                project_id, job_id, candidate_id, "image"
            )
            run_directory = run_directory or artifact.path.parent
            images.append(
                ResolvedAnalysisCandidateImage(
                    candidate_id=candidate_id,
                    path=artifact.path,
                    media_type=artifact.media_type,
                    size_bytes=artifact.size_bytes,
                    sha256=artifact.sha256,
                )
            )
        return ResolvedAnalysisCandidateImageBatch(
            run_directory=run_directory,
            images=tuple(images),
        )


class FakeOverlayStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.results = {}
        self.artifacts = {}
        self.manifest_bodies = {}
        self.get_calls = []

    @staticmethod
    def _key(
        project_id,
        analysis_job_id,
        pipeline_job_id,
        overlay_id,
    ):
        return (
            project_id,
            analysis_job_id,
            pipeline_job_id,
            overlay_id,
        )

    def add(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        overlay_id: str,
        candidate_ids,
    ):
        leaf = (
            self.root
            / str(project_id)
            / str(analysis_job_id)
            / str(pipeline_job_id)
            / overlay_id
        )
        artifacts_root = leaf / "artifacts"
        artifacts_root.mkdir(parents=True)
        manifest_body = json.dumps(
            {
                "project_id": str(project_id),
                "analysis_job_id": str(analysis_job_id),
                "pipeline_job_id": str(pipeline_job_id),
                "overlay_id": overlay_id,
            },
            sort_keys=True,
        ).encode()
        (leaf / "manifest.json").write_bytes(manifest_body)
        candidates = []
        for index, candidate_id in enumerate(candidate_ids):
            image_path, clip_path = _write_media(
                artifacts_root, candidate_id
            )
            image = CandidateEvidenceArtifact(
                artifact_id=f"cea_{(str(index + 1) * 32)[:32]}",
                media_type="image/jpeg",
                size_bytes=image_path.stat().st_size,
                sha256=_sha256(image_path),
            )
            clip = CandidateEvidenceArtifact(
                artifact_id=f"cea_{(str(index + 6) * 32)[:32]}",
                media_type="video/mp4",
                size_bytes=clip_path.stat().st_size,
                sha256=_sha256(clip_path),
            )
            candidates.append(
                CandidateEvidenceOverlayCandidate(
                    candidate_id=candidate_id,
                    issue_type="road_debris",
                    start_seconds=float(index + 1),
                    end_seconds=float(index + 2),
                    representative_timestamp_seconds=float(index + 1.5),
                    representative_frame_id=(
                        f"{index + 5:x}" * 24
                    )[:24],
                    representative_frame_sha256=(
                        f"{index + 7:x}" * 64
                    )[:64],
                    score=0.84,
                    normalized_box={
                        "x": 0.1,
                        "y": 0.2,
                        "width": 0.3,
                        "height": 0.4,
                    },
                    ocr_text=None,
                    urgent_review=True,
                    supporting_frame_ids=(
                        (f"{index + 5:x}" * 24)[:24],
                    ),
                    proposal_sources=("dense_detector_review_only",),
                    representative_proposal_provenance={
                        "model_manifest_sha256": "8" * 64
                    },
                    proposal_provenance_sha256="9" * 64,
                    image=image,
                    clip=clip,
                )
            )
            self.artifacts[
                (
                    project_id,
                    analysis_job_id,
                    pipeline_job_id,
                    overlay_id,
                    candidate_id,
                    "image",
                )
            ] = _artifact(image_path, "image/jpeg")
            self.artifacts[
                (
                    project_id,
                    analysis_job_id,
                    pipeline_job_id,
                    overlay_id,
                    candidate_id,
                    "clip",
                )
            ] = _artifact(clip_path, "video/mp4")
        key = self._key(
            project_id,
            analysis_job_id,
            pipeline_job_id,
            overlay_id,
        )
        result = CandidateEvidenceOverlayResult(
            schema_version=CANDIDATE_EVIDENCE_OVERLAY_SCHEMA,
            overlay_id=overlay_id,
            project_id=str(project_id),
            analysis_job_id=str(analysis_job_id),
            pipeline_job_id=str(pipeline_job_id),
            source_sha256=SOURCE_SHA256,
            discovery_revision="a" * 64,
            dense_index_sha256="b" * 64,
            provider_provenance_sha256="c" * 64,
            model_provenance_sha256="d" * 64,
            candidate_metadata_sha256="e" * 64,
            candidates=tuple(candidates),
            manifest_sha256=hashlib.sha256(manifest_body).hexdigest(),
            created_at="2026-07-29T00:00:00+00:00",
            idempotent_replay=True,
        )
        self.results[key] = result
        self.manifest_bodies[key] = manifest_body
        return result

    def get(
        self,
        project_id,
        analysis_job_id,
        pipeline_job_id,
        overlay_id,
    ):
        key = self._key(
            project_id,
            analysis_job_id,
            pipeline_job_id,
            overlay_id,
        )
        self.get_calls.append(key)
        result = self.results.get(key)
        if result is None:
            raise CandidateEvidenceOverlayNotFound("overlay not found")
        manifest = (
            self.root
            / str(project_id)
            / str(analysis_job_id)
            / str(pipeline_job_id)
            / overlay_id
            / "manifest.json"
        )
        if (
            not manifest.is_file()
            or manifest.read_bytes() != self.manifest_bodies[key]
        ):
            raise CandidateEvidenceOverlayIntegrityError(
                "manifest changed"
            )
        return result

    def resolve_artifact(
        self,
        project_id,
        analysis_job_id,
        pipeline_job_id,
        overlay_id,
        candidate_id,
        kind,
    ):
        key = (
            project_id,
            analysis_job_id,
            pipeline_job_id,
            overlay_id,
            candidate_id,
            kind,
        )
        artifact = self.artifacts.get(key)
        if artifact is None:
            raise CandidateEvidenceOverlayNotFound("artifact not found")
        return ResolvedCandidateEvidenceArtifact(
            overlay_id=overlay_id,
            candidate_id=candidate_id,
            kind=kind,
            path=artifact.path,
            media_type=artifact.media_type,
            size_bytes=artifact.size_bytes,
            sha256=artifact.sha256,
        )


@pytest.fixture()
def registry_setup(tmp_path):
    project_id = uuid4()
    analysis_job_id = uuid4()
    pipeline_job_id = uuid4()
    overlay_id = f"ceo_{'5' * 32}"
    root = (tmp_path / "overlays").resolve()
    root.mkdir()
    store = FakeOverlayStore(root)
    store.add(
        project_id,
        analysis_job_id,
        pipeline_job_id,
        overlay_id,
        [OVERLAY_CANDIDATE_ID],
    )
    base = FakeBase(project_id, analysis_job_id)
    return {
        "project_id": project_id,
        "analysis_job_id": analysis_job_id,
        "pipeline_job_id": pipeline_job_id,
        "overlay_id": overlay_id,
        "root": root,
        "store": store,
        "base": base,
    }


def test_startup_scan_reconstructs_path_free_binding_after_restart(
    registry_setup,
):
    item = registry_setup
    first = CandidateEvidenceRegistry(
        item["base"],
        item["store"],
        overlay_root=item["root"],
    )
    first_rows = first.list_bindings()
    assert len(first_rows) == 1
    assert first_rows[0]["candidate_count"] == 1
    assert str(item["root"]) not in json.dumps(first_rows)

    resolved = first.resolve_candidate(
        item["project_id"],
        item["analysis_job_id"],
        OVERLAY_CANDIDATE_ID,
    )
    assert resolved["candidate_source"] == "overlay"
    assert resolved["metadata"]["evidence_overlay"]["overlay_id"] == (
        item["overlay_id"]
    )
    assert str(item["root"]) not in json.dumps(resolved)

    second = CandidateEvidenceRegistry(
        item["base"],
        item["store"],
        overlay_root=item["root"],
    )
    assert second.list_bindings() == first_rows
    # One startup snapshot per registry plus the explicit single-candidate
    # resolution.  The catalog no longer reloads the full overlay per row.
    assert len(item["store"].get_calls) == 3


def test_bind_overlay_is_dynamic_idempotent_and_store_verified(
    registry_setup,
):
    item = registry_setup
    empty_root = (item["root"].parent / "dynamic").resolve()
    empty_root.mkdir()
    dynamic_store = FakeOverlayStore(empty_root)
    registry = CandidateEvidenceRegistry(
        item["base"],
        dynamic_store,
        overlay_root=empty_root,
    )
    dynamic_store.add(
        item["project_id"],
        item["analysis_job_id"],
        item["pipeline_job_id"],
        item["overlay_id"],
        [OVERLAY_CANDIDATE_ID],
    )

    first = registry.bind_overlay(
        item["project_id"],
        item["analysis_job_id"],
        item["pipeline_job_id"],
        item["overlay_id"],
    )
    second = registry.bind_overlay(
        item["project_id"],
        item["analysis_job_id"],
        item["pipeline_job_id"],
        item["overlay_id"],
    )

    assert first == second
    assert len(registry.list_bindings()) == 1
    assert len(dynamic_store.get_calls) == 2


def test_pipeline_snapshot_verifies_56_candidates_with_one_store_load(
    tmp_path,
):
    project_id = uuid4()
    analysis_job_id = uuid4()
    pipeline_job_id = uuid4()
    overlay_id = f"ceo_{'8' * 32}"
    root = (tmp_path / "batch-overlays").resolve()
    root.mkdir()
    store = FakeOverlayStore(root)
    candidate_ids = [
        f"{index + 1:020x}" for index in range(56)
    ]
    store.add(
        project_id,
        analysis_job_id,
        pipeline_job_id,
        overlay_id,
        candidate_ids,
    )
    registry = CandidateEvidenceRegistry(
        FakeBase(project_id, analysis_job_id),
        store,
        overlay_root=root,
    )
    store.get_calls.clear()

    snapshot = registry.resolve_pipeline_snapshot(
        project_id,
        analysis_job_id,
        pipeline_job_id,
        overlay_id,
        SOURCE_SHA256,
    )

    assert [
        row["candidate_id"] for row in snapshot.candidates
    ] == candidate_ids
    assert len(snapshot.bindings) == 1
    assert len(store.get_calls) == 1


def test_pipeline_snapshot_fails_closed_on_bound_address_tamper(
    registry_setup,
):
    item = registry_setup
    registry = CandidateEvidenceRegistry(
        item["base"],
        item["store"],
        overlay_root=item["root"],
    )
    leaf = (
        item["root"]
        / str(item["project_id"])
        / str(item["analysis_job_id"])
        / str(item["pipeline_job_id"])
        / item["overlay_id"]
    )
    (leaf / "unexpected.txt").write_text("tamper", encoding="utf-8")
    item["store"].get_calls.clear()

    with pytest.raises(CandidateEvidenceIntegrityError):
        registry.resolve_pipeline_snapshot(
            item["project_id"],
            item["analysis_job_id"],
            item["pipeline_job_id"],
            item["overlay_id"],
            SOURCE_SHA256,
        )
    assert item["store"].get_calls == []


def test_manifest_content_tamper_after_binding_fails_on_resolution(
    registry_setup,
):
    item = registry_setup
    registry = CandidateEvidenceRegistry(
        item["base"],
        item["store"],
        overlay_root=item["root"],
    )
    manifest = (
        item["root"]
        / str(item["project_id"])
        / str(item["analysis_job_id"])
        / str(item["pipeline_job_id"])
        / item["overlay_id"]
        / "manifest.json"
    )
    manifest.write_text('{"tampered":true}', encoding="utf-8")
    item["store"].get_calls.clear()

    with pytest.raises(CandidateEvidenceIntegrityError):
        registry.resolve_pipeline_snapshot(
            item["project_id"],
            item["analysis_job_id"],
            item["pipeline_job_id"],
            item["overlay_id"],
            SOURCE_SHA256,
        )
    assert len(item["store"].get_calls) == 1


def test_hardlinked_manifest_is_rejected_before_store_lookup(
    registry_setup,
):
    item = registry_setup
    manifest = (
        item["root"]
        / str(item["project_id"])
        / str(item["analysis_job_id"])
        / str(item["pipeline_job_id"])
        / item["overlay_id"]
        / "manifest.json"
    )
    backup = manifest.parent / "manifest-hardlink.json"
    manifest.replace(backup)
    os.link(backup, manifest)

    with pytest.raises(CandidateEvidenceIntegrityError):
        CandidateEvidenceRegistry(
            item["base"],
            item["store"],
            overlay_root=item["root"],
        )
    assert item["store"].get_calls == []


def test_candidate_collision_across_persisted_overlays_fails_closed(
    registry_setup,
):
    item = registry_setup
    second_pipeline_id = uuid4()
    second_overlay_id = f"ceo_{'6' * 32}"
    item["store"].add(
        item["project_id"],
        item["analysis_job_id"],
        second_pipeline_id,
        second_overlay_id,
        [OVERLAY_CANDIDATE_ID],
    )

    with pytest.raises(CandidateEvidenceConflict):
        CandidateEvidenceRegistry(
            item["base"],
            item["store"],
            overlay_root=item["root"],
        )


def test_base_overlay_candidate_collision_fails_closed(
    registry_setup, tmp_path
):
    item = registry_setup
    image, clip = _write_media(tmp_path / "base", OVERLAY_CANDIDATE_ID)
    base = FakeBase(
        item["project_id"],
        item["analysis_job_id"],
        candidates=[
            _base_metadata(OVERLAY_CANDIDATE_ID, image, clip)
        ],
        artifacts={
            (OVERLAY_CANDIDATE_ID, "image"): _artifact(
                image, "image/jpeg"
            ),
            (OVERLAY_CANDIDATE_ID, "clip"): _artifact(
                clip, "video/mp4"
            ),
        },
    )

    with pytest.raises(CandidateEvidenceConflict):
        CandidateEvidenceRegistry(
            base,
            item["store"],
            overlay_root=item["root"],
        )


def test_unknown_candidate_and_analysis_remain_404(registry_setup):
    item = registry_setup
    registry = CandidateEvidenceRegistry(
        item["base"],
        item["store"],
        overlay_root=item["root"],
    )

    with pytest.raises(HTTPException) as candidate_failure:
        registry.resolve_candidate(
            item["project_id"],
            item["analysis_job_id"],
            "f" * 20,
        )
    assert candidate_failure.value.status_code == 404

    with pytest.raises(HTTPException) as analysis_failure:
        registry.list_candidates(item["project_id"], uuid4())
    assert analysis_failure.value.status_code == 404


def test_non_404_base_artifact_failure_is_preserved(registry_setup):
    item = registry_setup
    item["base"].errors[
        (OVERLAY_CANDIDATE_ID, "image")
    ] = HTTPException(status_code=410, detail="base integrity failure")
    registry = CandidateEvidenceRegistry(
        item["base"],
        item["store"],
        overlay_root=item["root"],
    )

    with pytest.raises(HTTPException) as failure:
        registry.resolve_artifact(
            item["project_id"],
            item["analysis_job_id"],
            OVERLAY_CANDIDATE_ID,
            "image",
        )
    assert failure.value.status_code == 410
    assert failure.value.detail == "base integrity failure"


def test_overlay_only_and_mixed_candidate_image_batches(
    registry_setup, tmp_path
):
    item = registry_setup
    base_image, base_clip = _write_media(
        tmp_path / "base", BASE_CANDIDATE_ID
    )
    base = FakeBase(
        item["project_id"],
        item["analysis_job_id"],
        candidates=[
            _base_metadata(
                BASE_CANDIDATE_ID, base_image, base_clip
            )
        ],
        artifacts={
            (BASE_CANDIDATE_ID, "image"): _artifact(
                base_image, "image/jpeg"
            ),
            (BASE_CANDIDATE_ID, "clip"): _artifact(
                base_clip, "video/mp4"
            ),
        },
    )
    batch_root = (tmp_path / "batches").resolve()
    registry = CandidateEvidenceRegistry(
        base,
        item["store"],
        overlay_root=item["root"],
        batch_root=batch_root,
    )

    overlay_batch = registry.resolve_candidate_image_batch(
        item["project_id"],
        item["analysis_job_id"],
        [OVERLAY_CANDIDATE_ID],
    )
    assert overlay_batch.images[0].candidate_id == OVERLAY_CANDIDATE_ID

    mixed = registry.resolve_candidate_image_batch(
        item["project_id"],
        item["analysis_job_id"],
        [BASE_CANDIDATE_ID, OVERLAY_CANDIDATE_ID],
    )
    assert [value.candidate_id for value in mixed.images] == [
        BASE_CANDIDATE_ID,
        OVERLAY_CANDIDATE_ID,
    ]
    assert mixed.run_directory.is_relative_to(batch_root)
    assert all(
        value.path.parent == mixed.run_directory
        for value in mixed.images
    )

    external_batch_root = (tmp_path / "job-inference-batches").resolve()
    external_batch_root.mkdir()
    external = registry.resolve_candidate_image_batch(
        item["project_id"],
        item["analysis_job_id"],
        [BASE_CANDIDATE_ID, OVERLAY_CANDIDATE_ID],
        pipeline_job_id=item["pipeline_job_id"],
        artifact_root=external_batch_root,
    )
    assert external.run_directory.is_relative_to(
        external_batch_root
    )
    assert all(
        value.path.parent == external.run_directory
        for value in external.images
    )
    binding = (
        external_batch_root
        / "candidate-inference-batch-root-binding.json"
    )
    binding_text = binding.read_text(encoding="utf-8")
    assert str(external_batch_root) not in binding_text

    with pytest.raises(CandidateEvidenceIntegrityError):
        registry.resolve_candidate_image_batch(
            item["project_id"],
            item["analysis_job_id"],
            [BASE_CANDIDATE_ID, OVERLAY_CANDIDATE_ID],
            pipeline_job_id=uuid4(),
            artifact_root=external_batch_root,
        )


def test_refresh_binds_new_persisted_overlay_without_restart(
    registry_setup,
):
    item = registry_setup
    registry = CandidateEvidenceRegistry(
        item["base"],
        item["store"],
        overlay_root=item["root"],
    )
    pipeline_id = uuid4()
    overlay_id = f"ceo_{'7' * 32}"
    item["store"].add(
        item["project_id"],
        item["analysis_job_id"],
        pipeline_id,
        overlay_id,
        [SECOND_OVERLAY_CANDIDATE_ID],
    )

    assert registry.refresh_from_disk() == 2
    rows = registry.list_candidates(
        item["project_id"], item["analysis_job_id"]
    )
    assert {row["candidate_id"] for row in rows} == {
        OVERLAY_CANDIDATE_ID,
        SECOND_OVERLAY_CANDIDATE_ID,
    }


def test_scan_count_bounds_and_unexpected_entries_fail_closed(tmp_path):
    project_id = uuid4()
    analysis_job_id = uuid4()
    root = (tmp_path / "overlays").resolve()
    root.mkdir()
    store = FakeOverlayStore(root)
    base = FakeBase(project_id, analysis_job_id)
    (root / str(project_id)).mkdir()
    (root / str(uuid4())).mkdir()

    with pytest.raises(CandidateEvidenceIntegrityError):
        CandidateEvidenceRegistry(
            base,
            store,
            overlay_root=root,
            limits=CandidateEvidenceRegistryLimits(max_projects=1),
        )

    unexpected_root = (tmp_path / "unexpected").resolve()
    unexpected_root.mkdir()
    (unexpected_root / "not-a-project").mkdir()
    with pytest.raises(CandidateEvidenceIntegrityError):
        CandidateEvidenceRegistry(
            base,
            FakeOverlayStore(unexpected_root),
            overlay_root=unexpected_root,
        )
