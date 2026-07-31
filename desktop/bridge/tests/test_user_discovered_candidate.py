from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from app.candidate_evidence_overlay import (
    CANDIDATE_EVIDENCE_OVERLAY_SCHEMA,
    CandidateEvidenceArtifact,
    CandidateEvidenceOverlayCandidate,
    CandidateEvidenceOverlayLimits,
    CandidateEvidenceOverlayResult,
    _validated_discovery,
)
from app.user_discovered_candidate import (
    ResolvedIndependentSurvey,
    USER_DISCOVERED_CANDIDATE_SCHEMA,
    USER_DISCOVERED_PROPOSAL_SOURCE,
    USER_DISCOVERED_REVIEW_FRAME_SCHEMA,
    UserDiscoveredCandidateError,
    UserDiscoveredCandidateRequest,
    UserDiscoveredCandidateService,
    resolve_user_discovered_review_frame,
    resolve_user_discovered_review_frame_artifact,
)


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _canonical_sha(value) -> str:
    return _sha(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _frame_id(source_sha256: str, frame_index: int) -> str:
    return _sha(
        (
            "trafficvisson.dense-frame-index.v1\0"
            f"{source_sha256}\0fps=2\0frame={frame_index}"
        ).encode("ascii")
    )[:24]


def _jpeg(frame_index: int) -> bytes:
    return (
        b"\xff\xd8"
        + b"\xff\xc0\x00\x11\x08\x00\x02\x00\x02\x03"
        + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
        + frame_index.to_bytes(2, "big")
        + b"\xff\xd9"
    )


def _dense_index(tmp_path: Path, *, frame_count: int = 6):
    root = tmp_path / "dense-frame-index-fixture"
    frames_root = root / "frames"
    frames_root.mkdir(parents=True)
    source_sha256 = _sha(b"independent-survey-video")
    rows = []
    for frame_index in range(frame_count):
        body = _jpeg(frame_index)
        file_name = f"frame-{frame_index:08d}.jpg"
        (frames_root / file_name).write_bytes(body)
        rows.append(
            {
                "frame_id": _frame_id(source_sha256, frame_index),
                "frame_index": frame_index,
                "timestamp_seconds": frame_index / 2,
                "timestamp_numerator": frame_index,
                "timestamp_denominator": 2,
                "relative_path": f"frames/{file_name}",
                "width": 2,
                "height": 2,
                "size_bytes": len(body),
                "sha256": _sha(body),
                "media_type": "image/jpeg",
                "evidence_status": "unreviewed_candidate",
            }
        )
    payload = {
        "schema_version": "trafficvisson.dense-frame-index.v1",
        "frame_count": len(rows),
        "frames": rows,
        "provenance": {
            "schema_version": "trafficvisson.dense-frame-index.v1",
            "source_sha256": source_sha256,
            "sampling_fps": 2,
            "timestamp_method": (
                "zero_based_frame_index_divided_by_integer_sampling_fps"
            ),
            "frame_count": len(rows),
            "indexed_duration_seconds": frame_count / 2,
        },
    }
    index_path = root / "frame-index.json"
    index_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return (
        source_sha256,
        index_path,
        _sha(index_path.read_bytes()),
        tuple(rows),
    )


class _FakeMaterializer:
    def __init__(self) -> None:
        self.calls = []
        self._seen = set()
        self.force_truth_count = False

    def materialize(
        self,
        *,
        project_id,
        analysis_job_id,
        pipeline_job_id,
        discovery,
        cancel=None,
    ):
        self.calls.append(discovery)
        seed = discovery.candidates[0]
        overlay_id = _sha(
            f"overlay:{discovery.discovery_revision}".encode()
        )[:32]
        replay = discovery.discovery_revision in self._seen
        self._seen.add(discovery.discovery_revision)
        image = CandidateEvidenceArtifact(
            artifact_id=f"img:{seed.candidate_id}",
            media_type="image/jpeg",
            size_bytes=29,
            sha256=seed.representative_frame_sha256,
        )
        clip = CandidateEvidenceArtifact(
            artifact_id=f"clip:{seed.candidate_id}",
            media_type="video/mp4",
            size_bytes=512,
            sha256=_sha(f"clip:{seed.candidate_id}".encode()),
        )
        candidate = CandidateEvidenceOverlayCandidate(
            candidate_id=seed.candidate_id,
            issue_type=seed.issue_type,
            start_seconds=seed.start_seconds,
            end_seconds=seed.end_seconds,
            representative_timestamp_seconds=(
                seed.representative_timestamp_seconds
            ),
            representative_frame_id=seed.representative_frame_id,
            representative_frame_sha256=(
                seed.representative_frame_sha256
            ),
            score=seed.score,
            normalized_box=seed.normalized_box,
            ocr_text=seed.ocr_text,
            urgent_review=seed.urgent_review,
            supporting_frame_ids=seed.supporting_frame_ids,
            proposal_sources=seed.proposal_sources,
            representative_proposal_provenance=(
                seed.representative_proposal_provenance
            ),
            proposal_provenance_sha256=(
                seed.proposal_provenance_sha256
            ),
            image=image,
            clip=clip,
        )
        return CandidateEvidenceOverlayResult(
            schema_version=CANDIDATE_EVIDENCE_OVERLAY_SCHEMA,
            overlay_id=overlay_id,
            project_id=str(project_id),
            analysis_job_id=str(analysis_job_id),
            pipeline_job_id=str(pipeline_job_id),
            source_sha256=discovery.source_sha256,
            discovery_revision=discovery.discovery_revision,
            dense_index_sha256=discovery.dense_index_sha256,
            provider_provenance_sha256=_sha(b"provider"),
            model_provenance_sha256=_sha(b"model"),
            candidate_metadata_sha256=_sha(b"candidate"),
            candidates=(candidate,),
            manifest_sha256=_sha(
                f"manifest:{discovery.discovery_revision}".encode()
            ),
            created_at="2026-07-30T00:00:00+00:00",
            idempotent_replay=replay,
            annotations_created=1 if self.force_truth_count else 0,
        )


def _fixture(tmp_path: Path):
    source_sha256, index_path, index_sha256, rows = _dense_index(
        tmp_path
    )
    project_id = uuid4()
    analysis_job_id = uuid4()
    pipeline_job_id = uuid4()
    training_id = uuid4()
    validation_id = uuid4()
    lineage = {
        "training_analysis_job_ids": [str(training_id)],
        "fixed_validation_analysis_job_ids": [str(validation_id)],
    }
    grant = ResolvedIndependentSurvey(
        project_id=project_id,
        analysis_job_id=analysis_job_id,
        pipeline_job_id=pipeline_job_id,
        analysis_status="completed",
        survey_purpose="independent_promoted_model_qa",
        source_sha256=source_sha256,
        source_duration_seconds=3.0,
        dense_index_path=index_path,
        dense_index_sha256=index_sha256,
        dense_sampling_fps=2,
        promoted_model_state="active",
        promoted_model_version_id=uuid4(),
        promoted_model_artifact_manifest_sha256=_sha(b"model-manifest"),
        promoted_model_inference_sha256=_sha(b"survey-inference"),
        dataset_version_id=uuid4(),
        dataset_hash=_sha(b"dataset"),
        training_analysis_job_ids=(training_id,),
        fixed_validation_analysis_job_ids=(validation_id,),
        source_analysis_lineage_sha256=_canonical_sha(lineage),
    )
    request = UserDiscoveredCandidateRequest(
        project_id=project_id,
        analysis_job_id=analysis_job_id,
        pipeline_job_id=pipeline_job_id,
        timestamp_seconds=0.76,
        selection={"kind": "point", "x": 0.42, "y": 0.61},
        expected_dense_index_sha256=index_sha256,
        expected_model_version_id=grant.promoted_model_version_id,
        expected_model_artifact_manifest_sha256=(
            grant.promoted_model_artifact_manifest_sha256
        ),
        expected_model_inference_sha256=(
            grant.promoted_model_inference_sha256
        ),
        expected_source_analysis_lineage_sha256=(
            grant.source_analysis_lineage_sha256
        ),
        issue_type_proposal=None,
        request_sam2_mask=True,
    )
    materializer = _FakeMaterializer()
    service = UserDiscoveredCandidateService(
        survey_resolver=lambda project, analysis, pipeline: grant,
        evidence_materializer=materializer,
        sam2_runtime_provenance_resolver=lambda *_args: {
            "model_pack_id": "sam2-local",
            "model_pack_version": "2.1",
            "manifest_sha256": _sha(b"sam2-pack"),
            "runtime": "local_cuda",
        },
    )
    return grant, request, materializer, service, rows


def test_user_miss_selects_nearest_actual_frame_as_review_only_evidence(
    tmp_path,
):
    grant, request, materializer, service, rows = _fixture(tmp_path)

    result = service.create(request)

    assert result.schema_version == USER_DISCOVERED_CANDIDATE_SCHEMA
    assert result.candidate_kind == "user_discovered"
    assert result.selected_frame_id == rows[2]["frame_id"]
    assert result.selected_frame_timestamp_seconds == 1.0
    assert result.selected_frame_sha256 == rows[2]["sha256"]
    assert result.representative_image["sha256"] == rows[2]["sha256"]
    assert result.representative_image["media_type"] == "image/jpeg"
    assert result.preview_clip["media_type"] == "video/mp4"
    assert 2.0 <= result.preview_clip["duration_seconds"] <= 5.0
    assert result.review_issue_type == "issue_type_unclassified"
    assert result.issue_type_proposal is None
    assert result.review_only is True
    assert result.annotations_created == 0
    assert result.incidents_created == 0
    assert result.training_members_created == 0
    assert result.report_truth_records_created == 0
    assert result.sam2_refinement_request["point_prompt"] == {
        "x": 0.42,
        "y": 0.61,
    }
    assert (
        result.sam2_refinement_request["status"]
        == "requested_with_runtime_provenance"
    )
    discovery = materializer.calls[0]
    assert discovery.review_only is True
    assert discovery.annotations_created == 0
    assert discovery.incidents_created == 0
    assert discovery.training_members_created == 0
    assert discovery.candidates[0].proposal_sources == (
        USER_DISCOVERED_PROPOSAL_SOURCE,
    )
    provenance = (
        discovery.candidates[0].representative_proposal_provenance
    )
    assert provenance["promoted_model_version_id"] == str(
        grant.promoted_model_version_id
    )
    assert provenance["source_analysis_lineage_sha256"] == (
        grant.source_analysis_lineage_sha256
    )


def test_box_selection_and_issue_proposal_stay_review_only(tmp_path):
    _grant, request, materializer, service, _rows = _fixture(tmp_path)
    request = replace(
        request,
        timestamp_seconds=2.48,
        selection={
            "kind": "box",
            "x": 0.1,
            "y": 0.2,
            "width": 0.3,
            "height": 0.4,
        },
        issue_type_proposal="pothole",
    )

    result = service.create(request)

    assert result.selected_frame_timestamp_seconds == 2.5
    assert result.review_issue_type == "pothole"
    assert materializer.calls[0].candidates[0].normalized_box == {
        "x": 0.1,
        "y": 0.2,
        "width": 0.3,
        "height": 0.4,
    }
    assert result.sam2_refinement_request["point_prompt"] == {
        "x": 0.25,
        "y": 0.4,
    }
    assert result.annotations_created == 0
    assert result.incidents_created == 0


def test_identical_request_is_deterministic_and_overlay_idempotent(tmp_path):
    _grant, request, materializer, service, _rows = _fixture(tmp_path)

    first = service.create(request)
    second = service.create(request)

    assert first.request_sha256 == second.request_sha256
    assert first.candidate_id == second.candidate_id
    assert first.discovery_revision == second.discovery_revision
    assert first.overlay_id == second.overlay_id
    assert first.overlay_manifest_sha256 == second.overlay_manifest_sha256
    assert first.idempotent_replay is False
    assert second.idempotent_replay is True
    assert len(materializer.calls) == 2


def test_discovery_contract_is_accepted_by_existing_overlay_validator(
    tmp_path,
):
    _grant, request, materializer, service, _rows = _fixture(tmp_path)

    service.create(request)
    validated = _validated_discovery(
        materializer.calls[0],
        project_id=request.project_id,
        limits=CandidateEvidenceOverlayLimits(
            minimum_free_disk_bytes=0
        ),
    )

    assert len(validated[-1]) == 1
    assert validated[-1][0]["proposal_sources"] == [
        USER_DISCOVERED_PROPOSAL_SOURCE
    ]
    assert validated[-1][0]["representative_frame_sha256"] == (
        materializer.calls[0]
        .candidates[0]
        .representative_frame_sha256
    )


def test_cross_project_binding_is_rejected_before_materialization(tmp_path):
    grant, request, materializer, _service, _rows = _fixture(tmp_path)
    foreign_grant = replace(grant, project_id=uuid4())
    service = UserDiscoveredCandidateService(
        survey_resolver=lambda *_args: foreign_grant,
        evidence_materializer=materializer,
    )

    with pytest.raises(
        UserDiscoveredCandidateError,
        match="another project",
    ) as error:
        service.create(request)

    assert error.value.code == "independent_survey_scope_mismatch"
    assert materializer.calls == []


@pytest.mark.parametrize("timestamp", [-0.001, 3.00001, float("nan")])
def test_timestamp_must_be_inside_actual_dense_index_coverage(
    tmp_path,
    timestamp,
):
    _grant, request, materializer, service, _rows = _fixture(tmp_path)

    with pytest.raises(UserDiscoveredCandidateError) as error:
        service.create(replace(request, timestamp_seconds=timestamp))

    assert error.value.code in {
        "user_discovered_time_invalid",
        "user_discovered_time_out_of_bounds",
    }
    assert materializer.calls == []


@pytest.mark.parametrize("source_kind", ["training", "validation"])
def test_training_and_validation_source_video_reuse_is_blocked(
    tmp_path,
    source_kind,
):
    grant, request, materializer, _service, _rows = _fixture(tmp_path)
    if source_kind == "training":
        changed = replace(
            grant,
            analysis_job_id=grant.training_analysis_job_ids[0],
        )
    else:
        changed = replace(
            grant,
            analysis_job_id=grant.fixed_validation_analysis_job_ids[0],
        )
    changed_request = replace(
        request,
        analysis_job_id=changed.analysis_job_id,
    )
    service = UserDiscoveredCandidateService(
        survey_resolver=lambda *_args: changed,
        evidence_materializer=materializer,
    )

    with pytest.raises(UserDiscoveredCandidateError) as error:
        service.create(changed_request)

    assert error.value.code == "model_source_video_reuse_forbidden"
    assert materializer.calls == []


def test_lineage_digest_and_stale_model_revision_fail_closed(tmp_path):
    grant, request, materializer, _service, _rows = _fixture(tmp_path)
    tampered = replace(
        grant,
        source_analysis_lineage_sha256=_sha(b"tampered-lineage"),
    )
    service = UserDiscoveredCandidateService(
        survey_resolver=lambda *_args: tampered,
        evidence_materializer=materializer,
    )
    with pytest.raises(UserDiscoveredCandidateError) as lineage_error:
        service.create(
            replace(
                request,
                expected_source_analysis_lineage_sha256=(
                    tampered.source_analysis_lineage_sha256
                ),
            )
        )
    assert lineage_error.value.code == "model_source_lineage_tampered"

    stale_service = UserDiscoveredCandidateService(
        survey_resolver=lambda *_args: grant,
        evidence_materializer=materializer,
    )
    with pytest.raises(UserDiscoveredCandidateError) as stale_error:
        stale_service.create(
            replace(request, expected_model_version_id=uuid4())
        )
    assert stale_error.value.code == "independent_survey_revision_conflict"
    assert materializer.calls == []


def test_dense_index_and_frame_tamper_are_rejected(tmp_path):
    grant, request, materializer, service, rows = _fixture(tmp_path)
    grant.dense_index_path.write_bytes(
        grant.dense_index_path.read_bytes() + b"\n"
    )
    with pytest.raises(UserDiscoveredCandidateError) as index_error:
        service.create(request)
    assert index_error.value.code == "dense_index_hash_mismatch"
    assert materializer.calls == []

    grant2, request2, materializer2, service2, rows2 = _fixture(
        tmp_path / "second"
    )
    selected = (
        grant2.dense_index_path.parent
        / rows2[2]["relative_path"]
    )
    selected.write_bytes(selected.read_bytes() + b"tamper")
    with pytest.raises(UserDiscoveredCandidateError) as frame_error:
        service2.create(request2)
    assert frame_error.value.code == "dense_frame_integrity_failed"
    assert materializer2.calls == []


@pytest.mark.parametrize(
    "selection",
    [
        {"kind": "point", "x": -0.1, "y": 0.5},
        {"kind": "point", "x": 0.5, "y": 0.5, "extra": 1},
        {
            "kind": "box",
            "x": 0.8,
            "y": 0.2,
            "width": 0.3,
            "height": 0.2,
        },
    ],
)
def test_selection_is_exact_and_normalized(tmp_path, selection):
    _grant, request, materializer, service, _rows = _fixture(tmp_path)

    with pytest.raises(UserDiscoveredCandidateError) as error:
        service.create(replace(request, selection=selection))

    assert error.value.code == "user_discovered_selection_invalid"
    assert materializer.calls == []


def test_incomplete_analysis_and_materializer_truth_are_rejected(tmp_path):
    grant, request, materializer, _service, _rows = _fixture(tmp_path)
    incomplete = replace(grant, analysis_status="running")
    service = UserDiscoveredCandidateService(
        survey_resolver=lambda *_args: incomplete,
        evidence_materializer=materializer,
    )
    with pytest.raises(UserDiscoveredCandidateError) as status_error:
        service.create(request)
    assert status_error.value.code == "independent_survey_incomplete"
    assert materializer.calls == []

    materializer.force_truth_count = True
    service = UserDiscoveredCandidateService(
        survey_resolver=lambda *_args: grant,
        evidence_materializer=materializer,
    )
    with pytest.raises(UserDiscoveredCandidateError) as truth_error:
        service.create(request)
    assert truth_error.value.code == "user_discovered_evidence_invalid"


def test_full_journey_review_frame_is_path_free_and_timestamp_bound(
    tmp_path,
):
    grant, _request, _materializer, _service, rows = _fixture(tmp_path)

    frame = resolve_user_discovered_review_frame(
        grant,
        timestamp_seconds=2.24,
    )
    response = frame.to_response()

    assert response["schema_version"] == (
        USER_DISCOVERED_REVIEW_FRAME_SCHEMA
    )
    assert response["requested_timestamp_seconds"] == 2.24
    assert response["selected_frame_timestamp_seconds"] == 2.0
    assert response["selected_frame_id"] == rows[4]["frame_id"]
    assert response["selected_frame_sha256"] == rows[4]["sha256"]
    assert response["indexed_duration_seconds"] == 3.0
    assert response["sampling_fps"] == 2
    assert response["path_exposed"] is False
    assert len(response["review_frame_sha256"]) == 64
    assert all(
        "path" not in key.casefold() or key == "path_exposed"
        for key in response
    )
    assert frame.path.is_file()


def test_full_journey_review_frame_artifact_rejects_foreign_identity(
    tmp_path,
):
    grant, _request, _materializer, _service, rows = _fixture(tmp_path)

    frame = resolve_user_discovered_review_frame_artifact(
        grant,
        frame_id=rows[5]["frame_id"],
    )
    assert frame.selected_frame_index == 5
    assert frame.selected_frame_timestamp_seconds == 2.5
    assert frame.path.read_bytes() == _jpeg(5)

    with pytest.raises(UserDiscoveredCandidateError) as error:
        resolve_user_discovered_review_frame_artifact(
            grant,
            frame_id="0" * 24,
        )
    assert error.value.code == "dense_frame_not_found"
