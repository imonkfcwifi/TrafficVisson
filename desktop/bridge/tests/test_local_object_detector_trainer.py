from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.local_object_detector_trainer import (
    ADAPTER_ARTIFACT_SCHEMA,
    EXECUTION_PROVENANCE_SCHEMA,
    LOCAL_OBJECT_DETECTOR_MODEL_FORMAT,
    MODEL_PACK_SCHEMA_VERSION,
    DetectorBackendResult,
    DetectorBoxPrediction,
    DetectorRuntimeInfo,
    LocalCudaObjectDetectorTrainer,
    ResolvedDetectorArtifact,
    ResolvedDerivedDetectorArtifact,
    VerifiedDetectorCheckpoint,
    _pretrained_detector_config_identity,
    load_local_detector_model_pack,
    normalized_xywh_to_coco,
)
from app.external_dfine_training_backend import (
    EXTERNAL_RUNTIME_MANIFEST_SCHEMA,
    ExternalDfineCudaDetectorBackend,
)
from app.derived_detector_geometry import derive_detector_geometry_samples
from app.project_model_lifecycle import (
    ProjectModelLifecycleError,
    ProjectModelLifecycleStore,
    ProjectModelTrainerUnavailable,
    ResolvedActiveProjectModel,
    TRAINER_CONTRACT_VERSION,
    TrainerInvocation,
    TrainerResult,
)
from app.training_geometry import (
    SCHEMA_VERSION,
    TRACKED_FRAME_BINDING_SCHEMA,
    seal_training_geometry,
)
from app.validation_metrics import evaluate_model


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _write_pack(
    root: Path,
    *,
    commercial: bool = True,
    minimum_vram_bytes: int = 8 * 1024 * 1024 * 1024,
) -> tuple[Path, dict[str, Any]]:
    model = root / "model"
    model.mkdir(parents=True)
    files = {
        "NOTICE.txt": b"Apache-2.0 test notice",
        "model/config.json": b"{}",
        "model/model.safetensors": b"real-fake-weight-bytes",
        "model/preprocessor_config.json": b"{}",
    }
    for relative, body in files.items():
        path = root / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    value = {
        "schema_version": MODEL_PACK_SCHEMA_VERSION,
        "pack_id": "trafficvisson-project-detector",
        "pack_version": "1.0.0",
        "architecture": "dfine-small",
        "model_id": "ustc-community/dfine-small-coco",
        "model_version": "2026.07",
        "source": {
            "repository": (
                "https://huggingface.co/"
                "ustc-community/dfine-small-coco"
            ),
            "revision": "a" * 40,
        },
        "license": {
            "weights_spdx": "Apache-2.0",
            "runtime_spdx": "Apache-2.0",
            "commercial_use_confirmed": commercial,
            "redistribution_allowed": False,
            "license_url": (
                "https://www.apache.org/licenses/LICENSE-2.0"
            ),
            "notice_artifact": "NOTICE.txt",
        },
        "artifacts": [
            {
                "path": relative,
                "size_bytes": len(body),
                "sha256": _sha(body),
            }
            for relative, body in sorted(files.items())
        ],
        "model": {
            "directory": "model",
            "labels": [
                {"class_id": 0, "issue_type": "pothole"},
                {"class_id": 1, "issue_type": "road_debris"},
            ],
        },
        "hardware": {
            "requires_cuda": True,
            "minimum_cuda_version": "12.1",
            "minimum_vram_bytes": minimum_vram_bytes,
        },
        "training": {
            "image_size": 640,
            "train_batch_size": 1,
            "gradient_accumulation_steps": 4,
            "epochs": 2,
            "learning_rate": 0.00005,
            "score_threshold": 0.25,
            "iou_threshold": 0.5,
        },
    }
    manifest = root / "trafficsupervision-detector-training-pack.json"
    manifest.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return manifest, value


def test_official_coco_head_is_verified_then_reinitialized_for_road_types(
    tmp_path,
):
    manifest, _value = _write_pack(tmp_path / "pack")
    pack = load_local_detector_model_pack(manifest)
    identity = _pretrained_detector_config_identity(
        pack,
        SimpleNamespace(
            model_type="d_fine",
            id2label={index: f"COCO_{index}" for index in range(80)},
        ),
    )
    assert identity == {
        "model_type": "d_fine",
        "source_label_count": 80,
        "target_label_count": 2,
        "head_reinitialization_required": True,
    }


def test_model_pack_rejects_a_checkpoint_with_another_architecture(
    tmp_path,
):
    manifest, _value = _write_pack(tmp_path / "pack")
    pack = load_local_detector_model_pack(manifest)
    with pytest.raises(
        ValueError,
        match="pretrained detector architecture is invalid",
    ):
        _pretrained_detector_config_identity(
            pack,
            SimpleNamespace(
                model_type="rt_detr_v2",
                id2label={0: "object"},
            ),
        )


class _FakeBackend:
    backend_id = "fake-local-detector"
    backend_version = "1.0"

    def __init__(
        self,
        *,
        accelerator: str = "cuda",
        memory: int = 12 * 1024 * 1024 * 1024,
        cuda_version: str = "12.8",
        wrong_artifact_hash: bool = False,
    ) -> None:
        self.accelerator = accelerator
        self.memory = memory
        self.cuda_version = cuda_version
        self.wrong_artifact_hash = wrong_artifact_hash
        self.requests = []

    def runtime_info(self, _pack):
        cuda = self.accelerator == "cuda"
        return DetectorRuntimeInfo(
            dependencies_ready=True,
            accelerator=self.accelerator,
            resolved_device="cuda:0" if cuda else "cpu",
            accelerator_used=cuda,
            torch_version="2.7.0+cu128",
            transformers_version="4.57.6",
            torch_cuda_version=self.cuda_version if cuda else None,
            cuda_device_name="NVIDIA RTX A2000" if cuda else None,
            cuda_total_memory_bytes=self.memory if cuda else None,
            error_code=None if cuda else "cuda_unavailable",
        )

    def train(self, request, *, progress, cancelled):
        assert cancelled() is False
        self.requests.append(request)
        output = request.output_directory / "detector"
        output.mkdir()
        weight = output / "model.safetensors"
        weight.write_bytes(b"trained-detector-weight")
        progress({"stage": "fake_train", "current": 1, "total": 1})
        predictions = tuple(
            DetectorBoxPrediction(
                sample_id=sample.sample_id,
                issue_type=sample.issue_type,
                score=0.99,
                normalized_box=sample.normalized_box,
            )
            for sample in request.validation_samples
            if sample.partition == "approved"
        )
        execution = {
            "schema_version": EXECUTION_PROVENANCE_SCHEMA,
            "backend_id": self.backend_id,
            "backend_version": self.backend_version,
            "accelerator": "cuda",
            "resolved_device": "cuda:0",
            "accelerator_used": True,
            "torch_version": "2.7.0+cu128",
            "transformers_version": "4.57.6",
            "torch_cuda_version": "12.8",
            "cuda_device_name": "NVIDIA RTX A2000",
            "cuda_total_memory_bytes": self.memory,
            "automatic_mixed_precision": True,
            "gradient_accumulation_steps": 4,
            "hub_upload": False,
            "network_model_download": False,
            "trust_remote_code": False,
        }
        return DetectorBackendResult(
            primary_weight_relative_path=(
                "detector/missing.safetensors"
                if self.wrong_artifact_hash
                else "detector/model.safetensors"
            ),
            active_predictions=predictions,
            candidate_predictions=predictions,
            execution_provenance=execution,
            resumed_from_checkpoint_sha256=(
                request.resume_checkpoint.sha256
                if request.resume_checkpoint is not None
                else None
            ),
        )


def _geometry(
    *,
    image_sha256: str,
    artifact_ref: str,
    box: dict[str, float] | None,
    negative: bool = False,
) -> dict[str, Any]:
    return seal_training_geometry(
        {
            "schema_version": SCHEMA_VERSION,
            "source_kind": (
                "negative_evidence"
                if negative
                else "accepted_ai_proposal"
            ),
            "approval_authority": "local_user",
            "image_sha256": image_sha256,
            "image_artifact_ref": artifact_ref,
            "normalized_box": box,
            "polygon": None,
            "mask_artifact_ref": None,
            "mask_sha256": None,
            "tracked_frame_refs": [],
            "proposal_sha256": "b" * 64,
            "sam2_output_sha256": None,
            "sam2_model_manifest_sha256": None,
        }
    )


def _annotation(
    *,
    project_id: str,
    analysis_job_id: str,
    candidate_id: str,
    split: str,
    issue_type: str | None,
    image_sha256: str,
    artifact_ref: str,
    geometry: bool = True,
) -> dict[str, Any]:
    negative = issue_type is None
    box = (
        None
        if negative
        else {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}
    )
    return {
        "annotation_id": str(uuid4()),
        "project_id": project_id,
        "analysis_job_id": analysis_job_id,
        "candidate_id": candidate_id,
        "split": split,
        "partition": "negative" if negative else "approved",
        "include_in_training": True,
        "selected_issue_type": issue_type,
        "training_geometry": (
            _geometry(
                image_sha256=image_sha256,
                artifact_ref=artifact_ref,
                box=box,
                negative=negative,
            )
            if geometry
            else None
        ),
    }


def _fixture(tmp_path: Path):
    manifest, _value = _write_pack(tmp_path / "pack")
    project_id = str(uuid4())
    training_job_id = str(uuid4())
    train_analysis = str(uuid4())
    validation_analysis = str(uuid4())
    artifacts = {}
    train = []
    validation = []
    for split, analysis_id, target in (
        ("train", train_analysis, train),
        ("validation", validation_analysis, validation),
    ):
        for index, issue_type in enumerate(("pothole", "road_debris")):
            body = (
                b"\xff\xd8\xff"
                + f"{split}-{issue_type}".encode("utf-8")
            )
            image = tmp_path / f"{split}-{index}.jpg"
            image.write_bytes(body)
            candidate_id = f"{split}-candidate-{index}"
            artifact_ref = f"candidate-evidence:overlay:{split}-{index}"
            digest = _sha(body)
            annotation = _annotation(
                project_id=project_id,
                analysis_job_id=analysis_id,
                candidate_id=candidate_id,
                split=split,
                issue_type=issue_type,
                image_sha256=digest,
                artifact_ref=artifact_ref,
            )
            target.append(annotation)
            artifacts[
                (
                    project_id,
                    analysis_id,
                    candidate_id,
                    artifact_ref,
                )
            ] = ResolvedDetectorArtifact(
                path=image,
                media_type="image/jpeg",
                size_bytes=len(body),
                sha256=digest,
                project_id=project_id,
                analysis_job_id=analysis_id,
                candidate_id=candidate_id,
                artifact_ref=artifact_ref,
            )
        body = b"\xff\xd8\xff" + f"{split}-negative".encode("utf-8")
        image = tmp_path / f"{split}-negative.jpg"
        image.write_bytes(body)
        candidate_id = f"{split}-candidate-negative"
        artifact_ref = f"candidate-evidence:overlay:{split}-negative"
        digest = _sha(body)
        target.append(
            _annotation(
                project_id=project_id,
                analysis_job_id=analysis_id,
                candidate_id=candidate_id,
                split=split,
                issue_type=None,
                image_sha256=digest,
                artifact_ref=artifact_ref,
            )
        )
        artifacts[
            (
                project_id,
                analysis_id,
                candidate_id,
                artifact_ref,
            )
        ] = ResolvedDetectorArtifact(
            path=image,
            media_type="image/jpeg",
            size_bytes=len(body),
            sha256=digest,
            project_id=project_id,
            analysis_job_id=analysis_id,
            candidate_id=candidate_id,
            artifact_ref=artifact_ref,
        )
    truth = tuple(
        {
            "sample_id": annotation["annotation_id"],
            "issue_type": issue_type,
            "truth_event_ids": (
                [f"event-{annotation['candidate_id']}"]
                if annotation["selected_issue_type"] == issue_type
                else []
            ),
        }
        for annotation in validation
        for issue_type in ("pothole", "road_debris")
    )

    def resolve(
        project: UUID,
        analysis: UUID,
        candidate: str,
        artifact_ref: str,
    ):
        return artifacts[
            (str(project), str(analysis), candidate, artifact_ref)
        ]

    output = tmp_path / "output"
    output.mkdir()
    invocation = TrainerInvocation(
        schema_version=TRAINER_CONTRACT_VERSION,
        project_id=project_id,
        training_job_id=training_job_id,
        request_manifest_sha256="c" * 64,
        dataset_version_id=str(uuid4()),
        dataset_hash="d" * 64,
        base_model_version="road-common@1.0",
        required_issue_types=("pothole", "road_debris"),
        current_training_annotations=tuple(train),
        fixed_validation_annotations=tuple(validation),
        auxiliary_reference_seeds=(
            {"approved_reference": "must-not-enter-detector-fit"},
        ),
        fixed_validation_truth=truth,
        fixed_validation_sha256="e" * 64,
        fixed_validation_membership_sha256="f" * 64,
        output_directory=output,
    )
    return manifest, invocation, resolve, artifacts


def _with_bound_track(
    tmp_path: Path,
    invocation: TrainerInvocation,
    *,
    tracked_count: int = 2,
):
    annotation = dict(invocation.current_training_annotations[0])
    base_geometry = annotation["training_geometry"]
    refs = []
    resolved = {}
    for index in range(tracked_count):
        body = (
            b"\xff\xd8\xff"
            + f"tracked-fit-{index}".encode("utf-8")
        )
        image = tmp_path / f"tracked-fit-{index}.jpg"
        image.write_bytes(body)
        digest = _sha(body)
        evidence_candidate_id = f"{index + 3}" * 20
        artifact_ref = f"candidate-evidence:track:{index}"
        refs.append(
            {
                "binding_schema": TRACKED_FRAME_BINDING_SCHEMA,
                "evidence_candidate_id": evidence_candidate_id,
                "image_artifact_ref": artifact_ref,
                "frame_id": f"tracked-frame-{index}",
                "image_sha256": digest,
                "timestamp_seconds": 10.25 + index * 0.5,
                "normalized_box": {
                    "x": round(0.1 + index / 100, 2),
                    "y": 0.2,
                    "width": 0.3,
                    "height": 0.4,
                },
                "polygon": None,
                "mask_sha256": None,
            }
        )
    annotation["training_geometry"] = seal_training_geometry(
        {
            "schema_version": SCHEMA_VERSION,
            "source_kind": "user_sam2_refinement",
            "approval_authority": "local_user",
            "image_sha256": base_geometry["image_sha256"],
            "image_artifact_ref": base_geometry["image_artifact_ref"],
            "normalized_box": base_geometry["normalized_box"],
            "polygon": [[0.1, 0.2], [0.4, 0.2], [0.4, 0.6]],
            "mask_artifact_ref": "sam2-mask:accepted-track",
            "mask_sha256": "9" * 64,
            "tracked_frame_refs": refs,
            "proposal_sha256": base_geometry["proposal_sha256"],
            "sam2_output_sha256": "8" * 64,
            "sam2_model_manifest_sha256": "7" * 64,
        }
    )
    invocation = TrainerInvocation(
        **{
            **invocation.__dict__,
            "current_training_annotations": (
                annotation,
                *invocation.current_training_annotations[1:],
            ),
        }
    )
    derived = derive_detector_geometry_samples(
        annotation,
        include_tracked_fit_frames=True,
    )[1:]
    for index, sample in enumerate(derived):
        image = tmp_path / f"tracked-fit-{index}.jpg"
        resolved[
            (
                sample.project_id,
                sample.analysis_job_id,
                sample.candidate_id,
                sample.frame_id,
            )
        ] = ResolvedDerivedDetectorArtifact(
            path=image,
            media_type="image/jpeg",
            size_bytes=image.stat().st_size,
            sha256=sample.image_sha256,
            project_id=sample.project_id,
            analysis_job_id=sample.analysis_job_id,
            candidate_id=sample.candidate_id,
            evidence_candidate_id=sample.evidence_candidate_id,
            artifact_ref=sample.image_artifact_ref,
            event_group_id=sample.event_group_id,
            frame_id=sample.frame_id,
            frame_sha256=sample.frame_sha256,
            box_sha256=sample.box_sha256,
            mask_sha256=sample.mask_sha256,
        )

    def resolver(
        project: UUID,
        analysis: UUID,
        candidate: str,
        sample,
    ):
        return resolved[
            (
                str(project),
                str(analysis),
                candidate,
                sample.frame_id,
            )
        ]

    return invocation, resolver, resolved


def _trainer(
    manifest: Path,
    resolver,
    *,
    backend=None,
    derived_resolver=None,
    minimum_training_geometry=2,
):
    return LocalCudaObjectDetectorTrainer(
        model_pack_manifest=manifest,
        artifact_resolver=resolver,
        derived_artifact_resolver=derived_resolver,
        backend=backend or _FakeBackend(),
        minimum_training_geometry=minimum_training_geometry,
        minimum_validation_geometry_per_type=2,
    )


def test_verified_pack_and_fake_backend_produce_real_detector_result(
    tmp_path,
):
    manifest, invocation, resolver, _artifacts = _fixture(tmp_path)
    backend = _FakeBackend()
    progress = []
    trainer = LocalCudaObjectDetectorTrainer(
        model_pack_manifest=manifest,
        artifact_resolver=resolver,
        backend=backend,
        progress_hook=progress.append,
        minimum_training_geometry=2,
        minimum_validation_geometry_per_type=2,
    )

    assert trainer.status()["state"] == "ready"
    result = trainer.train(invocation)

    assert result.model_format == LOCAL_OBJECT_DETECTOR_MODEL_FORMAT
    assert result.active_prediction_records
    assert result.candidate_prediction_records
    assert (invocation.output_directory / "detector-adapter.json").is_file()
    artifact = json.loads(
        (
            invocation.output_directory / "detector-adapter.json"
        ).read_text(encoding="utf-8")
    )
    assert artifact["schema_version"] == ADAPTER_ARTIFACT_SCHEMA
    assert artifact["primary_weight"]["size_bytes"] > 0
    assert artifact["validation_used_for_fit"] is False
    assert artifact["approved_sample_references_used_for_fit"] is False
    assert backend.requests[0].train_samples
    assert backend.requests[0].validation_samples
    assert not {
        item.sample_id for item in backend.requests[0].train_samples
    }.intersection(
        item.sample_id
        for item in backend.requests[0].validation_samples
    )
    assert progress == [{"stage": "fake_train", "current": 1, "total": 1}]


def test_retraining_compares_the_verified_promoted_detector(
    tmp_path,
):
    manifest, invocation, resolver, _artifacts = _fixture(tmp_path)

    class ActiveEvaluator:
        def __init__(self):
            self.calls = []

        def evaluate_fixed_validation(
            self,
            model,
            samples,
            *,
            fixed_validation_sha256,
        ):
            self.calls.append(
                (model, tuple(samples), fixed_validation_sha256)
            )
            return tuple(
                DetectorBoxPrediction(
                    sample_id=sample.sample_id,
                    issue_type=sample.issue_type,
                    score=0.75,
                    normalized_box=sample.normalized_box,
                )
                for sample in samples
                if sample.partition == "approved"
            )

    evaluator = ActiveEvaluator()
    active = ResolvedActiveProjectModel(
        project_id=invocation.project_id,
        state_revision=2,
        model_version_id=str(uuid4()),
        model_version=invocation.base_model_version,
        model_format=LOCAL_OBJECT_DETECTOR_MODEL_FORMAT,
        dataset_version_id=str(uuid4()),
        dataset_hash="1" * 64,
        fixed_validation_sha256=invocation.fixed_validation_sha256,
        primary_artifact_path=tmp_path / "active-adapter.json",
        primary_artifact_sha256="2" * 64,
        artifact_manifest_path=tmp_path / "active-manifest.json",
        artifact_manifest_sha256="3" * 64,
        training_analysis_job_ids=(
            str(uuid4()),
        ),
        fixed_validation_analysis_job_ids=tuple(
            sorted(
                {
                    row["analysis_job_id"]
                    for row in invocation.fixed_validation_annotations
                }
            )
        ),
        source_analysis_lineage_sha256="4" * 64,
    )
    result = LocalCudaObjectDetectorTrainer(
        model_pack_manifest=manifest,
        artifact_resolver=resolver,
        backend=_FakeBackend(),
        active_model_resolver=lambda _project_id: active,
        active_model_evaluator=evaluator,
        minimum_training_geometry=2,
        minimum_validation_geometry_per_type=2,
    ).train(invocation)
    assert len(evaluator.calls) == 1
    assert (
        result.metadata["active_evaluation_mode"]
        == "verified_promoted_detector"
    )
    assert result.active_prediction_records


def test_classification_only_training_label_is_excluded_not_gate_credit(
    tmp_path,
):
    manifest, invocation, resolver, _artifacts = _fixture(tmp_path)
    classification_only = {
        **invocation.current_training_annotations[0],
        "annotation_id": str(uuid4()),
        "candidate_id": "classification-only",
        "training_geometry": None,
    }
    invocation = TrainerInvocation(
        **{
            **invocation.__dict__,
            "current_training_annotations": (
                *invocation.current_training_annotations,
                classification_only,
            ),
        }
    )
    backend = _FakeBackend()
    result = _trainer(
        manifest,
        resolver,
        backend=backend,
    ).train(invocation)
    assert result.metadata[
        "classification_only_training_member_count"
    ] == 1
    assert len(backend.requests[0].train_samples) == 3


def test_bound_track_increases_fit_samples_without_increasing_annotation_gate(
    tmp_path,
):
    manifest, invocation, resolver, _artifacts = _fixture(tmp_path)
    invocation, derived_resolver, _derived = _with_bound_track(
        tmp_path,
        invocation,
    )
    backend = _FakeBackend()

    result = _trainer(
        manifest,
        resolver,
        backend=backend,
        derived_resolver=derived_resolver,
    ).train(invocation)

    samples = backend.requests[0].train_samples
    assert len(samples) == 5
    assert len({sample.annotation_id for sample in samples}) == 3
    assert sum(
        sample.source_frame_kind == "sam2_tracked_frame"
        for sample in samples
    ) == 2
    assert all(
        sample.split == "train"
        for sample in samples
        if sample.source_frame_kind == "sam2_tracked_frame"
    )
    assert result.metadata["detector_training_geometry_count"] == 3
    assert result.metadata["detector_fit_sample_count"] == 5
    assert result.metadata["derived_tracked_fit_sample_count"] == 2
    assert (
        result.metadata["derived_frames_count_toward_training_gate"]
        is False
    )
    assert all(
        sample.source_frame_kind == "representative"
        for sample in backend.requests[0].validation_samples
    )


def test_tracked_frames_cannot_satisfy_unique_annotation_minimum(tmp_path):
    manifest, invocation, resolver, _artifacts = _fixture(tmp_path)
    invocation, derived_resolver, _derived = _with_bound_track(
        tmp_path,
        invocation,
        tracked_count=6,
    )

    with pytest.raises(ProjectModelLifecycleError) as error:
        _trainer(
            manifest,
            resolver,
            derived_resolver=derived_resolver,
            minimum_training_geometry=4,
        ).train(invocation)

    assert error.value.code == "insufficient_detector_training_geometry"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("missing", "training_evidence_unavailable"),
        ("cross_event", "training_evidence_boundary_violation"),
        ("cross_candidate", "training_evidence_boundary_violation"),
        ("cross_project", "training_evidence_boundary_violation"),
        ("cross_job", "training_evidence_boundary_violation"),
        ("cross_artifact", "training_evidence_boundary_violation"),
        ("cross_sha", "training_evidence_boundary_violation"),
    ],
)
def test_tracked_frame_resolution_is_fail_closed(
    tmp_path,
    mutation,
    expected_code,
):
    manifest, invocation, resolver, _artifacts = _fixture(tmp_path)
    invocation, derived_resolver, _derived = _with_bound_track(
        tmp_path,
        invocation,
    )

    def changed(project, analysis, candidate, sample):
        if mutation == "missing":
            raise KeyError(sample.frame_id)
        value = derived_resolver(project, analysis, candidate, sample)
        if mutation == "cross_event":
            return ResolvedDerivedDetectorArtifact(
                **{
                    **value.__dict__,
                    "event_group_id": "event_" + "0" * 32,
                }
            )
        replacements = {
            "cross_candidate": {"candidate_id": "0" * 20},
            "cross_project": {"project_id": str(uuid4())},
            "cross_job": {"analysis_job_id": str(uuid4())},
            "cross_artifact": {
                "artifact_ref": "candidate-evidence:other"
            },
            "cross_sha": {"sha256": "0" * 64},
        }
        return ResolvedDerivedDetectorArtifact(
            **{
                **value.__dict__,
                **replacements[mutation],
            }
        )

    with pytest.raises(ProjectModelLifecycleError) as error:
        _trainer(
            manifest,
            resolver,
            derived_resolver=changed,
        ).train(invocation)
    assert error.value.code == expected_code


def test_tracked_frame_file_tamper_is_rejected(tmp_path):
    manifest, invocation, resolver, _artifacts = _fixture(tmp_path)
    invocation, derived_resolver, derived = _with_bound_track(
        tmp_path,
        invocation,
    )
    first = next(iter(derived.values()))
    first.path.write_bytes(first.path.read_bytes() + b"tampered")

    with pytest.raises(ProjectModelLifecycleError) as error:
        _trainer(
            manifest,
            resolver,
            derived_resolver=derived_resolver,
        ).train(invocation)
    assert error.value.code == "training_evidence_integrity_failed"


def test_classification_only_fixed_validation_is_fail_closed(tmp_path):
    manifest, invocation, resolver, _artifacts = _fixture(tmp_path)
    changed = dict(invocation.fixed_validation_annotations[0])
    changed["training_geometry"] = None
    invocation = TrainerInvocation(
        **{
            **invocation.__dict__,
            "fixed_validation_annotations": (
                changed,
                *invocation.fixed_validation_annotations[1:],
            ),
        }
    )
    with pytest.raises(ProjectModelLifecycleError) as error:
        _trainer(manifest, resolver).train(invocation)
    assert error.value.code == "fixed_validation_geometry_incomplete"


def test_detector_rejects_cross_project_artifact_resolution(tmp_path):
    manifest, invocation, resolver, _artifacts = _fixture(tmp_path)

    def crossing(project, analysis, candidate, artifact_ref):
        original = resolver(project, analysis, candidate, artifact_ref)
        return ResolvedDetectorArtifact(
            **{
                **original.__dict__,
                "project_id": str(uuid4()),
            }
        )

    with pytest.raises(ProjectModelLifecycleError) as error:
        _trainer(manifest, crossing).train(invocation)
    assert error.value.code == "training_evidence_boundary_violation"


def test_detector_rejects_evidence_hash_tamper(tmp_path):
    manifest, invocation, resolver, artifacts = _fixture(tmp_path)
    first = next(iter(artifacts.values()))
    first.path.write_bytes(first.path.read_bytes() + b"tampered")
    with pytest.raises(ProjectModelLifecycleError) as error:
        _trainer(manifest, resolver).train(invocation)
    assert error.value.code == "training_evidence_integrity_failed"


def test_detector_rejects_split_group_image_leakage(tmp_path):
    manifest, invocation, resolver, artifacts = _fixture(tmp_path)
    train = invocation.current_training_annotations[0]
    validation = dict(invocation.fixed_validation_annotations[0])
    train_geometry = train["training_geometry"]
    validation["training_geometry"] = _geometry(
        image_sha256=train_geometry["image_sha256"],
        artifact_ref=validation["training_geometry"]["image_artifact_ref"],
        box=validation["training_geometry"]["normalized_box"],
    )
    key = next(
        key
        for key in artifacts
        if key[2] == validation["candidate_id"]
    )
    original = artifacts[key]
    train_artifact = next(
        value
        for value in artifacts.values()
        if value.candidate_id == train["candidate_id"]
    )
    artifacts[key] = ResolvedDetectorArtifact(
        **{
            **original.__dict__,
            "path": train_artifact.path,
            "size_bytes": train_artifact.size_bytes,
            "sha256": train_artifact.sha256,
        }
    )
    invocation = TrainerInvocation(
        **{
            **invocation.__dict__,
            "fixed_validation_annotations": (
                validation,
                *invocation.fixed_validation_annotations[1:],
            ),
        }
    )
    with pytest.raises(ProjectModelLifecycleError) as error:
        _trainer(manifest, resolver).train(invocation)
    assert error.value.code == "detector_split_leakage"


def test_same_event_identity_cannot_cross_train_and_validation(tmp_path):
    manifest, invocation, resolver, artifacts = _fixture(tmp_path)
    train = invocation.current_training_annotations[0]
    validation = dict(invocation.fixed_validation_annotations[0])
    original_key = next(
        key
        for key in artifacts
        if key[2] == validation["candidate_id"]
    )
    original = artifacts[original_key]
    validation["analysis_job_id"] = train["analysis_job_id"]
    validation["candidate_id"] = train["candidate_id"]
    rebound_key = (
        invocation.project_id,
        train["analysis_job_id"],
        train["candidate_id"],
        validation["training_geometry"]["image_artifact_ref"],
    )
    artifacts[rebound_key] = ResolvedDetectorArtifact(
        **{
            **original.__dict__,
            "analysis_job_id": train["analysis_job_id"],
            "candidate_id": train["candidate_id"],
        }
    )
    invocation = TrainerInvocation(
        **{
            **invocation.__dict__,
            "fixed_validation_annotations": (
                validation,
                *invocation.fixed_validation_annotations[1:],
            ),
        }
    )

    with pytest.raises(ProjectModelLifecycleError) as error:
        _trainer(manifest, resolver).train(invocation)
    assert error.value.code == "detector_split_leakage"


def test_detector_pack_manifest_tamper_and_license_fail_closed(tmp_path):
    manifest, _value = _write_pack(tmp_path / "tamper")
    pack = load_local_detector_model_pack(manifest)
    assert pack.commercial_use_confirmed is True
    (pack.root / "model" / "model.safetensors").write_bytes(b"changed")
    with pytest.raises(ValueError, match="integrity"):
        load_local_detector_model_pack(manifest)

    blocked, _value = _write_pack(
        tmp_path / "blocked-license",
        commercial=False,
    )
    trainer = LocalCudaObjectDetectorTrainer(
        model_pack_manifest=blocked,
        artifact_resolver=lambda *_args: None,
        backend=_FakeBackend(),
    )
    assert trainer.status()["state"] == "failed"
    assert trainer.status()["code"] == "detector_model_pack_invalid"


def test_detector_pack_rejects_path_traversal(tmp_path):
    manifest, value = _write_pack(tmp_path / "escape")
    value["artifacts"][0]["path"] = "../escape.bin"
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="unsafe"):
        load_local_detector_model_pack(manifest)


@pytest.mark.parametrize(
    ("backend", "expected_code"),
    [
        (_FakeBackend(accelerator="cpu"), "detector_cuda_required"),
        (
            _FakeBackend(memory=2 * 1024 * 1024 * 1024),
            "detector_vram_insufficient",
        ),
        (
            _FakeBackend(cuda_version="11.8"),
            "detector_cuda_version_unsupported",
        ),
    ],
)
def test_detector_runtime_cpu_and_vram_are_distinct_failures(
    tmp_path,
    backend,
    expected_code,
):
    manifest, _value = _write_pack(tmp_path / expected_code)
    trainer = LocalCudaObjectDetectorTrainer(
        model_pack_manifest=manifest,
        artifact_resolver=lambda *_args: None,
        backend=backend,
    )
    assert trainer.available is False
    assert trainer.status()["code"] == expected_code
    with pytest.raises(ProjectModelTrainerUnavailable):
        trainer.train(None)


def test_detector_unconfigured_is_unavailable_not_empty_success():
    trainer = LocalCudaObjectDetectorTrainer(
        model_pack_manifest=None,
        artifact_resolver=lambda *_args: None,
        backend=_FakeBackend(),
    )
    assert trainer.status()["state"] == "unavailable"
    assert trainer.available is False
    with pytest.raises(ProjectModelTrainerUnavailable):
        trainer.train(None)


def test_normalized_box_conversion_is_coco_xywh():
    assert normalized_xywh_to_coco(
        {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4},
        image_width=1000,
        image_height=500,
    ) == (100.0, 100.0, 300.0, 200.0)


def test_backend_missing_weight_is_not_reported_as_success(tmp_path):
    manifest, invocation, resolver, _artifacts = _fixture(tmp_path)
    with pytest.raises(ProjectModelLifecycleError) as error:
        _trainer(
            manifest,
            resolver,
            backend=_FakeBackend(wrong_artifact_hash=True),
        ).train(invocation)
    assert error.value.code == "detector_artifact_invalid"


def test_cancel_hook_stops_before_backend_and_checkpoint_is_bound(tmp_path):
    manifest, invocation, resolver, _artifacts = _fixture(tmp_path)
    backend = _FakeBackend()
    cancelled = LocalCudaObjectDetectorTrainer(
        model_pack_manifest=manifest,
        artifact_resolver=resolver,
        backend=backend,
        cancel_hook=lambda: True,
        minimum_training_geometry=2,
        minimum_validation_geometry_per_type=2,
    )
    with pytest.raises(ProjectModelLifecycleError) as error:
        cancelled.train(invocation)
    assert error.value.code == "detector_training_cancelled"
    assert backend.requests == []

    checkpoint = tmp_path / "checkpoint.pt"
    checkpoint.write_bytes(b"server-owned-checkpoint")
    digest = _sha(checkpoint.read_bytes())

    def resolve_checkpoint(project, job, request_hash):
        return VerifiedDetectorCheckpoint(
            path=checkpoint,
            sha256=digest,
            project_id=str(project),
            training_job_id=str(job),
            request_manifest_sha256=request_hash,
        )

    backend = _FakeBackend()
    resumed = LocalCudaObjectDetectorTrainer(
        model_pack_manifest=manifest,
        artifact_resolver=resolver,
        backend=backend,
        checkpoint_resolver=resolve_checkpoint,
        minimum_training_geometry=2,
        minimum_validation_geometry_per_type=2,
    ).train(invocation)
    assert backend.requests[0].resume_checkpoint.sha256 == digest
    artifact = json.loads(
        (
            invocation.output_directory / "detector-adapter.json"
        ).read_text(encoding="utf-8")
    )
    assert artifact["resumed_from_checkpoint_sha256"] == digest
    assert resumed.metadata["execution_provenance"]["accelerator"] == "cuda"


def _lifecycle_detector_pack_identity():
    return {
        "schema_version": (
            "trafficvisson.local-object-detector-training-pack.v1"
        ),
        "pack_id": "fixture-detector-pack",
        "pack_version": "1.0.0",
        "architecture": "dfine-small",
        "model_id": "ustc-community/dfine-small-coco",
        "model_version": "1.0.0",
        "source_repository": (
            "https://huggingface.co/"
            "ustc-community/dfine-small-coco"
        ),
        "source_revision": "1" * 40,
        "weights_spdx": "Apache-2.0",
        "runtime_spdx": "Apache-2.0",
        "commercial_use_confirmed": True,
        "redistribution_allowed": True,
        "manifest_sha256": "2" * 64,
        "artifact_count": 3,
    }


def test_lifecycle_finalizer_accepts_detector_format_and_fixed_metrics(
    tmp_path,
):
    project_id = str(uuid4())
    training_job_id = str(uuid4())
    training_analysis = str(uuid4())
    validation_analysis = str(uuid4())
    truth = tuple(
        {
            "sample_id": f"sample-{index:02d}",
            "issue_type": "pothole",
            "truth_event_ids": (f"truth-{index:02d}",),
        }
        for index in range(30)
    )
    active = tuple(
        {
            "prediction_event_id": f"active-{index:02d}",
            "sample_id": f"sample-{index:02d}",
            "issue_type": "pothole",
            "matched_truth_event_id": f"truth-{index:02d}",
        }
        for index in range(30)
    )
    candidate = tuple(
        {
            **row,
            "prediction_event_id": row["prediction_event_id"].replace(
                "active", "candidate"
            ),
        }
        for row in active
    )
    validation_hash = evaluate_model(
        model_version="road-common@1.0",
        truth_records=truth,
        prediction_records=active,
    ).validation_set_sha256
    attempt = tmp_path / "artifacts" / "attempt-1"
    staging = attempt / "staging"
    staging.mkdir(parents=True)
    (staging / "detector-adapter.json").write_text(
        '{"trained":true}',
        encoding="utf-8",
    )
    invocation = TrainerInvocation(
        schema_version=TRAINER_CONTRACT_VERSION,
        project_id=project_id,
        training_job_id=training_job_id,
        request_manifest_sha256="a" * 64,
        dataset_version_id=str(uuid4()),
        dataset_hash="b" * 64,
        base_model_version="road-common@1.0",
        required_issue_types=("pothole",),
        current_training_annotations=(
            {"analysis_job_id": training_analysis},
        ),
        fixed_validation_annotations=tuple(
            {"analysis_job_id": validation_analysis} for _ in range(30)
        ),
        auxiliary_reference_seeds=(),
        fixed_validation_truth=truth,
        fixed_validation_sha256=validation_hash,
        fixed_validation_membership_sha256="c" * 64,
        output_directory=staging,
    )
    result = TrainerResult(
        model_version="project-detector@aaaaaaaaaaaa",
        model_format=LOCAL_OBJECT_DETECTOR_MODEL_FORMAT,
        primary_artifact_relative_path="detector-adapter.json",
        active_prediction_records=active,
        candidate_prediction_records=candidate,
        metadata={
            "adapter_role": "contract-test",
            "model_pack_identity": (
                _lifecycle_detector_pack_identity()
            ),
            "execution_provenance": {
                "accelerator": "cuda",
                "accelerator_used": True,
                "resolved_device": "cuda:0",
            },
        },
    )
    store = object.__new__(ProjectModelLifecycleStore)
    store._root = tmp_path / "artifacts"
    store._trainer = type(
        "_DetectorTrainerIdentity",
        (),
        {
            "trainer_id": "local-cuda-object-detector",
            "trainer_version": "1.0-test",
            "test_only": False,
        },
    )()

    cpu_result = TrainerResult(
        model_version=result.model_version,
        model_format=result.model_format,
        primary_artifact_relative_path=(
            result.primary_artifact_relative_path
        ),
        active_prediction_records=result.active_prediction_records,
        candidate_prediction_records=result.candidate_prediction_records,
        metadata={
            "adapter_role": "contract-test",
            "model_pack_identity": (
                _lifecycle_detector_pack_identity()
            ),
            "execution_provenance": {
                "accelerator": "cpu",
                "accelerator_used": False,
                "resolved_device": "cpu",
            },
        },
    )
    with pytest.raises(
        ProjectModelLifecycleError,
        match="CPU fallback is forbidden",
    ):
        store._finalize_candidate(
            job={
                "request_sha256": "a" * 64,
                "artifact_storage": None,
                "attempt_count": 1,
            },
            invocation=invocation,
            result=cpu_result,
            staging=staging,
            attempt_root=attempt,
        )

    missing_pack_result = TrainerResult(
        model_version=result.model_version,
        model_format=result.model_format,
        primary_artifact_relative_path=(
            result.primary_artifact_relative_path
        ),
        active_prediction_records=result.active_prediction_records,
        candidate_prediction_records=result.candidate_prediction_records,
        metadata={
            "execution_provenance": {
                "accelerator": "cuda",
                "accelerator_used": True,
                "resolved_device": "cuda:0",
            },
        },
    )
    with pytest.raises(ProjectModelLifecycleError) as missing_pack:
        store._finalize_candidate(
            job={
                "request_sha256": "a" * 64,
                "artifact_storage": None,
                "attempt_count": 1,
            },
            invocation=invocation,
            result=missing_pack_result,
            staging=staging,
            attempt_root=attempt,
        )
    assert missing_pack.value.code == "model_pack_provenance_invalid"

    changed_license = _lifecycle_detector_pack_identity()
    changed_license["commercial_use_confirmed"] = False
    invalid_pack_result = TrainerResult(
        model_version=result.model_version,
        model_format=result.model_format,
        primary_artifact_relative_path=(
            result.primary_artifact_relative_path
        ),
        active_prediction_records=result.active_prediction_records,
        candidate_prediction_records=result.candidate_prediction_records,
        metadata={
            "model_pack_identity": changed_license,
            "execution_provenance": {
                "accelerator": "cuda",
                "accelerator_used": True,
                "resolved_device": "cuda:0",
            },
        },
    )
    with pytest.raises(ProjectModelLifecycleError) as invalid_pack:
        store._finalize_candidate(
            job={
                "request_sha256": "a" * 64,
                "artifact_storage": None,
                "attempt_count": 1,
            },
            invocation=invocation,
            result=invalid_pack_result,
            staging=staging,
            attempt_root=attempt,
        )
    assert invalid_pack.value.code == "model_pack_provenance_invalid"

    mismatched_execution_pack = TrainerResult(
        model_version=result.model_version,
        model_format=result.model_format,
        primary_artifact_relative_path=(
            result.primary_artifact_relative_path
        ),
        active_prediction_records=result.active_prediction_records,
        candidate_prediction_records=result.candidate_prediction_records,
        metadata={
            "model_pack_identity": (
                _lifecycle_detector_pack_identity()
            ),
            "execution_provenance": {
                "accelerator": "cuda",
                "accelerator_used": True,
                "resolved_device": "cuda:0",
                "model_pack_manifest_sha256": "9" * 64,
            },
        },
    )
    with pytest.raises(ProjectModelLifecycleError) as mismatched_pack:
        store._finalize_candidate(
            job={
                "request_sha256": "a" * 64,
                "artifact_storage": None,
                "attempt_count": 1,
            },
            invocation=invocation,
            result=mismatched_execution_pack,
            staging=staging,
            attempt_root=attempt,
        )
    assert mismatched_pack.value.code == "model_pack_provenance_invalid"

    model = store._finalize_candidate(
        job={
            "request_sha256": "a" * 64,
            "artifact_storage": None,
            "attempt_count": 1,
        },
        invocation=invocation,
        result=result,
        staging=staging,
        attempt_root=attempt,
    )

    assert model["model_format"] == LOCAL_OBJECT_DETECTOR_MODEL_FORMAT
    assert model["validation_decision"] == "eligible"
    manifest_path = attempt / "model" / "artifact-manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert model["model_pack_identity"] == (
        _lifecycle_detector_pack_identity()
    )
    assert model["model_pack_identity_sha256"] == manifest[
        "model_pack_identity_sha256"
    ]
    assert model["training_runtime_provenance_sha256"] == manifest[
        "training_runtime_provenance_sha256"
    ]
    assert manifest["model_pack_identity"] == (
        _lifecycle_detector_pack_identity()
    )


_EXTERNAL_ENTRYPOINT = r"""
import argparse
import hashlib
import json
from pathlib import Path
import time

BEHAVIOR = "__BEHAVIOR__"
SCHEMA = "trafficvisson.external-dfine-training-response.v1"

parser = argparse.ArgumentParser()
parser.add_argument("--trafficvisson-mode", required=True)
parser.add_argument("--request", required=True)
parser.add_argument("--result", required=True)
parser.add_argument("--progress", required=True)
args = parser.parse_args()

request = json.loads(Path(args.request).read_text(encoding="utf-8"))
result_path = Path(args.result)
progress_path = Path(args.progress)

if args.trafficvisson_mode == "probe":
    result = {
        "schema_version": SCHEMA,
        "mode": "probe",
        "status": "ready",
        "runtime_id": request["runtime"]["runtime_id"],
        "runtime_version": request["runtime"]["runtime_version"],
        "runtime_manifest_sha256": request["runtime"]["manifest_sha256"],
        "model_pack_manifest_sha256": (
            request["model_pack"]["manifest_sha256"]
        ),
        "runtime": {
            "dependencies_ready": True,
            "accelerator": "cuda",
            "resolved_device": "cuda:0",
            "accelerator_used": True,
            "torch_version": "2.7.0+cu128",
            "transformers_version": "4.57.6",
            "torch_cuda_version": "12.8",
            "cuda_device_name": "NVIDIA RTX A2000",
            "cuda_total_memory_bytes": 12 * 1024 * 1024 * 1024,
            "error_code": None,
        },
    }
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    raise SystemExit(0)

if BEHAVIOR == "sleep":
    time.sleep(3)
if BEHAVIOR == "log_flood":
    print("X" * 8192, flush=True)

output = Path(request["output_directory"]) / "detector"
output.mkdir(parents=True)
weight = output / "model.safetensors"
weight.write_bytes(b"external-real-fake-dfine-weight")
weight_body = weight.read_bytes()
progress_path.write_text(
    json.dumps(
        {
            "stage": "fine_tune",
            "epoch": 1,
            "epochs": 2,
            "current": 1,
            "total": 1,
            "loss": 0.125,
        },
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
binding = dict(request["binding"])
binding["training_input_sha256"] = request["training_input_sha256"]
if BEHAVIOR == "wrong_binding":
    binding["dataset_hash"] = "0" * 64
predictions = [
    {
        "sample_id": sample["sample_id"],
        "issue_type": sample["issue_type"],
        "score": 0.99,
        "normalized_box": sample["normalized_box"],
    }
    for sample in request["validation"]
    if sample["partition"] == "approved"
]
result = {
    "schema_version": SCHEMA,
    "mode": "train",
    "status": "completed",
    "runtime_id": request["runtime"]["runtime_id"],
    "runtime_version": request["runtime"]["runtime_version"],
    "runtime_manifest_sha256": request["runtime"]["manifest_sha256"],
    "binding": binding,
    "primary_weight": {
        "path": "detector/model.safetensors",
        "size_bytes": len(weight_body),
        "sha256": hashlib.sha256(weight_body).hexdigest(),
    },
    "active_predictions": [],
    "candidate_predictions": predictions,
    "execution": {
        "accelerator": "cuda",
        "resolved_device": "cuda:0",
        "accelerator_used": True,
        "torch_version": "2.7.0+cu128",
        "transformers_version": "4.57.6",
        "torch_cuda_version": "12.8",
        "cuda_device_name": "NVIDIA RTX A2000",
        "cuda_total_memory_bytes": 12 * 1024 * 1024 * 1024,
        "automatic_mixed_precision": True,
        "gradient_accumulation_steps": (
            request["configuration"]["gradient_accumulation_steps"]
        ),
        "hub_upload": False,
        "network_model_download": False,
        "trust_remote_code": False,
    },
    "resumed_from_checkpoint_sha256": (
        request["resume_checkpoint"]["sha256"]
        if request["resume_checkpoint"] is not None
        else None
    ),
}
result_path.write_text(
    json.dumps(result, ensure_ascii=False, sort_keys=True),
    encoding="utf-8",
)
"""


def _write_external_runtime(
    root: Path,
    *,
    behavior: str = "success",
    training_timeout_seconds: int = 5,
    max_log_bytes: int = 4096,
) -> tuple[Path, Path]:
    root.mkdir(parents=True)
    entrypoint = root / "fake_dfine_entrypoint.py"
    entrypoint.write_text(
        _EXTERNAL_ENTRYPOINT.replace("__BEHAVIOR__", behavior),
        encoding="utf-8",
    )
    executable = Path(sys.executable).resolve(strict=True)
    manifest = root / "external-dfine-runtime.json"
    value = {
        "schema_version": EXTERNAL_RUNTIME_MANIFEST_SCHEMA,
        "runtime_id": "trafficvisson-dfine-training-entrypoint",
        "runtime_version": "1.0.0",
        "protocol_version": "1.0",
        "executable": {
            "path": str(executable),
            "size_bytes": executable.stat().st_size,
            "sha256": _sha(executable.read_bytes()),
        },
        "entrypoint": {
            "path": str(entrypoint.resolve(strict=True)),
            "size_bytes": entrypoint.stat().st_size,
            "sha256": _sha(entrypoint.read_bytes()),
        },
        "limits": {
            "probe_timeout_seconds": 5,
            "training_timeout_seconds": training_timeout_seconds,
            "max_log_bytes": max_log_bytes,
            "poll_interval_ms": 20,
        },
    }
    manifest.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return manifest, entrypoint


def _external_backend(
    manifest: Path,
) -> ExternalDfineCudaDetectorBackend:
    return ExternalDfineCudaDetectorBackend(
        manifest,
        trusted_manifest_sha256=_sha(manifest.read_bytes()),
    )


def test_hash_pinned_external_dfine_entrypoint_trains_with_exact_bindings(
    tmp_path,
):
    manifest, invocation, resolver, _artifacts = _fixture(tmp_path)
    runtime_manifest, _entrypoint = _write_external_runtime(
        tmp_path / "runtime"
    )
    progress = []
    trainer = LocalCudaObjectDetectorTrainer(
        model_pack_manifest=manifest,
        artifact_resolver=resolver,
        backend=_external_backend(runtime_manifest),
        progress_hook=progress.append,
        minimum_training_geometry=2,
        minimum_validation_geometry_per_type=2,
    )

    assert trainer.status()["state"] == "ready"
    result = trainer.train(invocation)

    execution = result.metadata["execution_provenance"]
    assert execution["accelerator"] == "cuda"
    assert execution["subprocess_shell"] is False
    assert execution["dataset_hash"] == invocation.dataset_hash
    assert (
        execution["fixed_validation_sha256"]
        == invocation.fixed_validation_sha256
    )
    assert (
        execution["model_pack_manifest_sha256"]
        == load_local_detector_model_pack(manifest).manifest_sha256
    )
    assert execution["external_entrypoint_sha256"]
    assert execution["log_size_bytes"] <= 4096
    assert progress == [
        {
            "stage": "fine_tune",
            "epoch": 1,
            "epochs": 2,
            "current": 1,
            "total": 1,
            "loss": 0.125,
        }
    ]
    assert (
        invocation.output_directory
        / "detector"
        / "model.safetensors"
    ).is_file()


def test_external_dfine_runtime_requires_server_owned_manifest_trust_hash(
    tmp_path,
):
    manifest, _invocation, resolver, _artifacts = _fixture(tmp_path)
    runtime_manifest, _entrypoint = _write_external_runtime(
        tmp_path / "runtime"
    )
    trainer = LocalCudaObjectDetectorTrainer(
        model_pack_manifest=manifest,
        artifact_resolver=resolver,
        backend=ExternalDfineCudaDetectorBackend(
            runtime_manifest,
            trusted_manifest_sha256=None,
        ),
        minimum_training_geometry=2,
        minimum_validation_geometry_per_type=2,
    )

    assert trainer.status()["state"] == "failed"
    assert trainer.status()["code"] == "detector_runtime_unavailable"
    assert (
        trainer.status()["runtime"]["error_code"]
        == "external_runtime_manifest_invalid"
    )


def test_external_dfine_result_with_wrong_dataset_binding_fails_closed(
    tmp_path,
):
    manifest, invocation, resolver, _artifacts = _fixture(tmp_path)
    runtime_manifest, _entrypoint = _write_external_runtime(
        tmp_path / "runtime",
        behavior="wrong_binding",
    )
    trainer = LocalCudaObjectDetectorTrainer(
        model_pack_manifest=manifest,
        artifact_resolver=resolver,
        backend=_external_backend(runtime_manifest),
        minimum_training_geometry=2,
        minimum_validation_geometry_per_type=2,
    )

    with pytest.raises(ProjectModelLifecycleError) as error:
        trainer.train(invocation)

    assert error.value.code == "external_trainer_response_invalid"
    assert not (invocation.output_directory / "detector").exists()


def test_external_dfine_runtime_hash_is_reverified_before_training(
    tmp_path,
):
    manifest, invocation, resolver, _artifacts = _fixture(tmp_path)
    runtime_manifest, entrypoint = _write_external_runtime(
        tmp_path / "runtime"
    )
    trainer = LocalCudaObjectDetectorTrainer(
        model_pack_manifest=manifest,
        artifact_resolver=resolver,
        backend=_external_backend(runtime_manifest),
        minimum_training_geometry=2,
        minimum_validation_geometry_per_type=2,
    )
    assert trainer.status()["state"] == "ready"
    entrypoint.write_text(
        entrypoint.read_text(encoding="utf-8") + "\n# tampered\n",
        encoding="utf-8",
    )

    with pytest.raises(ProjectModelLifecycleError) as error:
        trainer.train(invocation)

    assert error.value.code == "external_trainer_runtime_changed"


def test_external_dfine_model_pack_is_reverified_before_training(
    tmp_path,
):
    manifest, invocation, resolver, _artifacts = _fixture(tmp_path)
    runtime_manifest, _entrypoint = _write_external_runtime(
        tmp_path / "runtime"
    )
    trainer = LocalCudaObjectDetectorTrainer(
        model_pack_manifest=manifest,
        artifact_resolver=resolver,
        backend=_external_backend(runtime_manifest),
        minimum_training_geometry=2,
        minimum_validation_geometry_per_type=2,
    )
    assert trainer.status()["state"] == "ready"
    (manifest.parent / "model" / "model.safetensors").write_bytes(
        b"tampered-model-pack"
    )

    with pytest.raises(ProjectModelLifecycleError) as error:
        trainer.train(invocation)

    assert error.value.code == "detector_model_pack_changed"


def test_external_dfine_timeout_terminates_without_promoting_partial_output(
    tmp_path,
):
    manifest, invocation, resolver, _artifacts = _fixture(tmp_path)
    runtime_manifest, _entrypoint = _write_external_runtime(
        tmp_path / "runtime",
        behavior="sleep",
        training_timeout_seconds=1,
    )
    trainer = LocalCudaObjectDetectorTrainer(
        model_pack_manifest=manifest,
        artifact_resolver=resolver,
        backend=_external_backend(runtime_manifest),
        minimum_training_geometry=2,
        minimum_validation_geometry_per_type=2,
    )
    started = time.monotonic()

    with pytest.raises(ProjectModelLifecycleError) as error:
        trainer.train(invocation)

    assert error.value.code == "external_trainer_timeout"
    assert time.monotonic() - started < 3
    assert not (invocation.output_directory / "detector").exists()


def test_external_dfine_cancel_terminates_without_promoting_partial_output(
    tmp_path,
):
    manifest, invocation, resolver, _artifacts = _fixture(tmp_path)
    runtime_manifest, _entrypoint = _write_external_runtime(
        tmp_path / "runtime",
        behavior="sleep",
        training_timeout_seconds=5,
    )
    started = [float("inf")]
    trainer = LocalCudaObjectDetectorTrainer(
        model_pack_manifest=manifest,
        artifact_resolver=resolver,
        backend=_external_backend(runtime_manifest),
        cancel_hook=lambda: time.monotonic() - started[0] > 0.15,
        minimum_training_geometry=2,
        minimum_validation_geometry_per_type=2,
    )
    started[0] = time.monotonic()

    with pytest.raises(ProjectModelLifecycleError) as error:
        trainer.train(invocation)

    assert error.value.code == "detector_training_cancelled"
    assert time.monotonic() - started[0] < 2
    assert not (invocation.output_directory / "detector").exists()


def test_external_dfine_log_limit_terminates_without_model_artifact(
    tmp_path,
):
    manifest, invocation, resolver, _artifacts = _fixture(tmp_path)
    runtime_manifest, _entrypoint = _write_external_runtime(
        tmp_path / "runtime",
        behavior="log_flood",
        max_log_bytes=1024,
    )
    trainer = LocalCudaObjectDetectorTrainer(
        model_pack_manifest=manifest,
        artifact_resolver=resolver,
        backend=_external_backend(runtime_manifest),
        minimum_training_geometry=2,
        minimum_validation_geometry_per_type=2,
    )

    with pytest.raises(ProjectModelLifecycleError) as error:
        trainer.train(invocation)

    assert error.value.code == "external_trainer_log_limit_exceeded"
    assert not (invocation.output_directory / "detector").exists()
