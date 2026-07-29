"""Server-owned, path-free access to the approved sample HWPX case sources.

The renderer is deliberately unable to register a document or choose an
archive member.  This catalog owns an exact allowlist of document name,
SHA-256 and read-only purpose, then exposes only opaque source/case ids.

Historical report cases remain ``weak_unconfirmed``.  Searching or viewing a
thumbnail never creates an Annotation, Incident, dataset membership or
training label.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from . import hwpx_structure as structure
from .hwpx_structure import (
    FileIdentity,
    HwpxLimitExceeded,
    HwpxStructureError,
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


WEAK_CASE_CATALOG_SCHEMA = "trafficvisson.weak-case-catalog.v1"
WEAK_CASE_SEARCH_SCHEMA = "trafficvisson.weak-case-search.v1"
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


@dataclass(frozen=True)
class ResolvedWeakCaseImage:
    body: bytes
    media_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class _IndexedSource:
    definition: WeakCaseSourceDefinition
    path: Path
    identity: FileIdentity
    index: HwpxWeakCaseIndex
    cases_by_id: Mapping[str, HwpxWeakCase]


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
        self._cache: Dict[str, _IndexedSource] = {}
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
            except WeakCaseCatalogError as error:
                source_states.append(
                    self._error_source_snapshot(definition, error.code)
                )
                continue
            source_states.append(self._ready_source_snapshot(indexed))
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
                        "A historical case violated the review-only boundary",
                    )
                score, matched_terms = _similarity_score(
                    case,
                    issue_terms=issue_terms,
                    query_tokens=query_tokens,
                )
                if score <= 0:
                    continue
                ranked.append(
                    (
                        -score,
                        _first_image_size(case),
                        definition.quarter,
                        case.source.page_hint,
                        case.case_id,
                        indexed,
                        case,
                        score,
                        matched_terms,
                    )
                )
        ranked.sort(key=lambda row: row[:5])
        cases = [
            self._public_case(indexed, case, score, terms, rank=index + 1)
            for index, (
                _neg_score,
                _image_size,
                _quarter,
                _page,
                _case_id,
                indexed,
                case,
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

    def resolve_case_image(
        self,
        source_id: str,
        case_id: str,
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
        if observation.size_bytes > self._maximum_stream_image_bytes:
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
                        maximum=self._maximum_stream_image_bytes,
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
        )

    def _source_snapshot(self, definition: WeakCaseSourceDefinition) -> dict:
        try:
            return self._ready_source_snapshot(
                self._ensure_indexed(definition.source_id)
            )
        except WeakCaseCatalogError as error:
            return self._error_source_snapshot(definition, error.code)

    def _ready_source_snapshot(self, indexed: _IndexedSource) -> dict:
        index = indexed.index
        return {
            "source_id": indexed.definition.source_id,
            "document_name": indexed.definition.document_name,
            "document_sha256": indexed.definition.expected_document_sha256,
            "authorization_purpose": indexed.definition.purpose,
            "quarter": indexed.definition.quarter,
            "status": "ready",
            "case_count": index.case_count,
            "resolved_case_count": index.resolved_case_count,
            "review_required_count": index.review_required_count,
            "approved_reference_count": 0,
            "training_eligible_count": index.training_eligible_count,
            "annotation_count": index.annotation_count,
            "incident_count": index.incident_count,
            "index_manifest_sha256": index.index_manifest_sha256,
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
        }

    def _public_case(
        self,
        indexed: _IndexedSource,
        case: HwpxWeakCase,
        score: int,
        matched_terms: Sequence[str],
        *,
        rank: int,
    ) -> dict:
        image = case.images[0] if len(case.images) == 1 else None
        return {
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
            "page_hint": case.source.page_hint,
            "page_hint_source": case.source.page_hint_source,
            "table_index": case.source.table_index,
            "cell_row": case.source.cell_row,
            "cell_column": case.source.cell_column,
            "picture_index": case.source.picture_index,
            "caption": _first_candidate(case.caption_candidate),
            "route": _first_candidate(case.route_candidates),
            "location": _first_candidate(case.location_candidates),
            "evaluation": _first_candidate(case.evaluation_candidates),
            "opinion": _first_candidate(case.opinion_candidates),
            "image_available": bool(
                image
                and image.size_bytes <= self._maximum_stream_image_bytes
            ),
            "image_media_type": image.media_type if image else None,
            "image_width": image.width if image else None,
            "image_height": image.height if image else None,
        }

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
                    raise WeakCaseCatalogError(
                        "source_unavailable",
                        "The reference source is no longer available",
                    ) from error
                if identity != cached.identity:
                    self._cache.pop(source_id, None)
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
    haystack = unicodedata.normalize(
        "NFC", " ".join(_case_texts(case))
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
    if score and case.caption_candidate is not None:
        caption = case.caption_candidate.text.casefold()
        score += sum(
            3 for term in (*issue_terms, *query_tokens)
            if term.casefold() in caption
        )
    return score, tuple(dict.fromkeys(matched))


def _first_candidate(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, tuple):
        return value[0].text if value else None
    return value.text


def _first_image_size(case: HwpxWeakCase) -> int:
    return case.images[0].size_bytes if case.images else 2**63 - 1


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
    "WEAK_CASE_SOURCE_PATTERN",
    "WeakCaseCatalog",
    "WeakCaseCatalogError",
    "WeakCaseSourceDefinition",
    "default_sample_root",
]
