import hashlib
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.analysis_jobs import (
    AnalysisJobRecord,
    AnalysisJobService,
    ResolvedAnalysisCandidateImage,
    ResolvedAnalysisCandidateImageBatch,
)
from app.main import create_app
from app.media_intake import CompletedMediaGrant
from app.pipeline_jobs import BridgePipelineRuntime
from conftest import TEST_TOKEN


OCR_PACK_ID = "trafficvisson-jaidedai-easyocr-ko-en"
DETECTOR_PACK_ID = (
    "trafficvisson-peterhdd-yolov8s-pothole-research"
)


def _ocr_status(*, enabled=True):
    if not enabled:
        return {
            "enabled": False,
            "mode": "scene_fallback",
            "warning_code": "model_pack_not_configured",
        }
    return {
        "enabled": True,
        "mode": "local_model",
        "pack_id": OCR_PACK_ID,
        "pack_version": "0.1.0",
        "engine": "easyocr_ocr_only",
        "capabilities": ["ocr"],
        "manifest_sha256": "e" * 64,
        "warning_code": None,
    }


def _profile():
    return {
        "detector_pack_id": DETECTOR_PACK_ID,
        "ocr_pack_id": OCR_PACK_ID,
        "sam2_pack_id": "sam2-test",
        "report_provider": "none",
        "candidate_rescan_fps": 8,
        "review_mode": "precision",
        "question_policy": "learning_assist",
    }


def test_analysis_job_resolves_rehashed_internal_candidate_image_batch(
    tmp_path,
):
    project_id = uuid4()
    analysis_job_id = uuid4()
    candidate_id = "a" * 20
    service = AnalysisJobService(
        object(),
        data_root=(tmp_path / "analysis").resolve(),
    )
    output_root = service._project_output_root(project_id)
    run_directory = output_root / "candidate-extraction-test"
    run_directory.mkdir()
    image_path = run_directory / f"{candidate_id}.jpg"
    image_bytes = b"\xff\xd8\xffverified-candidate"
    image_path.write_bytes(image_bytes)
    image_sha256 = hashlib.sha256(image_bytes).hexdigest()
    now = datetime.now(timezone.utc)
    grant = CompletedMediaGrant(
        project_id=project_id,
        snapshot_id=uuid4(),
        hash_job_id=uuid4(),
        file_name="drive.mp4",
        media_path=tmp_path / "drive.mp4",
        size_bytes=123,
        modified_time_ns=1,
        sha256_lower="f" * 64,
    )
    record = AnalysisJobRecord(
        job_id=analysis_job_id,
        grant=grant,
        status="completed",
        stage="completed",
        error_code=None,
        result={
            "candidates": [
                {
                    "candidate_id": candidate_id,
                    "representative_image": {
                        "relative_path": image_path.name,
                        "size_bytes": len(image_bytes),
                        "sha256": image_sha256,
                        "media_type": "image/jpeg",
                    },
                }
            ]
        },
        created_at=now,
        updated_at=now,
        cancel_event=threading.Event(),
        artifact_directory=run_directory,
    )
    with service._lock:
        service._jobs[analysis_job_id] = record

    batch = service.resolve_candidate_image_batch(
        project_id, analysis_job_id, [candidate_id]
    )

    assert batch.run_directory == run_directory.resolve()
    assert batch.images[0].path == image_path.resolve()
    assert (
        batch.inference_candidates()[0]["representative_image"][
            "sha256"
        ]
        == image_sha256
    )
    assert "path" not in batch.inference_candidates()[0]

    image_path.write_bytes(b"\xff\xd8\xfftampered")
    with pytest.raises(HTTPException) as rejected:
        service.resolve_candidate_image_batch(
            project_id, analysis_job_id, [candidate_id]
        )
    assert rejected.value.status_code == 410
    service.shutdown()


class FakeAnalysisJobs:
    def __init__(self, tmp_path: Path):
        self.project_id = uuid4()
        self.analysis_job_id = uuid4()
        self.source_sha256 = hashlib.sha256(b"video").hexdigest()
        self.candidate_id = "b" * 20
        self.run_directory = (tmp_path / "ocr-run").resolve()
        self.run_directory.mkdir()
        image_path = self.run_directory / f"{self.candidate_id}.jpg"
        image_path.write_bytes(b"\xff\xd8\xffocr")
        self.batch = ResolvedAnalysisCandidateImageBatch(
            run_directory=self.run_directory,
            images=(
                ResolvedAnalysisCandidateImage(
                    candidate_id=self.candidate_id,
                    path=image_path,
                    media_type="image/jpeg",
                    size_bytes=image_path.stat().st_size,
                    sha256=hashlib.sha256(
                        image_path.read_bytes()
                    ).hexdigest(),
                ),
            ),
        )
        self.row = {
            "job_id": self.analysis_job_id,
            "project_id": self.project_id,
            "source_sha256": self.source_sha256,
            "status": "completed",
            "result": {
                "candidates": [
                    {
                        "candidate_id": self.candidate_id,
                        "representative_timestamp_seconds": 2.5,
                    }
                ],
                "provenance": {
                    "effective_cadence_seconds": 0.5
                },
            },
        }

    def get(self, project_id, job_id):
        assert project_id == self.project_id
        assert job_id == self.analysis_job_id
        return self.row

    def resolve_candidate_image_batch(
        self, project_id, job_id, candidate_ids
    ):
        assert project_id == self.project_id
        assert job_id == self.analysis_job_id
        assert list(candidate_ids) == [self.candidate_id]
        return self.batch


class FakeEasyOcr:
    def __init__(self, mode):
        self.mode = mode
        self.calls = 0

    def infer_candidates(
        self, candidates, run_directory, *, cancel
    ):
        self.calls += 1
        assert run_directory.is_absolute()
        assert not cancel()
        candidate_id = candidates[0]["candidate_id"]
        if self.mode == "failed":
            item = {
                "status": "failed",
                "mode": "scene_fallback",
            }
        else:
            observations = (
                [
                    {
                        "text": "시험터널",
                        "confidence": 0.91,
                        "box": {
                            "x": 0.2,
                            "y": 0.1,
                            "width": 0.4,
                            "height": 0.2,
                        },
                        "kind": (
                            "sign_or_tunnel_text_unverified"
                        ),
                    }
                ]
                if self.mode == "completed"
                else []
            )
            item = {
                "schema_version": (
                    "trafficvisson.vision-inference.v1"
                ),
                "mode": "local_model",
                "status": (
                    "classified"
                    if observations
                    else "no_detection"
                ),
                "provider": "easyocr_ocr_only",
                "pack_id": OCR_PACK_ID,
                "pack_version": "0.1.0",
                "manifest_sha256": "e" * 64,
                "capabilities": ["ocr"],
                "predictions": [],
                "ocr_observations": observations,
                "duration_ms": 10,
                "warning_code": None,
            }
        return {candidate_id: item}, {}


def _detection_context(analysis: FakeAnalysisJobs, capabilities):
    return {
        "job_id": str(uuid4()),
        "project_id": str(analysis.project_id),
        "snapshot_id": str(uuid4()),
        "file_name": "drive.mp4",
        "profile": _profile(),
        "capabilities": capabilities,
        "checkpoints": {
            "candidate_rescan_5_10fps": {
                "analysis_job_id": str(
                    analysis.analysis_job_id
                ),
                "source_sha256": analysis.source_sha256,
            }
        },
        "review_completion_id": None,
    }


@pytest.mark.parametrize(
    ("mode", "expected_status", "expected_outcome"),
    [
        ("completed", "completed", "success"),
        ("zero", "zero_detections", "success"),
        ("failed", "failed", "success"),
    ],
)
def test_detection_stage_runs_separate_easyocr_and_keeps_truth_zero(
    tmp_path, mode, expected_status, expected_outcome
):
    analysis = FakeAnalysisJobs(tmp_path)
    ocr = FakeEasyOcr(mode)
    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        vision_status=lambda: {
            "enabled": False,
            "warning_code": "model_pack_not_configured",
        },
        ocr_inference_service=ocr,
        ocr_status=lambda: _ocr_status(),
        analysis_jobs=analysis,
    )
    capabilities = runtime.capability_snapshot(_profile())
    assert capabilities["detector"] == "missing"
    assert capabilities["ocr"] == "ready"
    assert (
        capabilities["installed_ocr_pack_id"] == OCR_PACK_ID
    )
    assert capabilities["installed_detector_pack_id"] is None

    result = runtime.execute(
        "detection_ocr",
        _detection_context(analysis, capabilities),
        lambda: False,
    )

    assert result.outcome == expected_outcome
    assert result.checkpoint["ocr_status"] == expected_status
    assert result.checkpoint["ocr_pack_id"] == OCR_PACK_ID
    assert (
        result.checkpoint["ocr_provenance"]["engine_role"]
        == "ocr_only"
    )
    assert (
        result.checkpoint["ocr_provenance"]["path_exposed"]
        is False
    )
    assert result.checkpoint["automatic_labeling_allowed"] is False
    assert result.checkpoint["annotation_count"] == 0
    assert result.checkpoint["incident_count"] == 0
    assert ocr.calls == 1
    if mode == "completed":
        rows = result.checkpoint[
            "ocr_observations_by_candidate"
        ][analysis.candidate_id]
        assert rows[0]["text"] == "시험터널"
        assert result.checkpoint["ocr_observation_count"] == 1
    else:
        assert (
            result.checkpoint["ocr_observations_by_candidate"]
            == {}
        )


def test_detection_stage_distinguishes_missing_ocr_pack(
    tmp_path,
):
    analysis = FakeAnalysisJobs(tmp_path)
    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        vision_status=lambda: {
            "enabled": False,
            "warning_code": "model_pack_not_configured",
        },
        analysis_jobs=analysis,
    )
    capabilities = runtime.capability_snapshot(_profile())

    result = runtime.execute(
        "detection_ocr",
        _detection_context(analysis, capabilities),
        lambda: False,
    )

    assert result.outcome == "model_unavailable"
    assert result.checkpoint["ocr_status"] == "model_unavailable"
    assert result.checkpoint["ocr_observation_count"] == 0
    assert result.error_code == "vision_models_unavailable"
    assert result.checkpoint["annotation_count"] == 0
    assert result.checkpoint["incident_count"] == 0


def test_create_app_discovers_installed_easyocr_pack_separately(
    tmp_path, monkeypatch
):
    program_data = tmp_path / "ProgramData"
    manifest = (
        program_data
        / "TrafficVisson"
        / "ModelPacks"
        / "trafficvisson-jaidedai-easyocr-ko-en-0.1.0"
        / "trafficvisson-model-pack.json"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    captured = {}

    class CapturingOcr:
        def __init__(self, *, model_pack_path):
            captured["model_pack_path"] = model_pack_path

        def status(self):
            return _ocr_status()

        def infer_candidates(self, *_args, **_kwargs):
            raise AssertionError("startup must not run OCR")

    class Detector:
        def status(self):
            return {
                "enabled": False,
                "warning_code": "model_pack_not_configured",
            }

        def infer_candidates(self, *_args, **_kwargs):
            raise AssertionError("startup must not run detection")

    monkeypatch.setenv("PROGRAMDATA", str(program_data))
    monkeypatch.delenv(
        "TRAFFICSUPERVISION_OCR_MODEL_PACK", raising=False
    )
    monkeypatch.setattr(
        "app.main.LocalVisionInferenceService",
        CapturingOcr,
    )
    app = create_app(
        TEST_TOKEN,
        fixture_mode=False,
        allow_test_client=True,
        bridge_data_root=str(tmp_path / "bridge"),
        vision_inference_service=Detector(),
    )
    with TestClient(app) as client:
        assert client.app.state.ocr_inference is not None
        assert Path(captured["model_pack_path"]) == manifest
        runtime = client.app.state.pipeline_jobs._runtime
        assert runtime._ocr_inference_service is (
            client.app.state.ocr_inference
        )


def test_create_app_does_not_auto_activate_rejected_pothole_pack(
    tmp_path, monkeypatch
):
    program_data = tmp_path / "ProgramData"
    detector_manifest = (
        program_data
        / "TrafficVisson"
        / "ModelPacks"
        / "trafficvisson-peterhdd-yolov8s-pothole-research-0.1.1"
        / "trafficvisson-model-pack.json"
    )
    detector_manifest.parent.mkdir(parents=True)
    detector_manifest.write_text("{}", encoding="utf-8")
    captured = []

    class CapturingVision:
        def __init__(self, *, model_pack_path):
            captured.append(model_pack_path)
            self._model_pack_path = model_pack_path

        def status(self):
            if (
                self._model_pack_path is not None
                and Path(self._model_pack_path)
                == detector_manifest
            ):
                return {
                    "enabled": True,
                    "mode": "local_model",
                    "pack_id": DETECTOR_PACK_ID,
                    "pack_version": "0.1.1",
                    "engine": "onnx_yolo_v8",
                    "capabilities": ["defect_detection"],
                    "manifest_sha256": "d" * 64,
                    "warning_code": None,
                }
            return {
                "enabled": False,
                "mode": "scene_fallback",
                "warning_code": "model_pack_not_configured",
            }

        def infer_candidates(self, *_args, **_kwargs):
            raise AssertionError("startup must not run local vision")

    monkeypatch.setenv("PROGRAMDATA", str(program_data))
    monkeypatch.delenv(
        "TRAFFICSUPERVISION_VISION_MODEL_PACK", raising=False
    )
    monkeypatch.delenv(
        "TRAFFICSUPERVISION_OCR_MODEL_PACK", raising=False
    )
    monkeypatch.setattr(
        "app.main.LocalVisionInferenceService",
        CapturingVision,
    )
    app = create_app(
        TEST_TOKEN,
        fixture_mode=False,
        allow_test_client=True,
        bridge_data_root=str(tmp_path / "bridge"),
    )
    with TestClient(app) as client:
        assert captured[0] is None
        assert client.app.state.vision_inference.status()["enabled"] is False
        runtime = client.app.state.pipeline_jobs._runtime
        capability = runtime.capability_snapshot(_profile())
        assert capability["detector"] == "missing"


def test_create_app_allows_explicit_reviewed_detector_binding(
    tmp_path, monkeypatch
):
    detector_manifest = tmp_path / "reviewed-detector-pack.json"
    detector_manifest.write_text("{}", encoding="utf-8")
    captured = []

    class CapturingVision:
        def __init__(self, *, model_pack_path):
            captured.append(model_pack_path)
            self._model_pack_path = model_pack_path

        def status(self):
            if (
                self._model_pack_path is not None
                and Path(self._model_pack_path) == detector_manifest
            ):
                return {
                    "enabled": True,
                    "mode": "local_model",
                    "pack_id": DETECTOR_PACK_ID,
                    "pack_version": "0.1.1",
                    "engine": "onnx_yolo_v8",
                    "capabilities": ["defect_detection"],
                    "manifest_sha256": "d" * 64,
                    "warning_code": None,
                }
            return {
                "enabled": False,
                "mode": "scene_fallback",
                "warning_code": "model_pack_not_configured",
            }

        def infer_candidates(self, *_args, **_kwargs):
            raise AssertionError("startup must not run local vision")

    monkeypatch.setenv(
        "TRAFFICSUPERVISION_VISION_MODEL_PACK",
        str(detector_manifest),
    )
    monkeypatch.delenv(
        "TRAFFICSUPERVISION_OCR_MODEL_PACK", raising=False
    )
    monkeypatch.setattr(
        "app.main.LocalVisionInferenceService",
        CapturingVision,
    )
    app = create_app(
        TEST_TOKEN,
        fixture_mode=False,
        allow_test_client=True,
        bridge_data_root=str(tmp_path / "bridge"),
    )
    with TestClient(app) as client:
        assert Path(captured[0]) == detector_manifest
        assert (
            client.app.state.vision_inference.status()["pack_id"]
            == DETECTOR_PACK_ID
        )
        runtime = client.app.state.pipeline_jobs._runtime
        capability = runtime.capability_snapshot(_profile())
        assert capability["detector"] == "ready"
