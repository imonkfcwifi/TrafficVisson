"""Safe, read-only weak-case indexing for authorized sample HWPX reports.

The sample reports are useful historical evidence, but their photographs and
surrounding text are not ground-truth labels.  This module therefore produces
only review candidates.  It never creates an Annotation, Incident, dataset
row, training label, extracted file, or document edit.

The implementation builds on :mod:`app.hwpx_structure` and keeps the exact
document hash and deterministic structure targets in every case.  Referenced
image members are read one at a time into memory, checked against strict ZIP
and image-header bounds, hashed, described, and then discarded.  No archive
member is extracted to disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import unicodedata
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import (
    BinaryIO,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)
from xml.etree import ElementTree

from . import hwpx_structure as structure
from .hwpx_structure import (
    CellSnapshot,
    FileIdentity,
    HwpxFormatError,
    HwpxLimitExceeded,
    HwpxSecurityError,
    HwpxSourceChanged,
    HwpxStructureLimits,
    HwpxStructureSnapshot,
    PictureSnapshot,
    TableSnapshot,
    inspect_hwpx_structure,
)


HWPX_WEAK_CASE_SCHEMA = "trafficvisson.hwpx-weak-case-index.v1"
HWPX_WEAK_CASE_PURPOSE = "sample_hwpx_read_only_weak_case_index"
_EXTRACTOR_VERSION = "1.0.0"
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_QUARTER_PATTERN = re.compile(r"(?<![0-9])([1-4])\s*분기")
_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_IMAGE_MEDIA_ALIASES = {
    "image/bmp": "image/bmp",
    "image/x-bmp": "image/bmp",
    "image/x-ms-bmp": "image/bmp",
    "image/png": "image/png",
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/gif": "image/gif",
    "image/webp": "image/webp",
}
_JPEG_START_OF_FRAME = frozenset(
    {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
)
_ROUTE_LABELS = ("노선명", "노선", "도로명")
_LOCATION_LABELS = ("점검위치", "발생위치", "위치", "구간", "지점")
_EVALUATION_LABELS = ("평가항목", "점검항목", "평가", "항목", "상태")
_OPINION_LABELS = (
    "자문·점검 의견",
    "자문점검의견",
    "자문의견",
    "점검의견",
    "조치의견",
    "검토의견",
    "의견",
)
_ROAD_EVIDENCE_TERMS = (
    "포트홀",
    "노면",
    "파손",
    "삭초",
    "수풀",
    "식생",
    "낙하물",
    "표지판",
    "이정표",
    "터널",
    "차선",
    "재도색",
    "유도선",
    "중앙분리대",
)
_LOCATION_TERMS = (
    "상행",
    "하행",
    "방향",
    "터널",
    "교량",
    "대교",
    "나들목",
    "분기점",
    "휴게소",
    "IC",
    "JCT",
    "TG",
    "km",
)


class HwpxWeakCaseError(RuntimeError):
    """Base class for weak-case indexing failures."""


class HwpxWeakCaseAuthorizationError(HwpxWeakCaseError):
    """Raised when an exact read-only source authorization is missing."""


class HwpxWeakCaseSecurityError(HwpxWeakCaseError):
    """Raised when a package image or manifest violates a security rule."""


class HwpxWeakCaseFormatError(HwpxWeakCaseError):
    """Raised when required weak-case package data is malformed."""


@dataclass(frozen=True)
class HwpxWeakCaseAuthorization:
    """Exact-document authorization supplied by the owning application.

    The caller must obtain user authorization before constructing this value.
    Requiring the already-known SHA-256 prevents this lower-level inspector
    from silently widening approval to another file with the same name.
    """

    document_name: str
    expected_document_sha256: str
    purpose: str
    user_confirmed_read_only: bool


@dataclass(frozen=True)
class HwpxWeakCaseLimits:
    """Hard bounds for metadata and embedded-image inspection."""

    structure_limits: HwpxStructureLimits = field(
        default_factory=HwpxStructureLimits
    )
    max_manifest_bytes: int = 2 * 1024**2
    max_manifest_items: int = 10_000
    max_metadata_values: int = 128
    max_metadata_chars: int = 16_384
    max_referenced_images: int = 2_048
    max_image_bytes: int = 64 * 1024**2
    max_total_image_bytes: int = 3 * 1024**3
    max_image_dimension: int = 65_535
    max_image_pixels: int = 300_000_000
    max_context_cells: int = 8
    max_context_chars: int = 512
    max_candidate_chars: int = 320
    max_candidates_per_field: int = 3

    def __post_init__(self) -> None:
        bounds = (
            ("max_manifest_bytes", self.max_manifest_bytes, 128, 32 * 1024**2),
            ("max_manifest_items", self.max_manifest_items, 1, 100_000),
            ("max_metadata_values", self.max_metadata_values, 1, 10_000),
            ("max_metadata_chars", self.max_metadata_chars, 1, 1024**2),
            (
                "max_referenced_images",
                self.max_referenced_images,
                1,
                100_000,
            ),
            ("max_image_bytes", self.max_image_bytes, 32, 512 * 1024**2),
            (
                "max_total_image_bytes",
                self.max_total_image_bytes,
                32,
                16 * 1024**3,
            ),
            ("max_image_dimension", self.max_image_dimension, 1, 1_000_000),
            ("max_image_pixels", self.max_image_pixels, 1, 1_000_000_000),
            ("max_context_cells", self.max_context_cells, 1, 128),
            ("max_context_chars", self.max_context_chars, 1, 16_384),
            ("max_candidate_chars", self.max_candidate_chars, 1, 16_384),
            (
                "max_candidates_per_field",
                self.max_candidates_per_field,
                1,
                32,
            ),
        )
        for name, value, minimum, maximum in bounds:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{name} is outside the supported range")
        if self.max_image_bytes > self.max_total_image_bytes:
            raise ValueError(
                "max_total_image_bytes must accommodate one image"
            )


@dataclass(frozen=True)
class CellTextEvidence:
    cell_target_id: str
    table_id: str
    table_index: int
    row: int
    column: int
    relationship: str
    distance: int
    text: str
    text_sha256: str
    text_truncated: bool


@dataclass(frozen=True)
class WeakTextCandidate:
    field: str
    text: str
    source_cell_target_id: str
    derivation: str
    confidence: str = "weak_unconfirmed"
    review_required: bool = True
    confirmed: bool = False


@dataclass(frozen=True)
class EmbeddedImageObservation:
    binary_item_ref: str
    package_path: str
    image_sha256: str
    media_type: str
    declared_media_type: str
    size_bytes: int
    width: int
    height: int
    header_validation: str
    duplicate_group_id: str
    duplicate_occurrence_count: int
    bytes_retained: bool = False


@dataclass(frozen=True)
class WeakCaseSourceReference:
    section_path: str
    section_index: int
    picture_target_id: str
    picture_index: int
    picture_xml_id: Optional[str]
    binary_item_refs: Tuple[str, ...]
    package_paths: Tuple[str, ...]
    table_id: Optional[str]
    table_index: Optional[int]
    cell_target_id: Optional[str]
    cell_row: Optional[int]
    cell_column: Optional[int]
    preceding_explicit_page_break_count: int
    page_hint: int
    page_hint_source: str


@dataclass(frozen=True)
class HwpxWeakCase:
    case_id: str
    document_sha256: str
    quarter: Optional[int]
    source: WeakCaseSourceReference
    images: Tuple[EmbeddedImageObservation, ...]
    image_resolution_state: str
    unresolved_image_refs: Tuple[str, ...]
    containing_cell_text: Optional[CellTextEvidence]
    nearby_cell_texts: Tuple[CellTextEvidence, ...]
    caption_candidate: Optional[WeakTextCandidate]
    route_candidates: Tuple[WeakTextCandidate, ...]
    location_candidates: Tuple[WeakTextCandidate, ...]
    evaluation_candidates: Tuple[WeakTextCandidate, ...]
    opinion_candidates: Tuple[WeakTextCandidate, ...]
    review_state: str = "unconfirmed"
    review_required: bool = True
    training_eligible: bool = False
    annotation_created: bool = False
    incident_created: bool = False


@dataclass(frozen=True)
class WeakCaseExtractionProvenance:
    module: str
    extractor_version: str
    structure_schema_version: str
    extraction_method: str
    source_role: str
    image_byte_handling: str
    authorization_purpose: str


@dataclass(frozen=True)
class WeakCaseSafety:
    read_only: bool = True
    no_archive_extraction: bool = True
    no_document_edit: bool = True
    no_com: bool = True
    no_external_transfer: bool = True
    images_inspected_in_memory_only: bool = True
    image_bytes_persisted: bool = False
    package_bounds_enforced: bool = True
    image_magic_and_dimensions_verified: bool = True
    source_identity_verified_before_and_after: bool = True
    source_sha256_verified_before_and_after: bool = True
    review_required_before_promotion: bool = True
    automatic_annotation_creation: bool = False
    automatic_incident_creation: bool = False
    automatic_training_eligibility: bool = False


@dataclass(frozen=True)
class HwpxWeakCaseIndex:
    schema_version: str
    document_name: str
    document_sha256: str
    structure_target_summary_sha256: str
    source_identity_fingerprint_sha256: str
    quarter: Optional[int]
    quarter_source: Optional[str]
    metadata_excerpt: Tuple[str, ...]
    case_count: int
    resolved_case_count: int
    unresolved_case_count: int
    unique_image_count: int
    duplicate_image_group_count: int
    review_required_count: int
    training_eligible_count: int
    annotation_count: int
    incident_count: int
    cases: Tuple[HwpxWeakCase, ...]
    index_manifest_sha256: str
    provenance: WeakCaseExtractionProvenance
    safety: WeakCaseSafety

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _ManifestItem:
    item_id: str
    package_path: str
    declared_media_type: str


@dataclass(frozen=True)
class _PackageInspection:
    manifest_items: Mapping[str, _ManifestItem]
    metadata_excerpt: Tuple[str, ...]
    page_hints: Mapping[Tuple[str, int], Tuple[int, int]]
    image_observations: Mapping[str, EmbeddedImageObservation]
    unresolved_refs: Mapping[str, str]
    identity_before: FileIdentity
    identity_after: FileIdentity


def inspect_hwpx_weak_cases(
    source_path: Union[str, os.PathLike],
    *,
    authorization: HwpxWeakCaseAuthorization,
    limits: Optional[HwpxWeakCaseLimits] = None,
) -> HwpxWeakCaseIndex:
    """Build a deterministic, review-only index from one authorized HWPX."""

    active_limits = limits or HwpxWeakCaseLimits()
    source = Path(source_path)
    _validate_authorization(source, authorization)

    try:
        snapshot = inspect_hwpx_structure(
            source,
            limits=active_limits.structure_limits,
        )
    except (
        HwpxFormatError,
        HwpxLimitExceeded,
        HwpxSecurityError,
        HwpxSourceChanged,
    ):
        raise
    if snapshot.document_sha256 != authorization.expected_document_sha256:
        raise HwpxWeakCaseAuthorizationError(
            "authorized HWPX SHA-256 does not match the inspected document"
        )

    package = _inspect_package_images(
        source,
        snapshot=snapshot,
        limits=active_limits,
    )
    quarter, quarter_source = _derive_quarter(
        snapshot.document_name,
        package.metadata_excerpt,
    )
    cases = _assemble_cases(
        snapshot,
        package=package,
        quarter=quarter,
        limits=active_limits,
    )

    image_hashes = {
        image.image_sha256
        for case in cases
        for image in case.images
    }
    duplicate_groups = {
        image.duplicate_group_id
        for case in cases
        for image in case.images
        if image.duplicate_occurrence_count > 1
    }
    resolved_count = sum(
        case.image_resolution_state == "resolved" for case in cases
    )
    manifest_hash = _index_manifest_sha256(
        document_name=snapshot.document_name,
        document_sha256=snapshot.document_sha256,
        target_summary_sha256=snapshot.target_summary_sha256,
        quarter=quarter,
        quarter_source=quarter_source,
        metadata_excerpt=package.metadata_excerpt,
        cases=cases,
    )
    identity_fingerprint = _identity_fingerprint(
        package.identity_before,
        package.identity_after,
    )
    return HwpxWeakCaseIndex(
        schema_version=HWPX_WEAK_CASE_SCHEMA,
        document_name=snapshot.document_name,
        document_sha256=snapshot.document_sha256,
        structure_target_summary_sha256=(
            snapshot.target_summary_sha256
        ),
        source_identity_fingerprint_sha256=identity_fingerprint,
        quarter=quarter,
        quarter_source=quarter_source,
        metadata_excerpt=package.metadata_excerpt,
        case_count=len(cases),
        resolved_case_count=resolved_count,
        unresolved_case_count=len(cases) - resolved_count,
        unique_image_count=len(image_hashes),
        duplicate_image_group_count=len(duplicate_groups),
        review_required_count=len(cases),
        training_eligible_count=0,
        annotation_count=0,
        incident_count=0,
        cases=cases,
        index_manifest_sha256=manifest_hash,
        provenance=WeakCaseExtractionProvenance(
            module="app.hwpx_weak_cases",
            extractor_version=_EXTRACTOR_VERSION,
            structure_schema_version=snapshot.schema_version,
            extraction_method=(
                "exact_structure_targets_plus_bounded_in_memory_images"
            ),
            source_role="weak_unconfirmed_historical_report_case",
            image_byte_handling=(
                "read_one_member_hash_validate_then_discard"
            ),
            authorization_purpose=authorization.purpose,
        ),
        safety=WeakCaseSafety(),
    )


def _validate_authorization(
    source: Path,
    authorization: HwpxWeakCaseAuthorization,
) -> None:
    if not isinstance(authorization, HwpxWeakCaseAuthorization):
        raise HwpxWeakCaseAuthorizationError(
            "an exact HWPX weak-case authorization is required"
        )
    if authorization.purpose != HWPX_WEAK_CASE_PURPOSE:
        raise HwpxWeakCaseAuthorizationError(
            "HWPX authorization purpose is not allowlisted"
        )
    if authorization.user_confirmed_read_only is not True:
        raise HwpxWeakCaseAuthorizationError(
            "read-only HWPX inspection was not confirmed"
        )
    if (
        not isinstance(authorization.document_name, str)
        or not authorization.document_name
        or Path(authorization.document_name).name
        != authorization.document_name
    ):
        raise HwpxWeakCaseAuthorizationError(
            "authorized HWPX document name is invalid"
        )
    if source.name != authorization.document_name:
        raise HwpxWeakCaseAuthorizationError(
            "authorized HWPX document name does not match the source"
        )
    if (
        not isinstance(authorization.expected_document_sha256, str)
        or _SHA256_PATTERN.fullmatch(
            authorization.expected_document_sha256
        )
        is None
    ):
        raise HwpxWeakCaseAuthorizationError(
            "authorized HWPX SHA-256 is invalid"
        )


def _inspect_package_images(
    source_path: Path,
    *,
    snapshot: HwpxStructureSnapshot,
    limits: HwpxWeakCaseLimits,
) -> _PackageInspection:
    path, identity_before = structure._validate_source_path(  # type: ignore
        source_path,
        limits.structure_limits,
    )
    if identity_before != snapshot.identity_after:
        raise HwpxSourceChanged(
            "HWPX source identity changed before weak-case inspection"
        )

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError as error:
        raise HwpxWeakCaseSecurityError(
            "HWPX source could not be reopened read-only"
        ) from error

    try:
        with os.fdopen(
            descriptor,
            "rb",
            buffering=0,
            closefd=True,
        ) as source:
            opened_identity = structure._file_identity(  # type: ignore[attr-defined]
                os.fstat(source.fileno())
            )
            if opened_identity != identity_before:
                raise HwpxSourceChanged(
                    "HWPX source identity changed while it was reopened"
                )
            sha256_before = structure._hash_open_file(  # type: ignore[attr-defined]
                source,
                expected_size=identity_before.size_bytes,
            )
            if sha256_before != snapshot.document_sha256:
                raise HwpxSourceChanged(
                    "HWPX source bytes changed before image inspection"
                )
            (
                manifest_items,
                metadata_excerpt,
                page_hints,
                observations,
                unresolved,
            ) = _read_package(
                source,
                snapshot=snapshot,
                limits=limits,
            )
            sha256_after = structure._hash_open_file(  # type: ignore[attr-defined]
                source,
                expected_size=identity_before.size_bytes,
            )
            descriptor_identity_after = (
                structure._file_identity(  # type: ignore
                    os.fstat(source.fileno())
                )
            )
    except (
        HwpxSourceChanged,
        HwpxWeakCaseError,
        HwpxFormatError,
        HwpxLimitExceeded,
        HwpxSecurityError,
    ):
        raise
    except (OSError, RuntimeError, NotImplementedError) as error:
        raise HwpxWeakCaseFormatError(
            "HWPX weak-case package could not be read safely"
        ) from error

    structure._assert_no_link_or_reparse_components(path)  # type: ignore[attr-defined]
    try:
        identity_after = structure._file_identity(  # type: ignore[attr-defined]
            os.lstat(str(path))
        )
    except OSError as error:
        raise HwpxSourceChanged(
            "HWPX source disappeared after image inspection"
        ) from error
    if sha256_before != sha256_after:
        raise HwpxSourceChanged(
            "HWPX source bytes changed during image inspection"
        )
    if (
        identity_before != descriptor_identity_after
        or identity_before != identity_after
    ):
        raise HwpxSourceChanged(
            "HWPX source identity changed during image inspection"
        )

    return _PackageInspection(
        manifest_items=manifest_items,
        metadata_excerpt=metadata_excerpt,
        page_hints=page_hints,
        image_observations=observations,
        unresolved_refs=unresolved,
        identity_before=identity_before,
        identity_after=identity_after,
    )


def _read_package(
    source: BinaryIO,
    *,
    snapshot: HwpxStructureSnapshot,
    limits: HwpxWeakCaseLimits,
) -> Tuple[
    Mapping[str, _ManifestItem],
    Tuple[str, ...],
    Mapping[Tuple[str, int], Tuple[int, int]],
    Mapping[str, EmbeddedImageObservation],
    Mapping[str, str],
]:
    source.seek(0)
    try:
        with zipfile.ZipFile(source, mode="r") as archive:
            infos = archive.infolist()
            normalized_infos = (
                structure._validate_archive_entries(  # type: ignore
                    infos,
                    limits.structure_limits,
                )
            )
            manifest_info = normalized_infos.get(
                "contents/content.hpf"
            )
            if manifest_info is None:
                raise HwpxWeakCaseFormatError(
                    "HWPX package is missing Contents/content.hpf"
                )
            if manifest_info.file_size > limits.max_manifest_bytes:
                raise HwpxLimitExceeded(
                    "HWPX content manifest exceeds the byte limit"
                )
            manifest_raw = structure._read_member_bounded(  # type: ignore[attr-defined]
                archive,
                manifest_info,
                maximum=limits.max_manifest_bytes,
            )
            manifest_items, metadata_excerpt = _parse_content_manifest(
                manifest_raw,
                limits=limits,
            )
            page_hints = _read_page_hints(
                archive,
                normalized_infos=normalized_infos,
                snapshot=snapshot,
                limits=limits,
            )
            observations, unresolved = _read_referenced_images(
                archive,
                normalized_infos=normalized_infos,
                snapshot=snapshot,
                manifest_items=manifest_items,
                limits=limits,
            )
            return (
                manifest_items,
                metadata_excerpt,
                page_hints,
                observations,
                unresolved,
            )
    except (
        HwpxWeakCaseError,
        HwpxFormatError,
        HwpxLimitExceeded,
        HwpxSecurityError,
    ):
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise HwpxWeakCaseFormatError(
            "HWPX ZIP package is invalid"
        ) from error


def _parse_content_manifest(
    raw: bytes,
    *,
    limits: HwpxWeakCaseLimits,
) -> Tuple[Mapping[str, _ManifestItem], Tuple[str, ...]]:
    structure._reject_dtd_or_entities(raw)  # type: ignore[attr-defined]
    root, _ = structure._parse_xml_bounded(  # type: ignore[attr-defined]
        raw,
        entry_name="Contents/content.hpf",
        limits=limits.structure_limits,
    )
    items: Dict[str, _ManifestItem] = {}
    metadata: List[str] = []
    metadata_characters = 0
    for element in root.iter():
        local = structure._local_name(  # type: ignore
            element.tag
        ).casefold()
        if local == "item":
            if len(items) >= limits.max_manifest_items:
                raise HwpxLimitExceeded(
                    "HWPX content manifest item count exceeds the limit"
                )
            item_id = _xml_attribute(element, "id")
            href = _xml_attribute(element, "href")
            declared_media = _xml_attribute(element, "media-type")
            if not item_id or not href or not declared_media:
                raise HwpxWeakCaseFormatError(
                    "HWPX content manifest item is incomplete"
                )
            item_id = _bounded_normalized_text(
                item_id,
                maximum=256,
                label="manifest item id",
            )
            if item_id in items:
                raise HwpxWeakCaseSecurityError(
                    "HWPX content manifest contains duplicate item ids"
                )
            package_path = _safe_package_href(href, limits)
            declared_media = _bounded_normalized_text(
                declared_media,
                maximum=128,
                label="manifest media type",
            ).casefold()
            items[item_id] = _ManifestItem(
                item_id=item_id,
                package_path=package_path,
                declared_media_type=declared_media,
            )
        elif local in {"title", "subject", "description"}:
            value = " ".join("".join(element.itertext()).split())
            if value:
                metadata_characters = _append_metadata(
                    metadata,
                    value,
                    current_characters=metadata_characters,
                    limits=limits,
                )
        elif local == "meta":
            value = _xml_attribute(element, "content")
            if value:
                metadata_characters = _append_metadata(
                    metadata,
                    value,
                    current_characters=metadata_characters,
                    limits=limits,
                )
    return items, tuple(_unique_in_order(metadata))


def _append_metadata(
    target: List[str],
    value: str,
    *,
    current_characters: int,
    limits: HwpxWeakCaseLimits,
) -> int:
    if len(target) >= limits.max_metadata_values:
        raise HwpxLimitExceeded(
            "HWPX package metadata value count exceeds the limit"
        )
    normalized = _bounded_normalized_text(
        value,
        maximum=limits.max_context_chars,
        label="package metadata",
    )
    new_total = current_characters + len(normalized)
    if new_total > limits.max_metadata_chars:
        raise HwpxLimitExceeded(
            "HWPX package metadata characters exceed the limit"
        )
    target.append(normalized)
    return new_total


def _safe_package_href(
    href: str,
    limits: HwpxWeakCaseLimits,
) -> str:
    normalized = unicodedata.normalize("NFC", href.strip())
    if (
        not normalized
        or "%" in normalized
        or "?" in normalized
        or "#" in normalized
        or "://" in normalized
    ):
        raise HwpxWeakCaseSecurityError(
            "HWPX manifest href is not a plain package path"
        )
    try:
        return structure._validate_archive_member_name(  # type: ignore[attr-defined]
            normalized,
            limits.structure_limits.max_entry_name_chars,
        )
    except (HwpxLimitExceeded, HwpxSecurityError) as error:
        raise HwpxWeakCaseSecurityError(
            "HWPX manifest href is unsafe"
        ) from error


def _read_page_hints(
    archive: zipfile.ZipFile,
    *,
    normalized_infos: Mapping[str, zipfile.ZipInfo],
    snapshot: HwpxStructureSnapshot,
    limits: HwpxWeakCaseLimits,
) -> Mapping[Tuple[str, int], Tuple[int, int]]:
    result: Dict[Tuple[str, int], Tuple[int, int]] = {}
    preceding_breaks = 0
    pictures_by_section: Dict[str, List[PictureSnapshot]] = {}
    for picture in snapshot.pictures:
        pictures_by_section.setdefault(picture.section_path, []).append(
            picture
        )
    for section in snapshot.sections:
        info = normalized_infos.get(_archive_key(section.path))
        if info is None:
            raise HwpxWeakCaseFormatError(
                "HWPX section disappeared during weak-case inspection"
            )
        raw = structure._read_member_bounded(  # type: ignore[attr-defined]
            archive,
            info,
            maximum=limits.structure_limits.max_xml_entry_bytes,
        )
        structure._reject_dtd_or_entities(raw)  # type: ignore[attr-defined]
        root, _ = structure._parse_xml_bounded(  # type: ignore[attr-defined]
            raw,
            entry_name=section.path,
            limits=limits.structure_limits,
        )
        picture_index = 0
        for element in root.iter():
            break_source = (
                structure._explicit_page_break_source(  # type: ignore
                    element
                )
            )
            local = structure._local_name(  # type: ignore
                element.tag
            ).casefold()
            if local == "pic":
                result[(section.path, picture_index)] = (
                    preceding_breaks,
                    preceding_breaks + 1,
                )
                picture_index += 1
            if break_source is not None:
                preceding_breaks += 1
        expected_count = len(pictures_by_section.get(section.path, ()))
        if picture_index != expected_count:
            raise HwpxWeakCaseFormatError(
                "HWPX picture traversal changed during weak-case inspection"
            )
    return result


def _read_referenced_images(
    archive: zipfile.ZipFile,
    *,
    normalized_infos: Mapping[str, zipfile.ZipInfo],
    snapshot: HwpxStructureSnapshot,
    manifest_items: Mapping[str, _ManifestItem],
    limits: HwpxWeakCaseLimits,
) -> Tuple[
    Mapping[str, EmbeddedImageObservation],
    Mapping[str, str],
]:
    refs = _unique_in_order(
        ref
        for picture in snapshot.pictures
        for ref in picture.binary_item_refs
    )
    if len(refs) > limits.max_referenced_images:
        raise HwpxLimitExceeded(
            "HWPX referenced image count exceeds the limit"
        )

    raw_rows: Dict[
        str,
        Tuple[_ManifestItem, str, int, int, int, str],
    ] = {}
    unresolved: Dict[str, str] = {}
    total_bytes = 0
    for ref in refs:
        item = manifest_items.get(ref)
        if item is None:
            unresolved[ref] = "manifest_item_missing"
            continue
        declared_media = _IMAGE_MEDIA_ALIASES.get(
            item.declared_media_type
        )
        if declared_media is None:
            unresolved[ref] = "manifest_item_is_not_supported_raster_image"
            continue
        info = normalized_infos.get(_archive_key(item.package_path))
        if info is None or info.is_dir():
            unresolved[ref] = "image_package_member_missing"
            continue
        if info.file_size > limits.max_image_bytes:
            raise HwpxLimitExceeded(
                "HWPX embedded image exceeds the byte limit"
            )
        total_bytes += info.file_size
        if total_bytes > limits.max_total_image_bytes:
            raise HwpxLimitExceeded(
                "HWPX referenced image bytes exceed the total limit"
            )
        payload = structure._read_member_bounded(  # type: ignore[attr-defined]
            archive,
            info,
            maximum=limits.max_image_bytes,
        )
        detected_media, width, height = _inspect_image_header(
            payload,
            limits=limits,
        )
        if detected_media != declared_media:
            raise HwpxWeakCaseSecurityError(
                "HWPX embedded image magic does not match its media type"
            )
        digest = hashlib.sha256(payload).hexdigest()
        raw_rows[ref] = (
            item,
            digest,
            len(payload),
            width,
            height,
            detected_media,
        )
        del payload

    digest_occurrences: Dict[str, int] = {}
    for picture in snapshot.pictures:
        for ref in picture.binary_item_refs:
            row = raw_rows.get(ref)
            if row is not None:
                digest_occurrences[row[1]] = (
                    digest_occurrences.get(row[1], 0) + 1
                )

    observations: Dict[str, EmbeddedImageObservation] = {}
    for ref, row in raw_rows.items():
        item, digest, size, width, height, media_type = row
        observations[ref] = EmbeddedImageObservation(
            binary_item_ref=ref,
            package_path=item.package_path,
            image_sha256=digest,
            media_type=media_type,
            declared_media_type=item.declared_media_type,
            size_bytes=size,
            width=width,
            height=height,
            header_validation="magic_and_bounded_dimensions",
            duplicate_group_id=f"hwpx-image-{digest[:24]}",
            duplicate_occurrence_count=digest_occurrences[digest],
        )
    return observations, unresolved


def _inspect_image_header(
    payload: bytes,
    *,
    limits: HwpxWeakCaseLimits,
) -> Tuple[str, int, int]:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(payload) < 24 or payload[12:16] != b"IHDR":
            raise HwpxWeakCaseSecurityError(
                "HWPX PNG image header is invalid"
            )
        width, height = struct.unpack(">II", payload[16:24])
        media = "image/png"
    elif payload.startswith(b"BM"):
        if len(payload) < 26:
            raise HwpxWeakCaseSecurityError(
                "HWPX BMP image header is truncated"
            )
        dib_size = struct.unpack("<I", payload[14:18])[0]
        if dib_size == 12:
            width, height = struct.unpack("<HH", payload[18:22])
        elif dib_size >= 40:
            signed_width, signed_height = struct.unpack(
                "<ii",
                payload[18:26],
            )
            width, height = abs(signed_width), abs(signed_height)
        else:
            raise HwpxWeakCaseSecurityError(
                "HWPX BMP DIB header is unsupported"
            )
        media = "image/bmp"
    elif payload.startswith(b"\xff\xd8\xff"):
        width, height = _jpeg_dimensions(payload)
        media = "image/jpeg"
    elif payload.startswith((b"GIF87a", b"GIF89a")):
        if len(payload) < 10:
            raise HwpxWeakCaseSecurityError(
                "HWPX GIF image header is truncated"
            )
        width, height = struct.unpack("<HH", payload[6:10])
        media = "image/gif"
    elif (
        len(payload) >= 30
        and payload.startswith(b"RIFF")
        and payload[8:12] == b"WEBP"
    ):
        width, height = _webp_dimensions(payload)
        media = "image/webp"
    else:
        raise HwpxWeakCaseSecurityError(
            "HWPX embedded image uses unknown or invalid magic"
        )

    if (
        width <= 0
        or height <= 0
        or width > limits.max_image_dimension
        or height > limits.max_image_dimension
        or width * height > limits.max_image_pixels
    ):
        raise HwpxLimitExceeded(
            "HWPX embedded image dimensions exceed the limit"
        )
    return media, width, height


def _jpeg_dimensions(payload: bytes) -> Tuple[int, int]:
    cursor = 2
    while cursor + 3 < len(payload):
        if payload[cursor] != 0xFF:
            cursor += 1
            continue
        while cursor < len(payload) and payload[cursor] == 0xFF:
            cursor += 1
        if cursor >= len(payload):
            break
        marker = payload[cursor]
        cursor += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if marker == 0xDA:
            break
        if cursor + 2 > len(payload):
            break
        segment_length = struct.unpack(">H", payload[cursor : cursor + 2])[0]
        if segment_length < 2 or cursor + segment_length > len(payload):
            raise HwpxWeakCaseSecurityError(
                "HWPX JPEG segment length is invalid"
            )
        if marker in _JPEG_START_OF_FRAME:
            if segment_length < 7:
                raise HwpxWeakCaseSecurityError(
                    "HWPX JPEG dimension segment is truncated"
                )
            height, width = struct.unpack(
                ">HH",
                payload[cursor + 3 : cursor + 7],
            )
            return width, height
        cursor += segment_length
    raise HwpxWeakCaseSecurityError(
        "HWPX JPEG dimensions could not be verified"
    )


def _webp_dimensions(payload: bytes) -> Tuple[int, int]:
    kind = payload[12:16]
    if kind == b"VP8X":
        if len(payload) < 30:
            raise HwpxWeakCaseSecurityError(
                "HWPX WebP VP8X header is truncated"
            )
        width = 1 + int.from_bytes(payload[24:27], "little")
        height = 1 + int.from_bytes(payload[27:30], "little")
        return width, height
    if kind == b"VP8L":
        if len(payload) < 25 or payload[20] != 0x2F:
            raise HwpxWeakCaseSecurityError(
                "HWPX WebP VP8L header is invalid"
            )
        bits = int.from_bytes(payload[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if kind == b"VP8 ":
        if len(payload) < 30 or payload[23:26] != b"\x9d\x01\x2a":
            raise HwpxWeakCaseSecurityError(
                "HWPX WebP VP8 header is invalid"
            )
        width, height = struct.unpack("<HH", payload[26:30])
        return width & 0x3FFF, height & 0x3FFF
    raise HwpxWeakCaseSecurityError(
        "HWPX WebP image variant is unsupported"
    )


def _assemble_cases(
    snapshot: HwpxStructureSnapshot,
    *,
    package: _PackageInspection,
    quarter: Optional[int],
    limits: HwpxWeakCaseLimits,
) -> Tuple[HwpxWeakCase, ...]:
    tables_by_id = {table.table_id: table for table in snapshot.tables}
    cells_by_id = {
        cell.target_id: cell
        for table in snapshot.tables
        for cell in table.cells
    }
    cases: List[HwpxWeakCase] = []
    for picture in snapshot.pictures:
        table = (
            tables_by_id.get(picture.nearest_table_id)
            if picture.nearest_table_id
            else None
        )
        containing_cell = (
            cells_by_id.get(picture.nearest_cell_target_id)
            if picture.nearest_cell_target_id
            else None
        )
        containing_evidence, nearby_evidence = _context_evidence(
            table,
            containing_cell,
            limits=limits,
        )
        all_context = tuple(
            evidence
            for evidence in (containing_evidence, *nearby_evidence)
            if evidence is not None
        )
        images = tuple(
            package.image_observations[ref]
            for ref in picture.binary_item_refs
            if ref in package.image_observations
        )
        unresolved_refs = tuple(
            ref
            for ref in picture.binary_item_refs
            if ref not in package.image_observations
        )
        package_paths = tuple(
            package.manifest_items[ref].package_path
            for ref in picture.binary_item_refs
            if ref in package.manifest_items
        )
        break_count, page_hint = package.page_hints.get(
            (picture.section_path, picture.picture_index),
            (0, 1),
        )
        caption = _caption_candidate(all_context, limits=limits)
        route = _derive_field_candidates(
            "route",
            all_context,
            labels=_ROUTE_LABELS,
            fallback_terms=("고속도로", "국도", "노선"),
            limits=limits,
        )
        location = _derive_field_candidates(
            "location",
            all_context,
            labels=_LOCATION_LABELS,
            fallback_terms=_LOCATION_TERMS,
            limits=limits,
        )
        evaluation = _derive_field_candidates(
            "evaluation",
            all_context,
            labels=_EVALUATION_LABELS,
            fallback_terms=_ROAD_EVIDENCE_TERMS,
            limits=limits,
        )
        opinion = _derive_field_candidates(
            "opinion",
            all_context,
            labels=_OPINION_LABELS,
            fallback_terms=("필요", "조치", "보수", "개선", "의견"),
            limits=limits,
        )
        source_ref = WeakCaseSourceReference(
            section_path=picture.section_path,
            section_index=picture.section_index,
            picture_target_id=picture.target_id,
            picture_index=picture.picture_index,
            picture_xml_id=picture.xml_id,
            binary_item_refs=picture.binary_item_refs,
            package_paths=package_paths,
            table_id=picture.nearest_table_id,
            table_index=picture.nearest_table_index,
            cell_target_id=picture.nearest_cell_target_id,
            cell_row=picture.nearest_cell_row,
            cell_column=picture.nearest_cell_column,
            preceding_explicit_page_break_count=break_count,
            page_hint=page_hint,
            page_hint_source=(
                "explicit_page_break_sequence_hint_not_rendered_page"
            ),
        )
        case_id = _case_id(
            snapshot.document_sha256,
            source_ref,
            images,
            unresolved_refs,
        )
        if images and not unresolved_refs:
            resolution_state = "resolved"
        elif images:
            resolution_state = "partially_resolved"
        else:
            resolution_state = "unresolved"
        cases.append(
            HwpxWeakCase(
                case_id=case_id,
                document_sha256=snapshot.document_sha256,
                quarter=quarter,
                source=source_ref,
                images=images,
                image_resolution_state=resolution_state,
                unresolved_image_refs=unresolved_refs,
                containing_cell_text=containing_evidence,
                nearby_cell_texts=nearby_evidence,
                caption_candidate=caption,
                route_candidates=route,
                location_candidates=location,
                evaluation_candidates=evaluation,
                opinion_candidates=opinion,
            )
        )
    return tuple(cases)


def _context_evidence(
    table: Optional[TableSnapshot],
    containing_cell: Optional[CellSnapshot],
    *,
    limits: HwpxWeakCaseLimits,
) -> Tuple[
    Optional[CellTextEvidence],
    Tuple[CellTextEvidence, ...],
]:
    if table is None or containing_cell is None:
        return None, ()
    containing = _cell_evidence(
        table,
        containing_cell,
        relationship="containing_cell",
        distance=0,
        limits=limits,
    )
    neighbors = sorted(
        (
            (
                abs(cell.row - containing_cell.row)
                + abs(cell.column - containing_cell.column),
                cell.row,
                cell.column,
                cell,
            )
            for cell in table.cells
            if cell.target_id != containing_cell.target_id
            and cell.text_excerpt
        ),
        key=lambda row: (row[0], row[1], row[2], row[3].target_id),
    )
    nearby = tuple(
        _cell_evidence(
            table,
            cell,
            relationship=(
                "same_row"
                if cell.row == containing_cell.row
                else "nearby_cell"
            ),
            distance=distance,
            limits=limits,
        )
        for distance, _, _, cell in neighbors[: limits.max_context_cells]
    )
    return containing, nearby


def _cell_evidence(
    table: TableSnapshot,
    cell: CellSnapshot,
    *,
    relationship: str,
    distance: int,
    limits: HwpxWeakCaseLimits,
) -> CellTextEvidence:
    text = cell.text_excerpt[: limits.max_context_chars]
    return CellTextEvidence(
        cell_target_id=cell.target_id,
        table_id=table.table_id,
        table_index=table.table_index,
        row=cell.row,
        column=cell.column,
        relationship=relationship,
        distance=distance,
        text=text,
        text_sha256=cell.text_sha256,
        text_truncated=(
            cell.text_truncated
            or len(cell.text_excerpt) > limits.max_context_chars
        ),
    )


def _caption_candidate(
    evidence: Sequence[CellTextEvidence],
    *,
    limits: HwpxWeakCaseLimits,
) -> Optional[WeakTextCandidate]:
    for row in evidence:
        text = _strip_known_label_prefix(row.text)
        if not text:
            continue
        return WeakTextCandidate(
            field="caption",
            text=text[: limits.max_candidate_chars],
            source_cell_target_id=row.cell_target_id,
            derivation=(
                "containing_cell_text"
                if row.relationship == "containing_cell"
                else "nearest_nonempty_cell_text"
            ),
        )
    return None


def _derive_field_candidates(
    field_name: str,
    evidence: Sequence[CellTextEvidence],
    *,
    labels: Sequence[str],
    fallback_terms: Sequence[str],
    limits: HwpxWeakCaseLimits,
) -> Tuple[WeakTextCandidate, ...]:
    results: List[WeakTextCandidate] = []
    seen = set()
    for row in evidence:
        for label in labels:
            if label not in row.text:
                continue
            inline = row.text.split(label, 1)[1].lstrip(" :：-")
            candidate_row = row
            candidate_text = inline
            derivation = "same_cell_after_explicit_label"
            if not candidate_text:
                adjacent = _next_same_row_evidence(row, evidence)
                if adjacent is not None:
                    candidate_row = adjacent
                    candidate_text = adjacent.text
                    derivation = "adjacent_cell_after_explicit_label"
            _append_candidate(
                results,
                seen,
                field_name=field_name,
                text=candidate_text,
                source=candidate_row,
                derivation=derivation,
                limits=limits,
            )
            break
        if len(results) >= limits.max_candidates_per_field:
            break
    if results:
        return tuple(results)
    for row in evidence:
        if not any(term.casefold() in row.text.casefold() for term in fallback_terms):
            continue
        _append_candidate(
            results,
            seen,
            field_name=field_name,
            text=_strip_known_label_prefix(row.text),
            source=row,
            derivation="bounded_keyword_heuristic",
            limits=limits,
        )
        if len(results) >= limits.max_candidates_per_field:
            break
    return tuple(results)


def _next_same_row_evidence(
    source: CellTextEvidence,
    evidence: Sequence[CellTextEvidence],
) -> Optional[CellTextEvidence]:
    candidates = [
        row
        for row in evidence
        if row.row == source.row
        and row.column > source.column
        and row.text
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda row: (row.column - source.column, row.column),
    )


def _append_candidate(
    target: List[WeakTextCandidate],
    seen: set,
    *,
    field_name: str,
    text: str,
    source: CellTextEvidence,
    derivation: str,
    limits: HwpxWeakCaseLimits,
) -> None:
    normalized = " ".join(text.split())[: limits.max_candidate_chars]
    if not normalized:
        return
    key = (normalized, source.cell_target_id)
    if key in seen:
        return
    seen.add(key)
    target.append(
        WeakTextCandidate(
            field=field_name,
            text=normalized,
            source_cell_target_id=source.cell_target_id,
            derivation=derivation,
        )
    )


def _strip_known_label_prefix(value: str) -> str:
    labels = (
        *_ROUTE_LABELS,
        *_LOCATION_LABELS,
        *_EVALUATION_LABELS,
        *_OPINION_LABELS,
    )
    result = value
    for label in labels:
        if result.startswith(label):
            result = result[len(label) :].lstrip(" :：-")
            break
    return " ".join(result.split())


def _derive_quarter(
    document_name: str,
    metadata: Sequence[str],
) -> Tuple[Optional[int], Optional[str]]:
    filename_match = _QUARTER_PATTERN.search(
        unicodedata.normalize("NFC", document_name)
    )
    if filename_match:
        return int(filename_match.group(1)), "filename"
    candidates = {
        int(match.group(1))
        for value in metadata
        for match in _QUARTER_PATTERN.finditer(value)
    }
    if len(candidates) == 1:
        return next(iter(candidates)), "package_metadata"
    return None, None


def _case_id(
    document_sha256: str,
    source: WeakCaseSourceReference,
    images: Sequence[EmbeddedImageObservation],
    unresolved_refs: Sequence[str],
) -> str:
    payload = {
        "document_sha256": document_sha256,
        "picture_target_id": source.picture_target_id,
        "image_sha256s": [image.image_sha256 for image in images],
        "unresolved_refs": list(unresolved_refs),
    }
    return f"hwpx-weak-case-{_canonical_sha256(payload)[:32]}"


def _index_manifest_sha256(
    *,
    document_name: str,
    document_sha256: str,
    target_summary_sha256: str,
    quarter: Optional[int],
    quarter_source: Optional[str],
    metadata_excerpt: Sequence[str],
    cases: Sequence[HwpxWeakCase],
) -> str:
    payload = {
        "schema_version": HWPX_WEAK_CASE_SCHEMA,
        "extractor_version": _EXTRACTOR_VERSION,
        "document_name": document_name,
        "document_sha256": document_sha256,
        "structure_target_summary_sha256": target_summary_sha256,
        "quarter": quarter,
        "quarter_source": quarter_source,
        "metadata_excerpt": list(metadata_excerpt),
        "cases": [asdict(case) for case in cases],
    }
    return _canonical_sha256(payload)


def _identity_fingerprint(
    before: FileIdentity,
    after: FileIdentity,
) -> str:
    return _canonical_sha256(
        {
            "before": asdict(before),
            "after": asdict(after),
        }
    )


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _xml_attribute(
    element: ElementTree.Element,
    local_name: str,
) -> Optional[str]:
    target = local_name.casefold()
    matches = [
        value
        for key, value in element.attrib.items()
        if structure._local_name(key).casefold() == target  # type: ignore[attr-defined]
    ]
    if len(matches) > 1:
        raise HwpxWeakCaseSecurityError(
            "HWPX XML contains duplicate local attribute names"
        )
    return matches[0] if matches else None


def _bounded_normalized_text(
    value: str,
    *,
    maximum: int,
    label: str,
) -> str:
    normalized = unicodedata.normalize("NFC", " ".join(value.split()))
    if not normalized:
        raise HwpxWeakCaseFormatError(f"HWPX {label} is empty")
    if len(normalized) > maximum:
        raise HwpxLimitExceeded(f"HWPX {label} exceeds the limit")
    if _CONTROL_PATTERN.search(normalized):
        raise HwpxWeakCaseSecurityError(
            f"HWPX {label} contains control characters"
        )
    return normalized


def _unique_in_order(values: Iterable[str]) -> Tuple[str, ...]:
    seen = set()
    result: List[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)


def _archive_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


__all__ = [
    "CellTextEvidence",
    "EmbeddedImageObservation",
    "HWPX_WEAK_CASE_PURPOSE",
    "HWPX_WEAK_CASE_SCHEMA",
    "HwpxWeakCase",
    "HwpxWeakCaseAuthorization",
    "HwpxWeakCaseAuthorizationError",
    "HwpxWeakCaseError",
    "HwpxWeakCaseFormatError",
    "HwpxWeakCaseIndex",
    "HwpxWeakCaseLimits",
    "HwpxWeakCaseSecurityError",
    "WeakCaseExtractionProvenance",
    "WeakCaseSafety",
    "WeakCaseSourceReference",
    "WeakTextCandidate",
    "inspect_hwpx_weak_cases",
]
