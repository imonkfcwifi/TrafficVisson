"""Server-owned, path-free access to the approved sample HWPX case sources.

The renderer is deliberately unable to register a document or choose an
archive member.  This catalog owns an exact allowlist of document name,
SHA-256 and read-only purpose, then exposes only opaque source/case ids.

Historical report cases remain ``weak_unconfirmed``.  Searching or viewing a
thumbnail never creates an Annotation, Incident, dataset membership or
training label.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import threading
import unicodedata
import warnings
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from . import hwpx_structure as structure
from .hwpx_structure import (
    FileIdentity,
    HwpxLimitExceeded,
    HwpxStructureError,
)
from .hwpx_sample_layout import (
    HWPX_SAMPLE_LAYOUT_SCHEMA,
    SAMPLE_LAYOUT_BINDING_REVISION,
    SAMPLE_REFERENCE_BINDING_REVISION,
    HwpxSampleLayoutError,
    SampleLayoutInspection,
    SampleReferenceCaseBinding,
    bind_sample_reference_case,
    identify_sample_layout,
)
from .hwpx_weak_cases import (
    HWPX_WEAK_CASE_PURPOSE,
    HwpxWeakCase,
    HwpxWeakCaseAuthorization,
    HwpxWeakCaseAuthorizationError,
    HwpxWeakCaseError,
    HwpxWeakCaseIndex,
    HwpxWeakCaseLimits,
    inspect_hwpx_weak_cases,
)
from .pdf_companion_audit import PdfCompanionCatalog


WEAK_CASE_CATALOG_SCHEMA = "trafficvisson.weak-case-catalog.v2"
WEAK_CASE_SEARCH_SCHEMA = "trafficvisson.weak-case-search.v2"
WEAK_CASE_VISUAL_SOURCE_SCHEMA = (
    "trafficvisson.weak-case-visual-source.v2"
)
WEAK_CASE_SEMANTIC_ATTESTATION_SCHEMA = (
    "trafficvisson.weak-case-semantic-attestation.v2"
)
WEAK_CASE_SOURCE_PATTERN = re.compile(
    r"^sample-hwpx-[34]q-[a-f0-9]{16}$"
)
WEAK_CASE_ID_PATTERN = re.compile(
    r"^hwpx-weak-case-[a-f0-9]{32}$"
)
_TOKEN_PATTERN = re.compile(r"[0-9A-Za-z가-힣]{2,}")
_STOP_WORDS = frozenset(
    {
        "도로",
        "필요",
        "확인",
        "검토",
        "발생",
        "점검",
        "현장",
        "사진",
        "여부",
        "사용자",
        "후보",
    }
)
_ISSUE_TERMS: Mapping[str, Tuple[str, ...]] = {
    "pothole": ("포트홀", "노면 홈", "패임"),
    "road_surface_damage": (
        "노면 파손",
        "포장 파손",
        "균열",
        "침하",
        "단차",
    ),
    "road_debris": ("낙하물", "적재물", "장애물", "이물질"),
    "median_barrier_damage": (
        "중앙분리대",
        "방호벽",
        "분리대 파손",
    ),
    "sign_damage": ("표지판 파손", "안내표지 파손", "교통표지 파손"),
    "sign_vegetation_occlusion": (
        "표지판 가림",
        "표지 가림",
        "수목 가림",
        "식생 가림",
    ),
    "roadside_mowing_required": (
        "삭초",
        "제초",
        "예초",
        "수풀",
        "잡초",
    ),
    "guide_sign_identity": ("이정표", "안내표지", "방향표지"),
    "tunnel_identity": ("터널명", "터널 이름", "터널"),
    "tunnel_name_occlusion": (
        "터널명 가림",
        "터널 표지 가림",
        "수풀",
        "삭초",
    ),
    "lane_marking_faded": (
        "차선 마모",
        "차선 도색",
        "차선 재도색",
        "차선 흐림",
    ),
    "colored_guidance_faded": (
        "색깔유도선",
        "색상유도선",
        "유도선 도색",
        "유도선 마모",
    ),
}


@dataclass(frozen=True)
class WeakCaseSourceDefinition:
    source_id: str
    document_name: str
    expected_document_sha256: str
    quarter: int
    purpose: str = HWPX_WEAK_CASE_PURPOSE

    def __post_init__(self) -> None:
        if WEAK_CASE_SOURCE_PATTERN.fullmatch(self.source_id) is None:
            raise ValueError("weak-case source_id is invalid")
        if (
            Path(self.document_name).name != self.document_name
            or not self.document_name.casefold().endswith(".hwpx")
        ):
            raise ValueError("weak-case document_name is invalid")
        if re.fullmatch(r"[a-f0-9]{64}", self.expected_document_sha256) is None:
            raise ValueError("weak-case document SHA-256 is invalid")
        if self.quarter not in {3, 4}:
            raise ValueError("weak-case source quarter is invalid")
        if self.purpose != HWPX_WEAK_CASE_PURPOSE:
            raise ValueError("weak-case source purpose is not allowlisted")


DEFAULT_SAMPLE_SOURCES: Tuple[WeakCaseSourceDefinition, ...] = (
    WeakCaseSourceDefinition(
        source_id="sample-hwpx-3q-24ffb4bc62cd86a6",
        document_name="3분기 점검 내용 정리.hwpx",
        expected_document_sha256=(
            "24ffb4bc62cd86a6c72d3a3829b8f1ddf1c824d7bf42452e7c495877a227bcb9"
        ),
        quarter=3,
    ),
    WeakCaseSourceDefinition(
        source_id="sample-hwpx-4q-bbdd61a6ef192a95",
        document_name="4분기 점검 내용 정리.hwpx",
        expected_document_sha256=(
            "bbdd61a6ef192a957a5e9480b0c2eedbfe956dd184abd5d76f59474254941539"
        ),
        quarter=4,
    ),
)


class WeakCaseCatalogError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def semantic_attestation_sha256(
    attestation: Mapping[str, object],
) -> str:
    """Hash the corrected semantic projection and evidence-group boundary.

    The approval store recomputes this value from its fresh server-owned
    attestation. Old caption projections and old layout bindings therefore
    cannot be replayed after the schema/revision changes.
    """

    sample_binding = attestation.get("sample_layout_binding")
    if not isinstance(sample_binding, Mapping):
        raise ValueError("sample layout binding is required")
    fields = (
        "source_id",
        "case_id",
        "document_sha256",
        "structure_target_summary_sha256",
        "index_manifest_sha256",
        "image_sha256",
        "caption",
        "route",
        "location",
        "evaluation",
        "opinion",
        "duplicate_image_occurrence_count",
        "duplicate_image_review_required",
        "duplicate_image_registry_complete",
        "duplicate_image_case_refs",
    )
    return _canonical_sha256(
        {
            "schema_version": (
                WEAK_CASE_SEMANTIC_ATTESTATION_SCHEMA
            ),
            "sample_layout_binding_schema": sample_binding.get(
                "sample_layout_binding_schema"
            ),
            "sample_layout_binding_revision": sample_binding.get(
                "sample_layout_binding_revision"
            ),
            "semantic_projection_revision": sample_binding.get(
                "semantic_projection_revision"
            ),
            "attested_fields": {
                field: attestation.get(field) for field in fields
            },
            "sample_layout_binding": dict(sample_binding),
        }
    )


@dataclass(frozen=True)
class ResolvedWeakCaseImage:
    body: bytes
    media_type: str
    size_bytes: int
    sha256: str
    source_sha256: Optional[str] = None
    source_size_bytes: Optional[int] = None
    derived_thumbnail: bool = False


@dataclass(frozen=True)
class _IndexedSource:
    definition: WeakCaseSourceDefinition
    path: Path
    identity: FileIdentity
    index: HwpxWeakCaseIndex
    cases_by_id: Mapping[str, HwpxWeakCase]


@dataclass(frozen=True)
class _AuditedSampleCases:
    inspection: SampleLayoutInspection
    bindings_by_case_id: Mapping[str, SampleReferenceCaseBinding]
    excluded_fixed_picture_count: int


def default_sample_root() -> Path:
    configured = os.environ.get("TRAFFICSUPERVISION_SAMPLE_HWPX_ROOT")
    if configured:
        return Path(configured).resolve()
    return Path(__file__).resolve().parents[3] / "sample"


class WeakCaseCatalog:
    """Lazy, immutable index over exact server-owned sample documents."""

    def __init__(
        self,
        sample_root: Path,
        *,
        definitions: Sequence[WeakCaseSourceDefinition] = (
            DEFAULT_SAMPLE_SOURCES
        ),
        limits: Optional[HwpxWeakCaseLimits] = None,
        maximum_stream_image_bytes: int = 8 * 1024**2,
        pdf_companion_catalog: Optional[
            PdfCompanionCatalog
        ] = None,
    ) -> None:
        root = Path(sample_root)
        if not root.is_absolute():
            raise ValueError("weak-case sample_root must be absolute")
        if (
            isinstance(maximum_stream_image_bytes, bool)
            or not isinstance(maximum_stream_image_bytes, int)
            or not 64 <= maximum_stream_image_bytes <= 64 * 1024**2
        ):
            raise ValueError("weak-case image stream bound is invalid")
        if not definitions:
            raise ValueError("at least one weak-case source is required")
        source_ids = [row.source_id for row in definitions]
        names = [unicodedata.normalize("NFC", row.document_name).casefold()
                 for row in definitions]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("weak-case source ids must be unique")
        if len(names) != len(set(names)):
            raise ValueError("weak-case document names must be unique")
        self._root = root.resolve()
        self._definitions = {
            row.source_id: row for row in definitions
        }
        self._limits = limits or HwpxWeakCaseLimits()
        self._maximum_stream_image_bytes = maximum_stream_image_bytes
        self._pdf_companions = (
            pdf_companion_catalog
            if pdf_companion_catalog is not None
            else PdfCompanionCatalog(self._root)
        )
        self._cache: Dict[str, _IndexedSource] = {}
        self._audited_sample_cache: Dict[str, _AuditedSampleCases] = {}
        self._visual_snapshot_cache: Optional[dict] = None
        self._lock = threading.RLock()

    @property
    def maximum_stream_image_bytes(self) -> int:
        return self._maximum_stream_image_bytes

    def list_sources(self) -> dict:
        sources = [self._source_snapshot(row) for row in self._definitions.values()]
        ready_count = sum(row["status"] == "ready" for row in sources)
        return {
            "schema_version": WEAK_CASE_CATALOG_SCHEMA,
            "catalog_role": "sample_hwpx_weak_unconfirmed_reference",
            "capability_state": (
                "weak_reference_only" if ready_count else "unavailable"
            ),
            "automatic_promotion": False,
            "automatic_annotation_creation": False,
            "automatic_incident_creation": False,
            "automatic_training_ingest": False,
            "sources": sources,
            "ready_source_count": ready_count,
            "source_count": len(sources),
        }

    def _audited_sample_cases(
        self,
        indexed: _IndexedSource,
    ) -> Optional[_AuditedSampleCases]:
        """Resolve only exact photo slots from a recognized sample layout.

        The generic HWPX extractor also observes the 24 fixed inspection-page
        signature/attestation pictures in each report.  Those pictures are
        layout chrome rather than road evidence, so production sample sources
        must be reduced to the exact photo slots proved by
        ``hwpx_sample_layout`` before search or approval.

        Custom catalogs remain useful for bounded unit fixtures.  They keep
        the legacy extractor-only path only when their document hash is not a
        registered production sample hash.
        """

        with self._lock:
            cached = self._audited_sample_cache.get(
                indexed.definition.source_id
            )
            if cached is not None:
                return cached
        try:
            inspection = identify_sample_layout(indexed.path)
        except HwpxSampleLayoutError as error:
            raise WeakCaseCatalogError(
                error.code,
                str(error),
            ) from error
        production_hashes = {
            definition.expected_document_sha256
            for definition in DEFAULT_SAMPLE_SOURCES
        }
        if inspection is None:
            if (
                indexed.definition.expected_document_sha256
                in production_hashes
            ):
                raise WeakCaseCatalogError(
                    "sample_template_not_recognized",
                    "The registered sample source is not an audited template",
                )
            return None
        if (
            inspection.baseline.document_sha256
            != indexed.definition.expected_document_sha256
            or inspection.baseline.weak_case_index_manifest_sha256
            != indexed.index.index_manifest_sha256
            or inspection.snapshot.target_summary_sha256
            != indexed.index.structure_target_summary_sha256
        ):
            raise WeakCaseCatalogError(
                "source_contract_mismatch",
                "The sample source no longer matches its audited manifest",
            )

        bindings: Dict[str, SampleReferenceCaseBinding] = {}
        excluded_fixed_pictures = 0
        for case in indexed.index.cases:
            try:
                binding = bind_sample_reference_case(
                    inspection,
                    case.source.picture_target_id,
                )
            except HwpxSampleLayoutError as error:
                if (
                    error.code
                    == "sample_reference_picture_not_photo_evidence"
                ):
                    excluded_fixed_pictures += 1
                    continue
                raise WeakCaseCatalogError(
                    error.code,
                    str(error),
                ) from error
            if (
                case.image_resolution_state != "resolved"
                or len(case.images) != 1
                or binding.sample_layout_binding_schema
                != HWPX_SAMPLE_LAYOUT_SCHEMA
                or binding.sample_layout_binding_revision
                != SAMPLE_LAYOUT_BINDING_REVISION
                or binding.semantic_projection_revision
                != SAMPLE_REFERENCE_BINDING_REVISION
            ):
                raise WeakCaseCatalogError(
                    "image_unavailable",
                    "An audited photo-slot case does not resolve to one image",
                )
            bindings[case.case_id] = binding
        if (
            len(bindings)
            != inspection.baseline.existing_photo_picture_count
            or excluded_fixed_pictures
            != inspection.baseline.fixed_inspection_picture_count
        ):
            raise WeakCaseCatalogError(
                "source_contract_mismatch",
                "The audited photo-slot case count changed",
            )
        result = _AuditedSampleCases(
            inspection=inspection,
            bindings_by_case_id=bindings,
            excluded_fixed_picture_count=excluded_fixed_pictures,
        )
        with self._lock:
            self._audited_sample_cache[
                indexed.definition.source_id
            ] = result
        return result

    def _image_occurrence_index(
        self,
        *,
        require_complete: bool,
    ) -> Tuple[Mapping[str, Tuple[str, ...]], bool]:
        """Return every audited image-hash occurrence without merging cases."""

        occurrences: Dict[str, list[str]] = {}
        complete = True
        for definition in self._definitions.values():
            try:
                indexed = self._ensure_indexed(definition.source_id)
                audited = self._audited_sample_cases(indexed)
            except WeakCaseCatalogError as error:
                if require_complete:
                    raise WeakCaseCatalogError(
                        "duplicate_image_registry_incomplete",
                        (
                            "모든 3·4분기 표본의 중복 사진 상태를 "
                            "재검증하기 전에는 사례를 승인할 수 없습니다."
                        ),
                    ) from error
                complete = False
                continue
            for case in indexed.index.cases:
                if (
                    audited is not None
                    and case.case_id
                    not in audited.bindings_by_case_id
                ):
                    continue
                if len(case.images) != 1:
                    if require_complete:
                        raise WeakCaseCatalogError(
                            "duplicate_image_registry_incomplete",
                            "표본 사진 해시 결속이 불완전합니다.",
                        )
                    complete = False
                    continue
                image_sha256 = case.images[0].image_sha256
                occurrences.setdefault(image_sha256, []).append(
                    f"{definition.source_id}/{case.case_id}"
                )
        return (
            {
                image_sha256: tuple(sorted(case_refs))
                for image_sha256, case_refs in sorted(
                    occurrences.items()
                )
            },
            complete,
        )

    def search(
        self,
        *,
        issue_type: Optional[str],
        query_text: Optional[str],
        limit: int,
    ) -> dict:
        if not 3 <= limit <= 5:
            raise WeakCaseCatalogError(
                "invalid_limit",
                "Reference case search limit must be between 3 and 5",
            )
        normalized_query = _normalize_query(query_text)
        if not issue_type and not normalized_query:
            raise WeakCaseCatalogError(
                "empty_query",
                "Reference case search requires an issue type or query text",
            )
        issue_terms = _ISSUE_TERMS.get(issue_type or "", ())
        query_tokens = _tokens(normalized_query)
        ranked = []
        source_states = []
        for definition in self._definitions.values():
            try:
                indexed = self._ensure_indexed(definition.source_id)
                audited = self._audited_sample_cases(indexed)
                source_states.append(
                    self._ready_source_snapshot(
                        indexed,
                        audited=audited,
                    )
                )
            except WeakCaseCatalogError as error:
                source_states.append(
                    self._error_source_snapshot(definition, error.code)
                )
                continue
            for case in indexed.index.cases:
                binding = (
                    audited.bindings_by_case_id.get(case.case_id)
                    if audited is not None
                    else None
                )
                if audited is not None and binding is None:
                    continue
                if (
                    case.review_state != "unconfirmed"
                    or case.review_required is not True
                    or case.training_eligible
                    or case.annotation_created
                    or case.incident_created
                ):
                    raise WeakCaseCatalogError(
                        "unsafe_case_state",
                        "A historical case violated the review-only boundary",
                    )
                if binding is None:
                    score, matched_terms = _similarity_score(
                        case,
                        issue_terms=issue_terms,
                        query_tokens=query_tokens,
                    )
                    page_number = case.source.page_hint
                else:
                    exact = _exact_sample_case_fields(binding)
                    score, matched_terms = _similarity_score_texts(
                        exact["search_texts"],
                        caption=exact["caption"],
                        issue_terms=issue_terms,
                        query_tokens=query_tokens,
                    )
                    page_number = binding.photo_page_number
                if score <= 0:
                    continue
                ranked.append(
                    (
                        -score,
                        _first_image_size(case),
                        definition.quarter,
                        page_number,
                        case.case_id,
                        indexed,
                        case,
                        binding,
                        score,
                        matched_terms,
                    )
                )
        ranked.sort(key=lambda row: row[:5])
        occurrences, occurrence_registry_complete = (
            self._image_occurrence_index(
                require_complete=False,
            )
        )
        cases = [
            self._public_case(
                indexed,
                case,
                score,
                terms,
                rank=index + 1,
                sample_binding=binding,
                image_occurrences=occurrences,
                duplicate_image_registry_complete=(
                    occurrence_registry_complete
                ),
            )
            for index, (
                _neg_score,
                _image_size,
                _quarter,
                _page,
                _case_id,
                indexed,
                case,
                binding,
                score,
                terms,
            ) in enumerate(ranked[:limit])
        ]
        ready_count = sum(row["status"] == "ready" for row in source_states)
        return {
            "schema_version": WEAK_CASE_SEARCH_SCHEMA,
            "search_mode": "bounded_text_and_structure_metadata",
            "semantic_image_embedding_used": False,
            "capability_state": (
                "weak_reference_only" if ready_count else "unavailable"
            ),
            "issue_type": issue_type,
            "query_text": normalized_query or None,
            "requested_limit": limit,
            "result_count": len(cases),
            "cases": cases,
            "source_states": source_states,
            "review_required_before_promotion": True,
            "approved_reference_count": 0,
            "annotation_count": 0,
            "incident_count": 0,
            "training_eligible_count": 0,
        }

    def visual_index_snapshot(self) -> dict:
        """Return every audited photo-slot case for server-side embeddings.

        This is deliberately not a renderer API.  It contains path-free,
        immutable provenance and weak text only; image bytes still have to be
        resolved one case at a time through :meth:`resolve_case_image`.
        Fixed inspector signatures are excluded because the exact sample
        layout proves that they are not road-evidence photo slots.
        """

        with self._lock:
            if self._visual_snapshot_cache is not None:
                for source_id in self._definitions:
                    self._ensure_indexed(source_id)
                current_companions = {
                    definition.source_id: (
                        self._pdf_companions.snapshot(
                            definition.quarter
                        )
                    )
                    for definition in self._definitions.values()
                }
                cached_companions = {
                    str(source["source_id"]): {
                        key: value
                        for key, value in source[
                            "companion_pdf"
                        ].items()
                        if key
                        != "hwpx_photo_case_correspondence"
                    }
                    for source in self._visual_snapshot_cache[
                        "sources"
                    ]
                }
                if current_companions == cached_companions:
                    return copy.deepcopy(
                        self._visual_snapshot_cache
                    )
                self._visual_snapshot_cache = None

        cases = []
        sources = []
        image_occurrences, occurrence_registry_complete = (
            self._image_occurrence_index(require_complete=True)
        )
        if not occurrence_registry_complete:
            raise WeakCaseCatalogError(
                "duplicate_image_registry_incomplete",
                "표본 사진 해시 결속을 완성하지 못했습니다.",
            )
        for definition in self._definitions.values():
            indexed = self._ensure_indexed(definition.source_id)
            audited = self._audited_sample_cases(indexed)
            if audited is None:
                raise WeakCaseCatalogError(
                    "sample_template_not_recognized",
                    "The visual reference source is not an audited sample template",
                )
            inspection = audited.inspection

            source_cases = []
            excluded_fixed_pictures = (
                audited.excluded_fixed_picture_count
            )
            for case in indexed.index.cases:
                if (
                    case.review_state != "unconfirmed"
                    or case.review_required is not True
                    or case.training_eligible
                    or case.annotation_created
                    or case.incident_created
                ):
                    raise WeakCaseCatalogError(
                        "unsafe_case_state",
                        "A visual reference case violated the review-only boundary",
                    )
                binding = audited.bindings_by_case_id.get(
                    case.case_id
                )
                if binding is None:
                    continue
                image = case.images[0]
                exact = _exact_sample_case_fields(binding)
                search_texts = exact["search_texts"]
                picture_address_sha256 = hashlib.sha256(
                    "\0".join(
                        (
                            indexed.definition.expected_document_sha256,
                            case.source.section_path,
                            str(case.source.section_index),
                            case.source.picture_target_id,
                            str(case.source.picture_index),
                            case.source.picture_xml_id or "",
                            case.source.table_id or "",
                            str(case.source.table_index),
                            case.source.cell_target_id or "",
                            str(case.source.cell_row),
                            str(case.source.cell_column),
                            binding.photo_slot_id,
                            binding.binding_sha256,
                        )
                    ).encode("utf-8")
                ).hexdigest()
                evidence_identity = _evidence_identity_metadata(
                    image.image_sha256,
                    image_occurrences=image_occurrences,
                    registry_complete=True,
                )
                source_case = {
                        "source_id": indexed.definition.source_id,
                        "case_id": case.case_id,
                        "quarter": indexed.definition.quarter,
                        "document_name": indexed.definition.document_name,
                        "document_sha256": (
                            indexed.definition.expected_document_sha256
                        ),
                        "structure_target_summary_sha256": (
                            indexed.index.structure_target_summary_sha256
                        ),
                        "index_manifest_sha256": (
                            indexed.index.index_manifest_sha256
                        ),
                        "section_path": case.source.section_path,
                        "section_index": case.source.section_index,
                        "page_hint": binding.photo_page_number,
                        "page_hint_source": (
                            "audited_sample_photo_page"
                        ),
                        "inspection_page_number": (
                            binding.inspection_page_number
                        ),
                        "photo_page_number": binding.photo_page_number,
                        "page_pair_index": binding.page_pair_index,
                        "photo_slot_id": binding.photo_slot_id,
                        "sample_layout_binding_schema": (
                            binding.sample_layout_binding_schema
                        ),
                        "sample_layout_binding_revision": (
                            binding.sample_layout_binding_revision
                        ),
                        "semantic_projection_revision": (
                            binding.semantic_projection_revision
                        ),
                        "sample_evidence_group_id": (
                            binding.sample_evidence_group_id
                        ),
                        "group_photo_index": (
                            binding.group_photo_index
                        ),
                        "group_photo_count": (
                            binding.group_photo_count
                        ),
                        "sample_layout_binding_sha256": (
                            binding.binding_sha256
                        ),
                        "table_id": case.source.table_id,
                        "table_index": case.source.table_index,
                        "cell_target_id": case.source.cell_target_id,
                        "cell_row": case.source.cell_row,
                        "cell_column": case.source.cell_column,
                        "picture_target_id": (
                            case.source.picture_target_id
                        ),
                        "picture_index": case.source.picture_index,
                        "picture_xml_id": case.source.picture_xml_id,
                        "picture_address_sha256": (
                            picture_address_sha256
                        ),
                        "image_sha256": image.image_sha256,
                        "image_media_type": image.media_type,
                        "image_size_bytes": image.size_bytes,
                        "image_width": image.width,
                        "image_height": image.height,
                        "caption": exact["caption"],
                        "route": exact["route"],
                        "location": exact["location"],
                        "evaluation": exact["evaluation"],
                        "opinion": exact["opinion"],
                        "search_texts": list(search_texts),
                        "reference_state": "weak_unconfirmed",
                        "review_required": True,
                        "approved_reference": False,
                        "training_eligible": False,
                        "annotation_created": False,
                        "incident_created": False,
                        **evidence_identity,
                    }
                source_case["semantic_attestation_sha256"] = (
                    semantic_attestation_sha256(
                        {
                            **source_case,
                            "sample_layout_binding": (
                                _sample_binding_payload(binding)
                            ),
                        }
                    )
                )
                source_cases.append(source_case)
            if (
                len(source_cases)
                != inspection.baseline.existing_photo_picture_count
                or excluded_fixed_pictures
                != inspection.baseline.fixed_inspection_picture_count
            ):
                raise WeakCaseCatalogError(
                    "source_contract_mismatch",
                    "The audited photo-slot case count changed",
                )
            source_cases.sort(
                key=lambda item: (
                    int(item["photo_page_number"]),
                    int(item["table_index"]),
                    int(item["cell_row"]),
                    int(item["cell_column"]),
                    str(item["case_id"]),
                )
            )
            companion_pdf = self._pdf_companions.snapshot(
                indexed.definition.quarter
            )
            companion_pdf = {
                **companion_pdf,
                "hwpx_photo_case_correspondence": (
                    _pdf_photo_page_correspondence(
                        companion_pdf,
                        source_cases,
                    )
                ),
            }
            cases.extend(source_cases)
            sources.append(
                {
                    "source_id": indexed.definition.source_id,
                    "quarter": indexed.definition.quarter,
                    "document_name": indexed.definition.document_name,
                    "document_sha256": (
                        indexed.definition.expected_document_sha256
                    ),
                    "structure_target_summary_sha256": (
                        indexed.index.structure_target_summary_sha256
                    ),
                    "index_manifest_sha256": (
                        indexed.index.index_manifest_sha256
                    ),
                    "photo_case_count": len(source_cases),
                    "excluded_fixed_picture_count": (
                        excluded_fixed_pictures
                    ),
                    "companion_pdf": companion_pdf,
                }
            )
        cases.sort(
            key=lambda item: (
                int(item["quarter"]),
                int(item["photo_page_number"]),
                int(item["table_index"]),
                int(item["cell_row"]),
                int(item["cell_column"]),
                str(item["case_id"]),
            )
        )
        sources.sort(key=lambda item: (int(item["quarter"]), str(item["source_id"])))
        evidence_group_count = len(
            {
                str(case["sample_evidence_group_id"])
                for case in cases
            }
        )
        duplicate_image_groups = {
            str(case["image_sha256"])
            for case in cases
            if case["duplicate_image_review_required"]
        }
        result = {
            "schema_version": WEAK_CASE_VISUAL_SOURCE_SCHEMA,
            "catalog_role": (
                "sample_hwpx_audited_photo_slots_weak_unconfirmed"
            ),
            "case_count": len(cases),
            "evidence_group_count": evidence_group_count,
            "duplicate_image_group_count": len(
                duplicate_image_groups
            ),
            "duplicate_image_case_count": sum(
                bool(case["duplicate_image_review_required"])
                for case in cases
            ),
            "source_count": len(sources),
            "sources": sources,
            "cases": cases,
            "review_required_before_promotion": True,
            "automatic_annotation_creation": False,
            "automatic_incident_creation": False,
            "automatic_training_ingest": False,
            "annotation_count": 0,
            "incident_count": 0,
            "training_eligible_count": 0,
        }
        with self._lock:
            self._visual_snapshot_cache = copy.deepcopy(result)
        return result

    def resolve_case_image(
        self,
        source_id: str,
        case_id: str,
    ) -> ResolvedWeakCaseImage:
        return self._resolve_case_image_bounded(
            source_id,
            case_id,
            maximum_bytes=self._maximum_stream_image_bytes,
        )

    def resolve_case_preview(
        self,
        source_id: str,
        case_id: str,
    ) -> ResolvedWeakCaseImage:
        """Return an original image or a bounded server-made thumbnail.

        The source member is always read and revalidated at the extractor's
        private 64 MiB ceiling first.  Only the renderer-facing derivative is
        reduced.  Its hash is never substituted for the immutable source image
        hash used by case approval and dataset provenance.
        """

        original = self.resolve_case_image_for_visual_index(
            source_id,
            case_id,
        )
        if original.size_bytes <= self._maximum_stream_image_bytes:
            return ResolvedWeakCaseImage(
                body=original.body,
                media_type=original.media_type,
                size_bytes=original.size_bytes,
                sha256=original.sha256,
                source_sha256=original.sha256,
                source_size_bytes=original.size_bytes,
                derived_thumbnail=False,
            )
        return _bounded_preview_thumbnail(
            original,
            maximum_bytes=self._maximum_stream_image_bytes,
        )

    def resolve_case_image_for_visual_index(
        self,
        source_id: str,
        case_id: str,
    ) -> ResolvedWeakCaseImage:
        """Resolve one sample image at the extractor's verified local bound.

        This server-internal path exists so the embedding index can cover
        high-resolution sample photos without widening the renderer preview
        response bound.
        """

        return self._resolve_case_image_bounded(
            source_id,
            case_id,
            maximum_bytes=self._limits.max_image_bytes,
        )

    def _resolve_case_image_bounded(
        self,
        source_id: str,
        case_id: str,
        *,
        maximum_bytes: int,
    ) -> ResolvedWeakCaseImage:
        if WEAK_CASE_SOURCE_PATTERN.fullmatch(source_id) is None:
            raise WeakCaseCatalogError(
                "source_not_found",
                "The reference source identifier is invalid",
            )
        if WEAK_CASE_ID_PATTERN.fullmatch(case_id) is None:
            raise WeakCaseCatalogError(
                "case_not_found",
                "The reference case identifier is invalid",
            )
        indexed = self._ensure_indexed(source_id)
        case = indexed.cases_by_id.get(case_id)
        if case is None:
            raise WeakCaseCatalogError(
                "case_not_found",
                "The reference case was not found in the selected source",
            )
        if case.review_state != "unconfirmed" or case.review_required is not True:
            raise WeakCaseCatalogError(
                "unsafe_case_state",
                "The reference case state is not eligible for read-only viewing",
            )
        if len(case.images) != 1:
            raise WeakCaseCatalogError(
                "image_unavailable",
                "The reference case does not resolve to one raster image",
            )
        observation = case.images[0]
        if observation.size_bytes > maximum_bytes:
            raise WeakCaseCatalogError(
                "image_too_large",
                "The reference image exceeds the bounded preview size",
            )
        current_path, current_identity = structure._validate_source_path(  # type: ignore[attr-defined]
            indexed.path,
            self._limits.structure_limits,
        )
        if current_path != indexed.path or current_identity != indexed.identity:
            self._discard(source_id)
            raise WeakCaseCatalogError(
                "source_changed",
                "The reference source changed after it was indexed",
            )

        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(str(indexed.path), flags)
        except OSError as error:
            raise WeakCaseCatalogError(
                "source_unavailable",
                "The reference source could not be reopened",
            ) from error
        try:
            with os.fdopen(descriptor, "rb", buffering=0, closefd=True) as source:
                if (
                    structure._file_identity(os.fstat(source.fileno()))  # type: ignore[attr-defined]
                    != indexed.identity
                ):
                    raise WeakCaseCatalogError(
                        "source_changed",
                        "The reference source identity changed",
                    )
                with zipfile.ZipFile(source, mode="r") as archive:
                    normalized = structure._validate_archive_entries(  # type: ignore[attr-defined]
                        archive.infolist(),
                        self._limits.structure_limits,
                    )
                    info = normalized.get(
                        unicodedata.normalize(
                            "NFC", observation.package_path
                        ).casefold()
                    )
                    if info is None or info.is_dir():
                        raise WeakCaseCatalogError(
                            "image_unavailable",
                            "The reference image member is unavailable",
                        )
                    payload = structure._read_member_bounded(  # type: ignore[attr-defined]
                        archive,
                        info,
                        maximum=maximum_bytes,
                    )
                if (
                    structure._file_identity(os.fstat(source.fileno()))  # type: ignore[attr-defined]
                    != indexed.identity
                ):
                    raise WeakCaseCatalogError(
                        "source_changed",
                        "The reference source changed while the image was read",
                    )
        except WeakCaseCatalogError:
            raise
        except (
            OSError,
            RuntimeError,
            zipfile.BadZipFile,
            HwpxLimitExceeded,
        ) as error:
            raise WeakCaseCatalogError(
                "image_unavailable",
                "The reference image could not be read safely",
            ) from error

        detected_media, width, height = _validate_payload(
            payload,
            limits=self._limits,
        )
        digest = hashlib.sha256(payload).hexdigest()
        if (
            detected_media != observation.media_type
            or width != observation.width
            or height != observation.height
            or len(payload) != observation.size_bytes
            or digest != observation.image_sha256
        ):
            raise WeakCaseCatalogError(
                "image_integrity_mismatch",
                "The reference image no longer matches the indexed evidence",
            )
        return ResolvedWeakCaseImage(
            body=payload,
            media_type=detected_media,
            size_bytes=len(payload),
            sha256=digest,
            source_sha256=digest,
            source_size_bytes=len(payload),
            derived_thumbnail=False,
        )

    def case_attestation(
        self,
        source_id: str,
        case_id: str,
    ) -> dict:
        """Return exact, path-free provenance for explicit user approval."""

        indexed = self._ensure_indexed(source_id)
        case = indexed.cases_by_id.get(case_id)
        if case is None:
            raise WeakCaseCatalogError(
                "case_not_found",
                "The reference case was not found in the selected source",
            )
        if (
            case.review_state != "unconfirmed"
            or case.review_required is not True
            or case.training_eligible
            or case.annotation_created
            or case.incident_created
        ):
            raise WeakCaseCatalogError(
                "unsafe_case_state",
                "The reference case violated the approval boundary",
            )
        if len(case.images) != 1:
            raise WeakCaseCatalogError(
                "image_unavailable",
                "The reference case does not resolve to one raster image",
            )
        image = self.resolve_case_image_for_visual_index(
            source_id,
            case_id,
        )
        observation = case.images[0]
        if image.sha256 != observation.image_sha256:
            raise WeakCaseCatalogError(
                "image_integrity_mismatch",
                "The reference image no longer matches its case provenance",
            )

        audited = self._audited_sample_cases(indexed)
        sample_binding = (
            audited.bindings_by_case_id.get(case.case_id)
            if audited is not None
            else None
        )
        if audited is not None and sample_binding is None:
            raise WeakCaseCatalogError(
                "sample_reference_picture_not_photo_evidence",
                "The selected picture is not an audited road-evidence photo slot",
            )
        exact = (
            _exact_sample_case_fields(sample_binding)
            if sample_binding is not None
            else None
        )
        occurrences, registry_complete = self._image_occurrence_index(
            require_complete=True,
        )
        evidence_identity = _evidence_identity_metadata(
            observation.image_sha256,
            image_occurrences=occurrences,
            registry_complete=registry_complete,
        )

        attestation = {
            "source_id": indexed.definition.source_id,
            "case_id": case.case_id,
            "document_name": indexed.definition.document_name,
            "document_sha256": indexed.definition.expected_document_sha256,
            "structure_target_summary_sha256": (
                indexed.index.structure_target_summary_sha256
            ),
            "index_manifest_sha256": (
                indexed.index.index_manifest_sha256
            ),
            "quarter": indexed.definition.quarter,
            "section_path": case.source.section_path,
            "section_index": case.source.section_index,
            "picture_target_id": case.source.picture_target_id,
            "picture_index": case.source.picture_index,
            "picture_xml_id": case.source.picture_xml_id,
            "table_id": case.source.table_id,
            "table_index": case.source.table_index,
            "cell_target_id": case.source.cell_target_id,
            "cell_row": case.source.cell_row,
            "cell_column": case.source.cell_column,
            "page_hint": (
                sample_binding.photo_page_number
                if sample_binding is not None
                else case.source.page_hint
            ),
            "page_hint_source": (
                "audited_sample_photo_page"
                if sample_binding is not None
                else case.source.page_hint_source
            ),
            "caption": (
                exact["caption"]
                if exact is not None
                else _first_candidate(case.caption_candidate)
            ),
            "route": (
                exact["route"]
                if exact is not None
                else _first_candidate(case.route_candidates)
            ),
            "location": (
                exact["location"]
                if exact is not None
                else _first_candidate(case.location_candidates)
            ),
            "evaluation": (
                exact["evaluation"]
                if exact is not None
                else _first_candidate(case.evaluation_candidates)
            ),
            "opinion": (
                exact["opinion"]
                if exact is not None
                else _first_candidate(case.opinion_candidates)
            ),
            "image_sha256": observation.image_sha256,
            "image_media_type": observation.media_type,
            "image_width": observation.width,
            "image_height": observation.height,
            "image_size_bytes": observation.size_bytes,
            "sample_layout_binding": (
                _sample_binding_payload(sample_binding)
                if sample_binding is not None
                else None
            ),
            **evidence_identity,
            "reference_state": "weak_unconfirmed",
            "review_required": True,
            "approved_reference": False,
            "training_eligible": False,
            "annotation_created": False,
            "incident_created": False,
        }
        if sample_binding is not None:
            attestation["semantic_attestation_sha256"] = (
                semantic_attestation_sha256(attestation)
            )
        else:
            attestation["semantic_attestation_sha256"] = None
        return attestation

    def _source_snapshot(self, definition: WeakCaseSourceDefinition) -> dict:
        try:
            indexed = self._ensure_indexed(definition.source_id)
            return self._ready_source_snapshot(
                indexed,
                audited=self._audited_sample_cases(indexed),
            )
        except WeakCaseCatalogError as error:
            return self._error_source_snapshot(definition, error.code)

    def _ready_source_snapshot(
        self,
        indexed: _IndexedSource,
        *,
        audited: Optional[_AuditedSampleCases] = None,
    ) -> dict:
        index = indexed.index
        case_count = (
            len(audited.bindings_by_case_id)
            if audited is not None
            else index.case_count
        )
        return {
            "source_id": indexed.definition.source_id,
            "document_name": indexed.definition.document_name,
            "document_sha256": indexed.definition.expected_document_sha256,
            "authorization_purpose": indexed.definition.purpose,
            "quarter": indexed.definition.quarter,
            "status": "ready",
            "case_count": case_count,
            "resolved_case_count": case_count,
            "review_required_count": case_count,
            "approved_reference_count": 0,
            "training_eligible_count": index.training_eligible_count,
            "annotation_count": index.annotation_count,
            "incident_count": index.incident_count,
            "index_manifest_sha256": index.index_manifest_sha256,
            "companion_pdf": self._pdf_companions.snapshot(
                indexed.definition.quarter
            ),
        }

    def _error_source_snapshot(
        self,
        definition: WeakCaseSourceDefinition,
        error_code: str,
    ) -> dict:
        return {
            "source_id": definition.source_id,
            "document_name": definition.document_name,
            "document_sha256": definition.expected_document_sha256,
            "authorization_purpose": definition.purpose,
            "quarter": definition.quarter,
            "status": error_code,
            "case_count": 0,
            "resolved_case_count": 0,
            "review_required_count": 0,
            "approved_reference_count": 0,
            "training_eligible_count": 0,
            "annotation_count": 0,
            "incident_count": 0,
            "index_manifest_sha256": None,
            "companion_pdf": self._pdf_companions.snapshot(
                definition.quarter
            ),
        }

    def _public_case(
        self,
        indexed: _IndexedSource,
        case: HwpxWeakCase,
        score: int,
        matched_terms: Sequence[str],
        *,
        rank: int,
        sample_binding: Optional[
            SampleReferenceCaseBinding
        ] = None,
        image_occurrences: Mapping[str, Tuple[str, ...]],
        duplicate_image_registry_complete: bool,
    ) -> dict:
        image = case.images[0] if len(case.images) == 1 else None
        exact = (
            _exact_sample_case_fields(sample_binding)
            if sample_binding is not None
            else None
        )
        evidence_identity = _evidence_identity_metadata(
            image.image_sha256 if image is not None else None,
            image_occurrences=image_occurrences,
            registry_complete=(
                duplicate_image_registry_complete
            ),
        )
        public = {
            "source_id": indexed.definition.source_id,
            "case_id": case.case_id,
            "rank": rank,
            "similarity_score": score,
            "matched_terms": list(matched_terms[:8]),
            "reference_state": "weak_unconfirmed",
            "review_required": True,
            "approved_reference": False,
            "training_eligible": False,
            "annotation_created": False,
            "incident_created": False,
            "quarter": indexed.definition.quarter,
            "document_name": indexed.definition.document_name,
            "document_sha256": indexed.definition.expected_document_sha256,
            "structure_target_summary_sha256": (
                indexed.index.structure_target_summary_sha256
            ),
            "index_manifest_sha256": (
                indexed.index.index_manifest_sha256
            ),
            "section_path": case.source.section_path,
            "section_index": case.source.section_index,
            "picture_target_id": case.source.picture_target_id,
            "page_hint": (
                sample_binding.photo_page_number
                if sample_binding is not None
                else case.source.page_hint
            ),
            "page_hint_source": (
                "audited_sample_photo_page"
                if sample_binding is not None
                else case.source.page_hint_source
            ),
            "cell_target_id": case.source.cell_target_id,
            "table_index": case.source.table_index,
            "cell_row": case.source.cell_row,
            "cell_column": case.source.cell_column,
            "picture_index": case.source.picture_index,
            "sample_layout_binding_schema": (
                sample_binding.sample_layout_binding_schema
                if sample_binding is not None
                else None
            ),
            "sample_layout_binding_revision": (
                sample_binding.sample_layout_binding_revision
                if sample_binding is not None
                else None
            ),
            "semantic_projection_revision": (
                sample_binding.semantic_projection_revision
                if sample_binding is not None
                else None
            ),
            "sample_evidence_group_id": (
                sample_binding.sample_evidence_group_id
                if sample_binding is not None
                else None
            ),
            "group_photo_index": (
                sample_binding.group_photo_index
                if sample_binding is not None
                else None
            ),
            "group_photo_count": (
                sample_binding.group_photo_count
                if sample_binding is not None
                else None
            ),
            "photo_slot_id": (
                sample_binding.photo_slot_id
                if sample_binding is not None
                else None
            ),
            "sample_layout_binding_sha256": (
                sample_binding.binding_sha256
                if sample_binding is not None
                else None
            ),
            "caption": (
                exact["caption"]
                if exact is not None
                else _first_candidate(case.caption_candidate)
            ),
            "route": (
                exact["route"]
                if exact is not None
                else _first_candidate(case.route_candidates)
            ),
            "location": (
                exact["location"]
                if exact is not None
                else _first_candidate(case.location_candidates)
            ),
            "evaluation": (
                exact["evaluation"]
                if exact is not None
                else _first_candidate(case.evaluation_candidates)
            ),
            "opinion": (
                exact["opinion"]
                if exact is not None
                else _first_candidate(case.opinion_candidates)
            ),
            "image_available": bool(
                image
                and (
                    image.size_bytes
                    <= self._maximum_stream_image_bytes
                    or (
                        sample_binding is not None
                        and image.size_bytes
                        <= self._limits.max_image_bytes
                    )
                )
            ),
            "image_sha256": image.image_sha256 if image else None,
            "image_media_type": image.media_type if image else None,
            "image_width": image.width if image else None,
            "image_height": image.height if image else None,
            **evidence_identity,
        }
        if sample_binding is not None:
            public["semantic_attestation_sha256"] = (
                semantic_attestation_sha256(
                    {
                        **public,
                        "sample_layout_binding": (
                            _sample_binding_payload(
                                sample_binding
                            )
                        ),
                    }
                )
            )
        else:
            public["semantic_attestation_sha256"] = None
        return public

    def _ensure_indexed(self, source_id: str) -> _IndexedSource:
        definition = self._definitions.get(source_id)
        if definition is None:
            raise WeakCaseCatalogError(
                "source_not_found",
                "The reference source is not server-registered",
            )
        with self._lock:
            cached = self._cache.get(source_id)
            if cached is not None:
                try:
                    _, identity = structure._validate_source_path(  # type: ignore[attr-defined]
                        cached.path,
                        self._limits.structure_limits,
                    )
                except Exception as error:
                    self._cache.pop(source_id, None)
                    self._audited_sample_cache.pop(source_id, None)
                    self._visual_snapshot_cache = None
                    raise WeakCaseCatalogError(
                        "source_unavailable",
                        "The reference source is no longer available",
                    ) from error
                if identity != cached.identity:
                    self._cache.pop(source_id, None)
                    self._audited_sample_cache.pop(source_id, None)
                    self._visual_snapshot_cache = None
                    raise WeakCaseCatalogError(
                        "source_changed",
                        "The reference source changed after indexing",
                    )
                return cached

            path = (self._root / definition.document_name).resolve()
            if path.parent != self._root or path.name != definition.document_name:
                raise WeakCaseCatalogError(
                    "source_path_rejected",
                    "The registered reference source escaped the sample root",
                )
            if not path.exists():
                raise WeakCaseCatalogError(
                    "source_missing",
                    "The registered reference source is not installed",
                )
            authorization = HwpxWeakCaseAuthorization(
                document_name=definition.document_name,
                expected_document_sha256=(
                    definition.expected_document_sha256
                ),
                purpose=definition.purpose,
                user_confirmed_read_only=True,
            )
            try:
                index = inspect_hwpx_weak_cases(
                    path,
                    authorization=authorization,
                    limits=self._limits,
                )
                _, identity = structure._validate_source_path(  # type: ignore[attr-defined]
                    path,
                    self._limits.structure_limits,
                )
            except HwpxWeakCaseAuthorizationError as error:
                raise WeakCaseCatalogError(
                    "source_sha256_mismatch",
                    "The registered reference source SHA-256 did not match",
                ) from error
            except (
                HwpxWeakCaseError,
                HwpxStructureError,
            ) as error:
                raise WeakCaseCatalogError(
                    "source_invalid",
                    "The registered reference source could not be indexed safely",
                ) from error
            if (
                index.document_name != definition.document_name
                or index.document_sha256
                != definition.expected_document_sha256
                or index.quarter != definition.quarter
                or index.training_eligible_count != 0
                or index.annotation_count != 0
                or index.incident_count != 0
                or index.review_required_count != index.case_count
            ):
                raise WeakCaseCatalogError(
                    "source_contract_mismatch",
                    "The registered reference index violated its manifest",
                )
            cases_by_id = {case.case_id: case for case in index.cases}
            if (
                len(cases_by_id) != len(index.cases)
                or any(
                    WEAK_CASE_ID_PATTERN.fullmatch(case_id) is None
                    for case_id in cases_by_id
                )
            ):
                raise WeakCaseCatalogError(
                    "source_contract_mismatch",
                    "The reference index produced invalid case identifiers",
                )
            cached = _IndexedSource(
                definition=definition,
                path=path,
                identity=identity,
                index=index,
                cases_by_id=cases_by_id,
            )
            self._cache[source_id] = cached
            return cached

    def _discard(self, source_id: str) -> None:
        with self._lock:
            self._cache.pop(source_id, None)
            self._audited_sample_cache.pop(source_id, None)
            self._visual_snapshot_cache = None


def _exact_cell_text(value: str) -> Optional[str]:
    normalized = unicodedata.normalize("NFC", " ".join(value.split()))
    return normalized or None


def _exact_sample_case_fields(
    binding: SampleReferenceCaseBinding,
) -> Dict[str, object]:
    """Project audited semantic cells without nearby-cell guessing.

    The sample has no dedicated per-photo location cell.  A caption may contain
    a place and a defect in one sentence, but splitting that sentence would be
    a new weak inference.  ``location`` therefore remains explicitly absent
    until the user confirms or edits it during reference approval.
    """

    caption = _exact_cell_text(binding.photo_caption.text_excerpt)
    route = _exact_cell_text(binding.inspection_route.text_excerpt)
    opinion = _exact_cell_text(
        binding.inspection_opinion_body.text_excerpt
    )
    search_texts = tuple(
        value
        for value in (caption, route, opinion)
        if value is not None
    )
    return {
        "caption": caption,
        "route": route,
        "location": None,
        "evaluation": None,
        "opinion": opinion,
        "search_texts": search_texts,
    }


def _sample_binding_payload(
    binding: SampleReferenceCaseBinding,
) -> Dict[str, object]:
    payload = asdict(binding)
    payload["square_markers"] = [
        asdict(marker) for marker in binding.square_markers
    ]
    return payload


def _evidence_identity_metadata(
    image_sha256: Optional[str],
    *,
    image_occurrences: Mapping[str, Tuple[str, ...]],
    registry_complete: bool,
) -> Dict[str, object]:
    case_refs = (
        image_occurrences.get(image_sha256, ())
        if image_sha256 is not None
        else ()
    )
    occurrence_count = len(case_refs)
    duplicate = occurrence_count > 1
    return {
        "duplicate_image_occurrence_count": occurrence_count,
        "duplicate_image_review_required": (
            duplicate or not registry_complete
        ),
        "duplicate_image_registry_complete": (
            registry_complete
        ),
        "duplicate_image_case_refs": list(case_refs),
    }


def _bounded_preview_thumbnail(
    source: ResolvedWeakCaseImage,
    *,
    maximum_bytes: int,
) -> ResolvedWeakCaseImage:
    try:
        from PIL import Image, ImageOps
    except ImportError as error:
        raise WeakCaseCatalogError(
            "preview_thumbnail_unavailable",
            "Pillow is required to preview this high-resolution reference",
        ) from error

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with Image.open(io.BytesIO(source.body)) as opened:
                opened.load()
                normalized = ImageOps.exif_transpose(opened)
                if normalized.mode in {"RGBA", "LA"} or (
                    normalized.mode == "P"
                    and "transparency" in normalized.info
                ):
                    rgba = normalized.convert("RGBA")
                    canvas = Image.new("RGB", rgba.size, "white")
                    canvas.paste(rgba, mask=rgba.getchannel("A"))
                    working = canvas
                else:
                    working = normalized.convert("RGB")
    except (OSError, RuntimeError, Warning) as error:
        raise WeakCaseCatalogError(
            "preview_thumbnail_unavailable",
            "The verified reference image could not be decoded for preview",
        ) from error

    resampling = getattr(Image, "Resampling", Image).LANCZOS
    for maximum_dimension in (
        1600,
        1280,
        1024,
        800,
        640,
        480,
        320,
        240,
        160,
        96,
    ):
        candidate = working.copy()
        candidate.thumbnail(
            (maximum_dimension, maximum_dimension),
            resample=resampling,
        )
        for quality in (88, 80, 72, 64, 56):
            output = io.BytesIO()
            candidate.save(
                output,
                format="JPEG",
                quality=quality,
                optimize=False,
                progressive=False,
                subsampling=2,
            )
            payload = output.getvalue()
            if len(payload) <= maximum_bytes:
                digest = hashlib.sha256(payload).hexdigest()
                return ResolvedWeakCaseImage(
                    body=payload,
                    media_type="image/jpeg",
                    size_bytes=len(payload),
                    sha256=digest,
                    source_sha256=source.sha256,
                    source_size_bytes=source.size_bytes,
                    derived_thumbnail=True,
                )
    raise WeakCaseCatalogError(
        "preview_thumbnail_too_large",
        "A bounded thumbnail could not be produced for this reference image",
    )


def _normalize_query(value: Optional[str]) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFC", " ".join(value.split()))
    return normalized[:240]


def _tokens(value: str) -> Tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            token.casefold()
            for token in _TOKEN_PATTERN.findall(value)
            if token.casefold() not in _STOP_WORDS
        )
    )


def _case_texts(case: HwpxWeakCase) -> Iterable[str]:
    if case.caption_candidate is not None:
        yield case.caption_candidate.text
    for candidates in (
        case.route_candidates,
        case.location_candidates,
        case.evaluation_candidates,
        case.opinion_candidates,
    ):
        for candidate in candidates:
            yield candidate.text
    if case.containing_cell_text is not None:
        yield case.containing_cell_text.text
    for evidence in case.nearby_cell_texts:
        yield evidence.text


def _similarity_score(
    case: HwpxWeakCase,
    *,
    issue_terms: Sequence[str],
    query_tokens: Sequence[str],
) -> Tuple[int, Tuple[str, ...]]:
    return _similarity_score_texts(
        tuple(_case_texts(case)),
        caption=(
            case.caption_candidate.text
            if case.caption_candidate is not None
            else None
        ),
        issue_terms=issue_terms,
        query_tokens=query_tokens,
    )


def _similarity_score_texts(
    search_texts: Sequence[str],
    *,
    caption: Optional[str],
    issue_terms: Sequence[str],
    query_tokens: Sequence[str],
) -> Tuple[int, Tuple[str, ...]]:
    haystack = unicodedata.normalize(
        "NFC", " ".join(search_texts)
    ).casefold()
    score = 0
    matched = []
    for term in issue_terms:
        normalized = term.casefold()
        if normalized in haystack:
            score += 18 if " " in normalized else 14
            matched.append(term)
    for token in query_tokens:
        if token in haystack:
            score += 5
            matched.append(token)
    if score and caption is not None:
        normalized_caption = caption.casefold()
        score += sum(
            3 for term in (*issue_terms, *query_tokens)
            if term.casefold() in normalized_caption
        )
    return score, tuple(dict.fromkeys(matched))


def score_weak_case_metadata(
    *,
    search_texts: Sequence[str],
    caption: Optional[str],
    issue_type: Optional[str],
    query_text: Optional[str],
) -> Tuple[int, Tuple[str, ...]]:
    """Apply the catalog's existing bounded text score to indexed metadata."""

    if isinstance(search_texts, (str, bytes)) or any(
        not isinstance(value, str) or len(value) > 16_384
        for value in search_texts
    ):
        raise ValueError("weak-case search texts are invalid")
    if caption is not None and (
        not isinstance(caption, str) or len(caption) > 16_384
    ):
        raise ValueError("weak-case caption is invalid")
    normalized_query = _normalize_query(query_text)
    return _similarity_score_texts(
        search_texts,
        caption=caption,
        issue_terms=_ISSUE_TERMS.get(issue_type or "", ()),
        query_tokens=_tokens(normalized_query),
    )


def _first_candidate(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, tuple):
        return value[0].text if value else None
    return value.text


def _first_image_size(case: HwpxWeakCase) -> int:
    return case.images[0].size_bytes if case.images else 2**63 - 1


def _pdf_photo_page_correspondence(
    companion_pdf: Mapping[str, object],
    source_cases: Sequence[Mapping[str, object]],
) -> dict:
    """Map HWPX photo-slot cases to PDF pages without adding PDF cases."""

    counts: Dict[int, int] = {}
    for case in source_cases:
        page_number = case.get("photo_page_number")
        if (
            isinstance(page_number, bool)
            or not isinstance(page_number, int)
            or page_number <= 0
        ):
            raise WeakCaseCatalogError(
                "source_contract_mismatch",
                "An HWPX photo case has an invalid PDF page address",
            )
        counts[page_number] = counts.get(page_number, 0) + 1
    status = companion_pdf.get("status")
    raw_pages = companion_pdf.get("pages")
    pages_by_number = {
        int(page["page_number"]): page
        for page in raw_pages
        if isinstance(raw_pages, list)
        and isinstance(page, Mapping)
        and isinstance(page.get("page_number"), int)
    }
    render = companion_pdf.get("render")
    render_status = (
        render.get("status")
        if isinstance(render, Mapping)
        else "not_run"
    )
    rendered_pages = {
        int(page["page_number"])
        for page in (
            render.get("rendered_pages", [])
            if isinstance(render, Mapping)
            else []
        )
        if isinstance(page, Mapping)
        and isinstance(page.get("page_number"), int)
    }
    rows = []
    mapped_case_count = 0
    for page_number in sorted(counts):
        page = pages_by_number.get(page_number)
        if status == "ready" and page is not None:
            page_status = "verified_pdf_page_address"
            mapped_case_count += counts[page_number]
            if page_number in rendered_pages:
                render_validation = "page_render_smoke_passed"
            elif render_status == "smoke_passed":
                render_validation = (
                    "document_render_smoke_passed_page_not_sampled"
                )
            else:
                render_validation = str(render_status)
            text_sha256 = page.get("text_sha256")
        else:
            page_status = f"unverified_{status}"
            render_validation = str(render_status)
            text_sha256 = None
        rows.append(
            {
                "pdf_page_number": page_number,
                "mapped_hwpx_photo_case_count": counts[page_number],
                "correspondence_status": page_status,
                "page_text_sha256": text_sha256,
                "render_validation": render_validation,
            }
        )
    case_count = len(source_cases)
    return {
        "status": (
            "verified"
            if status == "ready"
            and mapped_case_count == case_count
            else f"unverified_{status}"
        ),
        "hwpx_photo_case_count": case_count,
        "mapped_hwpx_photo_case_count": mapped_case_count,
        "unverified_hwpx_photo_case_count": (
            case_count - mapped_case_count
        ),
        "pdf_photo_case_count_added": 0,
        "page_count": len(rows),
        "pages": rows,
    }


def _validate_payload(
    payload: bytes,
    *,
    limits: HwpxWeakCaseLimits,
) -> Tuple[str, int, int]:
    # Reuse the extractor's exact raster magic/dimension policy.
    from .hwpx_weak_cases import _inspect_image_header

    return _inspect_image_header(payload, limits=limits)


__all__ = [
    "DEFAULT_SAMPLE_SOURCES",
    "ResolvedWeakCaseImage",
    "WEAK_CASE_CATALOG_SCHEMA",
    "WEAK_CASE_ID_PATTERN",
    "WEAK_CASE_SEARCH_SCHEMA",
    "WEAK_CASE_SEMANTIC_ATTESTATION_SCHEMA",
    "WEAK_CASE_SOURCE_PATTERN",
    "WEAK_CASE_VISUAL_SOURCE_SCHEMA",
    "WeakCaseCatalog",
    "WeakCaseCatalogError",
    "WeakCaseSourceDefinition",
    "default_sample_root",
    "score_weak_case_metadata",
    "semantic_attestation_sha256",
]
