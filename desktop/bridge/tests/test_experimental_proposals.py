from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.experimental_proposals import (
    ExperimentalProposalConflict,
    ExperimentalProposalError,
    ExperimentalProposalStore,
)
from test_analysis_jobs import (
    DEMO_PROJECT_ID,
    _auth,
    _complete_analysis,
    _make_video,
    _mock_road_model_service,
    _production_client,
)


PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
JOB_ID = UUID("22222222-2222-4222-8222-222222222222")
CANDIDATE_ID = "0123456789abcdefabcd"
IMAGE_SHA256 = "a" * 64
MANIFEST_SHA256 = "b" * 64
DATASET_SHA256 = "c" * 64


def _candidate(candidate_id: str = CANDIDATE_ID):
    return {
        "candidate_id": candidate_id,
        "start_seconds": 1.0,
        "end_seconds": 3.0,
        "representative_timestamp_seconds": 2.0,
        "scene_score": 0.8,
        "sharpness_score": 0.7,
        "triggers": ["scene_change"],
        "observations": [{"timestamp_seconds": 2.0}],
        "representative_image": {
            "relative_path": f"{candidate_id}.jpg",
            "size_bytes": 123,
            "sha256": IMAGE_SHA256,
            "media_type": "image/jpeg",
            "artifact_ref": (
                f"analysis:{JOB_ID}:{candidate_id}.jpg"
            ),
        },
        "preview_clip": {
            "relative_path": f"{candidate_id}.mp4",
            "size_bytes": 456,
            "sha256": "d" * 64,
            "media_type": "video/mp4",
            "artifact_ref": (
                f"analysis:{JOB_ID}:{candidate_id}.mp4"
            ),
        },
        "preview_start_seconds": 0.0,
        "preview_duration_seconds": 3.0,
        "ai_inference": None,
    }


def _resolver_factory(metadata):
    def resolver(project_id, analysis_job_id, candidate_id):
        if (
            project_id != PROJECT_ID
            or analysis_job_id != JOB_ID
            or candidate_id != CANDIDATE_ID
        ):
            raise LookupError("not found")
        return {
            "status": "completed",
            "project_id": str(project_id),
            "analysis_job_id": str(analysis_job_id),
            "candidate_id": candidate_id,
            "metadata": metadata,
        }

    return resolver


def _model(provider: str = "transformers_clip_zero_shot"):
    if provider == "onnx_yolo_v8":
        return {
            "provider": provider,
            "pack_id": "trafficvisson-road-detector-experimental",
            "pack_version": "0.1.0",
            "manifest_sha256": MANIFEST_SHA256,
        }
    return {
        "provider": provider,
        "pack_id": "trafficvisson-laion-clip-b32-road-baseline",
        "pack_version": "0.1.0",
        "manifest_sha256": MANIFEST_SHA256,
    }


def _dataset():
    return {
        "dataset_id": "trafficvisson-road-seed",
        "dataset_version": "2026.07.28",
        "dataset_sha256": DATASET_SHA256,
    }


def _evaluation():
    return {
        "evaluation_id": "all-candidates-v1",
        "producer": "trafficvisson-evaluator",
        "producer_version": "0.1.0",
    }


def _classified(candidate_id: str = CANDIDATE_ID, score: float = 0.09):
    return {
        "candidate_id": candidate_id,
        "status": "classified",
        "predictions": [
            {
                "issue_type": "roadside_mowing_required",
                "relative_score": score,
                "box": None,
                "source_label": "overgrown grass beside a highway",
                "calibration": "zero_shot_relative",
            }
        ],
    }


def _detector_classified(
    candidate_id: str = CANDIDATE_ID, score: float = 0.61
):
    return {
        "candidate_id": candidate_id,
        "status": "classified",
        "predictions": [
            {
                "issue_type": "pothole",
                "model_score": score,
                "relative_score": None,
                "box": {
                    "x": 0.2,
                    "y": 0.5,
                    "width": 0.3,
                    "height": 0.2,
                },
                "source_label": "pothole",
                "calibration": "model_score",
            }
        ],
    }


def test_sidecar_round_trip_preserves_provenance_and_review_only_semantics(
    tmp_path,
):
    metadata = _candidate()
    run_id = uuid4()
    now = datetime(2026, 7, 28, 3, 4, 5, tzinfo=timezone.utc)
    store = ExperimentalProposalStore(
        tmp_path.resolve(),
        _resolver_factory(metadata),
        now_factory=lambda: now,
    )

    written = store.record_run(
        PROJECT_ID,
        JOB_ID,
        run_id=run_id,
        model=_model(),
        dataset=_dataset(),
        evaluation=_evaluation(),
        proposals=[_classified()],
    )

    assert len(written) == 1
    proposal = written[0]
    assert proposal["run_id"] == str(run_id)
    assert proposal["project_id"] == str(PROJECT_ID)
    assert proposal["analysis_job_id"] == str(JOB_ID)
    assert proposal["candidate_id"] == CANDIDATE_ID
    assert proposal["review_only"] is True
    assert proposal["automatic_labeling_allowed"] is False
    assert (
        proposal["score_semantics"]
        == "zero_shot_relative_not_probability"
    )
    assert proposal["predictions"][0]["calibration"] == "zero_shot_relative"
    assert proposal["predictions"][0]["relative_score"] == 0.09
    assert proposal["predictions"][0]["model_score"] is None
    assert proposal["model"] == _model()
    assert proposal["dataset"] == _dataset()
    assert proposal["evaluation"] == _evaluation()
    assert proposal["source"]["representative_image_sha256"] == IMAGE_SHA256

    expected_candidate_hash = hashlib.sha256(
        json.dumps(
            metadata,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert (
        proposal["source"]["analysis_candidate_sha256"]
        == expected_candidate_hash
    )

    reloaded = ExperimentalProposalStore(
        tmp_path.resolve(),
        _resolver_factory(metadata),
    )
    assert reloaded.latest_for_job(PROJECT_ID, JOB_ID) == {
        CANDIDATE_ID: proposal
    }


def test_detector_sidecar_uses_uncalibrated_model_score_and_requires_box(
    tmp_path,
):
    store = ExperimentalProposalStore(
        tmp_path.resolve(),
        _resolver_factory(_candidate()),
    )
    proposal = store.record_run(
        PROJECT_ID,
        JOB_ID,
        model=_model("onnx_yolo_v8"),
        evaluation=_evaluation(),
        proposals=[_detector_classified()],
    )[0]

    assert proposal["score_semantics"] == "model_score_not_probability"
    prediction = proposal["predictions"][0]
    assert prediction["calibration"] == "model_score"
    assert prediction["model_score"] == 0.61
    assert prediction["relative_score"] is None
    assert prediction["box"] == {
        "x": 0.2,
        "y": 0.5,
        "width": 0.3,
        "height": 0.2,
    }
    assert proposal["review_only"] is True
    assert proposal["automatic_labeling_allowed"] is False

    no_box = _detector_classified()
    no_box["predictions"][0]["box"] = None
    with pytest.raises(ExperimentalProposalError):
        store.record_run(
            PROJECT_ID,
            JOB_ID,
            model=_model("onnx_yolo_v8"),
            evaluation=_evaluation(),
            proposals=[no_box],
        )


@pytest.mark.parametrize(
    "invalid_score",
    [True, False, "0.61", float("nan"), float("inf")],
)
def test_sidecar_rejects_coerced_or_non_finite_scores(
    tmp_path, invalid_score
):
    store = ExperimentalProposalStore(
        tmp_path.resolve(),
        _resolver_factory(_candidate()),
    )
    proposal = _classified()
    proposal["predictions"][0]["relative_score"] = invalid_score

    with pytest.raises(ExperimentalProposalError):
        store.record_run(
            PROJECT_ID,
            JOB_ID,
            model=_model(),
            evaluation=_evaluation(),
            proposals=[proposal],
        )


@pytest.mark.parametrize("invalid_coordinate", [False, "0.2"])
def test_detector_sidecar_rejects_coerced_box_coordinates(
    tmp_path, invalid_coordinate
):
    store = ExperimentalProposalStore(
        tmp_path.resolve(),
        _resolver_factory(_candidate()),
    )
    proposal = _detector_classified()
    proposal["predictions"][0]["box"]["x"] = invalid_coordinate

    with pytest.raises(ExperimentalProposalError):
        store.record_run(
            PROJECT_ID,
            JOB_ID,
            model=_model("onnx_yolo_v8"),
            evaluation=_evaluation(),
            proposals=[proposal],
        )


@pytest.mark.parametrize(
    ("model", "proposal"),
    [
        (_model(), _detector_classified()),
        (_model("onnx_yolo_v8"), _classified()),
    ],
)
def test_calibration_cannot_be_mixed_with_another_provider(
    tmp_path, model, proposal
):
    store = ExperimentalProposalStore(
        tmp_path.resolve(),
        _resolver_factory(_candidate()),
    )
    with pytest.raises(ExperimentalProposalError):
        store.record_run(
            PROJECT_ID,
            JOB_ID,
            model=model,
            evaluation=_evaluation(),
            proposals=[proposal],
        )


def test_invalid_proposal_contract_is_rejected_before_any_sidecar_is_written(
    tmp_path,
):
    store = ExperimentalProposalStore(
        tmp_path.resolve(),
        _resolver_factory(_candidate()),
    )
    invalid = _classified()
    invalid["predictions"][0]["calibration"] = "probability"

    with pytest.raises(ExperimentalProposalError):
        store.record_run(
            PROJECT_ID,
            JOB_ID,
            model=_model(),
            evaluation=_evaluation(),
            proposals=[invalid],
        )

    assert not list(
        (tmp_path / "experimental-proposals").rglob("*.json")
    )


def test_immutable_run_id_rejects_different_payload(tmp_path):
    now = datetime(2026, 7, 28, 3, 4, 5, tzinfo=timezone.utc)
    run_id = uuid4()
    store = ExperimentalProposalStore(
        tmp_path.resolve(),
        _resolver_factory(_candidate()),
        now_factory=lambda: now,
    )
    store.record_run(
        PROJECT_ID,
        JOB_ID,
        run_id=run_id,
        model=_model(),
        evaluation=_evaluation(),
        proposals=[_classified(score=0.09)],
    )

    with pytest.raises(ExperimentalProposalConflict):
        store.record_run(
            PROJECT_ID,
            JOB_ID,
            run_id=run_id,
            model=_model(),
            evaluation=_evaluation(),
            proposals=[_classified(score=0.25)],
        )


def test_malformed_newer_sidecar_is_ignored_and_valid_older_run_survives(
    tmp_path,
):
    now = datetime(2026, 7, 28, 3, 4, 5, tzinfo=timezone.utc)
    metadata = _candidate()
    store = ExperimentalProposalStore(
        tmp_path.resolve(),
        _resolver_factory(metadata),
        now_factory=lambda: now,
    )
    valid = store.record_run(
        PROJECT_ID,
        JOB_ID,
        model=_model(),
        evaluation=_evaluation(),
        proposals=[_classified()],
    )[0]
    job_root = (
        tmp_path
        / "experimental-proposals"
        / "projects"
        / str(PROJECT_ID)
        / str(JOB_ID)
    )
    malformed = job_root / f"{uuid4()}.json"
    malformed.write_text(
        '{"schema_version":"trafficvisson.experimental-proposal-run.v1",'
        '"schema_version":"duplicate"}',
        encoding="utf-8",
    )

    assert store.latest_for_job(PROJECT_ID, JOB_ID) == {
        CANDIDATE_ID: valid
    }


def test_malformed_files_do_not_consume_the_valid_run_scan_limit(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "app.experimental_proposals.MAX_RUNS_PER_JOB", 2
    )
    store = ExperimentalProposalStore(
        tmp_path.resolve(),
        _resolver_factory(_candidate()),
    )
    valid = store.record_run(
        PROJECT_ID,
        JOB_ID,
        model=_model(),
        evaluation=_evaluation(),
        proposals=[_classified()],
    )[0]
    job_root = (
        tmp_path
        / "experimental-proposals"
        / "projects"
        / str(PROJECT_ID)
        / str(JOB_ID)
    )
    for _ in range(2):
        (job_root / f"{uuid4()}.json").write_text(
            "{not-json", encoding="utf-8"
        )

    assert store.latest_for_job(PROJECT_ID, JOB_ID) == {
        CANDIDATE_ID: valid
    }


def test_stale_source_hash_is_ignored_without_modifying_candidate(tmp_path):
    metadata = _candidate()
    store = ExperimentalProposalStore(
        tmp_path.resolve(),
        _resolver_factory(metadata),
    )
    store.record_run(
        PROJECT_ID,
        JOB_ID,
        model=_model(),
        evaluation=_evaluation(),
        proposals=[_classified()],
    )
    metadata["representative_image"]["sha256"] = "e" * 64

    assert store.latest_for_job(PROJECT_ID, JOB_ID) == {}


def test_label_question_api_merges_sidecar_without_mutating_analysis_or_answers(
    tmp_path,
):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    with _production_client(tmp_path, ffmpeg, ffprobe) as client:
        terminal = _complete_analysis(client, source)
        job_id = terminal["job_id"]
        candidate = terminal["result"]["candidates"][0]
        candidate_id = candidate["candidate_id"]
        record_path = (
            client.app.state.bridge_data_root
            / "projects"
            / str(DEMO_PROJECT_ID)
            / "analysis-job-records"
            / f"{job_id}.json"
        )
        before_bytes = record_path.read_bytes()
        before_job = client.app.state.analysis_jobs.get(
            DEMO_PROJECT_ID, UUID(job_id)
        )

        proposal = client.app.state.experimental_proposals.record_run(
            DEMO_PROJECT_ID,
            UUID(job_id),
            model=_model(),
            evaluation=_evaluation(),
            dataset=None,
            proposals=[_classified(candidate_id)],
        )[0]

        response = client.get(
            (
                f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/{job_id}"
                "/label-questions"
            ),
            headers=_auth(),
        )
        assert response.status_code == 200, response.text
        question = next(
            row
            for row in response.json()["questions"]
            if row["candidate_id"] == candidate_id
        )
        assert question["candidate"] == candidate
        assert question["experimental_proposal"] == proposal
        assert question["status"] == "unanswered"
        assert question["latest_answer"] is None
        assert question["answer_revision"] == 0
        assert (
            client.app.state.candidate_labels.latest_answers(
                DEMO_PROJECT_ID,
                analysis_job_id=UUID(job_id),
            )
            == []
        )
        assert record_path.read_bytes() == before_bytes
        assert (
            client.app.state.analysis_jobs.get(
                DEMO_PROJECT_ID, UUID(job_id)
            )
            == before_job
        )

        job_root = (
            client.app.state.bridge_data_root
            / "experimental-proposals"
            / "projects"
            / str(DEMO_PROJECT_ID)
            / job_id
        )
        malformed = job_root / f"{uuid4()}.json"
        malformed.write_text("{not-json", encoding="utf-8")
        after_malformed = client.get(
            (
                f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/{job_id}"
                "/label-questions"
            ),
            headers=_auth(),
        )
        assert after_malformed.status_code == 200
        after_question = next(
            row
            for row in after_malformed.json()["questions"]
            if row["candidate_id"] == candidate_id
        )
        assert after_question["experimental_proposal"] == proposal
        assert after_question["candidate"] == candidate


@pytest.mark.parametrize(
    (
        "provider",
        "proposal_factory",
        "expected_type",
        "expected_score",
        "expected_semantics",
        "expected_calibration",
        "expected_box",
    ),
    [
        (
            "transformers_clip_zero_shot",
            _classified,
            "roadside_mowing_required",
            "0.09",
            "zero_shot_relative_not_probability",
            "zero_shot_relative",
            None,
        ),
        (
            "onnx_yolo_v8",
            _detector_classified,
            "pothole",
            "0.61",
            "model_score_not_probability",
            "model_score",
            {
                "x": 0.2,
                "y": 0.5,
                "width": 0.3,
                "height": 0.2,
            },
        ),
    ],
)
def test_production_export_overlays_review_only_sidecar_without_confirmation(
    tmp_path,
    provider,
    proposal_factory,
    expected_type,
    expected_score,
    expected_semantics,
    expected_calibration,
    expected_box,
):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    export_root = tmp_path / f"{provider}-export"
    export_root.mkdir()
    vision_service = _mock_road_model_service(tmp_path)
    with _production_client(
        tmp_path,
        ffmpeg,
        ffprobe,
        vision_inference_service=vision_service,
    ) as client:
        terminal = _complete_analysis(client, source)
        job_id = terminal["job_id"]
        candidate = terminal["result"]["candidates"][0]
        candidate_id = candidate["candidate_id"]
        assert (
            candidate["ai_inference"]["predictions"][0]["issue_type"]
            == "road_debris"
        )
        questions_url = (
            f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/{job_id}"
            "/label-questions"
        )
        before_question = next(
            row
            for row in client.get(
                questions_url, headers=_auth()
            ).json()["questions"]
            if row["candidate_id"] == candidate_id
        )
        assert before_question["experimental_proposal"] is None

        record_path = (
            client.app.state.bridge_data_root
            / "projects"
            / str(DEMO_PROJECT_ID)
            / "analysis-job-records"
            / f"{job_id}.json"
        )
        analysis_record_before = record_path.read_bytes()
        run_id = uuid4()
        sidecar = client.app.state.experimental_proposals.record_run(
            DEMO_PROJECT_ID,
            UUID(job_id),
            run_id=run_id,
            model=_model(provider),
            evaluation=_evaluation(),
            dataset=None,
            proposals=[proposal_factory(candidate_id)],
        )[0]
        after_question = next(
            row
            for row in client.get(
                questions_url, headers=_auth()
            ).json()["questions"]
            if row["candidate_id"] == candidate_id
        )
        assert after_question["question_id"] == before_question["question_id"]
        assert after_question["candidate"] == candidate
        assert after_question["experimental_proposal"] == sidecar

        exported = client.post(
            f"/projects/{DEMO_PROJECT_ID}/exports",
            headers=_auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": job_id,
                "artifacts": [
                    "raw_detections",
                    "reviewed_incidents",
                    "label_questions",
                    "annotations",
                    "ocr_observations",
                    "route_points",
                    "maintenance_actions",
                    "report_payload",
                    "manifest",
                ],
                "destination_path": str(export_root),
            },
        )
        assert exported.status_code == 200, exported.text
        assert record_path.read_bytes() == analysis_record_before
        assert (
            client.app.state.candidate_labels.audit_records(
                DEMO_PROJECT_ID,
                analysis_job_id=UUID(job_id),
            )
            == []
        )

    output = next(export_root.glob("trafficvisson-export-*"))

    def csv_rows(file_name):
        with (output / file_name).open(
            "r", encoding="utf-8-sig", newline=""
        ) as source_file:
            return list(csv.DictReader(source_file))

    detections = csv_rows("raw_detections.csv")
    questions = csv_rows("label_questions.csv")
    incidents = csv_rows("reviewed_incidents.csv")
    annotations = csv_rows("annotations.csv")
    detection = next(
        row
        for row in detections
        if row["detection_id"] == f"{candidate_id}-experimental-1"
    )
    assert detection["type"] == expected_type
    assert detection["confidence"] == ""
    assert detection["score_value"] == expected_score
    assert detection["score_semantics"] == expected_semantics
    assert detection["score_calibration"] == expected_calibration
    assert detection["proposal_origin"] == "experimental_sidecar"
    assert detection["review_only"] == "true"
    assert detection["automatic_labeling_allowed"] == "false"
    assert detection["model_provider"] == provider
    assert detection["model_pack_id"] == _model(provider)["pack_id"]
    assert detection["model_pack_version"] == "0.1.0"
    assert detection["model_manifest_sha256"] == MANIFEST_SHA256
    assert detection["dataset_id"] == ""
    assert detection["dataset_version"] == ""
    assert detection["dataset_sha256"] == ""
    assert detection["proposal_run_id"] == str(run_id)
    assert detection["evaluation_id"] == _evaluation()["evaluation_id"]
    assert (
        detection["evaluation_producer"]
        == _evaluation()["producer"]
    )
    assert (
        detection["evaluation_producer_version"]
        == _evaluation()["producer_version"]
    )
    assert (
        detection["analysis_candidate_sha256"]
        == sidecar["source"]["analysis_candidate_sha256"]
    )
    assert (
        detection["representative_image_sha256"]
        == candidate["representative_image"]["sha256"]
    )
    assert detection["source"] == f"experimental_sidecar:{provider}"
    if expected_box is None:
        assert detection["bbox"] == ""
    else:
        assert json.loads(detection["bbox"]) == expected_box
    assert not any(
        row["detection_id"].startswith(f"{candidate_id}-model-")
        for row in detections
    )

    question = next(
        row
        for row in questions
        if row["candidate_incident_id"] == candidate_id
    )
    assert question["question_id"] == before_question["question_id"]
    assert question["proposed_type"] == expected_type
    assert question["proposed_confidence"] == ""
    assert question["proposed_score_value"] == expected_score
    assert question["proposed_score_semantics"] == expected_semantics
    assert question["proposed_score_calibration"] == expected_calibration
    assert question["proposal_origin"] == "experimental_sidecar"
    assert question["review_only"] == "true"
    assert question["automatic_labeling_allowed"] == "false"
    assert question["proposal_run_id"] == str(run_id)
    if expected_box is None:
        assert question["proposed_geometry"] == ""
    else:
        assert json.loads(question["proposed_geometry"]) == expected_box

    # A review-only sidecar is exportable raw evidence, never a confirmation.
    assert incidents == []
    assert annotations == []

    manifest = json.loads(
        (output / "manifest.json").read_text(encoding="utf-8")
    )
    provenance = manifest["provenance"]["experimental_sidecars"]
    assert provenance["selected_proposal_count"] == 1
    assert provenance["classified_count"] == 1
    assert provenance["no_suggestion_count"] == 0
    assert provenance["prediction_count"] == 1
    assert provenance["review_only"] is True
    assert provenance["automatic_labeling_allowed"] is False
    assert (
        provenance["excluded_from_confirmed_outputs_without_user_approval"]
        is True
    )
    assert provenance["eligible_as_evidence_after_user_approval"] is True
    assert len(provenance["selection_sha256"]) == 64
    assert provenance["runs"][0]["run_id"] == str(run_id)
    assert provenance["runs"][0]["selected_proposal_count"] == 1
    assert len(provenance["runs"][0]["selected_proposals_sha256"]) == 64
    assert provenance["models"][0]["provider"] == provider
    assert len(provenance["models"][0]["identity_sha256"]) == 64
    assert (
        provenance["models"][0]["manifest_sha256"]
        == MANIFEST_SHA256
    )
    assert (
        provenance["evaluations"][0]["evaluation_id"]
        == _evaluation()["evaluation_id"]
    )
    assert len(provenance["evaluations"][0]["identity_sha256"]) == 64
    assert provenance["datasets"] == []


def test_production_export_does_not_leak_sidecar_from_a_newer_analysis_job(
    tmp_path,
):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    export_root = tmp_path / "selected-job-export"
    export_root.mkdir()
    with _production_client(tmp_path, ffmpeg, ffprobe) as client:
        selected = _complete_analysis(client, source)
        newer = _complete_analysis(client, source)
        assert selected["job_id"] != newer["job_id"]
        newer_candidate = newer["result"]["candidates"][0]
        client.app.state.experimental_proposals.record_run(
            DEMO_PROJECT_ID,
            UUID(newer["job_id"]),
            model=_model(),
            evaluation=_evaluation(),
            proposals=[_classified(newer_candidate["candidate_id"])],
        )
        exported = client.post(
            f"/projects/{DEMO_PROJECT_ID}/exports",
            headers=_auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": selected["job_id"],
                "artifacts": [
                    "raw_detections",
                    "reviewed_incidents",
                    "label_questions",
                    "annotations",
                    "ocr_observations",
                    "route_points",
                    "maintenance_actions",
                    "report_payload",
                    "manifest",
                ],
                "destination_path": str(export_root),
            },
        )
        assert exported.status_code == 200, exported.text

    output = next(export_root.glob("trafficvisson-export-*"))
    with (output / "raw_detections.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as source_file:
        detections = list(csv.DictReader(source_file))
    assert all(row["proposal_origin"] == "" for row in detections)
    manifest = json.loads(
        (output / "manifest.json").read_text(encoding="utf-8")
    )
    assert (
        manifest["provenance"]["analysis_job_id"]
        == selected["job_id"]
    )
    sidecars = manifest["provenance"]["experimental_sidecars"]
    assert sidecars["selected_proposal_count"] == 0
    assert sidecars["runs"] == []
