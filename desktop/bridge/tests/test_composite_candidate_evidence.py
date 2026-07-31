import hashlib
import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.analysis_jobs import (
    ResolvedAnalysisArtifact,
    ResolvedAnalysisCandidateImage,
    ResolvedAnalysisCandidateImageBatch,
)
from app.composite_candidate_evidence import (
    CandidateEvidenceOverlayAdapter,
    CandidateEvidenceConflict,
    CandidateEvidenceIntegrityError,
    CompositeCandidateEvidence,
    OverlayCandidateNotFound,
)
from app.candidate_evidence_overlay import (
    CANDIDATE_EVIDENCE_OVERLAY_SCHEMA,
    CandidateEvidenceArtifact,
    CandidateEvidenceOverlayCandidate,
    CandidateEvidenceOverlayResult,
    ResolvedCandidateEvidenceArtifact,
)


SOURCE_SHA256 = "a" * 64
BASE_CANDIDATE_ID = "1" * 20
OVERLAY_CANDIDATE_ID = "2" * 20
OVERLAY_ID = f"ceo_{'3' * 32}"


def _canonical_sha256(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _write_artifacts(root: Path, prefix: str):
    root.mkdir(parents=True, exist_ok=True)
    image_path = root / f"{prefix}.jpg"
    clip_path = root / f"{prefix}.mp4"
    image_path.write_bytes(b"\xff\xd8\xff" + prefix.encode("ascii") * 16)
    clip_path.write_bytes(
        b"\x00\x00\x00\x18ftypmp42" + prefix.encode("ascii") * 16
    )
    image = ResolvedAnalysisArtifact(
        path=image_path.resolve(),
        media_type="image/jpeg",
        size_bytes=image_path.stat().st_size,
        sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(),
    )
    clip = ResolvedAnalysisArtifact(
        path=clip_path.resolve(),
        media_type="video/mp4",
        size_bytes=clip_path.stat().st_size,
        sha256=hashlib.sha256(clip_path.read_bytes()).hexdigest(),
    )
    return image, clip


def _candidate(
    candidate_id: str,
    image: ResolvedAnalysisArtifact,
    clip: ResolvedAnalysisArtifact,
    *,
    artifact_prefix: str,
):
    return {
        "candidate_id": candidate_id,
        "start_seconds": 1.0,
        "representative_timestamp_seconds": 1.5,
        "end_seconds": 2.0,
        "representative_image": {
            "relative_path": image.path.name,
            "size_bytes": image.size_bytes,
            "sha256": image.sha256,
            "media_type": image.media_type,
            "artifact_ref": f"{artifact_prefix}:image",
        },
        "preview_clip": {
            "relative_path": clip.path.name,
            "size_bytes": clip.size_bytes,
            "sha256": clip.sha256,
            "media_type": clip.media_type,
            "artifact_ref": f"{artifact_prefix}:clip",
        },
    }


class FakeBaseBackend:
    def __init__(
        self,
        *,
        project_id,
        analysis_job_id,
        candidates,
        artifacts,
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
                "candidates": deepcopy(candidates),
            },
        }
        self.artifacts = dict(artifacts)
        self.artifact_errors = {}
        self.artifact_calls = []
        self.batch_calls = []

    def get(self, project_id, job_id):
        if project_id != self.project_id or job_id != self.analysis_job_id:
            raise HTTPException(status_code=404, detail="job not found")
        return deepcopy(self.row)

    def resolve_artifact(
        self, project_id, job_id, candidate_id, kind
    ):
        self.artifact_calls.append((candidate_id, kind))
        error = self.artifact_errors.get((candidate_id, kind))
        if error is not None:
            raise error
        artifact = self.artifacts.get((candidate_id, kind))
        if artifact is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        return artifact

    def resolve_candidate_image_batch(
        self, project_id, job_id, candidate_ids
    ):
        self.batch_calls.append(tuple(candidate_ids))
        images = []
        run_directory = None
        for candidate_id in candidate_ids:
            artifact = self.resolve_artifact(
                project_id, job_id, candidate_id, "image"
            )
            run_directory = run_directory or artifact.path.parent
            if artifact.path.parent != run_directory:
                raise HTTPException(status_code=410, detail="mixed base roots")
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


class FakeOverlayBackend:
    def __init__(
        self,
        *,
        project_id,
        analysis_job_id,
        candidates,
        artifacts,
    ):
        self.project_id = project_id
        self.analysis_job_id = analysis_job_id
        self.records = {}
        for candidate in candidates:
            self.records[candidate["candidate_id"]] = {
                "status": "completed",
                "project_id": str(project_id),
                "analysis_job_id": str(analysis_job_id),
                "source_sha256": SOURCE_SHA256,
                "candidate_id": candidate["candidate_id"],
                "candidate_metadata_sha256": _canonical_sha256(candidate),
                "metadata": deepcopy(candidate),
            }
        self.artifacts = dict(artifacts)
        self.artifact_calls = []
        self.batch_calls = []

    def resolve_candidate(
        self, project_id, analysis_job_id, candidate_id
    ):
        record = self.records.get(candidate_id)
        if record is None:
            raise OverlayCandidateNotFound(candidate_id)
        return deepcopy(record)

    def list_candidates(self, project_id, analysis_job_id):
        return [deepcopy(self.records[key]) for key in sorted(self.records)]

    def resolve_artifact(
        self, project_id, analysis_job_id, candidate_id, kind
    ):
        self.artifact_calls.append((candidate_id, kind))
        artifact = self.artifacts.get((candidate_id, kind))
        if artifact is None:
            raise OverlayCandidateNotFound(candidate_id)
        return artifact

    def resolve_candidate_image_batch(
        self, project_id, analysis_job_id, candidate_ids
    ):
        self.batch_calls.append(tuple(candidate_ids))
        images = []
        run_directory = None
        for candidate_id in candidate_ids:
            artifact = self.resolve_artifact(
                project_id, analysis_job_id, candidate_id, "image"
            )
            run_directory = run_directory or artifact.path.parent
            if artifact.path.parent != run_directory:
                raise RuntimeError("mixed overlay roots")
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


class FakeCandidateEvidenceOverlayStore:
    def __init__(self, result, artifacts):
        self.result = result
        self.artifacts = artifacts

    def get(
        self,
        project_id,
        analysis_job_id,
        pipeline_job_id,
        overlay_id,
    ):
        if (
            str(project_id) != self.result.project_id
            or str(analysis_job_id) != self.result.analysis_job_id
            or str(pipeline_job_id) != self.result.pipeline_job_id
            or overlay_id != self.result.overlay_id
        ):
            raise AssertionError("overlay identity crossed adapter binding")
        return self.result

    def resolve_artifact(
        self,
        project_id,
        analysis_job_id,
        pipeline_job_id,
        overlay_id,
        candidate_id,
        kind,
    ):
        artifact = self.artifacts[(candidate_id, kind)]
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
def evidence_fixture(tmp_path):
    project_id = uuid4()
    analysis_job_id = uuid4()
    base_image, base_clip = _write_artifacts(tmp_path / "base", "base")
    overlay_image, overlay_clip = _write_artifacts(
        tmp_path / "overlay", "overlay"
    )
    base_candidate = _candidate(
        BASE_CANDIDATE_ID,
        base_image,
        base_clip,
        artifact_prefix=f"analysis:{analysis_job_id}:base",
    )
    overlay_candidate = _candidate(
        OVERLAY_CANDIDATE_ID,
        overlay_image,
        overlay_clip,
        artifact_prefix="candidate-evidence:set-1:overlay",
    )
    base = FakeBaseBackend(
        project_id=project_id,
        analysis_job_id=analysis_job_id,
        candidates=[base_candidate],
        artifacts={
            (BASE_CANDIDATE_ID, "image"): base_image,
            (BASE_CANDIDATE_ID, "clip"): base_clip,
        },
    )
    overlay = FakeOverlayBackend(
        project_id=project_id,
        analysis_job_id=analysis_job_id,
        candidates=[overlay_candidate],
        artifacts={
            (OVERLAY_CANDIDATE_ID, "image"): overlay_image,
            (OVERLAY_CANDIDATE_ID, "clip"): overlay_clip,
        },
    )
    return {
        "project_id": project_id,
        "analysis_job_id": analysis_job_id,
        "base": base,
        "overlay": overlay,
        "base_candidate": base_candidate,
        "overlay_candidate": overlay_candidate,
        "base_image": base_image,
        "overlay_image": overlay_image,
    }


def test_base_candidate_artifact_and_batch_behavior_is_preserved(
    evidence_fixture,
):
    item = evidence_fixture
    composite = CompositeCandidateEvidence(item["base"])

    resolved = composite.resolve_candidate(
        item["project_id"],
        item["analysis_job_id"],
        BASE_CANDIDATE_ID,
    )
    assert resolved["candidate_source"] == "analysis_job"
    assert resolved["metadata"] == item["base_candidate"]
    assert Path not in {
        type(value)
        for value in resolved.values()
    }
    json.dumps(resolved)

    artifact = composite.resolve_artifact(
        item["project_id"],
        item["analysis_job_id"],
        BASE_CANDIDATE_ID,
        "image",
    )
    assert artifact is item["base_image"]

    batch = composite.resolve_candidate_image_batch(
        item["project_id"],
        item["analysis_job_id"],
        [BASE_CANDIDATE_ID],
    )
    assert isinstance(batch, ResolvedAnalysisCandidateImageBatch)
    assert batch.images[0].path == item["base_image"].path
    assert item["base"].batch_calls == [(BASE_CANDIDATE_ID,)]


def test_overlay_is_used_only_after_base_artifact_404(evidence_fixture):
    item = evidence_fixture
    # The base catalog must not claim the overlay candidate.
    composite = CompositeCandidateEvidence(
        item["base"], [item["overlay"]]
    )

    resolved = composite.resolve_candidate(
        item["project_id"],
        item["analysis_job_id"],
        OVERLAY_CANDIDATE_ID,
    )
    assert resolved["candidate_source"] == "overlay"
    assert resolved["source_sha256"] == SOURCE_SHA256
    assert resolved["metadata"] == item["overlay_candidate"]
    encoded = json.dumps(resolved)
    assert str(item["overlay_image"].path) not in encoded

    artifact = composite.resolve_artifact(
        item["project_id"],
        item["analysis_job_id"],
        OVERLAY_CANDIDATE_ID,
        "image",
    )
    assert artifact.path == item["overlay_image"].path
    assert item["base"].artifact_calls[-1] == (
        OVERLAY_CANDIDATE_ID,
        "image",
    )
    assert item["overlay"].artifact_calls[-1] == (
        OVERLAY_CANDIDATE_ID,
        "image",
    )

    batch = composite.resolve_candidate_image_batch(
        item["project_id"],
        item["analysis_job_id"],
        [OVERLAY_CANDIDATE_ID],
    )
    assert batch.images[0].candidate_id == OVERLAY_CANDIDATE_ID
    assert item["overlay"].batch_calls == [(OVERLAY_CANDIDATE_ID,)]


def test_actual_candidate_evidence_overlay_store_contract_has_bound_adapter(
    evidence_fixture,
):
    item = evidence_fixture
    pipeline_job_id = uuid4()
    image_metadata = CandidateEvidenceArtifact(
        artifact_id=f"cea_{'4' * 32}",
        media_type="image/jpeg",
        size_bytes=item["overlay_image"].size_bytes,
        sha256=item["overlay_image"].sha256,
    )
    overlay_clip = item["overlay"].artifacts[
        (OVERLAY_CANDIDATE_ID, "clip")
    ]
    clip_metadata = CandidateEvidenceArtifact(
        artifact_id=f"cea_{'5' * 32}",
        media_type="video/mp4",
        size_bytes=overlay_clip.size_bytes,
        sha256=overlay_clip.sha256,
    )
    candidate = CandidateEvidenceOverlayCandidate(
        candidate_id=OVERLAY_CANDIDATE_ID,
        issue_type="road_debris",
        start_seconds=1.0,
        end_seconds=2.0,
        representative_timestamp_seconds=1.5,
        representative_frame_id="6" * 24,
        representative_frame_sha256="7" * 64,
        score=0.84,
        normalized_box={
            "x": 0.1,
            "y": 0.2,
            "width": 0.3,
            "height": 0.4,
        },
        ocr_text=None,
        urgent_review=True,
        supporting_frame_ids=("6" * 24,),
        proposal_sources=("grounding_dino_review_only",),
        representative_proposal_provenance={
            "model_manifest_sha256": "8" * 64
        },
        proposal_provenance_sha256="9" * 64,
        image=image_metadata,
        clip=clip_metadata,
    )
    result = CandidateEvidenceOverlayResult(
        schema_version=CANDIDATE_EVIDENCE_OVERLAY_SCHEMA,
        overlay_id=OVERLAY_ID,
        project_id=str(item["project_id"]),
        analysis_job_id=str(item["analysis_job_id"]),
        pipeline_job_id=str(pipeline_job_id),
        source_sha256=SOURCE_SHA256,
        discovery_revision="b" * 64,
        dense_index_sha256="c" * 64,
        provider_provenance_sha256="d" * 64,
        model_provenance_sha256="e" * 64,
        candidate_metadata_sha256="f" * 64,
        candidates=(candidate,),
        manifest_sha256="0" * 64,
        created_at="2026-07-29T00:00:00Z",
        idempotent_replay=False,
    )
    store = FakeCandidateEvidenceOverlayStore(
        result,
        {
            (OVERLAY_CANDIDATE_ID, "image"): item["overlay_image"],
            (OVERLAY_CANDIDATE_ID, "clip"): overlay_clip,
        },
    )
    adapter = CandidateEvidenceOverlayAdapter(
        store,
        pipeline_job_id=pipeline_job_id,
        overlay_id=OVERLAY_ID,
    )
    composite = CompositeCandidateEvidence(item["base"], [adapter])

    resolved = composite.resolve_candidate(
        item["project_id"],
        item["analysis_job_id"],
        OVERLAY_CANDIDATE_ID,
    )

    assert resolved["candidate_source"] == "overlay"
    metadata = resolved["metadata"]
    assert metadata["candidate_kind"] == "dense_candidate_evidence_overlay"
    assert metadata["issue_type"] == "road_debris"
    assert metadata["urgent_review"] is True
    assert metadata["representative_image"]["artifact_id"] == image_metadata.artifact_id
    assert metadata["preview_clip"]["artifact_id"] == clip_metadata.artifact_id
    assert metadata["evidence_overlay"] == {
        "schema_version": CANDIDATE_EVIDENCE_OVERLAY_SCHEMA,
        "overlay_id": OVERLAY_ID,
        "pipeline_job_id": str(pipeline_job_id),
        "manifest_sha256": "0" * 64,
        "discovery_revision": "b" * 64,
        "dense_index_sha256": "c" * 64,
        "provider_provenance_sha256": "d" * 64,
        "model_provenance_sha256": "e" * 64,
        "candidate_set_metadata_sha256": "f" * 64,
        "review_only": True,
    }
    json.dumps(resolved)

    artifact = composite.resolve_artifact(
        item["project_id"],
        item["analysis_job_id"],
        OVERLAY_CANDIDATE_ID,
        "image",
    )
    assert artifact.path == item["overlay_image"].path
    batch = composite.resolve_candidate_image_batch(
        item["project_id"],
        item["analysis_job_id"],
        [OVERLAY_CANDIDATE_ID],
    )
    assert batch.run_directory == item["overlay_image"].path.parent
    assert batch.images[0].sha256 == item["overlay_image"].sha256


def test_list_candidates_returns_path_free_base_then_overlay(
    evidence_fixture,
):
    item = evidence_fixture
    composite = CompositeCandidateEvidence(
        item["base"], [item["overlay"]]
    )

    rows = composite.list_candidates(
        item["project_id"], item["analysis_job_id"]
    )

    assert [row["candidate_id"] for row in rows] == [
        BASE_CANDIDATE_ID,
        OVERLAY_CANDIDATE_ID,
    ]
    assert [row["candidate_source"] for row in rows] == [
        "analysis_job",
        "overlay",
    ]
    encoded = json.dumps(rows)
    assert str(item["base_image"].path) not in encoded
    assert str(item["overlay_image"].path) not in encoded


def test_list_candidates_uses_one_immutable_catalog_not_per_row_resolve(
    evidence_fixture,
):
    item = evidence_fixture
    overlay = item["overlay"]

    def forbidden_per_row_resolve(*_args, **_kwargs):
        raise AssertionError("per-row overlay reload is forbidden")

    overlay.resolve_candidate = forbidden_per_row_resolve
    composite = CompositeCandidateEvidence(
        item["base"], [overlay]
    )

    rows = composite.list_candidates(
        item["project_id"], item["analysis_job_id"]
    )

    assert [row["candidate_id"] for row in rows] == [
        BASE_CANDIDATE_ID,
        OVERLAY_CANDIDATE_ID,
    ]


def test_base_and_overlay_candidate_identity_collision_fails_closed(
    evidence_fixture,
):
    item = evidence_fixture
    collision = deepcopy(item["overlay_candidate"])
    collision["candidate_id"] = BASE_CANDIDATE_ID
    collision_overlay = FakeOverlayBackend(
        project_id=item["project_id"],
        analysis_job_id=item["analysis_job_id"],
        candidates=[collision],
        artifacts={
            (BASE_CANDIDATE_ID, "image"): item["overlay_image"],
        },
    )
    composite = CompositeCandidateEvidence(
        item["base"], [collision_overlay]
    )

    with pytest.raises(CandidateEvidenceConflict):
        composite.resolve_candidate(
            item["project_id"],
            item["analysis_job_id"],
            BASE_CANDIDATE_ID,
        )
    with pytest.raises(CandidateEvidenceConflict):
        composite.resolve_artifact(
            item["project_id"],
            item["analysis_job_id"],
            BASE_CANDIDATE_ID,
            "image",
        )
    with pytest.raises(CandidateEvidenceConflict):
        composite.list_candidates(
            item["project_id"], item["analysis_job_id"]
        )


def test_same_candidate_in_multiple_overlay_sets_fails_even_when_identical(
    evidence_fixture,
):
    item = evidence_fixture
    second = FakeOverlayBackend(
        project_id=item["project_id"],
        analysis_job_id=item["analysis_job_id"],
        candidates=[item["overlay_candidate"]],
        artifacts={
            (OVERLAY_CANDIDATE_ID, "image"): item["overlay_image"],
        },
    )
    composite = CompositeCandidateEvidence(
        item["base"], [item["overlay"], second]
    )

    with pytest.raises(CandidateEvidenceConflict):
        composite.resolve_candidate(
            item["project_id"],
            item["analysis_job_id"],
            OVERLAY_CANDIDATE_ID,
        )
    with pytest.raises(CandidateEvidenceConflict):
        composite.list_candidates(
            item["project_id"], item["analysis_job_id"]
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda record: record.__setitem__("source_sha256", "b" * 64),
        lambda record: record.__setitem__(
            "candidate_metadata_sha256", "b" * 64
        ),
        lambda record: record.__setitem__("project_id", str(uuid4())),
    ],
)
def test_overlay_identity_and_metadata_hash_mismatches_fail_closed(
    evidence_fixture, mutate
):
    item = evidence_fixture
    mutate(item["overlay"].records[OVERLAY_CANDIDATE_ID])
    composite = CompositeCandidateEvidence(
        item["base"], [item["overlay"]]
    )

    with pytest.raises(CandidateEvidenceIntegrityError):
        composite.resolve_candidate(
            item["project_id"],
            item["analysis_job_id"],
            OVERLAY_CANDIDATE_ID,
        )


def test_overlay_artifact_is_rehashed_against_candidate_metadata(
    evidence_fixture,
):
    item = evidence_fixture
    composite = CompositeCandidateEvidence(
        item["base"], [item["overlay"]]
    )
    item["overlay_image"].path.write_bytes(b"\xff\xd8\xfftampered")

    with pytest.raises(CandidateEvidenceIntegrityError):
        composite.resolve_artifact(
            item["project_id"],
            item["analysis_job_id"],
            OVERLAY_CANDIDATE_ID,
            "image",
        )


def test_non_404_base_artifact_failure_never_falls_back_to_overlay(
    evidence_fixture,
):
    item = evidence_fixture
    item["base"].artifact_errors[
        (OVERLAY_CANDIDATE_ID, "image")
    ] = HTTPException(status_code=410, detail="base integrity failure")
    composite = CompositeCandidateEvidence(
        item["base"], [item["overlay"]]
    )

    with pytest.raises(HTTPException) as failure:
        composite.resolve_artifact(
            item["project_id"],
            item["analysis_job_id"],
            OVERLAY_CANDIDATE_ID,
            "image",
        )
    assert failure.value.status_code == 410
    assert item["overlay"].artifact_calls == []


def test_mixed_base_overlay_batch_is_staged_under_internal_root(
    evidence_fixture, tmp_path
):
    item = evidence_fixture
    composite = CompositeCandidateEvidence(
        item["base"],
        [item["overlay"]],
        batch_root=(tmp_path / "internal-batches").resolve(),
    )

    batch = composite.resolve_candidate_image_batch(
        item["project_id"],
        item["analysis_job_id"],
        [BASE_CANDIDATE_ID, OVERLAY_CANDIDATE_ID],
    )

    assert isinstance(batch, ResolvedAnalysisCandidateImageBatch)
    assert [image.candidate_id for image in batch.images] == [
        BASE_CANDIDATE_ID,
        OVERLAY_CANDIDATE_ID,
    ]
    assert all(image.path.parent == batch.run_directory for image in batch.images)
    assert batch.images[0].sha256 == item["base_image"].sha256
    assert batch.images[1].sha256 == item["overlay_image"].sha256
    assert batch.run_directory.is_relative_to(
        (tmp_path / "internal-batches").resolve()
    )
