"""Content-addressed visual retrieval for audited 3Q/4Q sample photos.

The HWPX catalog owns document and image provenance.  This module adds only a
local image-embedding index and a bounded cosine query.  Historical sample
photos remain ``weak_unconfirmed`` until the project-scoped approval store says
otherwise.  Building or querying this index never creates an Annotation,
Incident, dataset row, or training membership.

No model is downloaded here.  Production CLIP use is permitted only through a
fully verified local vision model-pack manifest; tests and alternative local
embedders use the small dependency-injected protocol below.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import stat
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence
from uuid import UUID

from .hwpx_weak_cases import HwpxWeakCaseLimits, _inspect_image_header
from .hwpx_sample_layout import (
    HWPX_SAMPLE_LAYOUT_SCHEMA,
    SAMPLE_LAYOUT_BINDING_REVISION,
    SAMPLE_REFERENCE_BINDING_REVISION,
)
from .pdf_companion_audit import (
    PDF_COMPANION_ROLE,
    PDF_COMPANION_SCHEMA,
)
from .vision_inference import (
    VisionModelPack,
    VisionModelPackError,
    load_model_pack,
)
from .weak_case_catalog import (
    WEAK_CASE_VISUAL_SOURCE_SCHEMA,
    ResolvedWeakCaseImage,
    WeakCaseCatalogError,
    score_weak_case_metadata,
)


WEAK_CASE_VISUAL_INDEX_SCHEMA = (
    "trafficvisson.weak-case-visual-index.v2"
)
WEAK_CASE_VISUAL_ACTIVE_SCHEMA = (
    "trafficvisson.weak-case-visual-active.v2"
)
WEAK_CASE_VISUAL_QUERY_SCHEMA = (
    "trafficvisson.weak-case-visual-query.v2"
)
IMAGE_EMBEDDING_MODEL_SCHEMA = (
    "trafficvisson.image-embedding-model.v1"
)
LOCAL_ROAD_SCENE_OBSERVATION_SCHEMA = (
    "trafficvisson.local-road-scene-observation.v1"
)
MAX_VISUAL_QUERY_CANDIDATES = 128
DEFAULT_VISUAL_QUERY_BATCH_SIZE = 8
# The physical width is part of the embedding computation contract.  Real
# CUDA fp16 CLIP kernels are stable across co-batch contents at one fixed
# width, but change observable scores when the width changes.
MAX_VISUAL_QUERY_BATCH_SIZE = DEFAULT_VISUAL_QUERY_BATCH_SIZE

_ROAD_SCENE_PROMPTS = (
    "a dashcam photograph looking forward at a visible road or highway",
    "a road surface visible from a moving vehicle",
    "a nighttime road scene with the roadway visible",
    "the inside of a highway tunnel with the roadway visible",
    "a wet road visible through a windshield in heavy rain",
    "an unmarked rural road clearly visible in front of a vehicle",
)
_NON_ROAD_VIEW_PROMPTS = (
    "a close-up photograph of the ceiling and sun visor inside a car with no road visible",
    "a close-up of a car interior with no road visible",
    "only sky and the roof of a car with no road visible",
    "the camera lens is covered or obstructed and no road is visible",
    "a blurry close-up with no road visible",
    "a car seat or clothing fills the camera view and no road is visible",
)
_ROAD_SCENE_PROMPT_SET_SHA256 = hashlib.sha256(
    json.dumps(
        {
            "positive": _ROAD_SCENE_PROMPTS,
            "negative": _NON_ROAD_VIEW_PROMPTS,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_MODEL_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CANDIDATE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_INDEX_FILE = re.compile(
    r"^weak-reference-index-([a-f0-9]{64})\.json$"
)
_ACTIVE_FILE = "active-weak-reference-index.json"
_SOURCE_ID = re.compile(r"^sample-hwpx-[34]q-[a-f0-9]{16}$")
_CASE_ID = re.compile(r"^hwpx-weak-case-[a-f0-9]{32}$")
_EVIDENCE_GROUP_ID = re.compile(
    r"^sample-evidence-group-[a-f0-9]{24}$"
)
_DUPLICATE_CASE_REF = re.compile(
    r"^sample-hwpx-[34]q-[a-f0-9]{16}/"
    r"hwpx-weak-case-[a-f0-9]{32}$"
)
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")

_CASE_REQUIRED_KEYS = frozenset(
    {
        "source_id",
        "case_id",
        "quarter",
        "document_name",
        "document_sha256",
        "structure_target_summary_sha256",
        "index_manifest_sha256",
        "section_path",
        "section_index",
        "page_hint",
        "page_hint_source",
        "inspection_page_number",
        "photo_page_number",
        "page_pair_index",
        "photo_slot_id",
        "sample_layout_binding_schema",
        "sample_layout_binding_revision",
        "semantic_projection_revision",
        "sample_evidence_group_id",
        "group_photo_index",
        "group_photo_count",
        "sample_layout_binding_sha256",
        "semantic_attestation_sha256",
        "table_id",
        "table_index",
        "cell_target_id",
        "cell_row",
        "cell_column",
        "picture_target_id",
        "picture_index",
        "picture_xml_id",
        "picture_address_sha256",
        "image_sha256",
        "image_media_type",
        "image_size_bytes",
        "image_width",
        "image_height",
        "duplicate_image_occurrence_count",
        "duplicate_image_review_required",
        "duplicate_image_registry_complete",
        "duplicate_image_case_refs",
        "caption",
        "route",
        "location",
        "evaluation",
        "opinion",
        "search_texts",
        "reference_state",
        "review_required",
        "approved_reference",
        "training_eligible",
        "annotation_created",
        "incident_created",
    }
)
_SOURCE_REQUIRED_KEYS = frozenset(
    {
        "source_id",
        "quarter",
        "document_name",
        "document_sha256",
        "structure_target_summary_sha256",
        "index_manifest_sha256",
        "photo_case_count",
        "excluded_fixed_picture_count",
    }
)
_SOURCE_WITH_PDF_KEYS = _SOURCE_REQUIRED_KEYS | {"companion_pdf"}
_PDF_COMPANION_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "role",
        "quarter",
        "document_name",
        "pdf_sha256",
        "expected_pdf_sha256",
        "size_bytes",
        "status",
        "page_count",
        "expected_page_count",
        "pages",
        "full_text_sha256",
        "full_text_char_count",
        "text_hash_method",
        "full_text_hash_method",
        "parser",
        "parser_version",
        "strict_parsing",
        "render",
        "pdf_images_added_as_cases",
        "case_source",
        "automatic_annotation_creation",
        "automatic_incident_creation",
        "automatic_training_ingest",
        "audit_sha256",
        "hwpx_photo_case_correspondence",
    }
)
_PDF_PAGE_KEYS = frozenset(
    {
        "page_number",
        "text_sha256",
        "text_char_count",
        "text_extraction_status",
    }
)
_PDF_RENDER_KEYS = frozenset(
    {
        "status",
        "renderer",
        "renderer_version",
        "render_dpi",
        "requested_pages",
        "rendered_pages",
    }
)
_PDF_RENDER_PAGE_KEYS = frozenset(
    {
        "page_number",
        "width",
        "height",
        "size_bytes",
        "sha256",
        "media_type",
    }
)
_PDF_CORRESPONDENCE_KEYS = frozenset(
    {
        "status",
        "hwpx_photo_case_count",
        "mapped_hwpx_photo_case_count",
        "unverified_hwpx_photo_case_count",
        "pdf_photo_case_count_added",
        "page_count",
        "pages",
    }
)
_PDF_CORRESPONDENCE_PAGE_KEYS = frozenset(
    {
        "pdf_page_number",
        "mapped_hwpx_photo_case_count",
        "correspondence_status",
        "page_text_sha256",
        "render_validation",
    }
)


class WeakCaseVisualIndexError(RuntimeError):
    """Bounded caller-safe visual-index failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WeakCaseVisualIndexLimits:
    max_cases: int = 2_048
    max_embedding_dimension: int = 4_096
    max_index_bytes: int = 64 * 1024**2
    max_active_pointer_bytes: int = 64 * 1024
    max_candidate_image_bytes: int = 64 * 1024**2
    max_search_texts_per_case: int = 32
    max_search_text_chars: int = 16_384
    max_query_candidates: int = MAX_VISUAL_QUERY_CANDIDATES
    max_query_batch_size: int = MAX_VISUAL_QUERY_BATCH_SIZE

    def __post_init__(self) -> None:
        bounds = (
            ("max_cases", self.max_cases, 1, 100_000),
            (
                "max_embedding_dimension",
                self.max_embedding_dimension,
                2,
                65_536,
            ),
            (
                "max_index_bytes",
                self.max_index_bytes,
                1_024,
                1024**3,
            ),
            (
                "max_active_pointer_bytes",
                self.max_active_pointer_bytes,
                256,
                1024**2,
            ),
            (
                "max_candidate_image_bytes",
                self.max_candidate_image_bytes,
                64,
                512 * 1024**2,
            ),
            (
                "max_search_texts_per_case",
                self.max_search_texts_per_case,
                1,
                128,
            ),
            (
                "max_search_text_chars",
                self.max_search_text_chars,
                1,
                1024**2,
            ),
            (
                "max_query_candidates",
                self.max_query_candidates,
                1,
                MAX_VISUAL_QUERY_CANDIDATES,
            ),
            (
                "max_query_batch_size",
                self.max_query_batch_size,
                DEFAULT_VISUAL_QUERY_BATCH_SIZE,
                MAX_VISUAL_QUERY_BATCH_SIZE,
            ),
        )
        for name, value, minimum, maximum in bounds:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{name} is outside the supported range")


@dataclass(frozen=True)
class CandidateFrameEvidence:
    project_id: UUID
    candidate_id: str
    body: bytes
    media_type: str
    expected_sha256: str


class WeakCaseVisualCatalog(Protocol):
    def visual_index_snapshot(self) -> Mapping[str, Any]:
        ...

    def resolve_case_image_for_visual_index(
        self,
        source_id: str,
        case_id: str,
    ) -> ResolvedWeakCaseImage:
        ...


class WeakCaseImageEmbedder(Protocol):
    def model_identity(self) -> Mapping[str, Any]:
        ...

    def embed_image(
        self,
        body: bytes,
        media_type: str,
    ) -> Sequence[float]:
        ...

    # Optional at runtime for backward-compatible injected embedders.
    # ``WeakCaseVisualIndexService.query_many`` discovers this method with
    # ``getattr`` and falls back to the exact legacy single-image path.
    def embed_images(
        self,
        images: Sequence[tuple[bytes, str]],
    ) -> Sequence[Sequence[float]]:
        ...


class ProjectReferenceApprovals(Protocol):
    def list_project(self, project_id: UUID) -> Mapping[str, Any]:
        ...


class VerifiedLocalClipImageEmbedder:
    """CLIP image features from one verified, local-only model pack.

    Loading is lazy so the bridge can inspect configuration without importing
    Torch, Pillow, or Transformers.  ``local_files_only`` and
    ``trust_remote_code=False`` are fixed; this class has no download path.
    """

    def __init__(self, manifest_path: Path | str) -> None:
        self._manifest_path = Path(manifest_path)
        self._pack = self._load_verified_pack()
        self._identity = _clip_pack_identity(self._pack)
        self._processor = None
        self._model = None
        self._torch = None
        self._image_module = None
        self._device: Optional[str] = None
        self._road_scene_text_features = None
        self._road_scene_observation_cache: dict[
            str, Mapping[str, Any]
        ] = {}
        self._lock = threading.RLock()

    def _load_verified_pack(self) -> VisionModelPack:
        try:
            pack = load_model_pack(self._manifest_path)
        except VisionModelPackError as error:
            raise WeakCaseVisualIndexError(
                "embedding_model_pack_invalid",
                "The local image-embedding model pack is invalid",
            ) from error
        if (
            pack.engine != "transformers_clip_zero_shot"
            or "candidate_classification" not in pack.capabilities
        ):
            raise WeakCaseVisualIndexError(
                "embedding_model_pack_unsupported",
                "The configured model pack is not a local CLIP pack",
            )
        return pack

    def model_identity(self) -> Mapping[str, Any]:
        current = self._load_verified_pack()
        current_identity = _clip_pack_identity(current)
        if current_identity != self._identity:
            raise WeakCaseVisualIndexError(
                "embedding_model_changed",
                "The local image-embedding model pack changed",
            )
        return dict(self._identity)

    def execution_provenance(self) -> Mapping[str, Any]:
        """Return bounded evidence for the runtime that performs embedding.

        The record deliberately excludes CUDA UUIDs and driver filesystem
        paths. It is suitable for a project-model artifact ledger and proves
        whether this invocation actually resolved to the required local GPU.
        """

        self._ensure_runtime()
        assert self._torch is not None
        assert self._device is not None
        torch = self._torch
        requested_device = str(
            self._pack.manifest["model"].get("device", "auto")
        )
        accelerator_used = self._device.startswith("cuda")
        cuda_device_name = None
        cuda_total_memory_bytes = None
        if accelerator_used:
            try:
                device = torch.device(self._device)
                device_index = (
                    device.index
                    if device.index is not None
                    else torch.cuda.current_device()
                )
                properties = torch.cuda.get_device_properties(
                    device_index
                )
                cuda_device_name = str(properties.name)[:256]
                cuda_total_memory_bytes = int(
                    properties.total_memory
                )
            except Exception as error:
                raise WeakCaseVisualIndexError(
                    "embedding_runtime_unavailable",
                    "CUDA runtime provenance could not be verified",
                ) from error
        return {
            "schema_version": (
                "trafficvisson.embedding-execution-provenance.v1"
            ),
            "requested_device": requested_device,
            "resolved_device": self._device,
            "accelerator": "cuda" if accelerator_used else "cpu",
            "accelerator_used": accelerator_used,
            "torch_version": str(torch.__version__)[:64],
            "torch_cuda_version": (
                str(torch.version.cuda)[:64]
                if torch.version.cuda is not None
                else None
            ),
            "cuda_device_name": cuda_device_name,
            "cuda_total_memory_bytes": cuda_total_memory_bytes,
            "model_pack_identity_sha256": hashlib.sha256(
                _canonical_json_bytes(self._identity)
            ).hexdigest(),
        }

    def _ensure_runtime(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            self.model_identity()
            try:
                import torch  # type: ignore
                import transformers  # type: ignore
                from PIL import Image  # type: ignore
            except Exception as error:
                raise WeakCaseVisualIndexError(
                    "embedding_runtime_unavailable",
                    "The local CLIP embedding runtime is unavailable",
                ) from error
            model_config = self._pack.manifest.get("model")
            if not isinstance(model_config, Mapping):
                raise WeakCaseVisualIndexError(
                    "embedding_model_pack_invalid",
                    "The local CLIP model configuration is missing",
                )
            directory = self._pack.root / str(model_config["directory"])
            requested_device = str(model_config.get("device", "auto"))
            if requested_device == "auto":
                device = "cuda" if torch.cuda.is_available() else "cpu"
            else:
                device = requested_device
            if device == "cuda" and not torch.cuda.is_available():
                raise WeakCaseVisualIndexError(
                    "embedding_runtime_unavailable",
                    "The configured CUDA CLIP runtime is unavailable",
                )
            try:
                processor = transformers.AutoProcessor.from_pretrained(
                    str(directory),
                    local_files_only=True,
                    trust_remote_code=False,
                )
                model = transformers.CLIPModel.from_pretrained(
                    str(directory),
                    local_files_only=True,
                    trust_remote_code=False,
                    torch_dtype=(
                        torch.float16
                        if device == "cuda"
                        else torch.float32
                    ),
                )
                model.to(device)
                model.eval()
            except Exception as error:
                raise WeakCaseVisualIndexError(
                    "embedding_runtime_unavailable",
                    "The verified local CLIP model could not be loaded",
                ) from error
            self._processor = processor
            self._model = model
            self._torch = torch
            self._image_module = Image
            self._device = device

    def embed_image(
        self,
        body: bytes,
        media_type: str,
    ) -> Sequence[float]:
        """Embed one image with the canonical fixed-width GPU contract."""

        return self.embed_images(
            tuple(
                (body, media_type)
                for _ in range(DEFAULT_VISUAL_QUERY_BATCH_SIZE)
            )
        )[0]

    def embed_images(
        self,
        images: Sequence[tuple[bytes, str]],
    ) -> Sequence[Sequence[float]]:
        """Embed one bounded batch with no CPU or smaller-batch fallback."""

        if (
            not isinstance(images, Sequence)
            or isinstance(images, (str, bytes, bytearray, Mapping))
            or not 1 <= len(images) <= MAX_VISUAL_QUERY_BATCH_SIZE
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], bytes)
                or not item[0]
                or not isinstance(item[1], str)
                for item in images
            )
        ):
            raise WeakCaseVisualIndexError(
                "embedding_batch_invalid",
                "The local CLIP image batch is invalid",
            )
        self._ensure_runtime()
        assert self._processor is not None
        assert self._model is not None
        assert self._torch is not None
        assert self._image_module is not None
        assert self._device is not None
        decoded_images = []
        try:
            for body, _media_type in images:
                with self._image_module.open(
                    io.BytesIO(body)
                ) as opened:
                    decoded_images.append(opened.convert("RGB"))
            inputs = self._processor(
                images=decoded_images,
                return_tensors="pt",
            )
            pixel_values = inputs["pixel_values"].to(self._device)
            with self._torch.inference_mode():
                features = self._model.get_image_features(
                    pixel_values=pixel_values
                )
            rows = features.float().cpu().tolist()
            if (
                not isinstance(rows, list)
                or len(rows) != len(images)
                or any(not isinstance(row, list) for row in rows)
            ):
                raise WeakCaseVisualIndexError(
                    "embedding_batch_result_invalid",
                    "Local CLIP returned an invalid image batch",
                )
            return tuple(
                tuple(float(value) for value in row)
                for row in rows
            )
        except WeakCaseVisualIndexError:
            raise
        except Exception as error:
            cuda_oom_type = getattr(
                getattr(self._torch, "cuda", None),
                "OutOfMemoryError",
                None,
            )
            is_cuda_oom = (
                self._device.startswith("cuda")
                and (
                    (
                        isinstance(cuda_oom_type, type)
                        and isinstance(error, cuda_oom_type)
                    )
                    or "out of memory" in str(error).casefold()
                )
            )
            if is_cuda_oom:
                try:
                    self._torch.cuda.empty_cache()
                except Exception:
                    pass
                raise WeakCaseVisualIndexError(
                    "embedding_batch_oom",
                    (
                        "Local CLIP batch exceeded GPU memory; no CPU "
                        "fallback was used"
                    ),
                ) from error
            raise WeakCaseVisualIndexError(
                "embedding_inference_failed",
                "Local CLIP image embedding failed",
            ) from error
        finally:
            for image in decoded_images:
                try:
                    image.close()
                except Exception:
                    pass

    def observe_road_scene(
        self,
        body: bytes,
        media_type: str,
        expected_sha256: str,
    ) -> Mapping[str, Any]:
        """Return local-only road-view vs non-road-view support.

        This is a conservative evidence-usability signal.  It does not
        identify a maintenance defect and cannot create review, annotation,
        incident, training, or report truth.
        """

        if (
            media_type != "image/jpeg"
            or not isinstance(body, bytes)
            or not 1 <= len(body) <= 32 * 1024 * 1024
            or not isinstance(expected_sha256, str)
            or _SHA256.fullmatch(expected_sha256) is None
            or hashlib.sha256(body).hexdigest() != expected_sha256
        ):
            raise WeakCaseVisualIndexError(
                "road_scene_evidence_invalid",
                "Road-scene evidence is not an intact bounded JPEG",
            )
        cached = self._road_scene_observation_cache.get(
            expected_sha256
        )
        if cached is not None:
            return dict(cached)
        self._ensure_runtime()
        assert self._processor is not None
        assert self._model is not None
        assert self._torch is not None
        assert self._image_module is not None
        assert self._device is not None
        prompts = (*_ROAD_SCENE_PROMPTS, *_NON_ROAD_VIEW_PROMPTS)
        try:
            with self._lock:
                if self._road_scene_text_features is None:
                    text_inputs = self._processor(
                        text=list(prompts),
                        return_tensors="pt",
                        padding=True,
                    )
                    text_inputs = {
                        key: value.to(self._device)
                        for key, value in text_inputs.items()
                    }
                    with self._torch.inference_mode():
                        text_features = (
                            self._model.get_text_features(
                                **text_inputs
                            ).float()
                        )
                    self._road_scene_text_features = (
                        text_features
                        / text_features.norm(
                            p=2,
                            dim=-1,
                            keepdim=True,
                        ).clamp_min(1e-12)
                    )
                with self._image_module.open(
                    io.BytesIO(body)
                ) as opened:
                    image = opened.convert("RGB")
                    image_inputs = self._processor(
                        images=image,
                        return_tensors="pt",
                    )
                pixel_values = image_inputs["pixel_values"].to(
                    self._device
                )
                with self._torch.inference_mode():
                    image_features = (
                        self._model.get_image_features(
                            pixel_values=pixel_values
                        ).float()
                    )
                    image_features = image_features / image_features.norm(
                        p=2,
                        dim=-1,
                        keepdim=True,
                    ).clamp_min(1e-12)
                    scale = self._model.logit_scale.exp().float()
                    probabilities = (
                        scale
                        * image_features
                        @ self._road_scene_text_features.T
                    )[0].softmax(dim=-1)
                road_probability = float(
                    probabilities[
                        : len(_ROAD_SCENE_PROMPTS)
                    ].sum().item()
                )
                non_road_probability = float(
                    probabilities[
                        len(_ROAD_SCENE_PROMPTS) :
                    ].sum().item()
                )
                total = road_probability + non_road_probability
                if (
                    not math.isfinite(total)
                    or total <= 0.0
                ):
                    raise ValueError(
                        "road-scene probability is invalid"
                    )
                road_probability /= total
                non_road_probability /= total
                result = {
                    "schema_version": (
                        LOCAL_ROAD_SCENE_OBSERVATION_SCHEMA
                    ),
                    "status": "completed",
                    "image_sha256": expected_sha256,
                    "model_identity_sha256": hashlib.sha256(
                        _canonical_json_bytes(self._identity)
                    ).hexdigest(),
                    "prompt_set_sha256": (
                        _ROAD_SCENE_PROMPT_SET_SHA256
                    ),
                    "road_scene_probability": round(
                        road_probability, 8
                    ),
                    "non_road_view_probability": round(
                        non_road_probability, 8
                    ),
                    "local_only": True,
                    "review_only": True,
                    "automatic_annotation_creation": False,
                    "automatic_incident_creation": False,
                    "automatic_training_membership": False,
                    "automatic_report_truth": False,
                }
                if len(self._road_scene_observation_cache) >= 1_024:
                    oldest = next(
                        iter(self._road_scene_observation_cache)
                    )
                    self._road_scene_observation_cache.pop(
                        oldest, None
                    )
                self._road_scene_observation_cache[
                    expected_sha256
                ] = result
                return dict(result)
        except WeakCaseVisualIndexError:
            raise
        except Exception as error:
            raise WeakCaseVisualIndexError(
                "road_scene_inference_failed",
                "Local CLIP road-scene observation failed",
            ) from error


class WeakCaseVisualIndexService:
    """Build and query one project-scoped, immutable reference index."""

    def __init__(
        self,
        data_root: Path | str,
        *,
        catalog: WeakCaseVisualCatalog,
        embedder: WeakCaseImageEmbedder,
        approvals: Optional[ProjectReferenceApprovals] = None,
        limits: Optional[WeakCaseVisualIndexLimits] = None,
    ) -> None:
        root = Path(data_root)
        if not root.is_absolute():
            raise ValueError("visual-index data_root must be absolute")
        _assert_no_linked_existing_components(root)
        root.mkdir(parents=True, exist_ok=True)
        self._root = _ensure_real_directory(root)
        self._catalog = catalog
        self._embedder = embedder
        self._approvals = approvals
        self._limits = limits or WeakCaseVisualIndexLimits()
        self._image_limits = HwpxWeakCaseLimits(
            max_image_bytes=self._limits.max_candidate_image_bytes,
            max_total_image_bytes=self._limits.max_candidate_image_bytes,
        )
        self._lock = threading.RLock()

    def build(self, project_id: UUID) -> dict:
        """Embed every audited photo case and atomically activate the index."""

        project = _project_uuid(project_id)
        with self._lock:
            try:
                existing = self._load_active(project)
            except WeakCaseVisualIndexError as error:
                if error.code not in {
                    "visual_index_not_built",
                    "embedding_model_changed",
                    "visual_reference_source_changed",
                }:
                    raise
            else:
                return _build_result(existing, idempotent_replay=True)

            model_identity_before = _validated_model_identity(
                self._embedder.model_identity()
            )
            source_before = _validated_source_snapshot(
                self._catalog.visual_index_snapshot(),
                self._limits,
            )
            cases = []
            embedding_dimension: Optional[int] = None
            resolved_cases = []
            for source_case in source_before["cases"]:
                image = self._resolve_verified_image(source_case)
                resolved_cases.append((source_case, image))
            embed_many = getattr(self._embedder, "embed_images", None)
            embedded_rows = []
            if callable(embed_many):
                for offset in range(
                    0,
                    len(resolved_cases),
                    DEFAULT_VISUAL_QUERY_BATCH_SIZE,
                ):
                    batch = resolved_cases[
                        offset : offset
                        + DEFAULT_VISUAL_QUERY_BATCH_SIZE
                    ]
                    physical_images = [
                        (image.body, image.media_type)
                        for _source_case, image in batch
                    ]
                    physical_images.extend(
                        [physical_images[-1]]
                        * (
                            DEFAULT_VISUAL_QUERY_BATCH_SIZE
                            - len(physical_images)
                        )
                    )
                    raw_vectors = embed_many(tuple(physical_images))
                    if (
                        not isinstance(raw_vectors, Sequence)
                        or isinstance(
                            raw_vectors,
                            (str, bytes, bytearray, Mapping),
                        )
                        or len(raw_vectors)
                        != DEFAULT_VISUAL_QUERY_BATCH_SIZE
                    ):
                        raise WeakCaseVisualIndexError(
                            "embedding_batch_result_invalid",
                            (
                                "The embedding index batch result count "
                                "is invalid"
                            ),
                        )
                    embedded_rows.extend(
                        zip(batch, raw_vectors[: len(batch)])
                    )
            else:
                embedded_rows.extend(
                    (
                        (source_case, image),
                        self._embedder.embed_image(
                            image.body,
                            image.media_type,
                        ),
                    )
                    for source_case, image in resolved_cases
                )
            for (source_case, _image), raw_vector in embedded_rows:
                vector = _normalized_vector(
                    raw_vector,
                    limits=self._limits,
                    expected_dimension=embedding_dimension,
                )
                if embedding_dimension is None:
                    embedding_dimension = len(vector)
                case_without_embedding = dict(source_case)
                embedding_sha256 = _canonical_sha256(list(vector))
                cases.append(
                    {
                        **case_without_embedding,
                        "embedding": list(vector),
                        "embedding_sha256": embedding_sha256,
                    }
                )
            if embedding_dimension is None or not cases:
                raise WeakCaseVisualIndexError(
                    "visual_reference_cases_unavailable",
                    "No audited sample photo case was available for indexing",
                )

            model_identity_after = _validated_model_identity(
                self._embedder.model_identity()
            )
            source_after = _validated_source_snapshot(
                self._catalog.visual_index_snapshot(),
                self._limits,
            )
            if (
                model_identity_before != model_identity_after
                or _canonical_sha256(source_before)
                != _canonical_sha256(source_after)
            ):
                raise WeakCaseVisualIndexError(
                    "visual_index_inputs_changed",
                    "The sample source or embedding model changed during indexing",
                )
            source_snapshot_sha256 = _canonical_sha256(source_before)
            model_identity_sha256 = _canonical_sha256(
                model_identity_before
            )
            payload_without_hash = {
                "schema_version": WEAK_CASE_VISUAL_INDEX_SCHEMA,
                "project_id": str(project),
                "source_schema_version": (
                    WEAK_CASE_VISUAL_SOURCE_SCHEMA
                ),
                "source_snapshot_sha256": source_snapshot_sha256,
                "sources": source_before["sources"],
                "model_identity": model_identity_before,
                "model_identity_sha256": model_identity_sha256,
                "embedding_dimension": embedding_dimension,
                "case_count": len(cases),
                "cases": cases,
                "reference_state": "weak_unconfirmed",
                "review_required_before_promotion": True,
                "automatic_annotation_creation": False,
                "automatic_incident_creation": False,
                "automatic_training_ingest": False,
                "annotation_count": 0,
                "incident_count": 0,
                "training_eligible_count": 0,
            }
            index_manifest_sha256 = _canonical_sha256(
                payload_without_hash
            )
            payload = {
                **payload_without_hash,
                "index_manifest_sha256": index_manifest_sha256,
            }
            raw = _canonical_json_bytes(payload)
            if len(raw) > self._limits.max_index_bytes:
                raise WeakCaseVisualIndexError(
                    "visual_index_too_large",
                    "The visual reference index exceeds its storage bound",
                )
            project_directory = self._project_directory(project)
            index_name = (
                f"weak-reference-index-{index_manifest_sha256}.json"
            )
            index_path = project_directory / index_name
            _write_content_addressed(
                index_path,
                raw,
                maximum=self._limits.max_index_bytes,
            )
            pointer_without_hash = {
                "schema_version": WEAK_CASE_VISUAL_ACTIVE_SCHEMA,
                "project_id": str(project),
                "index_file": index_name,
                "index_manifest_sha256": index_manifest_sha256,
                "source_snapshot_sha256": source_snapshot_sha256,
                "model_identity_sha256": model_identity_sha256,
            }
            pointer = {
                **pointer_without_hash,
                "pointer_sha256": _canonical_sha256(
                    pointer_without_hash
                ),
            }
            pointer_raw = _canonical_json_bytes(pointer)
            if len(pointer_raw) > self._limits.max_active_pointer_bytes:
                raise WeakCaseVisualIndexError(
                    "visual_index_pointer_too_large",
                    "The visual reference pointer exceeds its storage bound",
                )
            _write_atomic_replace(
                project_directory / _ACTIVE_FILE,
                pointer_raw,
            )
            return _build_result(payload, idempotent_replay=False)

    def query(
        self,
        project_id: UUID,
        candidate: CandidateFrameEvidence,
        *,
        issue_type: Optional[str],
        query_text: Optional[str],
        limit: int,
    ) -> dict:
        """Return 3-5 combined visual and existing metadata matches."""

        if not isinstance(candidate, CandidateFrameEvidence):
            raise WeakCaseVisualIndexError(
                "cross_project_candidate_rejected",
                "The candidate frame belongs to another project",
            )
        result = self.query_many(
            project_id,
            (candidate,),
            issue_types_by_candidate={
                candidate.candidate_id: issue_type,
            },
            query_texts_by_candidate={
                candidate.candidate_id: query_text,
            },
            limit=limit,
        )
        return result[candidate.candidate_id]

    def query_many(
        self,
        project_id: UUID,
        candidates: Sequence[CandidateFrameEvidence],
        *,
        issue_types_by_candidate: Mapping[str, Optional[str]],
        query_texts_by_candidate: Optional[
            Mapping[str, Optional[str]]
        ] = None,
        limit: int,
    ) -> dict[str, dict]:
        """Return query-equivalent results using canonical bounded batches."""

        project = _project_uuid(project_id)
        if (
            not isinstance(candidates, Sequence)
            or isinstance(
                candidates, (str, bytes, bytearray, Mapping)
            )
            or not 1
            <= len(candidates)
            <= self._limits.max_query_candidates
            or not isinstance(issue_types_by_candidate, Mapping)
            or (
                query_texts_by_candidate is not None
                and not isinstance(query_texts_by_candidate, Mapping)
            )
            or self._limits.max_query_batch_size
            != DEFAULT_VISUAL_QUERY_BATCH_SIZE
        ):
            raise WeakCaseVisualIndexError(
                "visual_query_batch_invalid",
                "The visual reference query batch is invalid",
            )
        candidate_ids = [
            candidate.candidate_id
            if isinstance(candidate, CandidateFrameEvidence)
            else None
            for candidate in candidates
        ]
        if (
            any(not isinstance(value, str) for value in candidate_ids)
            or len(candidate_ids) != len(set(candidate_ids))
            or any(
                key not in set(candidate_ids)
                for key in issue_types_by_candidate
            )
            or (
                query_texts_by_candidate is not None
                and any(
                    key not in set(candidate_ids)
                    for key in query_texts_by_candidate
                )
            )
        ):
            raise WeakCaseVisualIndexError(
                "visual_query_batch_invalid",
                "Visual query candidate identities are invalid",
            )
        validated = []
        for candidate in candidates:
            issue_type = issue_types_by_candidate.get(
                candidate.candidate_id
            )
            query_text = (
                query_texts_by_candidate.get(candidate.candidate_id)
                if query_texts_by_candidate is not None
                else None
            )
            self._validate_query_options(
                issue_type=issue_type,
                query_text=query_text,
                limit=limit,
            )
            body, detected_media = self._validated_candidate_evidence(
                project,
                candidate,
            )
            validated.append(
                (
                    candidate,
                    issue_type,
                    query_text,
                    body,
                    detected_media,
                )
            )

        with self._lock:
            index = self._load_active(project)
            model_sha256 = index["model_identity_sha256"]
            if (
                _canonical_sha256(
                    _validated_model_identity(
                        self._embedder.model_identity()
                    )
                )
                != model_sha256
            ):
                raise WeakCaseVisualIndexError(
                    "embedding_model_changed",
                    "The embedding model changed before visual batch search",
                )
            approvals = self._project_approvals(
                project,
                index["cases"],
            )
            embed_many = getattr(
                self._embedder,
                "embed_images",
                None,
            )
            responses: dict[str, dict] = {}
            for offset in range(
                0,
                len(validated),
                DEFAULT_VISUAL_QUERY_BATCH_SIZE,
            ):
                batch = validated[
                    offset : offset
                    + DEFAULT_VISUAL_QUERY_BATCH_SIZE
                ]
                if callable(embed_many):
                    # CUDA fp16 kernels can return observably different
                    # vectors for batch-size 1 and batch-size N.  Every batch,
                    # including the single-candidate ``query`` compatibility
                    # path and the final partial batch, therefore uses one
                    # fixed physical width.  CLIP inference is row-independent
                    # in eval mode, so deterministic tail padding preserves
                    # candidate order while keeping the public query result
                    # exactly comparable across both APIs.
                    physical_images = [
                        (body, detected_media)
                        for (
                            _candidate,
                            _issue_type,
                            _query_text,
                            body,
                            detected_media,
                        ) in batch
                    ]
                    physical_width = (
                        DEFAULT_VISUAL_QUERY_BATCH_SIZE
                    )
                    physical_images.extend(
                        [physical_images[-1]]
                        * (physical_width - len(physical_images))
                    )
                    raw_vectors = embed_many(
                        tuple(physical_images)
                    )
                else:
                    raw_vectors = tuple(
                        self._embedder.embed_image(
                            body,
                            detected_media,
                        )
                        for (
                            _candidate,
                            _issue_type,
                            _query_text,
                            body,
                            detected_media,
                        ) in batch
                    )
                if (
                    not isinstance(raw_vectors, Sequence)
                    or isinstance(
                        raw_vectors,
                        (str, bytes, bytearray, Mapping),
                    )
                    or len(raw_vectors)
                    != (
                        DEFAULT_VISUAL_QUERY_BATCH_SIZE
                        if callable(embed_many)
                        else len(batch)
                    )
                ):
                    raise WeakCaseVisualIndexError(
                        "embedding_batch_result_invalid",
                        "The embedding batch result count is invalid",
                    )
                for row, raw_vector in zip(
                    batch,
                    raw_vectors[: len(batch)],
                ):
                    (
                        candidate,
                        issue_type,
                        query_text,
                        _body,
                        _detected_media,
                    ) = row
                    vector = _normalized_vector(
                        raw_vector,
                        limits=self._limits,
                        expected_dimension=int(
                            index["embedding_dimension"]
                        ),
                    )
                    responses[candidate.candidate_id] = (
                        self._query_response(
                            project=project,
                            index=index,
                            candidate=candidate,
                            vector=vector,
                            issue_type=issue_type,
                            query_text=query_text,
                            limit=limit,
                            approvals=approvals,
                        )
                    )
            if (
                _canonical_sha256(
                    _validated_model_identity(
                        self._embedder.model_identity()
                    )
                )
                != model_sha256
            ):
                raise WeakCaseVisualIndexError(
                    "embedding_model_changed",
                    "The embedding model changed during visual batch search",
                )
            return responses

    def _validate_query_options(
        self,
        *,
        issue_type: Optional[str],
        query_text: Optional[str],
        limit: int,
    ) -> None:
        if not 3 <= limit <= 5:
            raise WeakCaseVisualIndexError(
                "invalid_limit",
                "Visual reference search limit must be between 3 and 5",
            )
        if issue_type is not None and (
            not isinstance(issue_type, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", issue_type)
            is None
        ):
            raise WeakCaseVisualIndexError(
                "invalid_issue_type",
                "The visual reference issue type is invalid",
            )
        if query_text is not None and (
            not isinstance(query_text, str)
            or len(query_text) > 4_096
        ):
            raise WeakCaseVisualIndexError(
                "invalid_query_text",
                "The visual reference query text is invalid",
            )

    def _validated_candidate_evidence(
        self,
        project: UUID,
        candidate: CandidateFrameEvidence,
    ) -> tuple[bytes, str]:
        if (
            not isinstance(candidate, CandidateFrameEvidence)
            or candidate.project_id != project
        ):
            raise WeakCaseVisualIndexError(
                "cross_project_candidate_rejected",
                "The candidate frame belongs to another project",
            )
        if (
            not isinstance(candidate.candidate_id, str)
            or _CANDIDATE_ID.fullmatch(candidate.candidate_id) is None
        ):
            raise WeakCaseVisualIndexError(
                "invalid_candidate_id",
                "The candidate frame identifier is invalid",
            )
        body = candidate.body
        if (
            not isinstance(body, bytes)
            or not body
            or len(body) > self._limits.max_candidate_image_bytes
        ):
            raise WeakCaseVisualIndexError(
                "candidate_image_invalid",
                "The candidate frame bytes are invalid",
            )
        if (
            not isinstance(candidate.expected_sha256, str)
            or _SHA256.fullmatch(candidate.expected_sha256) is None
            or hashlib.sha256(body).hexdigest()
            != candidate.expected_sha256
        ):
            raise WeakCaseVisualIndexError(
                "candidate_image_hash_mismatch",
                "The candidate frame hash does not match its evidence",
            )
        try:
            detected_media, _width, _height = _inspect_image_header(
                body,
                limits=self._image_limits,
            )
        except Exception as error:
            raise WeakCaseVisualIndexError(
                "candidate_image_invalid",
                "The candidate frame is not a supported bounded image",
            ) from error
        if detected_media != candidate.media_type:
            raise WeakCaseVisualIndexError(
                "candidate_image_media_mismatch",
                "The candidate frame media type does not match its bytes",
            )
        return body, detected_media

    def _query_response(
        self,
        *,
        project: UUID,
        index: Mapping[str, Any],
        candidate: CandidateFrameEvidence,
        vector: Sequence[float],
        issue_type: Optional[str],
        query_text: Optional[str],
        limit: int,
        approvals: Mapping[tuple[str, str], Mapping[str, Any]],
    ) -> dict:
        ranked = []
        for record in index["cases"]:
            stored_vector = tuple(record["embedding"])
            cosine = max(
                -1.0,
                min(
                    1.0,
                    math.fsum(
                        left * right
                        for left, right in zip(
                            vector,
                            stored_vector,
                        )
                    ),
                ),
            )
            text_score, matched_terms = score_weak_case_metadata(
                search_texts=record["search_texts"],
                caption=record.get("caption"),
                issue_type=issue_type,
                query_text=query_text,
            )
            combined = (
                0.8 * ((cosine + 1.0) / 2.0)
                + 0.2 * min(1.0, text_score / 60.0)
            )
            ranked.append(
                (
                    -combined,
                    -cosine,
                    -text_score,
                    int(record["quarter"]),
                    int(record["photo_page_number"]),
                    str(record["case_id"]),
                    record,
                    cosine,
                    text_score,
                    matched_terms,
                    combined,
                )
            )
        ranked.sort(key=lambda row: row[:6])
        response_cases = []
        for rank, row in enumerate(ranked[:limit], start=1):
            (
                _negative_combined,
                _negative_cosine,
                _negative_text,
                _quarter,
                _page,
                _case_id,
                record,
                cosine,
                text_score,
                matched_terms,
                combined,
            ) = row
            approval = approvals.get(
                (record["source_id"], record["case_id"])
            )
            public = {
                key: value
                for key, value in record.items()
                if key not in {"embedding", "search_texts"}
            }
            public.pop("embedding_sha256", None)
            public.update(
                {
                    "rank": rank,
                    "visual_cosine_score": round(cosine, 8),
                    "text_structure_score": text_score,
                    "matched_terms": list(matched_terms[:8]),
                    "combined_similarity_score": round(
                        combined,
                        8,
                    ),
                }
            )
            if approval is not None:
                public.update(
                    {
                        "reference_state": "approved_reference",
                        "review_required": False,
                        "approved_reference": True,
                        "training_eligible": True,
                        "approval_id": approval["approval_id"],
                        "approval_revision": approval["revision"],
                        "approval_record_sha256": approval[
                            "record_sha256"
                        ],
                        "approved_issue_type": approval.get(
                            "issue_type"
                        ),
                        "approved_action": approval.get(
                            "maintenance_action"
                        ),
                    }
                )
            response_cases.append(public)
        approved_count = sum(
            case["reference_state"] == "approved_reference"
            for case in response_cases
        )
        return {
            "schema_version": WEAK_CASE_VISUAL_QUERY_SCHEMA,
            "project_id": str(project),
            "candidate_id": candidate.candidate_id,
            "candidate_sha256": candidate.expected_sha256,
            "index_manifest_sha256": index[
                "index_manifest_sha256"
            ],
            "source_snapshot_sha256": index[
                "source_snapshot_sha256"
            ],
            "model_identity_sha256": index[
                "model_identity_sha256"
            ],
            "search_mode": (
                "local_image_cosine_plus_bounded_text_structure"
            ),
            "semantic_image_embedding_used": True,
            "issue_type": issue_type,
            "query_text": _normalize_query_text(query_text),
            "requested_limit": limit,
            "result_count": len(response_cases),
            "cases": response_cases,
            "weak_unconfirmed_count": (
                len(response_cases) - approved_count
            ),
            "approved_reference_count": approved_count,
            "review_required_before_promotion": True,
            "automatic_annotation_creation": False,
            "automatic_incident_creation": False,
            "automatic_training_ingest": False,
            "annotation_count": 0,
            "incident_count": 0,
        }

    def _resolve_verified_image(
        self,
        record: Mapping[str, Any],
    ) -> ResolvedWeakCaseImage:
        try:
            image = self._catalog.resolve_case_image_for_visual_index(
                str(record["source_id"]),
                str(record["case_id"]),
            )
        except WeakCaseCatalogError as error:
            raise WeakCaseVisualIndexError(
                error.code,
                str(error),
            ) from error
        if (
            image.sha256 != record["image_sha256"]
            or image.media_type != record["image_media_type"]
            or image.size_bytes != record["image_size_bytes"]
        ):
            raise WeakCaseVisualIndexError(
                "image_integrity_mismatch",
                "A sample image changed after catalog indexing",
            )
        return image

    def _project_directory(self, project_id: UUID) -> Path:
        expected = self._root / str(project_id)
        if expected.parent != self._root:
            raise WeakCaseVisualIndexError(
                "project_path_rejected",
                "The visual-index project path escaped its data root",
            )
        try:
            expected.mkdir(mode=0o700, exist_ok=True)
        except OSError as error:
            raise WeakCaseVisualIndexError(
                "visual_index_storage_unavailable",
                "The visual-index project directory is unavailable",
            ) from error
        resolved = _ensure_real_directory(expected)
        if resolved.parent != self._root or resolved.name != str(project_id):
            raise WeakCaseVisualIndexError(
                "project_path_rejected",
                "The visual-index project path escaped its data root",
            )
        return resolved

    def _load_active(self, project_id: UUID) -> dict:
        project_directory = self._project_directory(project_id)
        pointer_path = project_directory / _ACTIVE_FILE
        pointer = _read_json_bounded(
            pointer_path,
            maximum=self._limits.max_active_pointer_bytes,
            missing_code="visual_index_not_built",
        )
        required_pointer_keys = {
            "schema_version",
            "project_id",
            "index_file",
            "index_manifest_sha256",
            "source_snapshot_sha256",
            "model_identity_sha256",
            "pointer_sha256",
        }
        if set(pointer) != required_pointer_keys:
            raise WeakCaseVisualIndexError(
                "visual_index_pointer_invalid",
                "The visual reference pointer is invalid",
            )
        pointer_hash = pointer.pop("pointer_sha256")
        if (
            pointer.get("schema_version")
            != WEAK_CASE_VISUAL_ACTIVE_SCHEMA
            or pointer.get("project_id") != str(project_id)
            or not isinstance(pointer_hash, str)
            or _SHA256.fullmatch(pointer_hash) is None
            or _canonical_sha256(pointer) != pointer_hash
        ):
            raise WeakCaseVisualIndexError(
                "visual_index_pointer_invalid",
                "The visual reference pointer failed integrity checks",
            )
        index_file = pointer.get("index_file")
        if (
            not isinstance(index_file, str)
            or _INDEX_FILE.fullmatch(index_file) is None
            or _INDEX_FILE.fullmatch(index_file).group(1)
            != pointer.get("index_manifest_sha256")
        ):
            raise WeakCaseVisualIndexError(
                "visual_index_pointer_invalid",
                "The active visual reference file name is invalid",
            )
        index_path = project_directory / index_file
        if index_path.parent != project_directory:
            raise WeakCaseVisualIndexError(
                "visual_index_path_rejected",
                "The active visual reference path escaped its project",
            )
        index = _read_json_bounded(
            index_path,
            maximum=self._limits.max_index_bytes,
            missing_code="visual_index_missing",
        )
        validated = _validated_index_payload(
            index,
            project_id=project_id,
            limits=self._limits,
        )
        if (
            validated["index_manifest_sha256"]
            != pointer["index_manifest_sha256"]
            or validated["source_snapshot_sha256"]
            != pointer["source_snapshot_sha256"]
            or validated["model_identity_sha256"]
            != pointer["model_identity_sha256"]
        ):
            raise WeakCaseVisualIndexError(
                "visual_index_pointer_invalid",
                "The active pointer does not match the visual index",
            )
        current_model = _validated_model_identity(
            self._embedder.model_identity()
        )
        if (
            _canonical_sha256(current_model)
            != validated["model_identity_sha256"]
        ):
            raise WeakCaseVisualIndexError(
                "embedding_model_changed",
                "The active visual index uses another embedding model",
            )
        current_source = _validated_source_snapshot(
            self._catalog.visual_index_snapshot(),
            self._limits,
        )
        if (
            _canonical_sha256(current_source)
            != validated["source_snapshot_sha256"]
        ):
            raise WeakCaseVisualIndexError(
                "visual_reference_source_changed",
                "The active visual index uses stale sample evidence",
            )
        indexed_source_cases = [
            {
                key: value
                for key, value in case.items()
                if key not in {"embedding", "embedding_sha256"}
            }
            for case in validated["cases"]
        ]
        if (
            validated["sources"] != current_source["sources"]
            or indexed_source_cases != current_source["cases"]
        ):
            raise WeakCaseVisualIndexError(
                "visual_index_invalid",
                "The active visual index metadata does not match sample evidence",
            )
        return validated

    def _project_approvals(
        self,
        project_id: UUID,
        cases: Sequence[Mapping[str, Any]],
    ) -> dict[tuple[str, str], Mapping[str, Any]]:
        if self._approvals is None:
            return {}
        snapshot = self._approvals.list_project(project_id)
        if not isinstance(snapshot, Mapping):
            raise WeakCaseVisualIndexError(
                "approval_store_invalid",
                "The project approval snapshot is invalid",
            )
        records = snapshot.get("records")
        if not isinstance(records, list):
            raise WeakCaseVisualIndexError(
                "approval_store_invalid",
                "The project approval records are invalid",
            )
        current = {
            (str(case["source_id"]), str(case["case_id"])): case
            for case in cases
        }
        approved: dict[tuple[str, str], Mapping[str, Any]] = {}
        for record in records:
            if not isinstance(record, Mapping):
                raise WeakCaseVisualIndexError(
                    "approval_store_invalid",
                    "A project approval record is invalid",
                )
            if record.get("project_id") != str(project_id):
                raise WeakCaseVisualIndexError(
                    "cross_project_approval_rejected",
                    "A reference approval belongs to another project",
                )
            identity = (
                str(record.get("source_id", "")),
                str(record.get("case_id", "")),
            )
            case = current.get(identity)
            if case is None:
                raise WeakCaseVisualIndexError(
                    "stale_approval_reference",
                    "A project approval no longer resolves to this index",
                )
            expected_pairs = (
                ("document_sha256", "document_sha256"),
                ("index_manifest_sha256", "index_manifest_sha256"),
                ("image_sha256", "image_sha256"),
                (
                    "sample_layout_binding_sha256",
                    "sample_layout_binding_sha256",
                ),
                (
                    "sample_layout_binding_schema",
                    "sample_layout_binding_schema",
                ),
                (
                    "sample_layout_binding_revision",
                    "sample_layout_binding_revision",
                ),
                (
                    "semantic_projection_revision",
                    "semantic_projection_revision",
                ),
                (
                    "semantic_attestation_sha256",
                    "semantic_attestation_sha256",
                ),
                (
                    "sample_evidence_group_id",
                    "sample_evidence_group_id",
                ),
                (
                    "group_photo_index",
                    "group_photo_index",
                ),
                (
                    "group_photo_count",
                    "group_photo_count",
                ),
                (
                    "duplicate_image_occurrence_count",
                    "duplicate_image_occurrence_count",
                ),
                (
                    "duplicate_image_review_required",
                    "duplicate_image_review_required",
                ),
                (
                    "duplicate_image_case_refs",
                    "duplicate_image_case_refs",
                ),
            )
            if (
                record.get("reference_state") != "approved_reference"
                or record.get("approved_reference") is not True
                or record.get("user_confirmed") is not True
                or record.get("training_eligible") is not True
                or record.get("annotation_created") is not False
                or record.get("incident_created") is not False
                or any(
                    record.get(left) != case.get(right)
                    for left, right in expected_pairs
                )
            ):
                raise WeakCaseVisualIndexError(
                    "stale_approval_reference",
                    "A project approval does not match current sample evidence",
                )
            approval_id = record.get("approval_id")
            revision = record.get("revision")
            record_sha256 = record.get("record_sha256")
            if (
                not isinstance(approval_id, str)
                or not approval_id
                or isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
                or not isinstance(record_sha256, str)
                or _SHA256.fullmatch(record_sha256) is None
                or identity in approved
            ):
                raise WeakCaseVisualIndexError(
                    "approval_store_invalid",
                    "A project approval identity is invalid",
                )
            approved[identity] = record
        return approved


def _clip_pack_identity(pack: VisionModelPack) -> dict:
    artifacts = [
        {
            "relative_path": relative,
            "size_bytes": artifact.size_bytes,
            "sha256": artifact.sha256,
        }
        for relative, artifact in sorted(pack.artifacts.items())
    ]
    model_sha256 = _canonical_sha256(
        {
            "manifest_sha256": pack.manifest_sha256,
            "artifacts": artifacts,
        }
    )
    return {
        "schema_version": IMAGE_EMBEDDING_MODEL_SCHEMA,
        "model_id": pack.pack_id,
        "model_version": pack.pack_version,
        "engine": "transformers_clip_image_features_fixed_batch8",
        "model_sha256": model_sha256,
        "manifest_sha256": pack.manifest_sha256,
        "local_only": True,
        "network_download_allowed": False,
    }


def _build_result(
    index: Mapping[str, Any],
    *,
    idempotent_replay: bool,
) -> dict:
    project_id = str(index["project_id"])
    manifest_sha256 = str(index["index_manifest_sha256"])
    case_count = int(index["case_count"])
    return {
        "schema_version": WEAK_CASE_VISUAL_INDEX_SCHEMA,
        "project_id": project_id,
        "state": "ready",
        "idempotency_key": (
            f"visual-index:{project_id}:{manifest_sha256}"
        ),
        "idempotent_replay": idempotent_replay,
        "index_manifest_sha256": manifest_sha256,
        "source_snapshot_sha256": index[
            "source_snapshot_sha256"
        ],
        "model_identity_sha256": index["model_identity_sha256"],
        "embedding_dimension": index["embedding_dimension"],
        "case_count": case_count,
        "weak_unconfirmed_count": case_count,
        "approved_reference_count": 0,
        "annotation_count": 0,
        "incident_count": 0,
        "training_eligible_count": 0,
        "automatic_training_ingest": False,
    }


def _validated_model_identity(value: Mapping[str, Any]) -> dict:
    required = {
        "schema_version",
        "model_id",
        "model_version",
        "engine",
        "model_sha256",
        "manifest_sha256",
        "local_only",
        "network_download_allowed",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise WeakCaseVisualIndexError(
            "embedding_model_identity_invalid",
            "The image-embedding model identity is invalid",
        )
    result = dict(value)
    if (
        result["schema_version"] != IMAGE_EMBEDDING_MODEL_SCHEMA
        or not isinstance(result["model_id"], str)
        or _MODEL_TOKEN.fullmatch(result["model_id"]) is None
        or not isinstance(result["model_version"], str)
        or _MODEL_TOKEN.fullmatch(result["model_version"]) is None
        or not isinstance(result["engine"], str)
        or _MODEL_TOKEN.fullmatch(result["engine"]) is None
        or not isinstance(result["model_sha256"], str)
        or _SHA256.fullmatch(result["model_sha256"]) is None
        or not isinstance(result["manifest_sha256"], str)
        or _SHA256.fullmatch(result["manifest_sha256"]) is None
        or result["local_only"] is not True
        or result["network_download_allowed"] is not False
    ):
        raise WeakCaseVisualIndexError(
            "embedding_model_identity_invalid",
            "The image-embedding model identity failed validation",
        )
    return result


def _validated_pdf_companion(
    value: Any,
    *,
    source: Mapping[str, Any],
) -> dict:
    if (
        not isinstance(value, Mapping)
        or set(value) != _PDF_COMPANION_REQUIRED_KEYS
    ):
        raise WeakCaseVisualIndexError(
            "visual_reference_source_invalid",
            "The official PDF companion has an invalid schema",
        )
    result = dict(value)
    status = result.get("status")
    if status != "ready":
        if (
            not isinstance(status, str)
            or re.fullmatch(r"pdf_[a-z0-9_]{1,95}", status) is None
        ):
            raise WeakCaseVisualIndexError(
                "visual_reference_source_invalid",
                "The official PDF companion has an invalid state",
            )
        raise WeakCaseVisualIndexError(
            f"visual_reference_{status}",
            (
                "The official PDF companion is not ready for "
                f"cross-validation ({status})"
            ),
        )
    document_name = result.get("document_name")
    page_count = result.get("page_count")
    expected_page_count = result.get("expected_page_count")
    pages = result.get("pages")
    if (
        result.get("schema_version") != PDF_COMPANION_SCHEMA
        or result.get("role") != PDF_COMPANION_ROLE
        or result.get("quarter") != source.get("quarter")
        or not isinstance(document_name, str)
        or Path(document_name).name != document_name
        or not document_name.casefold().endswith(".pdf")
        or Path(document_name).stem
        != Path(str(source.get("document_name", ""))).stem
        or any(
            not isinstance(result.get(field), str)
            or _SHA256.fullmatch(result[field]) is None
            for field in (
                "pdf_sha256",
                "expected_pdf_sha256",
                "full_text_sha256",
                "audit_sha256",
            )
        )
        or result.get("pdf_sha256")
        != result.get("expected_pdf_sha256")
        or isinstance(result.get("size_bytes"), bool)
        or not isinstance(result.get("size_bytes"), int)
        or result["size_bytes"] <= 0
        or isinstance(page_count, bool)
        or not isinstance(page_count, int)
        or isinstance(expected_page_count, bool)
        or not isinstance(expected_page_count, int)
        or not 1 <= page_count <= 256
        or page_count != expected_page_count
        or not isinstance(pages, list)
        or len(pages) != page_count
        or isinstance(result.get("full_text_char_count"), bool)
        or not isinstance(result.get("full_text_char_count"), int)
        or result["full_text_char_count"] < 0
        or result.get("text_hash_method")
        != "sha256(nfc_lf_normalized_pypdf_text_utf8)"
        or result.get("full_text_hash_method")
        != (
            "sha256(form_feed_joined_normalized_page_text_utf8)"
        )
        or result.get("parser") != "pypdf"
        or not isinstance(result.get("parser_version"), str)
        or not 1 <= len(result["parser_version"]) <= 64
        or result.get("strict_parsing") is not True
        or result.get("pdf_images_added_as_cases") is not False
        or result.get("case_source")
        != "paired_hwpx_photo_slots_only"
        or result.get("automatic_annotation_creation") is not False
        or result.get("automatic_incident_creation") is not False
        or result.get("automatic_training_ingest") is not False
    ):
        raise WeakCaseVisualIndexError(
            "visual_reference_source_invalid",
            "The official PDF companion failed provenance validation",
        )

    normalized_pages = []
    page_by_number = {}
    for expected_number, page in enumerate(pages, start=1):
        if (
            not isinstance(page, Mapping)
            or set(page) != _PDF_PAGE_KEYS
        ):
            raise WeakCaseVisualIndexError(
                "visual_reference_source_invalid",
                "An official PDF page hash has an invalid schema",
            )
        record = dict(page)
        if (
            record.get("page_number") != expected_number
            or not isinstance(record.get("text_sha256"), str)
            or _SHA256.fullmatch(record["text_sha256"]) is None
            or isinstance(record.get("text_char_count"), bool)
            or not isinstance(record.get("text_char_count"), int)
            or record["text_char_count"] < 0
            or record.get("text_extraction_status") != "extracted"
        ):
            raise WeakCaseVisualIndexError(
                "visual_reference_source_invalid",
                "An official PDF page hash failed validation",
            )
        normalized_pages.append(record)
        page_by_number[expected_number] = record
    if sum(
        page["text_char_count"] for page in normalized_pages
    ) != result["full_text_char_count"]:
        raise WeakCaseVisualIndexError(
            "visual_reference_source_invalid",
            "The official PDF text counts do not match",
        )

    render = result.get("render")
    if (
        not isinstance(render, Mapping)
        or set(render) != _PDF_RENDER_KEYS
        or render.get("status") != "smoke_passed"
        or render.get("renderer") != "pdftoppm"
        or not isinstance(render.get("renderer_version"), str)
        or not 1 <= len(render["renderer_version"]) <= 256
        or isinstance(render.get("render_dpi"), bool)
        or not isinstance(render.get("render_dpi"), int)
        or not 18 <= render["render_dpi"] <= 300
        or not isinstance(render.get("requested_pages"), list)
        or not 1 <= len(render["requested_pages"]) <= 3
        or not isinstance(render.get("rendered_pages"), list)
        or len(render["rendered_pages"])
        != len(render["requested_pages"])
    ):
        raise WeakCaseVisualIndexError(
            "visual_reference_pdf_render_unavailable",
            "The official PDF companion did not pass render smoke",
        )
    requested_pages = render["requested_pages"]
    if (
        any(
            isinstance(page, bool)
            or not isinstance(page, int)
            or not 1 <= page <= page_count
            for page in requested_pages
        )
        or requested_pages != sorted(set(requested_pages))
    ):
        raise WeakCaseVisualIndexError(
            "visual_reference_source_invalid",
            "The official PDF render page request is invalid",
        )
    normalized_rendered_pages = []
    for expected_number, page in zip(
        requested_pages, render["rendered_pages"]
    ):
        if (
            not isinstance(page, Mapping)
            or set(page) != _PDF_RENDER_PAGE_KEYS
        ):
            raise WeakCaseVisualIndexError(
                "visual_reference_source_invalid",
                "An official PDF render artifact has an invalid schema",
            )
        record = dict(page)
        if (
            record.get("page_number") != expected_number
            or any(
                isinstance(record.get(field), bool)
                or not isinstance(record.get(field), int)
                or record[field] <= 0
                for field in ("width", "height", "size_bytes")
            )
            or not isinstance(record.get("sha256"), str)
            or _SHA256.fullmatch(record["sha256"]) is None
            or record.get("media_type") != "image/png"
        ):
            raise WeakCaseVisualIndexError(
                "visual_reference_source_invalid",
                "An official PDF render artifact failed validation",
            )
        normalized_rendered_pages.append(record)

    correspondence = result.get("hwpx_photo_case_correspondence")
    if (
        not isinstance(correspondence, Mapping)
        or set(correspondence) != _PDF_CORRESPONDENCE_KEYS
        or correspondence.get("status") != "verified"
        or correspondence.get("hwpx_photo_case_count")
        != source.get("photo_case_count")
        or correspondence.get("mapped_hwpx_photo_case_count")
        != source.get("photo_case_count")
        or correspondence.get("unverified_hwpx_photo_case_count") != 0
        or correspondence.get("pdf_photo_case_count_added") != 0
        or not isinstance(correspondence.get("pages"), list)
        or correspondence.get("page_count")
        != len(correspondence["pages"])
    ):
        raise WeakCaseVisualIndexError(
            "visual_reference_source_invalid",
            "The HWPX-to-PDF page correspondence is invalid",
        )
    normalized_correspondence_pages = []
    seen_correspondence_pages = set()
    for page in correspondence["pages"]:
        if (
            not isinstance(page, Mapping)
            or set(page) != _PDF_CORRESPONDENCE_PAGE_KEYS
        ):
            raise WeakCaseVisualIndexError(
                "visual_reference_source_invalid",
                "An HWPX-to-PDF page mapping has an invalid schema",
            )
        record = dict(page)
        page_number = record.get("pdf_page_number")
        pdf_page = page_by_number.get(page_number)
        if (
            page_number in seen_correspondence_pages
            or pdf_page is None
            or isinstance(
                record.get("mapped_hwpx_photo_case_count"), bool
            )
            or not isinstance(
                record.get("mapped_hwpx_photo_case_count"), int
            )
            or record["mapped_hwpx_photo_case_count"] <= 0
            or record.get("correspondence_status")
            != "verified_pdf_page_address"
            or record.get("page_text_sha256")
            != pdf_page["text_sha256"]
            or record.get("render_validation")
            not in {
                "page_render_smoke_passed",
                "document_render_smoke_passed_page_not_sampled",
            }
        ):
            raise WeakCaseVisualIndexError(
                "visual_reference_source_invalid",
                "An HWPX-to-PDF page mapping failed validation",
            )
        seen_correspondence_pages.add(page_number)
        normalized_correspondence_pages.append(record)

    audit_payload = dict(result)
    audit_sha256 = audit_payload.pop("audit_sha256")
    audit_payload.pop("hwpx_photo_case_correspondence")
    if _canonical_sha256(audit_payload) != audit_sha256:
        raise WeakCaseVisualIndexError(
            "visual_reference_source_invalid",
            "The official PDF audit hash does not match",
        )
    result["pages"] = normalized_pages
    result["render"] = {
        **dict(render),
        "rendered_pages": normalized_rendered_pages,
    }
    result["hwpx_photo_case_correspondence"] = {
        **dict(correspondence),
        "pages": normalized_correspondence_pages,
    }
    return result


def _validated_source_snapshot(
    value: Mapping[str, Any],
    limits: WeakCaseVisualIndexLimits,
) -> dict:
    required = {
        "schema_version",
        "catalog_role",
        "case_count",
        "evidence_group_count",
        "duplicate_image_group_count",
        "duplicate_image_case_count",
        "source_count",
        "sources",
        "cases",
        "review_required_before_promotion",
        "automatic_annotation_creation",
        "automatic_incident_creation",
        "automatic_training_ingest",
        "annotation_count",
        "incident_count",
        "training_eligible_count",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise WeakCaseVisualIndexError(
            "visual_reference_source_invalid",
            "The visual reference source snapshot is invalid",
        )
    sources = value.get("sources")
    cases = value.get("cases")
    if (
        value.get("schema_version") != WEAK_CASE_VISUAL_SOURCE_SCHEMA
        or value.get("catalog_role")
        != "sample_hwpx_audited_photo_slots_weak_unconfirmed"
        or not isinstance(sources, list)
        or not sources
        or not isinstance(cases, list)
        or not cases
        or len(cases) > limits.max_cases
        or value.get("case_count") != len(cases)
        or value.get("source_count") != len(sources)
        or value.get("review_required_before_promotion") is not True
        or value.get("automatic_annotation_creation") is not False
        or value.get("automatic_incident_creation") is not False
        or value.get("automatic_training_ingest") is not False
        or value.get("annotation_count") != 0
        or value.get("incident_count") != 0
        or value.get("training_eligible_count") != 0
    ):
        raise WeakCaseVisualIndexError(
            "visual_reference_source_invalid",
            "The visual reference source violated its weak-only contract",
        )
    source_by_id = {}
    normalized_sources = []
    for source in sources:
        source_keys = (
            frozenset(source)
            if isinstance(source, Mapping)
            else frozenset()
        )
        if (
            not isinstance(source, Mapping)
            or source_keys != _SOURCE_WITH_PDF_KEYS
        ):
            raise WeakCaseVisualIndexError(
                "visual_reference_source_invalid",
                "A visual reference source has an invalid schema",
            )
        source_record = dict(source)
        source_id = source_record.get("source_id")
        quarter = source_record.get("quarter")
        if (
            not isinstance(source_id, str)
            or _SOURCE_ID.fullmatch(source_id) is None
            or source_id in source_by_id
            or quarter not in {3, 4}
            or not isinstance(source_record.get("document_name"), str)
            or Path(source_record["document_name"]).name
            != source_record["document_name"]
            or not source_record["document_name"].casefold().endswith(
                ".hwpx"
            )
            or any(
                not isinstance(source_record.get(field), str)
                or _SHA256.fullmatch(source_record[field]) is None
                for field in (
                    "document_sha256",
                    "structure_target_summary_sha256",
                    "index_manifest_sha256",
                )
            )
            or isinstance(
                source_record.get("photo_case_count"), bool
            )
            or not isinstance(
                source_record.get("photo_case_count"), int
            )
            or source_record["photo_case_count"] < 1
            or isinstance(
                source_record.get("excluded_fixed_picture_count"),
                bool,
            )
            or not isinstance(
                source_record.get("excluded_fixed_picture_count"),
                int,
            )
            or source_record["excluded_fixed_picture_count"] < 0
        ):
            raise WeakCaseVisualIndexError(
                "visual_reference_source_invalid",
                "A visual reference source failed validation",
            )
        if "companion_pdf" in source_record:
            source_record["companion_pdf"] = (
                _validated_pdf_companion(
                    source_record["companion_pdf"],
                    source=source_record,
                )
            )
        source_by_id[source_id] = source_record
        normalized_sources.append(source_record)
    if sum(
        source["photo_case_count"]
        for source in normalized_sources
    ) != len(cases):
        raise WeakCaseVisualIndexError(
            "visual_reference_source_invalid",
            "The visual reference source case counts do not match",
        )

    identities = set()
    normalized_cases = []
    for case in cases:
        if not isinstance(case, Mapping) or set(case) != _CASE_REQUIRED_KEYS:
            raise WeakCaseVisualIndexError(
                "visual_reference_case_invalid",
                "A visual reference case has an invalid schema",
            )
        record = dict(case)
        identity = (
            str(record.get("source_id", "")),
            str(record.get("case_id", "")),
        )
        if identity in identities:
            raise WeakCaseVisualIndexError(
                "visual_reference_case_invalid",
                "Visual reference case identities must be unique",
            )
        identities.add(identity)
        source = source_by_id.get(identity[0])
        search_texts = record.get("search_texts")
        if (
            source is None
            or not isinstance(record.get("case_id"), str)
            or _CASE_ID.fullmatch(record["case_id"]) is None
            or record.get("quarter") != source["quarter"]
            or record.get("document_name") != source["document_name"]
            or record.get("document_sha256")
            != source["document_sha256"]
            or record.get("structure_target_summary_sha256")
            != source["structure_target_summary_sha256"]
            or record.get("index_manifest_sha256")
            != source["index_manifest_sha256"]
            or not isinstance(search_texts, list)
            or len(search_texts) > limits.max_search_texts_per_case
            or any(
                not isinstance(text, str)
                or len(text) > limits.max_search_text_chars
                for text in search_texts
            )
            or record.get("reference_state") != "weak_unconfirmed"
            or record.get("review_required") is not True
            or record.get("approved_reference") is not False
            or record.get("training_eligible") is not False
            or record.get("annotation_created") is not False
            or record.get("incident_created") is not False
        ):
            raise WeakCaseVisualIndexError(
                "visual_reference_case_invalid",
                "A visual reference case violated its weak-only boundary",
            )
        hash_fields = (
            "document_sha256",
            "structure_target_summary_sha256",
            "index_manifest_sha256",
            "sample_layout_binding_sha256",
            "semantic_attestation_sha256",
            "picture_address_sha256",
            "image_sha256",
        )
        if any(
            not isinstance(record.get(field), str)
            or _SHA256.fullmatch(record[field]) is None
            for field in hash_fields
        ):
            raise WeakCaseVisualIndexError(
                "visual_reference_case_invalid",
                "A visual reference case hash is invalid",
            )
        section_path = record.get("section_path")
        if (
            not isinstance(section_path, str)
            or not section_path
            or "\\" in section_path
            or section_path.startswith("/")
            or ".." in section_path.split("/")
        ):
            raise WeakCaseVisualIndexError(
                "visual_reference_case_invalid",
                "A visual reference package address is invalid",
            )
        if (
            record.get("quarter") not in {3, 4}
            or record.get("sample_layout_binding_schema")
            != HWPX_SAMPLE_LAYOUT_SCHEMA
            or record.get("sample_layout_binding_revision")
            != SAMPLE_LAYOUT_BINDING_REVISION
            or record.get("semantic_projection_revision")
            != SAMPLE_REFERENCE_BINDING_REVISION
            or not isinstance(
                record.get("sample_evidence_group_id"), str
            )
            or _EVIDENCE_GROUP_ID.fullmatch(
                record["sample_evidence_group_id"]
            )
            is None
            or any(
                isinstance(record.get(field), bool)
                or not isinstance(record.get(field), int)
                or record[field] < 0
                for field in (
                    "section_index",
                    "page_hint",
                    "inspection_page_number",
                    "photo_page_number",
                    "page_pair_index",
                    "table_index",
                    "cell_row",
                    "cell_column",
                    "picture_index",
                )
            )
            or any(
                isinstance(record.get(field), bool)
                or not isinstance(record.get(field), int)
                or record[field] < 1
                for field in (
                    "group_photo_index",
                    "group_photo_count",
                    "duplicate_image_occurrence_count",
                )
            )
            or record["group_photo_index"]
            > record["group_photo_count"]
            or record.get("duplicate_image_registry_complete")
            is not True
            or not isinstance(
                record.get("duplicate_image_review_required"),
                bool,
            )
            or (
                record["duplicate_image_review_required"]
                != (
                    record["duplicate_image_occurrence_count"]
                    > 1
                )
            )
            or not isinstance(
                record.get("duplicate_image_case_refs"), list
            )
            or len(record["duplicate_image_case_refs"])
            != record["duplicate_image_occurrence_count"]
            or record["duplicate_image_case_refs"]
            != sorted(set(record["duplicate_image_case_refs"]))
            or any(
                not isinstance(case_ref, str)
                or _DUPLICATE_CASE_REF.fullmatch(case_ref)
                is None
                for case_ref in record[
                    "duplicate_image_case_refs"
                ]
            )
            or any(
                not isinstance(record.get(field), str)
                or _OPAQUE_ID.fullmatch(record[field]) is None
                for field in (
                    "photo_slot_id",
                    "table_id",
                    "cell_target_id",
                    "picture_target_id",
                )
            )
            or (
                record.get("picture_xml_id") is not None
                and (
                    not isinstance(record["picture_xml_id"], str)
                    or len(record["picture_xml_id"]) > 256
                )
            )
            or record.get("image_media_type")
            not in {
                "image/bmp",
                "image/png",
                "image/jpeg",
                "image/gif",
                "image/webp",
            }
            or any(
                record.get(field) is not None
                and (
                    not isinstance(record[field], str)
                    or len(record[field])
                    > limits.max_search_text_chars
                )
                for field in (
                    "caption",
                    "route",
                    "location",
                    "evaluation",
                    "opinion",
                )
            )
            or not isinstance(record.get("image_size_bytes"), int)
            or record["image_size_bytes"] <= 0
            or not isinstance(record.get("image_width"), int)
            or record["image_width"] <= 0
            or not isinstance(record.get("image_height"), int)
            or record["image_height"] <= 0
        ):
            raise WeakCaseVisualIndexError(
                "visual_reference_case_invalid",
                "A visual reference image description is invalid",
            )
        normalized_cases.append(record)
    cases_by_group: dict[str, list[dict]] = {}
    cases_by_image: dict[str, list[dict]] = {}
    for record in normalized_cases:
        cases_by_group.setdefault(
            str(record["sample_evidence_group_id"]),
            [],
        ).append(record)
        cases_by_image.setdefault(
            str(record["image_sha256"]),
            [],
        ).append(record)
    for group_records in cases_by_group.values():
        expected_count = len(group_records)
        if (
            {record["group_photo_count"] for record in group_records}
            != {expected_count}
            or {
                record["group_photo_index"]
                for record in group_records
            }
            != set(range(1, expected_count + 1))
            or len(
                {
                    (
                        record["source_id"],
                        record["document_sha256"],
                        record["table_index"],
                        record["cell_target_id"],
                        record["cell_row"],
                        record["cell_column"],
                        record["caption"],
                    )
                    for record in group_records
                }
            )
            != 1
        ):
            raise WeakCaseVisualIndexError(
                "visual_reference_case_invalid",
                "A multi-photo sample evidence group is inconsistent",
            )
    for image_records in cases_by_image.values():
        expected_refs = sorted(
            f"{record['source_id']}/{record['case_id']}"
            for record in image_records
        )
        duplicate = len(image_records) > 1
        if any(
            record["duplicate_image_occurrence_count"]
            != len(image_records)
            or record["duplicate_image_review_required"]
            != duplicate
            or record["duplicate_image_case_refs"]
            != expected_refs
            for record in image_records
        ):
            raise WeakCaseVisualIndexError(
                "visual_reference_case_invalid",
                "Duplicate sample images were not surfaced consistently",
            )
    if (
        value.get("evidence_group_count") != len(cases_by_group)
        or value.get("duplicate_image_group_count")
        != sum(len(records) > 1 for records in cases_by_image.values())
        or value.get("duplicate_image_case_count")
        != sum(
            len(records)
            for records in cases_by_image.values()
            if len(records) > 1
        )
    ):
        raise WeakCaseVisualIndexError(
            "visual_reference_source_invalid",
            "The sample evidence group counts are inconsistent",
        )
    cases_by_source_page = {}
    for record in normalized_cases:
        key = (
            record["source_id"],
            record["photo_page_number"],
        )
        cases_by_source_page[key] = (
            cases_by_source_page.get(key, 0) + 1
        )
    for source in normalized_sources:
        companion = source.get("companion_pdf")
        if not isinstance(companion, Mapping):
            continue
        correspondence = companion[
            "hwpx_photo_case_correspondence"
        ]
        expected_counts = {
            page["pdf_page_number"]: page[
                "mapped_hwpx_photo_case_count"
            ]
            for page in correspondence["pages"]
        }
        actual_counts = {
            page_number: count
            for (source_id, page_number), count in (
                cases_by_source_page.items()
            )
            if source_id == source["source_id"]
        }
        if (
            actual_counts != expected_counts
            or sum(actual_counts.values())
            != source["photo_case_count"]
        ):
            raise WeakCaseVisualIndexError(
                "visual_reference_source_invalid",
                (
                    "The HWPX photo cases do not match their "
                    "official PDF page correspondence"
                ),
            )
    result = dict(value)
    result["sources"] = normalized_sources
    result["cases"] = normalized_cases
    return result


def _validated_index_payload(
    value: Mapping[str, Any],
    *,
    project_id: UUID,
    limits: WeakCaseVisualIndexLimits,
) -> dict:
    required = {
        "schema_version",
        "project_id",
        "source_schema_version",
        "source_snapshot_sha256",
        "sources",
        "model_identity",
        "model_identity_sha256",
        "embedding_dimension",
        "case_count",
        "cases",
        "reference_state",
        "review_required_before_promotion",
        "automatic_annotation_creation",
        "automatic_incident_creation",
        "automatic_training_ingest",
        "annotation_count",
        "incident_count",
        "training_eligible_count",
        "index_manifest_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise WeakCaseVisualIndexError(
            "visual_index_invalid",
            "The visual reference index schema is invalid",
        )
    result = dict(value)
    manifest_sha256 = result.pop("index_manifest_sha256", None)
    dimension = result.get("embedding_dimension")
    cases = result.get("cases")
    model_identity = _validated_model_identity(
        result.get("model_identity")
        if isinstance(result.get("model_identity"), Mapping)
        else {}
    )
    if (
        result.get("schema_version") != WEAK_CASE_VISUAL_INDEX_SCHEMA
        or result.get("project_id") != str(project_id)
        or result.get("source_schema_version")
        != WEAK_CASE_VISUAL_SOURCE_SCHEMA
        or not isinstance(result.get("source_snapshot_sha256"), str)
        or _SHA256.fullmatch(result["source_snapshot_sha256"]) is None
        or not isinstance(result.get("model_identity_sha256"), str)
        or _SHA256.fullmatch(result["model_identity_sha256"]) is None
        or _canonical_sha256(model_identity)
        != result["model_identity_sha256"]
        or isinstance(dimension, bool)
        or not isinstance(dimension, int)
        or not 2 <= dimension <= limits.max_embedding_dimension
        or not isinstance(cases, list)
        or not cases
        or len(cases) > limits.max_cases
        or result.get("case_count") != len(cases)
        or result.get("reference_state") != "weak_unconfirmed"
        or result.get("review_required_before_promotion") is not True
        or result.get("automatic_annotation_creation") is not False
        or result.get("automatic_incident_creation") is not False
        or result.get("automatic_training_ingest") is not False
        or result.get("annotation_count") != 0
        or result.get("incident_count") != 0
        or result.get("training_eligible_count") != 0
        or not isinstance(manifest_sha256, str)
        or _SHA256.fullmatch(manifest_sha256) is None
        or _canonical_sha256(result) != manifest_sha256
    ):
        raise WeakCaseVisualIndexError(
            "visual_index_invalid",
            "The visual reference index failed integrity checks",
        )
    identities = set()
    normalized_cases = []
    for case in cases:
        expected = _CASE_REQUIRED_KEYS | {
            "embedding",
            "embedding_sha256",
        }
        if not isinstance(case, Mapping) or set(case) != expected:
            raise WeakCaseVisualIndexError(
                "visual_index_invalid",
                "A persisted visual reference case is invalid",
            )
        vector = _normalized_vector(
            case.get("embedding"),
            limits=limits,
            expected_dimension=dimension,
            require_unit=True,
        )
        if (
            not isinstance(case.get("embedding_sha256"), str)
            or case["embedding_sha256"] != _canonical_sha256(list(vector))
        ):
            raise WeakCaseVisualIndexError(
                "visual_index_invalid",
                "A persisted visual reference embedding changed",
            )
        identity = (case.get("source_id"), case.get("case_id"))
        if identity in identities:
            raise WeakCaseVisualIndexError(
                "visual_index_invalid",
                "Persisted visual reference identities must be unique",
            )
        identities.add(identity)
        normalized_cases.append(
            {**dict(case), "embedding": list(vector)}
        )
    return {
        **result,
        "model_identity": model_identity,
        "cases": normalized_cases,
        "index_manifest_sha256": manifest_sha256,
    }


def _normalized_vector(
    values: Any,
    *,
    limits: WeakCaseVisualIndexLimits,
    expected_dimension: Optional[int],
    require_unit: bool = False,
) -> tuple[float, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(
        values,
        Sequence,
    ):
        raise WeakCaseVisualIndexError(
            "embedding_vector_invalid",
            "The image embedding vector is invalid",
        )
    if (
        not 2 <= len(values) <= limits.max_embedding_dimension
        or (
            expected_dimension is not None
            and len(values) != expected_dimension
        )
    ):
        raise WeakCaseVisualIndexError(
            "embedding_dimension_mismatch",
            "The image embedding dimension is invalid",
        )
    vector = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise WeakCaseVisualIndexError(
                "embedding_vector_invalid",
                "The image embedding contains a non-number",
            )
        number = float(value)
        if not math.isfinite(number) or abs(number) > 1_000_000:
            raise WeakCaseVisualIndexError(
                "embedding_vector_invalid",
                "The image embedding contains an unsafe value",
            )
        vector.append(number)
    norm = math.sqrt(math.fsum(value * value for value in vector))
    if not math.isfinite(norm) or norm <= 1e-12:
        raise WeakCaseVisualIndexError(
            "embedding_vector_invalid",
            "The image embedding has zero magnitude",
        )
    if require_unit and not math.isclose(
        norm,
        1.0,
        rel_tol=1e-6,
        abs_tol=1e-6,
    ):
        raise WeakCaseVisualIndexError(
            "embedding_vector_invalid",
            "The persisted image embedding is not normalized",
        )
    if require_unit:
        return tuple(vector)
    return tuple(value / norm for value in vector)


def _project_uuid(value: UUID) -> UUID:
    if not isinstance(value, UUID):
        raise WeakCaseVisualIndexError(
            "invalid_project_id",
            "The visual-index project id is invalid",
        )
    return value


def _normalize_query_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFC", " ".join(value.split()))[:240]
    return normalized or None


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise WeakCaseVisualIndexError(
            "visual_index_serialization_failed",
            "The visual reference index could not be serialized",
        ) from error


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _is_link_or_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _ensure_real_directory(path: Path) -> Path:
    absolute = path.absolute()
    _assert_no_linked_existing_components(absolute)
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as error:
        raise WeakCaseVisualIndexError(
            "visual_index_storage_unavailable",
            "The visual-index storage directory is unavailable",
        ) from error
    if (
        not resolved.is_dir()
        or absolute.is_symlink()
        or _is_link_or_reparse(absolute)
        or _is_link_or_reparse(resolved)
    ):
        raise WeakCaseVisualIndexError(
            "visual_index_path_rejected",
            "The visual-index storage directory is linked or invalid",
        )
    return resolved


def _assert_no_linked_existing_components(path: Path) -> None:
    absolute = path.absolute()
    existing_components = []
    cursor = absolute
    while True:
        if cursor.exists():
            existing_components.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    if any(
        component.is_symlink() or _is_link_or_reparse(component)
        for component in existing_components
    ):
        raise WeakCaseVisualIndexError(
            "visual_index_path_rejected",
            "The visual-index storage path contains a linked component",
        )


def _safe_regular_file(path: Path) -> None:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise WeakCaseVisualIndexError(
            "visual_index_missing",
            "The visual reference index file is unavailable",
        ) from error
    if (
        path.is_symlink()
        or _is_link_or_reparse(path)
        or resolved != path
        or not stat.S_ISREG(metadata.st_mode)
    ):
        raise WeakCaseVisualIndexError(
            "visual_index_path_rejected",
            "The visual reference index file is linked or invalid",
        )


def _read_json_bounded(
    path: Path,
    *,
    maximum: int,
    missing_code: str,
) -> dict:
    if not path.exists():
        raise WeakCaseVisualIndexError(
            missing_code,
            "The visual reference index has not been prepared",
        )
    _safe_regular_file(path)
    try:
        size = path.stat().st_size
        if not 1 <= size <= maximum:
            raise WeakCaseVisualIndexError(
                "visual_index_invalid",
                "The visual reference index file size is invalid",
            )
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(str(path), flags)
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            raw = stream.read(maximum + 1)
        if not raw or len(raw) > maximum:
            raise WeakCaseVisualIndexError(
                "visual_index_invalid",
                "The visual reference index exceeded its read bound",
            )
        value = json.loads(raw.decode("utf-8"))
    except WeakCaseVisualIndexError:
        raise
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise WeakCaseVisualIndexError(
            "visual_index_invalid",
            "The visual reference index could not be read",
        ) from error
    if not isinstance(value, dict):
        raise WeakCaseVisualIndexError(
            "visual_index_invalid",
            "The visual reference index root is invalid",
        )
    return value


def _write_content_addressed(
    path: Path,
    payload: bytes,
    *,
    maximum: int,
) -> None:
    if path.exists():
        _safe_regular_file(path)
        try:
            size = path.stat().st_size
            if not 1 <= size <= maximum:
                raise WeakCaseVisualIndexError(
                    "visual_index_content_conflict",
                    "A content-addressed visual index has an invalid size",
                )
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(str(path), flags)
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                existing = stream.read(maximum + 1)
        except OSError as error:
            raise WeakCaseVisualIndexError(
                "visual_index_storage_unavailable",
                "The existing visual reference index could not be read",
            ) from error
        if (
            len(existing) > maximum
            or existing != payload
            or hashlib.sha256(existing).digest()
            != hashlib.sha256(payload).digest()
        ):
            raise WeakCaseVisualIndexError(
                "visual_index_content_conflict",
                "A content-addressed visual index has conflicting bytes",
            )
        return
    _write_atomic_new(path, payload)


def _write_atomic_new(path: Path, payload: bytes) -> None:
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(temporary), flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as error:
            raise WeakCaseVisualIndexError(
                "visual_index_content_conflict",
                "The visual reference index destination already exists",
            ) from error
        temporary.unlink()
    except WeakCaseVisualIndexError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise WeakCaseVisualIndexError(
            "visual_index_storage_unavailable",
            "The visual reference index could not be stored atomically",
        ) from error


def _write_atomic_replace(path: Path, payload: bytes) -> None:
    if path.exists():
        _safe_regular_file(path)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(temporary), flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise WeakCaseVisualIndexError(
            "visual_index_storage_unavailable",
            "The active visual reference pointer could not be stored",
        ) from error


__all__ = [
    "CandidateFrameEvidence",
    "IMAGE_EMBEDDING_MODEL_SCHEMA",
    "LOCAL_ROAD_SCENE_OBSERVATION_SCHEMA",
    "VerifiedLocalClipImageEmbedder",
    "WEAK_CASE_VISUAL_ACTIVE_SCHEMA",
    "WEAK_CASE_VISUAL_INDEX_SCHEMA",
    "WEAK_CASE_VISUAL_QUERY_SCHEMA",
    "WeakCaseImageEmbedder",
    "WeakCaseVisualIndexError",
    "WeakCaseVisualIndexLimits",
    "WeakCaseVisualIndexService",
]
