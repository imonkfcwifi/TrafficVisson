"""Read-only, bounded HWPX/HWPML structure snapshots.

This module is deliberately independent from the HWP COM writer and the HTTP
bridge.  It opens one already-authorized local ``.hwpx`` file read-only and
derives deterministic, exact-document-bound addresses for table cells and
pictures.  It never extracts archive members, creates files, invokes COM,
edits a document, or transfers document content.

The parser treats an HWPX document as untrusted input:

* only an absolute, regular, non-link ``.hwpx`` path is accepted;
* the source identity and SHA-256 are verified before and after inspection;
* unsafe, duplicate, encrypted, linked, oversized, or traversal ZIP entries
  are rejected before member data is read;
* XML and section counts, byte sizes, compression ratios, tree depth, element
  counts, attributes, text, and derived target counts are bounded;
* DTD and entity declarations are prohibited;
* only in-memory parsing is used -- archive members are never extracted.

The resulting target IDs are suitable for a later preview/addressing layer.
They are not write authorizations.  Every actionable target carries the same
``target_summary_sha256`` so a later approval token can bind the complete
snapshot rather than a loose table index.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import unicodedata
import zipfile
from dataclasses import asdict, dataclass, replace
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


HWPX_STRUCTURE_SCHEMA = "trafficvisson.hwpx-structure.v1"
HWPX_MIMETYPE = b"application/hwp+zip"
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
TARGET_ID_PATTERN = re.compile(r"^hwpx-(?:cell|picture)-[a-f0-9]{24}$")
_SECTION_PATH_PATTERN = re.compile(
    r"^Contents/section(?P<index>[0-9]{1,5})\.xml$",
    re.IGNORECASE,
)
_INTEGER_PATTERN = re.compile(r"^(?:0|[1-9][0-9]{0,9})$")
_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TRUE_XML_VALUES = frozenset({"1", "true", "yes"})
_ALLOWED_COMPRESSION = frozenset(
    {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
)
_READ_CHUNK_BYTES = 4 * 1024 * 1024
_WINDOWS_REPARSE_ATTRIBUTE = 0x400


class HwpxStructureError(RuntimeError):
    """Base class for safe HWPX structure inspection failures."""


class HwpxSecurityError(HwpxStructureError):
    """Raised when a path, archive, or XML security rule is violated."""


class HwpxFormatError(HwpxStructureError):
    """Raised when the input is not a structurally readable HWPX package."""


class HwpxLimitExceeded(HwpxStructureError):
    """Raised when an input or derived structure exceeds a hard bound."""


class HwpxSourceChanged(HwpxStructureError):
    """Raised when source identity or bytes change during inspection."""


@dataclass(frozen=True)
class HwpxStructureLimits:
    """Resource limits for one read-only structure snapshot.

    Defaults accommodate the two large, real TrafficSuperVision reference
    documents while keeping XML and derived structures substantially tighter
    than the binary archive payload.
    """

    max_archive_bytes: int = 2 * 1024**3
    max_archive_entries: int = 4_096
    max_archive_entry_bytes: int = 256 * 1024**2
    max_total_uncompressed_bytes: int = 4 * 1024**3
    max_compression_ratio: float = 200.0
    max_entry_name_chars: int = 1_024
    max_xml_entries: int = 256
    max_xml_entry_bytes: int = 32 * 1024**2
    max_total_xml_bytes: int = 128 * 1024**2
    max_sections: int = 128
    max_elements_per_xml: int = 500_000
    max_total_xml_elements: int = 1_000_000
    max_xml_depth: int = 96
    max_attributes_per_element: int = 64
    max_attribute_chars: int = 32_768
    max_text_chars_per_element: int = 256_000
    max_tables: int = 10_000
    max_cells: int = 100_000
    max_pictures: int = 50_000
    max_page_breaks: int = 100_000
    max_named_fields: int = 10_000
    max_named_bookmarks: int = 10_000
    max_table_rows: int = 10_000
    max_table_columns: int = 2_000
    max_cell_span: int = 10_000
    max_cell_text_chars: int = 65_536
    text_excerpt_chars: int = 320
    max_name_chars: int = 256
    max_picture_refs: int = 32

    def __post_init__(self) -> None:
        integer_bounds = (
            ("max_archive_bytes", self.max_archive_bytes, 1, 8 * 1024**3),
            ("max_archive_entries", self.max_archive_entries, 1, 100_000),
            (
                "max_archive_entry_bytes",
                self.max_archive_entry_bytes,
                1,
                2 * 1024**3,
            ),
            (
                "max_total_uncompressed_bytes",
                self.max_total_uncompressed_bytes,
                1,
                16 * 1024**3,
            ),
            ("max_entry_name_chars", self.max_entry_name_chars, 8, 8_192),
            ("max_xml_entries", self.max_xml_entries, 1, 4_096),
            (
                "max_xml_entry_bytes",
                self.max_xml_entry_bytes,
                128,
                256 * 1024**2,
            ),
            (
                "max_total_xml_bytes",
                self.max_total_xml_bytes,
                128,
                1024 * 1024**2,
            ),
            ("max_sections", self.max_sections, 1, 4_096),
            (
                "max_elements_per_xml",
                self.max_elements_per_xml,
                1,
                5_000_000,
            ),
            (
                "max_total_xml_elements",
                self.max_total_xml_elements,
                1,
                20_000_000,
            ),
            ("max_xml_depth", self.max_xml_depth, 2, 1_024),
            (
                "max_attributes_per_element",
                self.max_attributes_per_element,
                1,
                1_024,
            ),
            (
                "max_attribute_chars",
                self.max_attribute_chars,
                1,
                4 * 1024**2,
            ),
            (
                "max_text_chars_per_element",
                self.max_text_chars_per_element,
                1,
                16 * 1024**2,
            ),
            ("max_tables", self.max_tables, 1, 100_000),
            ("max_cells", self.max_cells, 1, 1_000_000),
            ("max_pictures", self.max_pictures, 1, 1_000_000),
            ("max_page_breaks", self.max_page_breaks, 1, 1_000_000),
            ("max_named_fields", self.max_named_fields, 1, 1_000_000),
            (
                "max_named_bookmarks",
                self.max_named_bookmarks,
                1,
                1_000_000,
            ),
            ("max_table_rows", self.max_table_rows, 1, 1_000_000),
            ("max_table_columns", self.max_table_columns, 1, 100_000),
            ("max_cell_span", self.max_cell_span, 1, 1_000_000),
            (
                "max_cell_text_chars",
                self.max_cell_text_chars,
                1,
                4 * 1024**2,
            ),
            ("text_excerpt_chars", self.text_excerpt_chars, 1, 16_384),
            ("max_name_chars", self.max_name_chars, 1, 4_096),
            ("max_picture_refs", self.max_picture_refs, 1, 4_096),
        )
        for name, value, minimum, maximum in integer_bounds:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{name} is outside the supported range")
        if (
            isinstance(self.max_compression_ratio, bool)
            or not isinstance(self.max_compression_ratio, (int, float))
            or not 1.0 <= float(self.max_compression_ratio) <= 10_000.0
        ):
            raise ValueError(
                "max_compression_ratio is outside the supported range"
            )
        if self.max_xml_entry_bytes > self.max_total_xml_bytes:
            raise ValueError(
                "max_total_xml_bytes must accommodate one XML entry"
            )
        if self.text_excerpt_chars > self.max_cell_text_chars:
            raise ValueError(
                "text_excerpt_chars cannot exceed max_cell_text_chars"
            )


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    mode: int
    size_bytes: int
    modified_time_ns: int
    changed_time_ns: int


@dataclass(frozen=True)
class HwpxReadOnlySafety:
    read_only: bool = True
    no_edit: bool = True
    no_external_transfer: bool = True
    no_extraction: bool = True
    no_com: bool = True
    no_writes: bool = True
    source_identity_verified_before_and_after: bool = True
    source_sha256_verified_before_and_after: bool = True
    automatic_document_edit: bool = False


@dataclass(frozen=True)
class CellSnapshot:
    target_id: str
    target_summary_sha256: str
    document_sha256: str
    section_path: str
    section_index: int
    table_id: str
    table_index: int
    table_xml_id: Optional[str]
    cell_index: int
    row: int
    column: int
    row_span: int
    column_span: int
    address_source: str
    text_excerpt: str
    text_sha256: str
    text_truncated: bool


@dataclass(frozen=True)
class TableSnapshot:
    table_id: str
    document_sha256: str
    section_path: str
    section_index: int
    table_index: int
    xml_id: Optional[str]
    rows: int
    columns: int
    declared_rows: Optional[int]
    declared_columns: Optional[int]
    cell_count: int
    cells: Tuple[CellSnapshot, ...]


@dataclass(frozen=True)
class PictureSnapshot:
    target_id: str
    target_summary_sha256: str
    document_sha256: str
    section_path: str
    section_index: int
    picture_index: int
    xml_id: Optional[str]
    binary_item_refs: Tuple[str, ...]
    nearest_table_id: Optional[str]
    nearest_table_index: Optional[int]
    nearest_cell_target_id: Optional[str]
    nearest_cell_row: Optional[int]
    nearest_cell_column: Optional[int]


@dataclass(frozen=True)
class PageBreakSnapshot:
    page_break_id: str
    document_sha256: str
    section_path: str
    section_index: int
    page_break_index: int
    source: str
    xml_id: Optional[str]
    nearest_table_id: Optional[str]
    nearest_cell_target_id: Optional[str]


@dataclass(frozen=True)
class SectionSnapshot:
    path: str
    section_index: int
    xml_size_bytes: int
    xml_sha256: str
    element_count: int
    table_count: int
    cell_count: int
    picture_count: int
    explicit_page_break_count: int
    named_field_count: int
    named_bookmark_count: int


@dataclass(frozen=True)
class HwpxStructureSnapshot:
    schema_version: str
    document_name: str
    document_sha256: str
    sha256_before: str
    sha256_after: str
    identity_before: FileIdentity
    identity_after: FileIdentity
    archive_entry_count: int
    xml_entry_count: int
    section_count: int
    sections: Tuple[SectionSnapshot, ...]
    tables: Tuple[TableSnapshot, ...]
    pictures: Tuple[PictureSnapshot, ...]
    page_breaks: Tuple[PageBreakSnapshot, ...]
    field_names: Tuple[str, ...]
    field_count: int
    bookmark_names: Tuple[str, ...]
    bookmark_count: int
    target_count: int
    target_summary_sha256: str
    safety: HwpxReadOnlySafety

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _ParsedXml:
    raw: bytes
    root: ElementTree.Element
    element_count: int


@dataclass(frozen=True)
class _ArchiveSnapshot:
    archive_entry_count: int
    xml_entry_count: int
    sections: Tuple[SectionSnapshot, ...]
    tables: Tuple[TableSnapshot, ...]
    pictures: Tuple[PictureSnapshot, ...]
    page_breaks: Tuple[PageBreakSnapshot, ...]
    field_names: Tuple[str, ...]
    field_count: int
    bookmark_names: Tuple[str, ...]
    bookmark_count: int
    target_summary_sha256: str


def inspect_hwpx_structure(
    source_path: Union[str, os.PathLike],
    *,
    limits: Optional[HwpxStructureLimits] = None,
) -> HwpxStructureSnapshot:
    """Inspect one exact HWPX file without modifying or extracting it."""

    active_limits = limits or HwpxStructureLimits()
    path, identity_before = _validate_source_path(
        source_path,
        active_limits,
    )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError as error:
        raise HwpxSecurityError(
            "HWPX source could not be opened read-only"
        ) from error

    try:
        with os.fdopen(
            descriptor,
            "rb",
            buffering=0,
            closefd=True,
        ) as source:
            opened_identity = _file_identity(os.fstat(source.fileno()))
            if opened_identity != identity_before:
                raise HwpxSourceChanged(
                    "HWPX source identity changed while it was opened"
                )
            sha256_before = _hash_open_file(
                source,
                expected_size=identity_before.size_bytes,
            )
            archive = _read_archive(
                source,
                document_sha256=sha256_before,
                limits=active_limits,
            )
            sha256_after = _hash_open_file(
                source,
                expected_size=identity_before.size_bytes,
            )
            descriptor_identity_after = _file_identity(
                os.fstat(source.fileno())
            )
    except HwpxStructureError:
        raise
    except OSError as error:
        raise HwpxFormatError("HWPX source could not be read") from error

    _assert_no_link_or_reparse_components(path)
    try:
        identity_after = _file_identity(os.lstat(str(path)))
    except OSError as error:
        raise HwpxSourceChanged(
            "HWPX source disappeared after inspection"
        ) from error
    if sha256_before != sha256_after:
        raise HwpxSourceChanged(
            "HWPX source bytes changed during inspection"
        )
    if (
        identity_before != descriptor_identity_after
        or identity_before != identity_after
    ):
        raise HwpxSourceChanged(
            "HWPX source identity changed during inspection"
        )

    return HwpxStructureSnapshot(
        schema_version=HWPX_STRUCTURE_SCHEMA,
        document_name=path.name,
        document_sha256=sha256_before,
        sha256_before=sha256_before,
        sha256_after=sha256_after,
        identity_before=identity_before,
        identity_after=identity_after,
        archive_entry_count=archive.archive_entry_count,
        xml_entry_count=archive.xml_entry_count,
        section_count=len(archive.sections),
        sections=archive.sections,
        tables=archive.tables,
        pictures=archive.pictures,
        page_breaks=archive.page_breaks,
        field_names=archive.field_names,
        field_count=archive.field_count,
        bookmark_names=archive.bookmark_names,
        bookmark_count=archive.bookmark_count,
        target_count=sum(
            table.cell_count for table in archive.tables
        )
        + len(archive.pictures),
        target_summary_sha256=archive.target_summary_sha256,
        safety=HwpxReadOnlySafety(),
    )


def _validate_source_path(
    source_path: Union[str, os.PathLike],
    limits: HwpxStructureLimits,
) -> Tuple[Path, FileIdentity]:
    try:
        path = Path(source_path)
    except (TypeError, ValueError) as error:
        raise HwpxSecurityError("HWPX source path is invalid") from error
    if not path.is_absolute():
        raise HwpxSecurityError("HWPX source path must be absolute")
    if any(part == ".." for part in path.parts):
        raise HwpxSecurityError(
            "HWPX source path cannot contain traversal components"
        )
    if path.suffix.casefold() != ".hwpx":
        raise HwpxFormatError("HWPX source must use the .hwpx extension")
    _assert_no_link_or_reparse_components(path)
    try:
        source_stat = os.lstat(str(path))
    except OSError as error:
        raise HwpxFormatError("HWPX source does not exist") from error
    if not stat.S_ISREG(source_stat.st_mode):
        raise HwpxSecurityError("HWPX source must be a regular file")
    identity = _file_identity(source_stat)
    if identity.size_bytes <= 0:
        raise HwpxFormatError("HWPX source is empty")
    if identity.size_bytes > limits.max_archive_bytes:
        raise HwpxLimitExceeded("HWPX archive exceeds the byte limit")
    return path, identity


def _assert_no_link_or_reparse_components(path: Path) -> None:
    components = [path] + list(path.parents)
    for component in reversed(components):
        if str(component) == component.anchor:
            continue
        try:
            component_stat = os.lstat(str(component))
        except OSError as error:
            raise HwpxSecurityError(
                "HWPX source path contains an inaccessible component"
            ) from error
        file_attributes = int(
            getattr(component_stat, "st_file_attributes", 0)
        )
        if stat.S_ISLNK(component_stat.st_mode) or (
            file_attributes & _WINDOWS_REPARSE_ATTRIBUTE
        ):
            raise HwpxSecurityError(
                "HWPX source path cannot contain links or reparse points"
            )


def _file_identity(source_stat: os.stat_result) -> FileIdentity:
    return FileIdentity(
        device=int(source_stat.st_dev),
        inode=int(source_stat.st_ino),
        mode=int(source_stat.st_mode),
        size_bytes=int(source_stat.st_size),
        modified_time_ns=int(source_stat.st_mtime_ns),
        changed_time_ns=int(source_stat.st_ctime_ns),
    )


def _hash_open_file(source: BinaryIO, *, expected_size: int) -> str:
    source.seek(0)
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = source.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > expected_size:
            raise HwpxSourceChanged(
                "HWPX source grew while it was hashed"
            )
        digest.update(chunk)
    if total != expected_size:
        raise HwpxSourceChanged(
            "HWPX source size changed while it was hashed"
        )
    return digest.hexdigest()


def _read_archive(
    source: BinaryIO,
    *,
    document_sha256: str,
    limits: HwpxStructureLimits,
) -> _ArchiveSnapshot:
    source.seek(0)
    try:
        with zipfile.ZipFile(source, mode="r") as archive:
            infos = archive.infolist()
            normalized_infos = _validate_archive_entries(infos, limits)
            mimetype_info = normalized_infos.get("mimetype")
            if mimetype_info is None or mimetype_info.filename != "mimetype":
                raise HwpxFormatError(
                    "HWPX archive is missing its exact mimetype entry"
                )
            mimetype = _read_member_bounded(
                archive,
                mimetype_info,
                maximum=128,
            )
            if mimetype != HWPX_MIMETYPE:
                raise HwpxFormatError("HWPX mimetype is invalid")

            xml_infos = [
                info
                for info in infos
                if not info.is_dir()
                and info.filename.casefold().endswith(".xml")
            ]
            if len(xml_infos) > limits.max_xml_entries:
                raise HwpxLimitExceeded(
                    "HWPX XML entry count exceeds the limit"
                )
            total_xml_size = sum(info.file_size for info in xml_infos)
            if total_xml_size > limits.max_total_xml_bytes:
                raise HwpxLimitExceeded(
                    "HWPX total XML bytes exceed the limit"
                )

            parsed_xml: Dict[str, _ParsedXml] = {}
            total_elements = 0
            for info in xml_infos:
                if info.file_size > limits.max_xml_entry_bytes:
                    raise HwpxLimitExceeded(
                        "HWPX XML entry exceeds the byte limit"
                    )
                raw = _read_member_bounded(
                    archive,
                    info,
                    maximum=limits.max_xml_entry_bytes,
                )
                _reject_dtd_or_entities(raw)
                root, element_count = _parse_xml_bounded(
                    raw,
                    entry_name=info.filename,
                    limits=limits,
                )
                total_elements += element_count
                if total_elements > limits.max_total_xml_elements:
                    raise HwpxLimitExceeded(
                        "HWPX total XML element count exceeds the limit"
                    )
                parsed_xml[info.filename] = _ParsedXml(
                    raw=raw,
                    root=root,
                    element_count=element_count,
                )

            section_rows: List[Tuple[int, str, _ParsedXml]] = []
            section_indexes = set()
            for name, parsed in parsed_xml.items():
                match = _SECTION_PATH_PATTERN.fullmatch(name)
                if match is None:
                    continue
                section_index = int(match.group("index"))
                if section_index in section_indexes:
                    raise HwpxSecurityError(
                        "HWPX contains duplicate section indexes"
                    )
                section_indexes.add(section_index)
                section_rows.append((section_index, name, parsed))
            if not section_rows:
                raise HwpxFormatError(
                    "HWPX archive contains no section XML"
                )
            if len(section_rows) > limits.max_sections:
                raise HwpxLimitExceeded(
                    "HWPX section count exceeds the limit"
                )
            section_rows.sort(key=lambda row: (row[0], row[1]))

            return _snapshot_sections(
                section_rows,
                document_sha256=document_sha256,
                archive_entry_count=len(infos),
                xml_entry_count=len(xml_infos),
                limits=limits,
            )
    except HwpxStructureError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise HwpxFormatError("HWPX ZIP package is invalid") from error
    except (RuntimeError, NotImplementedError, OSError) as error:
        raise HwpxFormatError(
            "HWPX ZIP package could not be read safely"
        ) from error


def _validate_archive_entries(
    infos: Sequence[zipfile.ZipInfo],
    limits: HwpxStructureLimits,
) -> Dict[str, zipfile.ZipInfo]:
    if not infos:
        raise HwpxFormatError("HWPX archive has no entries")
    if len(infos) > limits.max_archive_entries:
        raise HwpxLimitExceeded(
            "HWPX archive entry count exceeds the limit"
        )
    normalized: Dict[str, zipfile.ZipInfo] = {}
    total_uncompressed = 0
    offsets = set()
    for info in infos:
        original_name = getattr(info, "orig_filename", info.filename)
        if "\x00" in original_name:
            raise HwpxSecurityError(
                "HWPX ZIP entry name contains a NUL byte"
            )
        if original_name != info.filename:
            raise HwpxSecurityError(
                "HWPX ZIP entry path was normalized unsafely"
            )
        canonical_name = _validate_archive_member_name(
            info.filename,
            limits.max_entry_name_chars,
        )
        duplicate_key = unicodedata.normalize(
            "NFC",
            canonical_name,
        ).casefold()
        if duplicate_key in normalized:
            raise HwpxSecurityError(
                "HWPX ZIP contains duplicate entry names"
            )
        normalized[duplicate_key] = info

        if info.flag_bits & 0x1:
            raise HwpxSecurityError(
                "Encrypted HWPX ZIP entries are not supported"
            )
        if info.compress_type not in _ALLOWED_COMPRESSION:
            raise HwpxSecurityError(
                "HWPX ZIP entry uses an unsupported compression method"
            )
        if info.file_size < 0 or info.compress_size < 0:
            raise HwpxFormatError("HWPX ZIP entry size is invalid")
        if info.file_size > limits.max_archive_entry_bytes:
            raise HwpxLimitExceeded(
                "HWPX ZIP entry exceeds the byte limit"
            )
        total_uncompressed += info.file_size
        if total_uncompressed > limits.max_total_uncompressed_bytes:
            raise HwpxLimitExceeded(
                "HWPX uncompressed bytes exceed the limit"
            )
        if info.file_size:
            if info.compress_size <= 0:
                raise HwpxSecurityError(
                    "HWPX ZIP entry has an invalid compressed size"
                )
            ratio = info.file_size / float(info.compress_size)
            if ratio > limits.max_compression_ratio:
                raise HwpxLimitExceeded(
                    "HWPX ZIP entry compression ratio exceeds the limit"
                )
        _reject_link_or_special_archive_entry(info)
        if info.header_offset < 0 or info.header_offset in offsets:
            raise HwpxSecurityError(
                "HWPX ZIP entry offsets are invalid or overlapping"
            )
        offsets.add(info.header_offset)
    return normalized


def _validate_archive_member_name(name: str, maximum: int) -> str:
    if not isinstance(name, str) or not name:
        raise HwpxSecurityError("HWPX ZIP entry name is empty")
    if len(name) > maximum:
        raise HwpxLimitExceeded(
            "HWPX ZIP entry name exceeds the character limit"
        )
    if _CONTROL_PATTERN.search(name):
        raise HwpxSecurityError(
            "HWPX ZIP entry name contains control characters"
        )
    if name.startswith(("/", "\\")) or "\\" in name:
        raise HwpxSecurityError(
            "HWPX ZIP entry path must be a safe relative POSIX path"
        )
    trimmed = name[:-1] if name.endswith("/") else name
    if not trimmed:
        raise HwpxSecurityError("HWPX ZIP entry path is invalid")
    components = trimmed.split("/")
    if any(
        not component
        or component in {".", ".."}
        or ":" in component
        for component in components
    ):
        raise HwpxSecurityError(
            "HWPX ZIP entry path contains traversal or drive syntax"
        )
    return name


def _reject_link_or_special_archive_entry(info: zipfile.ZipInfo) -> None:
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    if unix_mode:
        kind = stat.S_IFMT(unix_mode)
        if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise HwpxSecurityError(
                "HWPX ZIP cannot contain links or special entries"
            )
    dos_attributes = info.external_attr & 0xFFFF
    if dos_attributes & _WINDOWS_REPARSE_ATTRIBUTE:
        raise HwpxSecurityError(
            "HWPX ZIP cannot contain reparse-point entries"
        )


def _read_member_bounded(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    maximum: int,
) -> bytes:
    if info.file_size > maximum:
        raise HwpxLimitExceeded("HWPX ZIP member exceeds the read limit")
    chunks: List[bytes] = []
    total = 0
    try:
        with archive.open(info, mode="r") as member:
            while True:
                chunk = member.read(min(_READ_CHUNK_BYTES, maximum + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise HwpxLimitExceeded(
                        "HWPX ZIP member exceeded the read limit"
                    )
                chunks.append(chunk)
    except HwpxStructureError:
        raise
    except (zipfile.BadZipFile, RuntimeError, OSError) as error:
        raise HwpxFormatError(
            "HWPX ZIP member could not be read safely"
        ) from error
    payload = b"".join(chunks)
    if len(payload) != info.file_size:
        raise HwpxFormatError(
            "HWPX ZIP member size does not match its directory record"
        )
    return payload


def _reject_dtd_or_entities(raw: bytes) -> None:
    upper = raw.upper()
    collapsed_utf16 = upper.replace(b"\x00", b"")
    if b"<!DOCTYPE" in upper or b"<!DOCTYPE" in collapsed_utf16:
        raise HwpxSecurityError("HWPX XML cannot contain a DOCTYPE")
    if b"<!ENTITY" in upper or b"<!ENTITY" in collapsed_utf16:
        raise HwpxSecurityError(
            "HWPX XML cannot contain entity declarations"
        )


def _parse_xml_bounded(
    raw: bytes,
    *,
    entry_name: str,
    limits: HwpxStructureLimits,
) -> Tuple[ElementTree.Element, int]:
    depth = 0
    count = 0
    try:
        iterator = ElementTree.iterparse(
            io.BytesIO(raw),
            events=("start", "end"),
        )
        for event, element in iterator:
            if event == "start":
                depth += 1
                count += 1
                if depth > limits.max_xml_depth:
                    raise HwpxLimitExceeded(
                        "HWPX XML nesting depth exceeds the limit"
                    )
                if count > limits.max_elements_per_xml:
                    raise HwpxLimitExceeded(
                        "HWPX XML element count exceeds the limit"
                    )
                if not isinstance(element.tag, str) or len(
                    element.tag
                ) > limits.max_name_chars * 4:
                    raise HwpxLimitExceeded(
                        "HWPX XML element name exceeds the limit"
                    )
                if len(element.attrib) > limits.max_attributes_per_element:
                    raise HwpxLimitExceeded(
                        "HWPX XML attribute count exceeds the limit"
                    )
                attribute_chars = 0
                for name, value in element.attrib.items():
                    attribute_chars += len(str(name)) + len(str(value))
                    if attribute_chars > limits.max_attribute_chars:
                        raise HwpxLimitExceeded(
                            "HWPX XML attributes exceed the character limit"
                        )
                    if _CONTROL_PATTERN.search(str(value)):
                        raise HwpxSecurityError(
                            "HWPX XML attribute contains control characters"
                        )
            else:
                for text in (element.text, element.tail):
                    if text is not None and len(
                        text
                    ) > limits.max_text_chars_per_element:
                        raise HwpxLimitExceeded(
                            "HWPX XML text node exceeds the character limit"
                        )
                depth -= 1
        root = iterator.root
    except HwpxStructureError:
        raise
    except ElementTree.ParseError as error:
        raise HwpxFormatError(
            f"HWPX XML entry is malformed: {entry_name}"
        ) from error
    if depth != 0 or count <= 0:
        raise HwpxFormatError(
            f"HWPX XML entry has an invalid tree: {entry_name}"
        )
    return root, count


def _snapshot_sections(
    section_rows: Sequence[Tuple[int, str, _ParsedXml]],
    *,
    document_sha256: str,
    archive_entry_count: int,
    xml_entry_count: int,
    limits: HwpxStructureLimits,
) -> _ArchiveSnapshot:
    all_sections: List[SectionSnapshot] = []
    all_tables: List[TableSnapshot] = []
    all_pictures: List[PictureSnapshot] = []
    all_breaks: List[PageBreakSnapshot] = []
    all_field_names: List[str] = []
    all_bookmark_names: List[str] = []
    field_count = 0
    bookmark_count = 0

    for section_index, path, parsed in section_rows:
        (
            tables,
            pictures,
            page_breaks,
            field_names,
            section_field_count,
            bookmark_names,
            section_bookmark_count,
        ) = _snapshot_one_section(
            parsed.root,
            section_path=path,
            section_index=section_index,
            document_sha256=document_sha256,
            limits=limits,
        )
        all_tables.extend(tables)
        all_pictures.extend(pictures)
        all_breaks.extend(page_breaks)
        all_field_names.extend(field_names)
        all_bookmark_names.extend(bookmark_names)
        field_count += section_field_count
        bookmark_count += section_bookmark_count
        all_sections.append(
            SectionSnapshot(
                path=path,
                section_index=section_index,
                xml_size_bytes=len(parsed.raw),
                xml_sha256=hashlib.sha256(parsed.raw).hexdigest(),
                element_count=parsed.element_count,
                table_count=len(tables),
                cell_count=sum(table.cell_count for table in tables),
                picture_count=len(pictures),
                explicit_page_break_count=len(page_breaks),
                named_field_count=section_field_count,
                named_bookmark_count=section_bookmark_count,
            )
        )
        if len(all_tables) > limits.max_tables:
            raise HwpxLimitExceeded("HWPX table count exceeds the limit")
        if sum(table.cell_count for table in all_tables) > limits.max_cells:
            raise HwpxLimitExceeded("HWPX cell count exceeds the limit")
        if len(all_pictures) > limits.max_pictures:
            raise HwpxLimitExceeded("HWPX picture count exceeds the limit")
        if len(all_breaks) > limits.max_page_breaks:
            raise HwpxLimitExceeded(
                "HWPX explicit page-break count exceeds the limit"
            )
        if field_count > limits.max_named_fields:
            raise HwpxLimitExceeded(
                "HWPX named-field count exceeds the limit"
            )
        if bookmark_count > limits.max_named_bookmarks:
            raise HwpxLimitExceeded(
                "HWPX named-bookmark count exceeds the limit"
            )

    target_summary = _target_summary_sha256(
        document_sha256,
        all_tables,
        all_pictures,
    )
    tables_with_summary = tuple(
        replace(
            table,
            cells=tuple(
                replace(cell, target_summary_sha256=target_summary)
                for cell in table.cells
            ),
        )
        for table in all_tables
    )
    pictures_with_summary = tuple(
        replace(picture, target_summary_sha256=target_summary)
        for picture in all_pictures
    )
    return _ArchiveSnapshot(
        archive_entry_count=archive_entry_count,
        xml_entry_count=xml_entry_count,
        sections=tuple(all_sections),
        tables=tables_with_summary,
        pictures=pictures_with_summary,
        page_breaks=tuple(all_breaks),
        field_names=_unique_in_order(all_field_names),
        field_count=field_count,
        bookmark_names=_unique_in_order(all_bookmark_names),
        bookmark_count=bookmark_count,
        target_summary_sha256=target_summary,
    )


def _snapshot_one_section(
    root: ElementTree.Element,
    *,
    section_path: str,
    section_index: int,
    document_sha256: str,
    limits: HwpxStructureLimits,
) -> Tuple[
    List[TableSnapshot],
    List[PictureSnapshot],
    List[PageBreakSnapshot],
    List[str],
    int,
    List[str],
    int,
]:
    parent_map = {
        child: parent
        for parent in root.iter()
        for child in list(parent)
    }
    table_elements = [
        element
        for element in root.iter()
        if _local_name(element.tag).casefold() == "tbl"
    ]
    if len(table_elements) > limits.max_tables:
        raise HwpxLimitExceeded("HWPX table count exceeds the limit")

    tables: List[TableSnapshot] = []
    table_by_element: Dict[ElementTree.Element, TableSnapshot] = {}
    cell_by_element: Dict[ElementTree.Element, CellSnapshot] = {}
    cell_total = 0
    for table_index, table_element in enumerate(table_elements):
        table = _snapshot_table(
            table_element,
            section_path=section_path,
            section_index=section_index,
            table_index=table_index,
            document_sha256=document_sha256,
            limits=limits,
        )
        cell_total += table.cell_count
        if cell_total > limits.max_cells:
            raise HwpxLimitExceeded("HWPX cell count exceeds the limit")
        tables.append(table)
        table_by_element[table_element] = table
        direct_cells = _direct_table_cells(table_element)
        if len(direct_cells) != len(table.cells):
            raise HwpxFormatError(
                "HWPX table cell traversal is inconsistent"
            )
        for element, cell in zip(direct_cells, table.cells):
            cell_by_element[element] = cell

    pictures: List[PictureSnapshot] = []
    for element in root.iter():
        if _local_name(element.tag).casefold() != "pic":
            continue
        if len(pictures) >= limits.max_pictures:
            raise HwpxLimitExceeded("HWPX picture count exceeds the limit")
        nearest_table, nearest_cell = _nearest_context(
            element,
            parent_map=parent_map,
            table_by_element=table_by_element,
            cell_by_element=cell_by_element,
        )
        refs = _picture_refs(element, limits)
        xml_id = _optional_xml_token(
            _attribute(element, "id"),
            limits=limits,
        )
        picture_index = len(pictures)
        target_id = _target_id(
            "picture",
            document_sha256,
            section_path,
            str(section_index),
            str(picture_index),
            xml_id or "",
            ",".join(refs),
            nearest_table.table_id if nearest_table else "",
            nearest_cell.target_id if nearest_cell else "",
        )
        pictures.append(
            PictureSnapshot(
                target_id=target_id,
                target_summary_sha256="",
                document_sha256=document_sha256,
                section_path=section_path,
                section_index=section_index,
                picture_index=picture_index,
                xml_id=xml_id,
                binary_item_refs=refs,
                nearest_table_id=(
                    nearest_table.table_id if nearest_table else None
                ),
                nearest_table_index=(
                    nearest_table.table_index if nearest_table else None
                ),
                nearest_cell_target_id=(
                    nearest_cell.target_id if nearest_cell else None
                ),
                nearest_cell_row=(
                    nearest_cell.row if nearest_cell else None
                ),
                nearest_cell_column=(
                    nearest_cell.column if nearest_cell else None
                ),
            )
        )

    page_breaks: List[PageBreakSnapshot] = []
    for element in root.iter():
        source = _explicit_page_break_source(element)
        if source is None:
            continue
        if len(page_breaks) >= limits.max_page_breaks:
            raise HwpxLimitExceeded(
                "HWPX explicit page-break count exceeds the limit"
            )
        nearest_table, nearest_cell = _nearest_context(
            element,
            parent_map=parent_map,
            table_by_element=table_by_element,
            cell_by_element=cell_by_element,
        )
        xml_id = _optional_xml_token(
            _attribute(element, "id"),
            limits=limits,
        )
        break_index = len(page_breaks)
        break_id = _stable_id(
            "hwpx-page-break",
            document_sha256,
            section_path,
            str(section_index),
            str(break_index),
            source,
            xml_id or "",
            nearest_table.table_id if nearest_table else "",
            nearest_cell.target_id if nearest_cell else "",
        )
        page_breaks.append(
            PageBreakSnapshot(
                page_break_id=break_id,
                document_sha256=document_sha256,
                section_path=section_path,
                section_index=section_index,
                page_break_index=break_index,
                source=source,
                xml_id=xml_id,
                nearest_table_id=(
                    nearest_table.table_id if nearest_table else None
                ),
                nearest_cell_target_id=(
                    nearest_cell.target_id if nearest_cell else None
                ),
            )
        )

    field_names: List[str] = []
    bookmark_names: List[str] = []
    for element in root.iter():
        local = _local_name(element.tag).casefold()
        if local in {"fieldbegin", "fieldstart", "field"}:
            name = _first_named_attribute(
                element,
                ("name", "fieldName", "fieldname"),
                limits=limits,
            )
            if name is not None:
                field_names.append(name)
                if len(field_names) > limits.max_named_fields:
                    raise HwpxLimitExceeded(
                        "HWPX named-field count exceeds the limit"
                    )
        if local in {
            "bookmark",
            "bookmarkstart",
            "bookmarkbegin",
        }:
            name = _first_named_attribute(
                element,
                ("name", "bookmarkName", "bookmarkname"),
                limits=limits,
            )
            if name is not None:
                bookmark_names.append(name)
                if len(bookmark_names) > limits.max_named_bookmarks:
                    raise HwpxLimitExceeded(
                        "HWPX named-bookmark count exceeds the limit"
                    )

    return (
        tables,
        pictures,
        page_breaks,
        field_names,
        len(field_names),
        bookmark_names,
        len(bookmark_names),
    )


def _snapshot_table(
    table: ElementTree.Element,
    *,
    section_path: str,
    section_index: int,
    table_index: int,
    document_sha256: str,
    limits: HwpxStructureLimits,
) -> TableSnapshot:
    xml_id = _optional_xml_token(_attribute(table, "id"), limits=limits)
    declared_rows = _optional_bounded_integer(
        _attribute(table, "rowCnt"),
        minimum=1,
        maximum=limits.max_table_rows,
        label="table rowCnt",
    )
    declared_columns = _optional_bounded_integer(
        _attribute(table, "colCnt"),
        minimum=1,
        maximum=limits.max_table_columns,
        label="table colCnt",
    )
    table_id = _stable_id(
        "hwpx-table",
        document_sha256,
        section_path,
        str(section_index),
        str(table_index),
        xml_id or "",
    )

    cells: List[CellSnapshot] = []
    maximum_row = 0
    maximum_column = 0
    direct_rows = [
        child
        for child in list(table)
        if _local_name(child.tag).casefold() == "tr"
    ]
    for row_order, row_element in enumerate(direct_rows):
        direct_cells = [
            child
            for child in list(row_element)
            if _local_name(child.tag).casefold() == "tc"
        ]
        for column_order, cell_element in enumerate(direct_cells):
            cell_index = len(cells)
            row, column, address_source = _cell_address(
                cell_element,
                fallback_row=row_order,
                fallback_column=column_order,
                limits=limits,
            )
            row_span, column_span = _cell_span(
                cell_element,
                limits=limits,
            )
            maximum_row = max(maximum_row, row + row_span)
            maximum_column = max(
                maximum_column,
                column + column_span,
            )
            normalized_text = _cell_text(cell_element, limits)
            text_hash = hashlib.sha256(
                normalized_text.encode("utf-8")
            ).hexdigest()
            target_id = _target_id(
                "cell",
                document_sha256,
                section_path,
                str(section_index),
                str(table_index),
                xml_id or "",
                str(cell_index),
                str(row),
                str(column),
                str(row_span),
                str(column_span),
                text_hash,
            )
            cells.append(
                CellSnapshot(
                    target_id=target_id,
                    target_summary_sha256="",
                    document_sha256=document_sha256,
                    section_path=section_path,
                    section_index=section_index,
                    table_id=table_id,
                    table_index=table_index,
                    table_xml_id=xml_id,
                    cell_index=cell_index,
                    row=row,
                    column=column,
                    row_span=row_span,
                    column_span=column_span,
                    address_source=address_source,
                    text_excerpt=normalized_text[
                        : limits.text_excerpt_chars
                    ],
                    text_sha256=text_hash,
                    text_truncated=(
                        len(normalized_text) > limits.text_excerpt_chars
                    ),
                )
            )
    if declared_rows is not None and maximum_row > declared_rows:
        raise HwpxFormatError(
            "HWPX cell address exceeds the table row count"
        )
    if (
        declared_columns is not None
        and maximum_column > declared_columns
    ):
        raise HwpxFormatError(
            "HWPX cell address exceeds the table column count"
        )
    rows = declared_rows if declared_rows is not None else maximum_row
    columns = (
        declared_columns
        if declared_columns is not None
        else maximum_column
    )
    if rows <= 0 or columns <= 0:
        raise HwpxFormatError("HWPX table has no addressable grid")
    return TableSnapshot(
        table_id=table_id,
        document_sha256=document_sha256,
        section_path=section_path,
        section_index=section_index,
        table_index=table_index,
        xml_id=xml_id,
        rows=rows,
        columns=columns,
        declared_rows=declared_rows,
        declared_columns=declared_columns,
        cell_count=len(cells),
        cells=tuple(cells),
    )


def _direct_table_cells(
    table: ElementTree.Element,
) -> List[ElementTree.Element]:
    cells: List[ElementTree.Element] = []
    for row in list(table):
        if _local_name(row.tag).casefold() != "tr":
            continue
        cells.extend(
            child
            for child in list(row)
            if _local_name(child.tag).casefold() == "tc"
        )
    return cells


def _cell_address(
    cell: ElementTree.Element,
    *,
    fallback_row: int,
    fallback_column: int,
    limits: HwpxStructureLimits,
) -> Tuple[int, int, str]:
    address_nodes = [
        child
        for child in list(cell)
        if _local_name(child.tag).casefold() == "celladdr"
    ]
    if len(address_nodes) > 1:
        raise HwpxFormatError(
            "HWPX cell has multiple direct cellAddr elements"
        )
    if not address_nodes:
        return fallback_row, fallback_column, "row_order_fallback"
    address = address_nodes[0]
    row = _required_bounded_integer(
        _attribute(address, "rowAddr"),
        minimum=0,
        maximum=limits.max_table_rows - 1,
        label="cell rowAddr",
    )
    column = _required_bounded_integer(
        _attribute(address, "colAddr"),
        minimum=0,
        maximum=limits.max_table_columns - 1,
        label="cell colAddr",
    )
    return row, column, "direct_cellAddr"


def _cell_span(
    cell: ElementTree.Element,
    *,
    limits: HwpxStructureLimits,
) -> Tuple[int, int]:
    span_nodes = [
        child
        for child in list(cell)
        if _local_name(child.tag).casefold() == "cellspan"
    ]
    if len(span_nodes) > 1:
        raise HwpxFormatError(
            "HWPX cell has multiple direct cellSpan elements"
        )
    if not span_nodes:
        return 1, 1
    span = span_nodes[0]
    row_span = _required_bounded_integer(
        _attribute(span, "rowSpan"),
        minimum=1,
        maximum=limits.max_cell_span,
        label="cell rowSpan",
    )
    column_span = _required_bounded_integer(
        _attribute(span, "colSpan"),
        minimum=1,
        maximum=limits.max_cell_span,
        label="cell colSpan",
    )
    return row_span, column_span


def _cell_text(
    cell: ElementTree.Element,
    limits: HwpxStructureLimits,
) -> str:
    fragments: List[str] = []
    raw_characters = 0

    def visit(element: ElementTree.Element) -> None:
        nonlocal raw_characters
        for child in list(element):
            local = _local_name(child.tag).casefold()
            if local == "tbl":
                continue
            if local == "t":
                text = "".join(child.itertext())
                raw_characters += len(text)
                if raw_characters > limits.max_cell_text_chars:
                    raise HwpxLimitExceeded(
                        "HWPX cell text exceeds the character limit"
                    )
                fragments.append(text)
                continue
            visit(child)

    visit(cell)
    normalized = unicodedata.normalize("NFC", " ".join(fragments))
    if _CONTROL_PATTERN.search(normalized):
        raise HwpxSecurityError(
            "HWPX cell text contains control characters"
        )
    normalized = " ".join(normalized.split())
    if len(normalized) > limits.max_cell_text_chars:
        raise HwpxLimitExceeded(
            "HWPX normalized cell text exceeds the character limit"
        )
    return normalized


def _picture_refs(
    picture: ElementTree.Element,
    limits: HwpxStructureLimits,
) -> Tuple[str, ...]:
    refs: List[str] = []
    for descendant in picture.iter():
        if _local_name(descendant.tag).casefold() != "img":
            continue
        for candidate in (
            "binaryItemIDRef",
            "binaryItemIdRef",
            "binaryItemID",
        ):
            value = _attribute(descendant, candidate)
            if value is None:
                continue
            token = _required_xml_token(value, limits=limits)
            if token not in refs:
                refs.append(token)
                if len(refs) > limits.max_picture_refs:
                    raise HwpxLimitExceeded(
                        "HWPX picture reference count exceeds the limit"
                    )
            break
    return tuple(refs)


def _nearest_context(
    element: ElementTree.Element,
    *,
    parent_map: Mapping[ElementTree.Element, ElementTree.Element],
    table_by_element: Mapping[ElementTree.Element, TableSnapshot],
    cell_by_element: Mapping[ElementTree.Element, CellSnapshot],
) -> Tuple[Optional[TableSnapshot], Optional[CellSnapshot]]:
    table: Optional[TableSnapshot] = None
    cell: Optional[CellSnapshot] = None
    cursor = parent_map.get(element)
    while cursor is not None:
        if cell is None:
            cell = cell_by_element.get(cursor)
        if table is None:
            table = table_by_element.get(cursor)
        if table is not None and cell is not None:
            break
        cursor = parent_map.get(cursor)
    return table, cell


def _explicit_page_break_source(
    element: ElementTree.Element,
) -> Optional[str]:
    page_break = _attribute(element, "pageBreak")
    if (
        page_break is not None
        and page_break.strip().casefold() in _TRUE_XML_VALUES
    ):
        return "pageBreak_attribute"
    local = _local_name(element.tag).casefold()
    if local in {"pagebreak", "page-break"}:
        return "page_break_element"
    if local == "break":
        break_type = _attribute(element, "type")
        if (
            break_type is not None
            and break_type.strip().casefold() == "page"
        ):
            return "page_break_element"
    return None


def _first_named_attribute(
    element: ElementTree.Element,
    candidates: Iterable[str],
    *,
    limits: HwpxStructureLimits,
) -> Optional[str]:
    for candidate in candidates:
        value = _attribute(element, candidate)
        if value is None or not value:
            continue
        return _required_xml_token(value, limits=limits)
    return None


def _attribute(
    element: ElementTree.Element,
    requested_local_name: str,
) -> Optional[str]:
    requested = requested_local_name.casefold()
    for name, value in element.attrib.items():
        if _local_name(name).casefold() == requested:
            return value
    return None


def _local_name(name: object) -> str:
    if not isinstance(name, str):
        return ""
    if "}" in name:
        return name.rsplit("}", 1)[-1]
    if ":" in name:
        return name.rsplit(":", 1)[-1]
    return name


def _optional_xml_token(
    value: Optional[str],
    *,
    limits: HwpxStructureLimits,
) -> Optional[str]:
    if value is None or value == "":
        return None
    return _required_xml_token(value, limits=limits)


def _required_xml_token(
    value: str,
    *,
    limits: HwpxStructureLimits,
) -> str:
    if not isinstance(value, str) or not value:
        raise HwpxFormatError("HWPX XML token is empty")
    normalized = unicodedata.normalize("NFC", value)
    if len(normalized) > limits.max_name_chars:
        raise HwpxLimitExceeded(
            "HWPX XML token exceeds the character limit"
        )
    if _CONTROL_PATTERN.search(normalized):
        raise HwpxSecurityError(
            "HWPX XML token contains control characters"
        )
    return normalized


def _optional_bounded_integer(
    value: Optional[str],
    *,
    minimum: int,
    maximum: int,
    label: str,
) -> Optional[int]:
    if value is None or value == "":
        return None
    return _required_bounded_integer(
        value,
        minimum=minimum,
        maximum=maximum,
        label=label,
    )


def _required_bounded_integer(
    value: Optional[str],
    *,
    minimum: int,
    maximum: int,
    label: str,
) -> int:
    if (
        value is None
        or not isinstance(value, str)
        or _INTEGER_PATTERN.fullmatch(value) is None
    ):
        raise HwpxFormatError(f"HWPX {label} is invalid")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise HwpxLimitExceeded(f"HWPX {label} exceeds the limit")
    return parsed


def _target_id(
    kind: str,
    document_sha256: str,
    *parts: str,
) -> str:
    if kind not in {"cell", "picture"}:
        raise ValueError("unsupported HWPX target kind")
    return _stable_id(
        f"hwpx-{kind}",
        document_sha256,
        *parts,
    )


def _stable_id(prefix: str, *parts: str) -> str:
    encoded = "\x00".join(
        (HWPX_STRUCTURE_SCHEMA, prefix, *parts)
    ).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(encoded).hexdigest()[:24]}"


def _target_summary_sha256(
    document_sha256: str,
    tables: Sequence[TableSnapshot],
    pictures: Sequence[PictureSnapshot],
) -> str:
    targets: List[Dict[str, object]] = []
    for table in tables:
        for cell in table.cells:
            targets.append(
                {
                    "kind": "cell",
                    "target_id": cell.target_id,
                    "section_path": cell.section_path,
                    "table_index": cell.table_index,
                    "table_xml_id": cell.table_xml_id,
                    "cell_index": cell.cell_index,
                    "row": cell.row,
                    "column": cell.column,
                    "row_span": cell.row_span,
                    "column_span": cell.column_span,
                    "text_sha256": cell.text_sha256,
                }
            )
    for picture in pictures:
        targets.append(
            {
                "kind": "picture",
                "target_id": picture.target_id,
                "section_path": picture.section_path,
                "picture_index": picture.picture_index,
                "xml_id": picture.xml_id,
                "binary_item_refs": list(picture.binary_item_refs),
                "nearest_table_id": picture.nearest_table_id,
                "nearest_cell_target_id": (
                    picture.nearest_cell_target_id
                ),
            }
        )
    targets.sort(key=lambda row: str(row["target_id"]))
    payload = {
        "schema_version": HWPX_STRUCTURE_SCHEMA,
        "document_sha256": document_sha256,
        "targets": targets,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _unique_in_order(values: Iterable[str]) -> Tuple[str, ...]:
    seen = set()
    result: List[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)


__all__ = [
    "CellSnapshot",
    "FileIdentity",
    "HWPX_STRUCTURE_SCHEMA",
    "HwpxFormatError",
    "HwpxLimitExceeded",
    "HwpxReadOnlySafety",
    "HwpxSecurityError",
    "HwpxSourceChanged",
    "HwpxStructureError",
    "HwpxStructureLimits",
    "HwpxStructureSnapshot",
    "PageBreakSnapshot",
    "PictureSnapshot",
    "SectionSnapshot",
    "TableSnapshot",
    "inspect_hwpx_structure",
]
