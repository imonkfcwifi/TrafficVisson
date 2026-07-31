"""Real local CLIP centroid-head training for project candidate review.

This adapter does not replace the road detector.  Grounding DINO and the
dedicated road models still find candidate regions; this project model learns
how the user's confirmed candidate images map to TrafficVisson issue types.

Only server-resolved, hash-verified current-video evidence and explicitly
approved 3Q/4Q reference images are embedded.  Validation images are never
used to fit centroids or thresholds.  The resulting JSON head is a real,
content-addressed model artifact that can be applied to later candidate
frames, but it remains a candidate until the lifecycle store's fixed-set
comparison and an explicit user promotion.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from uuid import UUID

from .dataset_versions import canonical_json_bytes
from .project_model_lifecycle import (
    ProjectModelLifecycleError,
    ResolvedActiveProjectModel,
    TrainerInvocation,
    TrainerResult,
    VISUAL_CENTROID_MODEL_FORMAT,
)
from .weak_case_catalog import ResolvedWeakCaseImage
from .weak_case_visual_index import WeakCaseImageEmbedder


CENTROID_HEAD_TRAINER_VERSION = "1.1"
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_EMBEDDING_DIMENSION = 4_096
MAX_TRAINING_SAMPLES = 100_000
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class CandidateArtifact(Protocol):
    path: Path
    media_type: str
    size_bytes: int
    sha256: str


class ApprovedReferenceCatalog(Protocol):
    def resolve_case_image_for_visual_index(
        self,
        source_id: str,
        case_id: str,
    ) -> ResolvedWeakCaseImage:
        ...


CandidateArtifactResolver = Callable[
    [UUID, UUID, str, str],
    CandidateArtifact,
]
ActiveModelResolver = Callable[
    [UUID],
    ResolvedActiveProjectModel,
]


@dataclass(frozen=True)
class CentroidPrediction:
    issue_type: str
    score: float
    threshold: float


def _sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _finite_vector(
    raw: Sequence[float],
    *,
    expected_dimension: Optional[int] = None,
) -> tuple[float, ...]:
    if (
        isinstance(raw, (str, bytes, bytearray))
        or not 2 <= len(raw) <= MAX_EMBEDDING_DIMENSION
    ):
        raise ProjectModelLifecycleError(
            "embedding_contract_violation",
            "the local embedding vector has an invalid dimension",
        )
    values = tuple(float(value) for value in raw)
    if any(not math.isfinite(value) for value in values):
        raise ProjectModelLifecycleError(
            "embedding_contract_violation",
            "the local embedding vector contains a non-finite value",
        )
    if expected_dimension is not None and len(values) != expected_dimension:
        raise ProjectModelLifecycleError(
            "embedding_contract_violation",
            "the local embedding dimension changed during training",
        )
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 1e-12:
        raise ProjectModelLifecycleError(
            "embedding_contract_violation",
            "the local embedding vector has zero magnitude",
        )
    return tuple(value / norm for value in values)


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ProjectModelLifecycleError(
            "centroid_artifact_invalid",
            "centroid dimensions do not match",
        )
    return sum(a * b for a, b in zip(left, right))


def _weighted_centroid(
    samples: Sequence[tuple[Sequence[float], float]],
    *,
    dimension: int,
) -> tuple[float, ...]:
    if not samples:
        raise ProjectModelLifecycleError(
            "insufficient_training_positives",
            "each requested issue type requires a current-video training positive",
        )
    totals = [0.0] * dimension
    total_weight = 0.0
    for vector, weight in samples:
        if (
            len(vector) != dimension
            or not math.isfinite(weight)
            or weight <= 0
            or weight > 1
        ):
            raise ProjectModelLifecycleError(
                "embedding_contract_violation",
                "training embedding weight or dimension is invalid",
            )
        total_weight += weight
        for index, value in enumerate(vector):
            totals[index] += value * weight
    return _finite_vector(tuple(value / total_weight for value in totals))


def _calibrate_threshold(
    positive_scores: Sequence[float],
    negative_scores: Sequence[float],
) -> float:
    if not positive_scores:
        raise ProjectModelLifecycleError(
            "insufficient_training_positives",
            "a requested issue type has no current-video training positive",
        )
    if not negative_scores:
        return round(max(-1.0, min(1.0, min(positive_scores) - 0.05)), 8)

    ordered = sorted(set((*positive_scores, *negative_scores)))
    candidates = {-1.0, 1.0}
    candidates.update(ordered)
    candidates.update(
        (ordered[index - 1] + ordered[index]) / 2
        for index in range(1, len(ordered))
    )
    best_threshold = 0.0
    best_key: Optional[tuple[float, float, float, float]] = None
    for threshold in sorted(candidates):
        true_positive = sum(score >= threshold for score in positive_scores)
        true_negative = sum(score < threshold for score in negative_scores)
        false_positive = len(negative_scores) - true_negative
        recall = true_positive / len(positive_scores)
        specificity = true_negative / len(negative_scores)
        precision = (
            true_positive / (true_positive + false_positive)
            if true_positive + false_positive
            else 0.0
        )
        key = (
            (recall + specificity) / 2,
            recall,
            precision,
            -threshold,
        )
        if best_key is None or key > best_key:
            best_key = key
            best_threshold = threshold
    return round(max(-1.0, min(1.0, best_threshold)), 8)


def _artifact_models(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if (
        value.get("schema_version") != VISUAL_CENTROID_MODEL_FORMAT
        or not isinstance(value.get("embedding_model"), Mapping)
        or not isinstance(value.get("embedding_dimension"), int)
        or not 2 <= value["embedding_dimension"] <= MAX_EMBEDDING_DIMENSION
        or not isinstance(value.get("models"), Mapping)
        or not value["models"]
    ):
        raise ProjectModelLifecycleError(
            "centroid_artifact_invalid",
            "the project centroid-head artifact contract is invalid",
        )
    return value["models"]


def _predict(
    vector: Sequence[float],
    artifact: Mapping[str, Any],
) -> tuple[CentroidPrediction, ...]:
    models = _artifact_models(artifact)
    dimension = int(artifact["embedding_dimension"])
    if len(vector) != dimension:
        raise ProjectModelLifecycleError(
            "centroid_artifact_invalid",
            "the project model embedding dimension does not match",
        )
    predictions = []
    for issue_type in artifact.get("required_issue_types", ()):
        model = models.get(issue_type)
        if not isinstance(model, Mapping):
            raise ProjectModelLifecycleError(
                "centroid_artifact_invalid",
                "the project model is missing a requested issue type",
            )
        centroid = _finite_vector(
            model.get("positive_centroid", ()),
            expected_dimension=dimension,
        )
        threshold = model.get("threshold")
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not math.isfinite(float(threshold))
            or not -1 <= float(threshold) <= 1
        ):
            raise ProjectModelLifecycleError(
                "centroid_artifact_invalid",
                "the project model threshold is invalid",
            )
        score = _dot(vector, centroid)
        if score >= float(threshold):
            predictions.append(
                CentroidPrediction(
                    issue_type=str(issue_type),
                    score=round(score, 8),
                    threshold=round(float(threshold), 8),
                )
            )
    return tuple(
        sorted(
            predictions,
            key=lambda item: (-item.score, item.issue_type),
        )
    )


class VisualEmbeddingCentroidHeadTrainer:
    """Fit and evaluate a project-scoped classifier on verified local images."""

    trainer_id = "visual-embedding-centroid-head"
    available = True
    test_only = False

    def __init__(
        self,
        *,
        embedder: WeakCaseImageEmbedder,
        candidate_artifact_resolver: CandidateArtifactResolver,
        approved_reference_catalog: ApprovedReferenceCatalog,
        active_model_resolver: Optional[ActiveModelResolver] = None,
        require_gpu_execution: bool = False,
    ) -> None:
        identity = dict(embedder.model_identity())
        identity_bytes = canonical_json_bytes(identity)
        self._embedder = embedder
        self._candidate_artifact_resolver = candidate_artifact_resolver
        self._approved_reference_catalog = approved_reference_catalog
        self._active_model_resolver = active_model_resolver
        self._require_gpu_execution = require_gpu_execution
        self._embedding_identity = identity
        self._embedding_identity_sha256 = _sha256_bytes(identity_bytes)
        self.trainer_version = (
            f"{CENTROID_HEAD_TRAINER_VERSION}-"
            f"{self._embedding_identity_sha256[:12]}"
        )
        self._feature_cache: dict[str, tuple[float, ...]] = {}
        self._dimension: Optional[int] = None

    def _execution_provenance(self) -> Mapping[str, Any]:
        provider = getattr(
            self._embedder,
            "execution_provenance",
            None,
        )
        if callable(provider):
            value = provider()
            if not isinstance(value, Mapping):
                raise ProjectModelLifecycleError(
                    "training_runtime_provenance_invalid",
                    "the embedding runtime returned invalid provenance",
                )
            execution = dict(value)
        else:
            execution = {
                "schema_version": (
                    "trafficvisson.embedding-execution-provenance.v1"
                ),
                "requested_device": "unreported",
                "resolved_device": "unreported",
                "accelerator": "unreported",
                "accelerator_used": False,
                "torch_version": None,
                "torch_cuda_version": None,
                "cuda_device_name": None,
                "cuda_total_memory_bytes": None,
                "model_pack_identity_sha256": (
                    self._embedding_identity_sha256
                ),
            }
        try:
            canonical_json_bytes(execution)
        except Exception as error:
            raise ProjectModelLifecycleError(
                "training_runtime_provenance_invalid",
                "the embedding runtime provenance is not canonical JSON",
            ) from error
        if (
            execution.get("schema_version")
            != "trafficvisson.embedding-execution-provenance.v1"
            or not isinstance(
                execution.get("accelerator_used"),
                bool,
            )
            or not isinstance(
                execution.get("resolved_device"),
                str,
            )
            or execution.get("model_pack_identity_sha256")
            != self._embedding_identity_sha256
        ):
            raise ProjectModelLifecycleError(
                "training_runtime_provenance_invalid",
                "the embedding runtime provenance contract is invalid",
            )
        if self._require_gpu_execution and (
            execution["accelerator_used"] is not True
            or execution.get("accelerator") != "cuda"
            or not execution["resolved_device"].startswith("cuda")
        ):
            raise ProjectModelLifecycleError(
                "gpu_training_required",
                "project model training requires verified local CUDA execution",
            )
        return execution

    def _embed_verified(
        self,
        body: bytes,
        media_type: str,
        expected_sha256: str,
    ) -> tuple[float, ...]:
        if (
            not isinstance(body, bytes)
            or not 1 <= len(body) <= MAX_IMAGE_BYTES
            or not isinstance(expected_sha256, str)
            or _SHA256.fullmatch(expected_sha256) is None
            or _sha256_bytes(body) != expected_sha256
        ):
            raise ProjectModelLifecycleError(
                "training_evidence_integrity_failed",
                "training image bytes do not match their authorized digest",
            )
        cached = self._feature_cache.get(expected_sha256)
        if cached is not None:
            return cached
        vector = _finite_vector(
            self._embedder.embed_image(body, media_type),
            expected_dimension=self._dimension,
        )
        if self._dimension is None:
            self._dimension = len(vector)
        self._feature_cache[expected_sha256] = vector
        return vector

    def _candidate_feature(
        self,
        project_id: UUID,
        annotation: Mapping[str, Any],
    ) -> tuple[tuple[float, ...], str]:
        try:
            artifact = self._candidate_artifact_resolver(
                project_id,
                UUID(str(annotation["analysis_job_id"])),
                str(annotation["candidate_id"]),
                "image",
            )
            path = Path(artifact.path)
            body = path.read_bytes()
        except Exception as error:
            raise ProjectModelLifecycleError(
                "training_evidence_unavailable",
                "a current-video training image could not be resolved",
            ) from error
        if (
            len(body) != artifact.size_bytes
            or not 1 <= artifact.size_bytes <= MAX_IMAGE_BYTES
        ):
            raise ProjectModelLifecycleError(
                "training_evidence_integrity_failed",
                "a current-video training image changed after resolution",
            )
        return (
            self._embed_verified(
                body,
                artifact.media_type,
                artifact.sha256,
            ),
            artifact.sha256,
        )

    def _reference_feature(
        self,
        reference: Mapping[str, Any],
    ) -> tuple[tuple[float, ...], str]:
        try:
            image = (
                self._approved_reference_catalog
                .resolve_case_image_for_visual_index(
                    str(reference["source_id"]),
                    str(reference["case_id"]),
                )
            )
        except Exception as error:
            raise ProjectModelLifecycleError(
                "approved_reference_image_unavailable",
                "an approved 3Q/4Q reference image could not be resolved",
            ) from error
        if image.sha256 != reference["image_sha256"]:
            raise ProjectModelLifecycleError(
                "approved_reference_image_changed",
                "an approved reference image no longer matches its approval",
            )
        return (
            self._embed_verified(
                image.body,
                image.media_type,
                image.sha256,
            ),
            image.sha256,
        )

    @staticmethod
    def _load_artifact(
        resolved: ResolvedActiveProjectModel,
    ) -> Mapping[str, Any]:
        try:
            body = resolved.primary_artifact_path.read_bytes()
            if (
                _sha256_bytes(body)
                != resolved.primary_artifact_sha256
            ):
                raise ValueError("digest mismatch")
            value = json.loads(body)
        except Exception as error:
            raise ProjectModelLifecycleError(
                "active_model_artifact_invalid",
                "the active project model artifact could not be verified",
            ) from error
        if not isinstance(value, Mapping):
            raise ProjectModelLifecycleError(
                "active_model_artifact_invalid",
                "the active project model artifact is not an object",
            )
        _artifact_models(value)
        return value

    @staticmethod
    def _prediction_records(
        *,
        features: Mapping[str, tuple[float, ...]],
        truth: Sequence[Mapping[str, Any]],
        artifact: Mapping[str, Any],
        prefix: str,
    ) -> tuple[dict[str, Any], ...]:
        truth_by_sample_type = {
            (str(item["sample_id"]), str(item["issue_type"])): tuple(
                item["truth_event_ids"]
            )
            for item in truth
        }
        rows = []
        for sample_id in sorted(features):
            for prediction in _predict(features[sample_id], artifact):
                truth_ids = truth_by_sample_type.get(
                    (sample_id, prediction.issue_type),
                    (),
                )
                digest = hashlib.sha256(
                    (
                        f"{prefix}:{sample_id}:"
                        f"{prediction.issue_type}"
                    ).encode("utf-8")
                ).hexdigest()[:20]
                rows.append(
                    {
                        "prediction_event_id": (
                            f"{prefix}-{digest}"
                        ),
                        "sample_id": sample_id,
                        "issue_type": prediction.issue_type,
                        "matched_truth_event_id": (
                            truth_ids[0] if truth_ids else None
                        ),
                    }
                )
        return tuple(rows)

    def train(self, invocation: TrainerInvocation) -> TrainerResult:
        if (
            invocation.schema_version == ""
            or len(invocation.current_training_annotations)
            + len(invocation.fixed_validation_annotations)
            > MAX_TRAINING_SAMPLES
        ):
            raise ProjectModelLifecycleError(
                "training_request_invalid",
                "the project training request is invalid or too large",
            )
        progress = invocation.progress_callback or (lambda _value: None)
        cancelled = invocation.cancel_callback or (lambda: False)
        if cancelled():
            raise ProjectModelLifecycleError(
                "training_cancelled",
                "project centroid training was cancelled",
            )
        # Bound memory to one serialized lifecycle invocation.  The durable
        # artifact and evidence hashes, not Python feature objects, are the
        # restart/cache boundary.
        self._feature_cache = {}
        self._dimension = None
        project_id = UUID(invocation.project_id)
        required = tuple(invocation.required_issue_types)
        train_features: dict[str, tuple[float, ...]] = {}
        train_hashes: dict[str, str] = {}
        training_total = len(invocation.current_training_annotations)
        for index, annotation in enumerate(
            invocation.current_training_annotations
        ):
            if cancelled():
                raise ProjectModelLifecycleError(
                    "training_cancelled",
                    "project centroid training was cancelled",
                )
            vector, digest = self._candidate_feature(
                project_id,
                annotation,
            )
            sample_id = str(annotation["annotation_id"])
            train_features[sample_id] = vector
            train_hashes[sample_id] = digest
            progress(
                {
                    "stage": "training_embedding",
                    "current": index + 1,
                    "total": training_total,
                }
            )

        reference_features = []
        reference_total = len(invocation.auxiliary_reference_seeds)
        for index, reference in enumerate(
            invocation.auxiliary_reference_seeds
        ):
            if cancelled():
                raise ProjectModelLifecycleError(
                    "training_cancelled",
                    "project centroid training was cancelled",
                )
            vector, digest = self._reference_feature(reference)
            reference_features.append((reference, vector, digest))
            progress(
                {
                    "stage": "reference_embedding",
                    "current": index + 1,
                    "total": reference_total,
                }
            )

        if self._dimension is None:
            raise ProjectModelLifecycleError(
                "training_evidence_unavailable",
                "no project training image could be embedded",
            )
        execution_provenance = dict(
            self._execution_provenance()
        )
        models: dict[str, Any] = {}
        for issue_type in required:
            positives = []
            negatives = []
            current_positive_count = 0
            for annotation in invocation.current_training_annotations:
                vector = train_features[str(annotation["annotation_id"])]
                is_positive = (
                    annotation["partition"] == "approved"
                    and annotation["selected_issue_type"] == issue_type
                )
                if is_positive:
                    positives.append((vector, 1.0))
                    current_positive_count += 1
                else:
                    negatives.append((vector, 1.0))
            if current_positive_count == 0:
                raise ProjectModelLifecycleError(
                    "insufficient_training_positives",
                    (
                        f"{issue_type} has no current-video positive in "
                        "the training split"
                    ),
                )
            for reference, vector, _digest in reference_features:
                weight = float(reference["training_weight"])
                if reference["issue_type"] == issue_type:
                    positives.append((vector, weight))
                else:
                    negatives.append((vector, weight))
            positive_centroid = _weighted_centroid(
                positives,
                dimension=self._dimension,
            )
            negative_centroid = (
                _weighted_centroid(
                    negatives,
                    dimension=self._dimension,
                )
                if negatives
                else None
            )
            positive_scores = [
                _dot(vector, positive_centroid)
                for vector, _weight in positives[:current_positive_count]
            ]
            negative_scores = [
                _dot(vector, positive_centroid)
                for vector, _weight in negatives
            ]
            models[issue_type] = {
                "positive_centroid": [
                    round(value, 10) for value in positive_centroid
                ],
                "negative_centroid": (
                    [
                        round(value, 10)
                        for value in negative_centroid
                    ]
                    if negative_centroid is not None
                    else None
                ),
                "threshold": _calibrate_threshold(
                    positive_scores,
                    negative_scores,
                ),
                "current_positive_count": current_positive_count,
                "current_negative_count": len(
                    invocation.current_training_annotations
                ) - current_positive_count,
                "auxiliary_positive_weight": round(
                    sum(
                        float(reference["training_weight"])
                        for reference, _vector, _digest
                        in reference_features
                        if reference["issue_type"] == issue_type
                    ),
                    6,
                ),
            }

        artifact = {
            "schema_version": VISUAL_CENTROID_MODEL_FORMAT,
            "project_id": invocation.project_id,
            "dataset_version_id": invocation.dataset_version_id,
            "dataset_hash": invocation.dataset_hash,
            "fixed_validation_sha256": (
                invocation.fixed_validation_sha256
            ),
            "base_model_version": invocation.base_model_version,
            "embedding_model": self._embedding_identity,
            "embedding_model_identity_sha256": (
                self._embedding_identity_sha256
            ),
            "embedding_dimension": self._dimension,
            "embedding_execution_provenance": execution_provenance,
            "required_issue_types": list(required),
            "models": models,
            "fit_split": "train_only",
            "validation_used_for_fit": False,
            "current_training_image_sha256": [
                train_hashes[key] for key in sorted(train_hashes)
            ],
            "approved_reference_image_sha256": [
                digest
                for _reference, _vector, digest
                in reference_features
            ],
            "approved_reference_role": (
                "lower_weight_auxiliary_seed_only"
            ),
            "automatic_promotion": False,
        }
        artifact_path = (
            invocation.output_directory / "centroid-head.json"
        )
        artifact_path.write_bytes(canonical_json_bytes(artifact))

        validation_features: dict[str, tuple[float, ...]] = {}
        validation_hashes: dict[str, str] = {}
        validation_total = len(
            invocation.fixed_validation_annotations
        )
        for index, annotation in enumerate(
            invocation.fixed_validation_annotations
        ):
            if cancelled():
                raise ProjectModelLifecycleError(
                    "training_cancelled",
                    "project centroid training was cancelled",
                )
            vector, digest = self._candidate_feature(
                project_id,
                annotation,
            )
            sample_id = str(annotation["annotation_id"])
            validation_features[sample_id] = vector
            validation_hashes[sample_id] = digest
            progress(
                {
                    "stage": "fixed_validation_embedding",
                    "current": index + 1,
                    "total": validation_total,
                }
            )
        expected_validation_samples = {
            str(item["sample_id"])
            for item in invocation.fixed_validation_truth
        }
        if set(validation_features) != expected_validation_samples:
            raise ProjectModelLifecycleError(
                "fixed_validation_evidence_mismatch",
                "fixed validation truth and image evidence differ",
            )

        candidate_predictions = self._prediction_records(
            features=validation_features,
            truth=invocation.fixed_validation_truth,
            artifact=artifact,
            prefix="candidate",
        )
        active_predictions: tuple[dict[str, Any], ...] = ()
        active_evaluation_mode = "no_compatible_active_head"
        if self._active_model_resolver is not None:
            try:
                active = self._active_model_resolver(project_id)
            except ProjectModelLifecycleError as error:
                if error.code != (
                    "active_project_model_artifact_unavailable"
                ):
                    raise
            else:
                if (
                    active.model_version
                    == invocation.base_model_version
                    and active.model_format
                    == VISUAL_CENTROID_MODEL_FORMAT
                ):
                    active_artifact = self._load_artifact(active)
                    active_predictions = self._prediction_records(
                        features=validation_features,
                        truth=invocation.fixed_validation_truth,
                        artifact=active_artifact,
                        prefix="active",
                    )
                    active_evaluation_mode = (
                        "verified_promoted_project_head"
                    )

        model_version = (
            "project-centroid@"
            f"{invocation.request_manifest_sha256[:12]}"
        )
        return TrainerResult(
            model_version=model_version,
            model_format=VISUAL_CENTROID_MODEL_FORMAT,
            primary_artifact_relative_path="centroid-head.json",
            active_prediction_records=active_predictions,
            candidate_prediction_records=candidate_predictions,
            metadata={
                "trainer_version": self.trainer_version,
                "embedding_model_identity_sha256": (
                    self._embedding_identity_sha256
                ),
                "embedding_dimension": self._dimension,
                "execution_provenance": execution_provenance,
                "fit_split": "train_only",
                "validation_sample_count": len(
                    validation_features
                ),
                "validation_image_manifest_sha256": (
                    hashlib.sha256(
                        canonical_json_bytes(
                            [
                                {
                                    "sample_id": sample_id,
                                    "image_sha256": validation_hashes[
                                        sample_id
                                    ],
                                }
                                for sample_id in sorted(
                                    validation_hashes
                                )
                            ]
                        )
                    ).hexdigest()
                ),
                "active_evaluation_mode": active_evaluation_mode,
                "baseline_absence_does_not_authorize_promotion": True,
                "candidate_role": (
                    "candidate_classification_head_after_local_detector"
                ),
                "automatic_promotion": False,
            },
        )


class VisualEmbeddingCentroidHeadInference:
    """Apply one promoted, verified project head to a later candidate image."""

    def __init__(self, embedder: WeakCaseImageEmbedder) -> None:
        self._embedder = embedder
        self._embedding_identity = dict(embedder.model_identity())
        self._embedding_identity_sha256 = hashlib.sha256(
            canonical_json_bytes(self._embedding_identity)
        ).hexdigest()

    def classify(
        self,
        model: ResolvedActiveProjectModel,
        *,
        body: bytes,
        media_type: str,
        expected_sha256: str,
    ) -> Mapping[str, Any]:
        if (
            model.model_format != VISUAL_CENTROID_MODEL_FORMAT
            or _sha256_bytes(body) != expected_sha256
        ):
            raise ProjectModelLifecycleError(
                "project_model_inference_rejected",
                "the promoted model or candidate image is invalid",
            )
        artifact = VisualEmbeddingCentroidHeadTrainer._load_artifact(
            model
        )
        if (
            artifact.get("embedding_model_identity_sha256")
            != self._embedding_identity_sha256
        ):
            raise ProjectModelLifecycleError(
                "project_model_embedding_changed",
                "the promoted head requires another embedding model",
            )
        vector = _finite_vector(
            self._embedder.embed_image(body, media_type),
            expected_dimension=int(artifact["embedding_dimension"]),
        )
        predictions = _predict(vector, artifact)
        return {
            "schema_version": (
                "trafficvisson.project-centroid-inference.v1"
            ),
            "project_id": model.project_id,
            "model_version_id": model.model_version_id,
            "model_version": model.model_version,
            "dataset_version_id": model.dataset_version_id,
            "candidate_image_sha256": expected_sha256,
            "predictions": [
                {
                    "issue_type": prediction.issue_type,
                    "score": prediction.score,
                    "threshold": prediction.threshold,
                }
                for prediction in predictions
            ],
            "review_only_until_pipeline_question_answer": True,
            "annotation_created": False,
            "incident_created": False,
        }


__all__ = [
    "CENTROID_HEAD_TRAINER_VERSION",
    "CentroidPrediction",
    "VisualEmbeddingCentroidHeadInference",
    "VisualEmbeddingCentroidHeadTrainer",
]
