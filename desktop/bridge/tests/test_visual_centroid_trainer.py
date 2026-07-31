from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.project_model_lifecycle import (
    ProjectModelLifecycleError,
    ResolvedActiveProjectModel,
    TRAINER_CONTRACT_VERSION,
    TrainerInvocation,
    VISUAL_CENTROID_MODEL_FORMAT,
)
from app.visual_centroid_trainer import (
    VisualEmbeddingCentroidHeadInference,
    VisualEmbeddingCentroidHeadTrainer,
)
from app.weak_case_catalog import ResolvedWeakCaseImage


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class _Embedder:
    def __init__(self, vectors, accelerator="cpu"):
        self.vectors = vectors
        self.calls = 0
        self.accelerator = accelerator

    def model_identity(self):
        return {
            "schema_version": "trafficvisson.image-embedding-model.v1",
            "pack_id": "fixture-clip",
            "pack_version": "1.0",
            "manifest_sha256": "a" * 64,
        }

    def embed_image(self, body: bytes, media_type: str):
        assert media_type == "image/jpeg"
        self.calls += 1
        return self.vectors[body]

    def execution_provenance(self):
        is_cuda = self.accelerator == "cuda"
        identity_sha256 = hashlib.sha256(
            json.dumps(
                self.model_identity(),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "schema_version": (
                "trafficvisson.embedding-execution-provenance.v1"
            ),
            "requested_device": self.accelerator,
            "resolved_device": self.accelerator,
            "accelerator": self.accelerator,
            "accelerator_used": is_cuda,
            "torch_version": "fixture",
            "torch_cuda_version": "fixture" if is_cuda else None,
            "cuda_device_name": "fixture GPU" if is_cuda else None,
            "cuda_total_memory_bytes": 12_000_000_000 if is_cuda else None,
            "model_pack_identity_sha256": identity_sha256,
        }


@dataclass(frozen=True)
class _Artifact:
    path: Path
    media_type: str
    size_bytes: int
    sha256: str


class _ReferenceCatalog:
    def __init__(self, rows):
        self.rows = rows

    def resolve_case_image_for_visual_index(
        self, source_id: str, case_id: str
    ):
        body = self.rows[(source_id, case_id)]
        return ResolvedWeakCaseImage(
            body=body,
            media_type="image/jpeg",
            size_bytes=len(body),
            sha256=_sha(body),
        )


def _annotation(
    *,
    project_id: UUID,
    issue_type,
    partition: str,
    body_name: str,
):
    annotation_id = uuid4()
    return {
        "project_id": str(project_id),
        "annotation_id": str(annotation_id),
        "analysis_job_id": str(uuid4()),
        "candidate_id": hashlib.sha256(
            body_name.encode("utf-8")
        ).hexdigest()[:20],
        "partition": partition,
        "selected_issue_type": issue_type,
    }


def _truth(annotations, required_types):
    rows = []
    for annotation in annotations:
        for issue_type in required_types:
            rows.append(
                {
                    "sample_id": annotation["annotation_id"],
                    "issue_type": issue_type,
                    "truth_event_ids": (
                        [
                            "event-"
                            + annotation["candidate_id"]
                        ]
                        if annotation["selected_issue_type"]
                        == issue_type
                        else []
                    ),
                }
            )
    return tuple(rows)


def _fixture(
    tmp_path,
    *,
    require_gpu_execution=False,
    accelerator="cpu",
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    project_id = uuid4()
    required_types = ("pothole", "road_debris")
    named_annotations = {
        "train-pothole": _annotation(
            project_id=project_id,
            issue_type="pothole",
            partition="approved",
            body_name="train-pothole",
        ),
        "train-debris": _annotation(
            project_id=project_id,
            issue_type="road_debris",
            partition="approved",
            body_name="train-debris",
        ),
        "train-negative": _annotation(
            project_id=project_id,
            issue_type=None,
            partition="negative",
            body_name="train-negative",
        ),
        "validation-pothole": _annotation(
            project_id=project_id,
            issue_type="pothole",
            partition="approved",
            body_name="validation-pothole",
        ),
        "validation-debris": _annotation(
            project_id=project_id,
            issue_type="road_debris",
            partition="approved",
            body_name="validation-debris",
        ),
        "validation-negative": _annotation(
            project_id=project_id,
            issue_type=None,
            partition="negative",
            body_name="validation-negative",
        ),
    }
    vectors = {
        b"train-pothole": (1.0, 0.0),
        b"train-debris": (0.0, 1.0),
        b"train-negative": (-1.0, -1.0),
        b"validation-pothole": (0.95, 0.05),
        b"validation-debris": (0.05, 0.95),
        b"validation-negative": (-0.9, -0.9),
        b"reference-pothole": (0.9, 0.1),
    }
    artifact_by_identity = {}
    for name, annotation in named_annotations.items():
        body = name.encode("utf-8")
        path = tmp_path / f"{name}.jpg"
        path.write_bytes(body)
        artifact_by_identity[
            (
                annotation["analysis_job_id"],
                annotation["candidate_id"],
            )
        ] = _Artifact(
            path=path,
            media_type="image/jpeg",
            size_bytes=len(body),
            sha256=_sha(body),
        )

    def resolve(_project_id, analysis_job_id, candidate_id, kind):
        assert kind == "image"
        return artifact_by_identity[
            (str(analysis_job_id), candidate_id)
        ]

    reference_body = b"reference-pothole"
    reference = {
        "source_id": "sample-source",
        "case_id": "sample-case",
        "issue_type": "pothole",
        "training_weight": 0.25,
        "image_sha256": _sha(reference_body),
    }
    train = tuple(
        named_annotations[name]
        for name in (
            "train-pothole",
            "train-debris",
            "train-negative",
        )
    )
    validation = tuple(
        named_annotations[name]
        for name in (
            "validation-pothole",
            "validation-debris",
            "validation-negative",
        )
    )
    truth = _truth(validation, required_types)
    output = tmp_path / "output"
    output.mkdir()
    invocation = TrainerInvocation(
        schema_version=TRAINER_CONTRACT_VERSION,
        project_id=str(project_id),
        training_job_id=str(uuid4()),
        request_manifest_sha256="b" * 64,
        dataset_version_id=str(uuid4()),
        dataset_hash="c" * 64,
        base_model_version="common-initial@1.0.0",
        required_issue_types=required_types,
        current_training_annotations=train,
        fixed_validation_annotations=validation,
        auxiliary_reference_seeds=(reference,),
        fixed_validation_truth=truth,
        fixed_validation_sha256="d" * 64,
        fixed_validation_membership_sha256="e" * 64,
        output_directory=output,
    )
    embedder = _Embedder(vectors, accelerator=accelerator)
    trainer = VisualEmbeddingCentroidHeadTrainer(
        embedder=embedder,
        candidate_artifact_resolver=resolve,
        approved_reference_catalog=_ReferenceCatalog(
            {
                ("sample-source", "sample-case"): (
                    reference_body
                )
            }
        ),
        require_gpu_execution=require_gpu_execution,
    )
    return invocation, trainer, embedder


def test_real_centroid_head_uses_train_only_and_evaluates_fixed_images(
    tmp_path,
):
    invocation, trainer, embedder = _fixture(tmp_path)
    result = trainer.train(invocation)

    assert result.model_format == VISUAL_CENTROID_MODEL_FORMAT
    assert result.model_version.startswith("project-centroid@")
    assert result.active_prediction_records == ()
    assert {
        (
            row["sample_id"],
            row["issue_type"],
            row["matched_truth_event_id"] is not None,
        )
        for row in result.candidate_prediction_records
    } == {
        (
            invocation.fixed_validation_annotations[0][
                "annotation_id"
            ],
            "pothole",
            True,
        ),
        (
            invocation.fixed_validation_annotations[1][
                "annotation_id"
            ],
            "road_debris",
            True,
        ),
    }
    artifact_path = (
        invocation.output_directory
        / result.primary_artifact_relative_path
    )
    artifact = json.loads(artifact_path.read_bytes())
    assert artifact["fit_split"] == "train_only"
    assert artifact["validation_used_for_fit"] is False
    assert artifact["automatic_promotion"] is False
    assert artifact["embedding_execution_provenance"][
        "resolved_device"
    ] == "cpu"
    assert result.metadata["execution_provenance"][
        "accelerator_used"
    ] is False
    assert artifact["models"]["pothole"][
        "current_positive_count"
    ] == 1
    # Seven unique images: 3 train + 1 approved reference + 3 validation.
    assert embedder.calls == 7


def test_promoted_centroid_inference_keeps_review_boundary(tmp_path):
    invocation, trainer, embedder = _fixture(tmp_path)
    result = trainer.train(invocation)
    artifact_path = (
        invocation.output_directory
        / result.primary_artifact_relative_path
    )
    body = b"validation-pothole"
    model = ResolvedActiveProjectModel(
        project_id=invocation.project_id,
        state_revision=2,
        model_version_id=str(uuid4()),
        model_version=result.model_version,
        model_format=result.model_format,
        dataset_version_id=invocation.dataset_version_id,
        dataset_hash=invocation.dataset_hash,
        fixed_validation_sha256=(
            invocation.fixed_validation_sha256
        ),
        primary_artifact_path=artifact_path,
        primary_artifact_sha256=_sha(artifact_path.read_bytes()),
        artifact_manifest_path=tmp_path / "manifest.json",
        artifact_manifest_sha256="e" * 64,
    )

    inference = VisualEmbeddingCentroidHeadInference(embedder)
    classified = inference.classify(
        model,
        body=body,
        media_type="image/jpeg",
        expected_sha256=_sha(body),
    )

    assert classified["model_version_id"] == (
        model.model_version_id
    )
    assert classified["predictions"][0]["issue_type"] == "pothole"
    assert (
        classified["review_only_until_pipeline_question_answer"]
        is True
    )
    assert classified["annotation_created"] is False
    assert classified["incident_created"] is False


def test_fixed_validation_image_set_must_match_truth(tmp_path):
    invocation, trainer, _embedder = _fixture(tmp_path)
    truncated = TrainerInvocation(
        **{
            **invocation.__dict__,
            "fixed_validation_annotations": (
                invocation.fixed_validation_annotations[:-1]
            ),
        }
    )

    with pytest.raises(ProjectModelLifecycleError) as error:
        trainer.train(truncated)
    assert error.value.code == "fixed_validation_evidence_mismatch"


def test_production_gpu_gate_rejects_cpu_and_records_cuda(tmp_path):
    cpu_invocation, cpu_trainer, _embedder = _fixture(
        tmp_path / "cpu",
        require_gpu_execution=True,
        accelerator="cpu",
    )
    with pytest.raises(ProjectModelLifecycleError) as cpu_error:
        cpu_trainer.train(cpu_invocation)
    assert cpu_error.value.code == "gpu_training_required"
    assert not (
        cpu_invocation.output_directory / "centroid-head.json"
    ).exists()

    cuda_invocation, cuda_trainer, _embedder = _fixture(
        tmp_path / "cuda",
        require_gpu_execution=True,
        accelerator="cuda",
    )
    result = cuda_trainer.train(cuda_invocation)
    artifact = json.loads(
        (
            cuda_invocation.output_directory
            / result.primary_artifact_relative_path
        ).read_bytes()
    )
    execution = artifact["embedding_execution_provenance"]
    assert execution["accelerator"] == "cuda"
    assert execution["accelerator_used"] is True
    assert execution["cuda_device_name"] == "fixture GPU"
