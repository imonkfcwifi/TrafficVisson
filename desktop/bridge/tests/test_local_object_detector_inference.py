from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.local_object_detector_inference import (
    DenseDetectorFrame,
    DetectorInferenceBox,
    LoadedDetectorHandle,
    PromotedLocalObjectDetectorInference,
)
from app.local_object_detector_trainer import (
    ADAPTER_ARTIFACT_SCHEMA,
    DetectorSample,
)
from app.project_model_lifecycle import (
    LOCAL_OBJECT_DETECTOR_MODEL_FORMAT,
    MODEL_ARTIFACT_SCHEMA_VERSION,
    ProjectModelLifecycleError,
    ResolvedActiveProjectModel,
)


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class _FakeBackend:
    backend_id = "fake-promoted-detector"
    backend_version = "1.0"

    def __init__(self, *, state: str = "ready") -> None:
        self.state = state
        self.loads = []
        self.predictions = []

    def status(self):
        return {
            "state": self.state,
            "backend_id": self.backend_id,
            "backend_version": self.backend_version,
        }

    def load(self, model_directory, *, identity_sha256):
        self.loads.append((model_directory, identity_sha256))
        return LoadedDetectorHandle(
            identity_sha256=identity_sha256,
            value="fake-model",
            id_to_label={0: "pothole", 1: "road_debris"},
        )

    def predict(self, handle, *, body, media_type):
        self.predictions.append((handle, body, media_type))
        return (
            DetectorInferenceBox(
                issue_type="pothole",
                score=0.91,
                normalized_box={
                    "x": 0.1,
                    "y": 0.2,
                    "width": 0.3,
                    "height": 0.4,
                },
            ),
            DetectorInferenceBox(
                issue_type="road_debris",
                score=0.1,
                normalized_box={
                    "x": 0.2,
                    "y": 0.3,
                    "width": 0.1,
                    "height": 0.1,
                },
            ),
        )


def _fixture(tmp_path: Path):
    project_id = str(uuid4())
    training_analysis = str(uuid4())
    validation_analysis = str(uuid4())
    survey_analysis = str(uuid4())
    dataset_id = str(uuid4())
    model_id = str(uuid4())
    model_root = tmp_path / "model"
    detector_root = model_root / "detector"
    detector_root.mkdir(parents=True)
    weight = detector_root / "model.safetensors"
    weight.write_bytes(b"trained-project-detector")
    adapter = {
        "schema_version": ADAPTER_ARTIFACT_SCHEMA,
        "project_id": project_id,
        "training_job_id": str(uuid4()),
        "request_manifest_sha256": "a" * 64,
        "dataset_version_id": dataset_id,
        "dataset_hash": "b" * 64,
        "fixed_validation_sha256": "c" * 64,
        "model_pack_identity": {"manifest_sha256": "d" * 64},
        "dataset_manifest": {"dataset_manifest_sha256": "e" * 64},
        "primary_weight": {
            "path": "detector/model.safetensors",
            "size_bytes": weight.stat().st_size,
            "sha256": _sha(weight.read_bytes()),
        },
        "execution_provenance": {"accelerator": "cuda"},
        "active_box_metrics": {
            "score_threshold": 0.25,
            "map_at_iou": 0.5,
        },
        "candidate_box_metrics": {
            "score_threshold": 0.25,
            "map_at_iou": 0.9,
        },
        "active_incident_prediction_count": 1,
        "candidate_incident_prediction_count": 1,
        "resumed_from_checkpoint_sha256": None,
        "validation_used_for_fit": False,
        "approved_sample_references_used_for_fit": False,
        "automatic_promotion": False,
    }
    primary = model_root / "detector-adapter.json"
    primary.write_text(
        json.dumps(adapter, sort_keys=True),
        encoding="utf-8",
    )
    lineage = {
        "training_analysis_job_ids": [training_analysis],
        "fixed_validation_analysis_job_ids": [validation_analysis],
    }
    lineage_hash = _sha(
        json.dumps(
            lineage,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    manifest = {
        "schema_version": MODEL_ARTIFACT_SCHEMA_VERSION,
        "project_id": project_id,
        "model_version_id": model_id,
        "model_version": "project-detector@aaaaaaaaaaaa",
        "model_format": LOCAL_OBJECT_DETECTOR_MODEL_FORMAT,
        "dataset_version_id": dataset_id,
        "dataset_hash": "b" * 64,
        "fixed_validation_sha256": "c" * 64,
        "primary_artifact": "detector-adapter.json",
        "source_analysis_lineage_sha256": lineage_hash,
    }
    manifest_path = model_root / "artifact-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True),
        encoding="utf-8",
    )
    model = ResolvedActiveProjectModel(
        project_id=project_id,
        state_revision=3,
        model_version_id=model_id,
        model_version="project-detector@aaaaaaaaaaaa",
        model_format=LOCAL_OBJECT_DETECTOR_MODEL_FORMAT,
        dataset_version_id=dataset_id,
        dataset_hash="b" * 64,
        fixed_validation_sha256="c" * 64,
        primary_artifact_path=primary,
        primary_artifact_sha256=_sha(primary.read_bytes()),
        artifact_manifest_path=manifest_path,
        artifact_manifest_sha256=_sha(manifest_path.read_bytes()),
        training_analysis_job_ids=(training_analysis,),
        fixed_validation_analysis_job_ids=(validation_analysis,),
        source_analysis_lineage_sha256=lineage_hash,
    )
    return model, survey_analysis, primary, weight


def test_promoted_detector_scans_dense_frames_as_review_only_boxes(
    tmp_path,
):
    model, survey, _primary, _weight = _fixture(tmp_path)
    backend = _FakeBackend()
    inference = PromotedLocalObjectDetectorInference(backend)
    body = b"\xff\xd8\xffdense-frame"
    results = inference.scan(
        model,
        survey_analysis_job_id=survey,
        frames=(
            DenseDetectorFrame(
                frame_id="frame-000001",
                body=body,
                media_type="image/jpeg",
                sha256=_sha(body),
            ),
            DenseDetectorFrame(
                frame_id="frame-000002",
                body=body + b"2",
                media_type="image/jpeg",
                sha256=_sha(body + b"2"),
            ),
        ),
    )

    assert len(results) == 2
    assert len(backend.loads) == 1
    assert len(backend.predictions) == 2
    assert results[0]["predictions"] == [
        {
            "issue_type": "pothole",
            "score": 0.91,
            "box": {
                "x": 0.1,
                "y": 0.2,
                "width": 0.3,
                "height": 0.4,
            },
            "calibration": "promoted_project_detector_threshold",
        }
    ]
    assert results[0]["dataset_hash"] == model.dataset_hash
    assert (
        results[0]["artifact_manifest_sha256"]
        == model.artifact_manifest_sha256
    )
    assert results[0]["review_only_until_pipeline_question_answer"] is True
    assert results[0]["annotation_created"] is False
    assert results[0]["incident_created"] is False


@pytest.mark.parametrize("lineage_kind", ["training", "validation"])
def test_promoted_detector_rejects_source_analysis_reuse(
    tmp_path,
    lineage_kind,
):
    model, _survey, _primary, _weight = _fixture(tmp_path)
    analysis_id = (
        model.training_analysis_job_ids[0]
        if lineage_kind == "training"
        else model.fixed_validation_analysis_job_ids[0]
    )
    backend = _FakeBackend()
    with pytest.raises(ProjectModelLifecycleError) as error:
        PromotedLocalObjectDetectorInference(backend).classify(
            model,
            survey_analysis_job_id=analysis_id,
            body=b"\xff\xd8\xffframe",
            media_type="image/jpeg",
            expected_sha256=_sha(b"\xff\xd8\xffframe"),
        )
    assert error.value.code == "project_detector_survey_lineage_rejected"
    assert backend.loads == []


def test_promoted_detector_rejects_artifact_and_frame_tamper(tmp_path):
    model, survey, primary, _weight = _fixture(tmp_path)
    primary.write_bytes(primary.read_bytes() + b"tamper")
    inference = PromotedLocalObjectDetectorInference(_FakeBackend())
    with pytest.raises(ProjectModelLifecycleError) as artifact_error:
        inference.classify(
            model,
            survey_analysis_job_id=survey,
            body=b"\xff\xd8\xffframe",
            media_type="image/jpeg",
            expected_sha256=_sha(b"\xff\xd8\xffframe"),
        )
    assert artifact_error.value.code == "project_detector_artifact_changed"

    model, survey, _primary, _weight = _fixture(tmp_path / "fresh")
    with pytest.raises(ProjectModelLifecycleError) as frame_error:
        inference.classify(
            model,
            survey_analysis_job_id=survey,
            body=b"\xff\xd8\xffframe",
            media_type="image/jpeg",
            expected_sha256="f" * 64,
        )
    assert frame_error.value.code == "project_detector_frame_invalid"


def test_promoted_detector_rejects_weight_escape(tmp_path):
    model, survey, primary, _weight = _fixture(tmp_path)
    adapter = json.loads(primary.read_text(encoding="utf-8"))
    adapter["primary_weight"]["path"] = "../outside.safetensors"
    primary.write_text(json.dumps(adapter), encoding="utf-8")
    model = ResolvedActiveProjectModel(
        **{
            **model.__dict__,
            "primary_artifact_sha256": _sha(primary.read_bytes()),
        }
    )
    with pytest.raises(ProjectModelLifecycleError) as error:
        PromotedLocalObjectDetectorInference(_FakeBackend()).classify(
            model,
            survey_analysis_job_id=survey,
            body=b"\xff\xd8\xffframe",
            media_type="image/jpeg",
            expected_sha256=_sha(b"\xff\xd8\xffframe"),
        )
    assert error.value.code == "project_detector_artifact_invalid"


def test_promoted_detector_runtime_unavailable_is_not_zero_success(
    tmp_path,
):
    model, survey, _primary, _weight = _fixture(tmp_path)
    backend = _FakeBackend(state="unavailable")
    inference = PromotedLocalObjectDetectorInference(backend)
    assert inference.status()["state"] == "unavailable"
    with pytest.raises(ProjectModelLifecycleError) as error:
        inference.classify(
            model,
            survey_analysis_job_id=survey,
            body=b"\xff\xd8\xffframe",
            media_type="image/jpeg",
            expected_sha256=_sha(b"\xff\xd8\xffframe"),
        )
    assert error.value.code == "project_detector_runtime_unavailable"


def test_promoted_detector_evaluates_only_its_fixed_validation_lineage(
    tmp_path,
):
    model, _survey, _primary, _weight = _fixture(tmp_path)
    body = b"\xff\xd8\xfffixed-validation"
    image = tmp_path / "validation.jpg"
    image.write_bytes(body)
    sample = DetectorSample(
        sample_id=str(uuid4()),
        annotation_id=str(uuid4()),
        project_id=model.project_id,
        analysis_job_id=model.fixed_validation_analysis_job_ids[0],
        candidate_id="validation-candidate",
        split="validation",
        partition="approved",
        issue_type="pothole",
        image_path=image,
        image_sha256=_sha(body),
        image_size_bytes=len(body),
        media_type="image/jpeg",
        artifact_ref="candidate-evidence:validation:image",
        normalized_box={
            "x": 0.1,
            "y": 0.2,
            "width": 0.3,
            "height": 0.4,
        },
        geometry_sha256="f" * 64,
        group_hashes=(_sha(body),),
    )
    inference = PromotedLocalObjectDetectorInference(_FakeBackend())
    predictions = inference.evaluate_fixed_validation(
        model,
        (sample,),
        fixed_validation_sha256=model.fixed_validation_sha256,
    )
    assert len(predictions) == 1
    assert predictions[0].sample_id == sample.sample_id
    assert predictions[0].issue_type == "pothole"

    with pytest.raises(ProjectModelLifecycleError) as error:
        inference.evaluate_fixed_validation(
            model,
            (sample,),
            fixed_validation_sha256="0" * 64,
        )
    assert (
        error.value.code
        == "project_detector_validation_lineage_rejected"
    )
