"""Deterministic, fail-closed HWPX target writer.

This module is the package-writing boundary for an already validated,
server-owned exact-target plan.  It deliberately does not invoke HWP COM and
does not accept renderer-authored report facts.

The writer:

* requires the exact source SHA-256 and structure target-summary hash;
* resolves cells by section/table/cell index from the read-only structure
  snapshot and audited standalone paragraphs by exact XML path, ordinal,
  selected run paths, and source hashes, never by fuzzy text search;
* changes only XML text bytes inside the selected cell's existing ``t`` runs
  or audited paragraph's selected existing ``t`` runs; an exact preformatted
  empty cell whose sole existing ``run`` has no ``t`` may receive one ``t``
  child inside that run, without changing the run or its formatting
  attributes;
* removes only the direct ``linesegarray`` cache of the paragraph whose text
  changed so Hancom can recompute line layout;
* resolves pictures through ``Contents/content.hpf`` and replaces the
  existing package member without changing the picture XML or geometry;
* requires evidence bytes to match the existing declared/detected media type;
* refuses a shared image member unless every affected picture target is
  present in the same plan with the same evidence;
* creates a new output with exclusive-create semantics and never overwrites
  the source or an existing output;
* reopens the output with :func:`inspect_hwpx_structure`, verifies the complete
  structural projection and every target, and removes the output on failure.

For the audited 3Q/4Q samples, a successful result proves the pinned
72-table/48-page *layout proxy*.  It is not a rendered-page or COM proof.  The
caller must still run HWP COM apply/preview and the 48-page render validator.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import stat
import unicodedata
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import (
    BinaryIO,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)
from xml.etree import ElementTree
from xml.parsers import expat

from . import hwpx_structure as structure
from .hwpx_sample_layout import (
    EXPECTED_RENDERED_PAGE_COUNT,
    EXPECTED_TABLE_COUNT,
    SampleLayoutInspection,
    inspect_sample_layout,
)
from .hwpx_stale_factual_targets import XmlParagraphAddress
from .hwpx_structure import (
    HwpxStructureLimits,
    HwpxStructureSnapshot,
    PictureSnapshot,
    inspect_hwpx_structure,
)
from .hwpx_weak_cases import (
    HwpxWeakCaseLimits,
    _inspect_image_header,
)


HWPX_EXACT_WRITER_SCHEMA = "trafficvisson.hwpx-exact-writer.v1"
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_TARGET_ID = re.compile(
    r"^hwpx-(?:cell|picture|paragraph)-[a-f0-9]{24}$"
)
_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_INCIDENT_REF = re.compile(
    r"^incident:[A-Za-z0-9][A-Za-z0-9._:-]{0,110}$"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MEDIA_ALIASES = {
    "image/bmp": "image/bmp",
    "image/x-bmp": "image/bmp",
    "image/x-ms-bmp": "image/bmp",
    "image/png": "image/png",
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/gif": "image/gif",
    "image/webp": "image/webp",
}
_COPY_CHUNK_BYTES = 4 * 1024 * 1024


class HwpxExactWriterError(RuntimeError):
    """Caller-safe exact writer failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExactHwpxWriterLimits:
    structure_limits: HwpxStructureLimits = field(
        default_factory=HwpxStructureLimits
    )
    # A complete 48-page binder can legitimately exceed the provider-facing
    # DocumentEditPlan's historical 100-operation cap (photos, captions,
    # opinions, route cells, markers, and stale-slot neutralization).  Keep a
    # separate explicit bound and never truncate.
    # The complete 48-page projection can require roughly 3,500 exact
    # mutations: 149 photos/captions, 24 route/opinion/date groups, and all
    # physical cells in 24 evaluation grids.  Keep it one atomic document
    # write; splitting into stale-value-sensitive partial outputs is unsafe.
    max_operations: int = 5_000
    max_text_chars: int = 20_000
    max_manifest_bytes: int = 2 * 1024**2
    max_evidence_bytes: int = 64 * 1024**2
    max_total_evidence_bytes: int = 512 * 1024**2
    max_output_archive_bytes: int = 2 * 1024**3

    def __post_init__(self) -> None:
        bounds = (
            ("max_operations", self.max_operations, 1, 10_000),
            ("max_text_chars", self.max_text_chars, 1, 1_000_000),
            (
                "max_manifest_bytes",
                self.max_manifest_bytes,
                128,
                32 * 1024**2,
            ),
            (
                "max_evidence_bytes",
                self.max_evidence_bytes,
                32,
                512 * 1024**2,
            ),
            (
                "max_total_evidence_bytes",
                self.max_total_evidence_bytes,
                32,
                8 * 1024**3,
            ),
            (
                "max_output_archive_bytes",
                self.max_output_archive_bytes,
                1_024,
                8 * 1024**3,
            ),
        )
        for name, value, minimum, maximum in bounds:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{name} is outside the supported range")
        if self.max_evidence_bytes > self.max_total_evidence_bytes:
            raise ValueError(
                "max_total_evidence_bytes must accommodate one evidence file"
            )


@dataclass(frozen=True)
class ExactHwpxTextOperation:
    operation_id: str
    target_id: str
    expected_current_value: str
    expected_current_sha256: str
    value: str
    source_ref: str
    kind: str = field(default="replace_cell_text", init=False)


@dataclass(frozen=True)
class ExactHwpxParagraphTextOperation:
    """Exact standalone-paragraph mutation from the stale-target audit."""

    operation_id: str
    target_id: str
    address: XmlParagraphAddress
    expected_current_value: str
    expected_current_sha256: str
    value: str
    source_ref: str
    kind: str = field(default="replace_paragraph_text", init=False)


@dataclass(frozen=True)
class ExactHwpxPictureOperation:
    operation_id: str
    target_id: str
    expected_binary_item_ref: str
    expected_current_sha256: str
    evidence_path: str
    evidence_sha256: str
    source_ref: str
    kind: str = field(default="replace_picture", init=False)


ExactHwpxOperation = Union[
    ExactHwpxTextOperation,
    ExactHwpxParagraphTextOperation,
    ExactHwpxPictureOperation,
]

_ExactHwpxSectionTextOperation = Union[
    ExactHwpxTextOperation,
    ExactHwpxParagraphTextOperation,
]


@dataclass(frozen=True)
class ExactHwpxWritePlan:
    plan_id: str
    source_document_sha256: str
    source_target_summary_sha256: str
    incident_source_refs: Tuple[str, ...]
    operations: Tuple[ExactHwpxOperation, ...]
    schema_version: str = HWPX_EXACT_WRITER_SCHEMA


@dataclass(frozen=True)
class ExactHwpxTargetVerification:
    operation_id: str
    target_id: str
    kind: str
    source_ref: str
    output_value_sha256: str
    binary_item_ref: Optional[str] = None
    package_member: Optional[str] = None


@dataclass(frozen=True)
class ExactSampleWriterContract:
    template_id: str
    quarter: int
    document_sha256: str
    target_summary_sha256: str
    table_count: int
    expected_rendered_page_count: int
    page_contract_proxy_verified: bool
    source_read_only: bool = True
    no_com: bool = True


@dataclass(frozen=True)
class ExactHwpxWriteResult:
    schema_version: str
    state: str
    plan_id: str
    source_document_sha256: str
    output_document_sha256: str
    output_path: str
    operation_count: int
    target_verifications: Tuple[ExactHwpxTargetVerification, ...]
    table_count: int
    expected_rendered_page_count: Optional[int]
    output_rendered_page_count: Optional[int]
    page_contract_proxy_verified: bool
    output_render_validation_status: str
    structure_projection_sha256: str
    source_unchanged: bool
    mimetype_first_and_stored: bool
    com_invocation_count: int = 0

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _ManifestItem:
    item_id: str
    package_path: str
    declared_media_type: str


@dataclass(frozen=True)
class _Evidence:
    path: Path
    sha256: str
    payload: bytes
    media_type: str
    width: int
    height: int


@dataclass(frozen=True)
class _TextRange:
    start: int
    end: int
    self_closing_tag: bool = False
    empty_run_tag: bool = False
    empty_run_text_qname: bytes = b""


@dataclass(frozen=True)
class _LineSegRange:
    start: int
    end: int
    element_count: int


@dataclass(frozen=True)
class _ResolvedPicture:
    operation: ExactHwpxPictureOperation
    picture: PictureSnapshot
    item: _ManifestItem
    package_member: str
    source_payload_sha256: str
    evidence: _Evidence


@dataclass(frozen=True)
class _ResolvedParagraph:
    operation: ExactHwpxParagraphTextOperation
    paragraph: ElementTree.Element
    selected_text_nodes: Tuple[ElementTree.Element, ...]
    protected_structure_sha256: str
    untargeted_content_sha256: str


def inspect_exact_sample_writer_contract(
    source_path: Union[str, os.PathLike],
) -> ExactSampleWriterContract:
    """Read-only attestation for one byte-exact audited sample."""

    inspection = inspect_sample_layout(source_path)
    return ExactSampleWriterContract(
        template_id=inspection.baseline.template_id,
        quarter=inspection.baseline.quarter,
        document_sha256=inspection.snapshot.document_sha256,
        target_summary_sha256=(
            inspection.snapshot.target_summary_sha256
        ),
        table_count=len(inspection.snapshot.tables),
        expected_rendered_page_count=(
            inspection.baseline.rendered_page_count
        ),
        page_contract_proxy_verified=(
            len(inspection.snapshot.tables) == EXPECTED_TABLE_COUNT
            and inspection.baseline.rendered_page_count
            == EXPECTED_RENDERED_PAGE_COUNT
        ),
    )


def write_exact_hwpx(
    source_path: Union[str, os.PathLike],
    output_path: Union[str, os.PathLike],
    plan: ExactHwpxWritePlan,
    *,
    limits: Optional[ExactHwpxWriterLimits] = None,
    require_audited_sample: bool = True,
) -> ExactHwpxWriteResult:
    """Create and verify a new HWPX from an exact server-owned target plan."""

    active_limits = limits or ExactHwpxWriterLimits()
    source = _absolute_path(source_path, label="source", suffix=".hwpx")
    output = _absolute_path(output_path, label="output", suffix=".hwpx")
    _validate_output_path(source, output)
    _validate_plan(plan, active_limits)

    sample: Optional[SampleLayoutInspection]
    if require_audited_sample:
        try:
            sample = inspect_sample_layout(source)
        except Exception as error:
            raise HwpxExactWriterError(
                "audited_sample_required",
                "source is not a byte-exact audited 3Q/4Q sample",
            ) from error
        snapshot = sample.snapshot
    else:
        sample = None
        snapshot = inspect_hwpx_structure(
            source,
            limits=active_limits.structure_limits,
        )

    if plan.source_document_sha256 != snapshot.document_sha256:
        raise HwpxExactWriterError(
            "source_hash_mismatch",
            "plan source SHA-256 does not match the inspected HWPX",
        )
    if (
        plan.source_target_summary_sha256
        != snapshot.target_summary_sha256
    ):
        raise HwpxExactWriterError(
            "target_summary_mismatch",
            "plan target summary does not match the inspected HWPX",
        )

    source_identity = snapshot.identity_after
    source_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    source_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        source_descriptor = os.open(str(source), source_flags)
    except OSError as error:
        raise HwpxExactWriterError(
            "source_open_failed",
            "source could not be reopened read-only",
        ) from error

    output_created = False
    try:
        with os.fdopen(
            source_descriptor,
            "rb",
            buffering=0,
            closefd=True,
        ) as source_file:
            if (
                structure._file_identity(os.fstat(source_file.fileno()))
                != source_identity
            ):
                raise HwpxExactWriterError(
                    "source_changed",
                    "source identity changed after inspection",
                )
            if (
                structure._hash_open_file(
                    source_file,
                    expected_size=source_identity.size_bytes,
                )
                != snapshot.document_sha256
            ):
                raise HwpxExactWriterError(
                    "source_changed",
                    "source bytes changed after inspection",
                )

            output_descriptor = _create_output_exclusive(output)
            output_created = True
            try:
                with os.fdopen(
                    output_descriptor,
                    "w+b",
                    buffering=0,
                    closefd=True,
                ) as output_file:
                    (
                        verifications,
                        projection_sha,
                        section_element_removals,
                    ) = _write_package(
                        source_file,
                        output_file,
                        snapshot=snapshot,
                        plan=plan,
                        limits=active_limits,
                    )
                    output_file.flush()
                    os.fsync(output_file.fileno())
            except BaseException:
                raise

            source_sha_after = structure._hash_open_file(
                source_file,
                expected_size=source_identity.size_bytes,
            )
            if source_sha_after != snapshot.document_sha256:
                raise HwpxExactWriterError(
                    "source_changed",
                    "source bytes changed while output was written",
                )

        if output.stat().st_size > active_limits.max_output_archive_bytes:
            raise HwpxExactWriterError(
                "output_capacity_exceeded",
                "written HWPX exceeds the output byte limit",
            )
        with zipfile.ZipFile(output, mode="r") as generated_archive:
            for info in generated_archive.infolist():
                if (
                    info.file_size
                    and info.compress_size
                    and (
                        info.file_size / float(info.compress_size)
                        > active_limits.structure_limits.max_compression_ratio
                    )
                ):
                    raise HwpxExactWriterError(
                        "output_compression_ratio_exceeded",
                        (
                            "generated package member exceeds the safe "
                            f"compression ratio: {info.filename}"
                        ),
                    )

        output_snapshot = inspect_hwpx_structure(
            output,
            limits=active_limits.structure_limits,
        )
        _verify_output_structure(
            snapshot,
            output_snapshot,
            plan=plan,
            expected_projection_sha256=projection_sha,
            section_element_removals=section_element_removals,
        )
        _verify_output_package(
            source,
            output,
            snapshot=snapshot,
            plan=plan,
            verifications=verifications,
            limits=active_limits,
        )

        output_sha = output_snapshot.document_sha256
        source_stat_after = os.lstat(str(source))
        source_unchanged = (
            structure._file_identity(source_stat_after) == source_identity
            and inspect_hwpx_structure(
                source,
                limits=active_limits.structure_limits,
            ).document_sha256
            == snapshot.document_sha256
        )
        if not source_unchanged:
            raise HwpxExactWriterError(
                "source_changed",
                "source identity or bytes changed during output verification",
            )

        page_proxy = bool(
            sample is not None
            and len(output_snapshot.tables) == EXPECTED_TABLE_COUNT
            and sample.baseline.rendered_page_count
            == EXPECTED_RENDERED_PAGE_COUNT
        )
        return ExactHwpxWriteResult(
            schema_version=HWPX_EXACT_WRITER_SCHEMA,
            state="writer_completed_com_validation_required",
            plan_id=plan.plan_id,
            source_document_sha256=snapshot.document_sha256,
            output_document_sha256=output_sha,
            output_path=str(output),
            operation_count=len(plan.operations),
            target_verifications=verifications,
            table_count=len(output_snapshot.tables),
            expected_rendered_page_count=(
                sample.baseline.rendered_page_count if sample else None
            ),
            output_rendered_page_count=None,
            page_contract_proxy_verified=page_proxy,
            output_render_validation_status=(
                "not_run_com_validation_required"
            ),
            structure_projection_sha256=projection_sha,
            source_unchanged=True,
            mimetype_first_and_stored=True,
        )
    except BaseException:
        if output_created:
            _remove_owned_output(output)
        raise


def _write_package(
    source_file: BinaryIO,
    output_file: BinaryIO,
    *,
    snapshot: HwpxStructureSnapshot,
    plan: ExactHwpxWritePlan,
    limits: ExactHwpxWriterLimits,
) -> Tuple[
    Tuple[ExactHwpxTargetVerification, ...],
    str,
    Dict[str, int],
]:
    source_file.seek(0)
    with zipfile.ZipFile(source_file, mode="r") as source_archive:
        infos = source_archive.infolist()
        normalized_infos = structure._validate_archive_entries(
            infos,
            limits.structure_limits,
        )
        if (
            infos[0].filename != "mimetype"
            or infos[0].compress_type != zipfile.ZIP_STORED
        ):
            raise HwpxExactWriterError(
                "mimetype_contract_invalid",
                "mimetype must be the first stored HWPX member",
            )
        mimetype = structure._read_member_bounded(
            source_archive,
            infos[0],
            maximum=128,
        )
        if mimetype != structure.HWPX_MIMETYPE:
            raise HwpxExactWriterError(
                "mimetype_contract_invalid",
                "HWPX mimetype bytes are invalid",
            )

        manifest_info = normalized_infos.get("contents/content.hpf")
        if manifest_info is None:
            raise HwpxExactWriterError(
                "manifest_missing",
                "Contents/content.hpf is required for exact picture binding",
            )
        manifest_raw = structure._read_member_bounded(
            source_archive,
            manifest_info,
            maximum=limits.max_manifest_bytes,
        )
        manifest = _parse_manifest(
            manifest_raw,
            limits=limits,
        )

        cells = {
            cell.target_id: cell
            for table in snapshot.tables
            for cell in table.cells
        }
        pictures = {
            picture.target_id: picture
            for picture in snapshot.pictures
        }
        section_operations: Dict[
            str, List[_ExactHwpxSectionTextOperation]
        ] = {}
        picture_operations: List[ExactHwpxPictureOperation] = []
        for operation in plan.operations:
            if isinstance(operation, ExactHwpxTextOperation):
                cell = cells.get(operation.target_id)
                if cell is None:
                    raise HwpxExactWriterError(
                        "unknown_cell_target",
                        "text target does not exist in the exact source",
                    )
                section_operations.setdefault(
                    cell.section_path, []
                ).append(operation)
            elif isinstance(
                operation,
                ExactHwpxParagraphTextOperation,
            ):
                matching_sections = tuple(
                    section
                    for section in snapshot.sections
                    if (
                        section.path == operation.address.section_path
                        and section.section_index
                        == operation.address.section_index
                    )
                )
                if len(matching_sections) != 1:
                    raise HwpxExactWriterError(
                        "unknown_paragraph_section",
                        "paragraph target section is not in the exact source",
                    )
                section_operations.setdefault(
                    operation.address.section_path,
                    [],
                ).append(operation)
            elif isinstance(operation, ExactHwpxPictureOperation):
                if operation.target_id not in pictures:
                    raise HwpxExactWriterError(
                        "unknown_picture_target",
                        "picture target does not exist in the exact source",
                    )
                picture_operations.append(operation)
            else:
                raise HwpxExactWriterError(
                    "unsupported_operation",
                    "plan contains an unsupported operation object",
                )

        modified_members: Dict[str, bytes] = {}
        verifications: List[ExactHwpxTargetVerification] = []
        section_element_removals: Dict[str, int] = {}
        for section_path, operations in section_operations.items():
            info = normalized_infos.get(_archive_key(section_path))
            if info is None:
                raise HwpxExactWriterError(
                    "section_missing",
                    "target section disappeared from the source package",
                )
            raw = structure._read_member_bounded(
                source_archive,
                info,
                maximum=limits.structure_limits.max_xml_entry_bytes,
            )
            modified, rows, removed_elements = _replace_section_text(
                raw,
                section_path=section_path,
                snapshot=snapshot,
                operations=operations,
                limits=limits,
            )
            modified_members[info.filename] = modified
            verifications.extend(rows)
            section_element_removals[section_path] = removed_elements

        resolved_pictures = _resolve_picture_operations(
            source_archive,
            normalized_infos=normalized_infos,
            snapshot=snapshot,
            manifest=manifest,
            operations=picture_operations,
            limits=limits,
        )
        picture_replacements: Dict[str, bytes] = {}
        for resolved in resolved_pictures:
            member = resolved.package_member
            existing = picture_replacements.get(member)
            if (
                existing is not None
                and hashlib.sha256(existing).hexdigest()
                != resolved.evidence.sha256
            ):
                raise HwpxExactWriterError(
                    "conflicting_picture_replacement",
                    "one package image member cannot receive two payloads",
                )
            picture_replacements[member] = resolved.evidence.payload
            verifications.append(
                ExactHwpxTargetVerification(
                    operation_id=resolved.operation.operation_id,
                    target_id=resolved.operation.target_id,
                    kind=resolved.operation.kind,
                    source_ref=resolved.operation.source_ref,
                    output_value_sha256=resolved.evidence.sha256,
                    binary_item_ref=(
                        resolved.operation.expected_binary_item_ref
                    ),
                    package_member=member,
                )
            )

        source_projection = _structure_projection(
            snapshot,
            ignored_text_targets={
                operation.target_id
                for operation in plan.operations
                if isinstance(operation, ExactHwpxTextOperation)
            },
            section_element_count_adjustments=section_element_removals,
        )
        projection_sha = _canonical_sha256(source_projection)

        with zipfile.ZipFile(
            output_file,
            mode="w",
            allowZip64=True,
        ) as output_archive:
            output_archive.comment = source_archive.comment
            for info in infos:
                replacement = modified_members.get(info.filename)
                is_modified_xml = replacement is not None
                is_replaced_picture = False
                if replacement is None:
                    replacement = picture_replacements.get(info.filename)
                    is_replaced_picture = replacement is not None
                cloned = _clone_zip_info(info)
                if is_modified_xml or is_replaced_picture:
                    # Removing editor-computed line caches or transcoding a
                    # mostly uniform evidence frame can make a generated
                    # member much more compressible than its trusted source.
                    # Store server-generated members verbatim so the reopened
                    # package cannot trip the same ZIP-bomb ratio guard that
                    # protects untrusted inputs.
                    cloned.compress_type = zipfile.ZIP_STORED
                if replacement is not None:
                    output_archive.writestr(cloned, replacement)
                    continue
                with source_archive.open(info, mode="r") as source_member:
                    with output_archive.open(
                        cloned,
                        mode="w",
                        force_zip64=(info.file_size >= zipfile.ZIP64_LIMIT),
                    ) as output_member:
                        shutil.copyfileobj(
                            source_member,
                            output_member,
                            length=_COPY_CHUNK_BYTES,
                        )

    return (
        tuple(verifications),
        projection_sha,
        section_element_removals,
    )


def _replace_section_text(
    raw: bytes,
    *,
    section_path: str,
    snapshot: HwpxStructureSnapshot,
    operations: Sequence[_ExactHwpxSectionTextOperation],
    limits: ExactHwpxWriterLimits,
) -> Tuple[
    bytes,
    Tuple[ExactHwpxTargetVerification, ...],
    int,
]:
    structure._reject_dtd_or_entities(raw)
    cell_operations = tuple(
        operation
        for operation in operations
        if isinstance(operation, ExactHwpxTextOperation)
    )
    paragraph_operations = tuple(
        operation
        for operation in operations
        if isinstance(operation, ExactHwpxParagraphTextOperation)
    )
    cells = {
        cell.target_id: cell
        for table in snapshot.tables
        for cell in table.cells
        if cell.section_path == section_path
    }
    targets: Dict[Tuple[int, int], ExactHwpxTextOperation] = {}
    for operation in cell_operations:
        cell = cells[operation.target_id]
        targets[(cell.table_index, cell.cell_index)] = operation

    mutable_targets = {
        key: operation
        for key, operation in targets.items()
        if _normalize_cell_value(operation.value)
        != _normalize_cell_value(operation.expected_current_value)
    }
    ranges, cell_lineseg_ranges = _find_text_ranges(
        raw,
        mutable_targets,
    )
    tree, _ = structure._parse_xml_bounded(
        raw,
        entry_name=section_path,
        limits=limits.structure_limits,
    )
    table_elements = [
        element
        for element in tree.iter()
        if _local_name(element.tag) == "tbl"
    ]
    resolved_paragraphs = tuple(
        _resolve_source_paragraph_operation(
            tree,
            snapshot=snapshot,
            section_path=section_path,
            operation=operation,
        )
        for operation in paragraph_operations
    )
    (
        paragraph_ranges,
        paragraph_lineseg_ranges,
    ) = _find_paragraph_text_ranges(
        raw,
        paragraph_operations,
    )
    patches: List[Tuple[int, int, bytes]] = []
    verifications: List[ExactHwpxTargetVerification] = []
    inserted_text_elements = 0
    for (table_index, cell_index), operation in targets.items():
        try:
            table = table_elements[table_index]
            cell = _direct_table_cells(table)[cell_index]
        except IndexError as error:
            raise HwpxExactWriterError(
                "target_traversal_changed",
                "exact table/cell traversal changed after inspection",
            ) from error
        current = _normalized_cell_text(cell)
        expected = _normalize_cell_value(
            operation.expected_current_value
        )
        expected_hash = _sha256_text(expected)
        if operation.expected_current_sha256 != expected_hash:
            raise HwpxExactWriterError(
                "expected_text_hash_invalid",
                "expected cell value and expected SHA-256 disagree",
            )
        if current != expected or _sha256_text(current) != expected_hash:
            raise HwpxExactWriterError(
                "stale_expected_value",
                "target cell no longer contains the expected value",
            )
        planned_value = _normalize_cell_value(operation.value)
        if planned_value == expected:
            verifications.append(
                ExactHwpxTargetVerification(
                    operation_id=operation.operation_id,
                    target_id=operation.target_id,
                    kind=operation.kind,
                    source_ref=operation.source_ref,
                    output_value_sha256=_sha256_text(planned_value),
                )
            )
            continue
        target_ranges = ranges.get((table_index, cell_index))
        if not target_ranges:
            raise HwpxExactWriterError(
                "text_run_missing",
                (
                    f"target {operation.target_id} requires one existing "
                    "mutable text run for a non-empty value"
                ),
            )
        replacement = _xml_text_bytes(operation.value, limits)
        chosen = next(
            (
                index
                for index, candidate in enumerate(target_ranges)
                if (
                    not candidate.self_closing_tag
                    and raw[candidate.start : candidate.end].strip()
                )
            ),
            0,
        )
        for index, candidate in enumerate(target_ranges):
            payload = replacement if index == chosen else b""
            if candidate.self_closing_tag:
                payload = _replace_self_closing_text_tag(
                    raw[candidate.start : candidate.end],
                    payload,
                )
            elif candidate.empty_run_tag:
                payload = _replace_self_closing_run_tag(
                    raw[candidate.start : candidate.end],
                    payload,
                    text_qname=candidate.empty_run_text_qname,
                )
                if payload != raw[candidate.start : candidate.end]:
                    inserted_text_elements += 1
            patches.append(
                (
                    candidate.start,
                    candidate.end,
                    payload,
                )
            )
        verifications.append(
            ExactHwpxTargetVerification(
                operation_id=operation.operation_id,
                target_id=operation.target_id,
                kind=operation.kind,
                source_ref=operation.source_ref,
                output_value_sha256=_sha256_text(
                    _normalize_cell_value(operation.value)
                ),
            )
        )

    for resolved in resolved_paragraphs:
        operation = resolved.operation
        target_ranges = paragraph_ranges.get(operation.target_id)
        if not target_ranges:
            raise HwpxExactWriterError(
                "paragraph_text_run_missing",
                "paragraph target has no exact mutable text run",
            )
        replacement = _xml_text_bytes(operation.value, limits)
        chosen = next(
            (
                index
                for index, candidate in enumerate(target_ranges)
                if raw[candidate.start : candidate.end].strip()
            ),
            0,
        )
        for index, candidate in enumerate(target_ranges):
            patches.append(
                (
                    candidate.start,
                    candidate.end,
                    replacement if index == chosen else b"",
                )
            )
        verifications.append(
            ExactHwpxTargetVerification(
                operation_id=operation.operation_id,
                target_id=operation.target_id,
                kind=operation.kind,
                source_ref=operation.source_ref,
                output_value_sha256=_sha256_text(
                    _normalize_paragraph_value(operation.value)
                ),
            )
        )

    _assert_disjoint_patches(patches)
    unique_lineseg_ranges: Dict[Tuple[int, int], _LineSegRange] = {}
    for candidate in (
        *cell_lineseg_ranges,
        *paragraph_lineseg_ranges,
    ):
        key = (candidate.start, candidate.end)
        existing = unique_lineseg_ranges.setdefault(key, candidate)
        if existing.element_count != candidate.element_count:
            raise HwpxExactWriterError(
                "lineseg_removal_conflict",
                "one lineseg range has conflicting element counts",
            )
    for candidate in unique_lineseg_ranges.values():
        patches.append((candidate.start, candidate.end, b""))
    _assert_disjoint_patches(patches)
    modified = raw
    for start, end, replacement in sorted(
        patches,
        key=lambda row: row[0],
        reverse=True,
    ):
        modified = modified[:start] + replacement + modified[end:]
    return (
        modified,
        tuple(verifications),
        (
            sum(
                item.element_count
                for item in unique_lineseg_ranges.values()
            )
            - inserted_text_elements
        ),
    )


def _normalize_paragraph_value(value: str) -> str:
    """Normalize Unicode without collapsing meaningful header whitespace."""

    return unicodedata.normalize("NFC", value)


def _element_at_path(
    root: ElementTree.Element,
    path: Sequence[int],
) -> ElementTree.Element:
    current = root
    for index in path:
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
            or index >= len(current)
        ):
            raise HwpxExactWriterError(
                "stale_paragraph_address",
                "paragraph XML child path is stale",
            )
        current = list(current)[index]
    return current


def _paragraph_run_structure(
    paragraph: ElementTree.Element,
) -> Tuple[Tuple[object, ...], ...]:
    rows = []
    run_index = 0
    for child in list(paragraph):
        if _local_name(child.tag) != "run":
            continue
        rows.append(
            (
                run_index,
                tuple(
                    _local_name(run_child.tag)
                    for run_child in list(child)
                ),
                tuple(
                    sorted(
                        (_local_name(key), value)
                        for key, value in child.attrib.items()
                    )
                ),
            )
        )
        run_index += 1
    return tuple(rows)


def _paragraph_run_structure_sha256(
    paragraph: ElementTree.Element,
) -> str:
    payload = [
        {
            "run_index": row[0],
            "child_kinds": list(row[1]),
            "attributes": list(row[2]),
        }
        for row in _paragraph_run_structure(paragraph)
    ]
    return _canonical_sha256(payload)


def _paragraph_attributes_sha256(
    paragraph: ElementTree.Element,
) -> str:
    return _canonical_sha256(
        sorted(
            (_local_name(key), value)
            for key, value in paragraph.attrib.items()
        )
    )


def _direct_lineseg_rows(
    paragraph: ElementTree.Element,
) -> Tuple[Tuple[int, ElementTree.Element], ...]:
    return tuple(
        (index, child)
        for index, child in enumerate(list(paragraph))
        if _local_name(child.tag) == "linesegarray"
    )


def _paragraph_protected_structure_sha256(
    paragraph: ElementTree.Element,
) -> str:
    """Hash tags/attributes/order while allowing authorized layout-cache edits.

    Text values are intentionally omitted: selected standalone text changes
    here, while nested cell text is independently guarded by the structure
    snapshot.  Direct host-p lineseg cache and descendant-table lineseg caches
    are omitted because exact cell operations may remove those in the same
    atomic plan.  Every other run/table/control tag and attribute remains
    covered.
    """

    def project(
        element: ElementTree.Element,
        *,
        paragraph_root: bool,
        inside_nested_table: bool,
    ) -> Mapping[str, object]:
        children = []
        current_inside_table = (
            inside_nested_table or _local_name(element.tag) == "tbl"
        )
        for child in list(element):
            if (
                (paragraph_root or current_inside_table)
                and _local_name(child.tag) == "linesegarray"
            ):
                continue
            children.append(
                project(
                    child,
                    paragraph_root=False,
                    inside_nested_table=current_inside_table,
                )
            )
        return {
            "tag": _local_name(element.tag),
            "attributes": sorted(
                (_local_name(key), value)
                for key, value in element.attrib.items()
            ),
            "children": children,
        }

    return _canonical_sha256(
        project(
            paragraph,
            paragraph_root=True,
            inside_nested_table=False,
        )
    )


def _paragraph_untargeted_content_sha256(
    paragraph: ElementTree.Element,
    selected_text_nodes: Sequence[ElementTree.Element],
) -> str:
    """Hash non-target paragraph text and all protected XML structure.

    Text inside descendant tables is represented by a stable sentinel because
    exact cell operations may legitimately change it in the same atomic plan;
    the global cell projection independently protects every untargeted cell.
    """

    selected = set(selected_text_nodes)

    def project(
        element: ElementTree.Element,
        *,
        paragraph_root: bool,
        inside_selected_text: bool,
        inside_text_element: bool,
        inside_nested_table: bool,
    ) -> Mapping[str, object]:
        current_selected = inside_selected_text or element in selected
        current_inside_text = (
            inside_text_element or _local_name(element.tag) == "t"
        )
        current_inside_table = (
            inside_nested_table or _local_name(element.tag) == "tbl"
        )

        def protected_text(
            value: Optional[str],
            *,
            text_context: bool,
        ) -> Optional[str]:
            if not text_context and (value is None or not value.strip()):
                return None
            return value

        children = []
        for child in list(element):
            if (
                (paragraph_root or current_inside_table)
                and _local_name(child.tag) == "linesegarray"
            ):
                continue
            children.append(
                project(
                    child,
                    paragraph_root=False,
                    inside_selected_text=current_selected,
                    inside_text_element=current_inside_text,
                    inside_nested_table=current_inside_table,
                )
            )
        return {
            "tag": _local_name(element.tag),
            "attributes": sorted(
                (_local_name(key), value)
                for key, value in element.attrib.items()
            ),
            "text": (
                "<selected-text>"
                if current_selected
                else "<nested-table-text>"
                if current_inside_table
                else protected_text(
                    element.text,
                    text_context=current_inside_text,
                )
            ),
            # An element tail belongs to its parent.  The selected t node's own
            # tail is therefore protected; descendant tails inside that t are
            # part of the selected payload and may change.
            "tail": (
                "<selected-text>"
                if inside_selected_text
                else "<nested-table-text>"
                if inside_nested_table
                else protected_text(
                    element.tail,
                    text_context=inside_text_element,
                )
            ),
            "children": children,
        }

    return _canonical_sha256(
        project(
            paragraph,
            paragraph_root=True,
            inside_selected_text=False,
            inside_text_element=False,
            inside_nested_table=False,
        )
    )


def _paragraph_page_break(paragraph: ElementTree.Element) -> bool:
    return (_attribute(paragraph, "pageBreak") or "0").casefold() in {
        "1",
        "true",
        "yes",
    }


def _selected_paragraph_text_nodes(
    paragraph: ElementTree.Element,
    address: XmlParagraphAddress,
) -> Tuple[ElementTree.Element, ...]:
    text_nodes = tuple(
        element
        for element in paragraph.iter()
        if _local_name(element.tag) == "t"
    )
    if (
        not address.selected_text_run_paths
        or len(address.selected_text_run_paths)
        != len(address.selected_text_run_ordinals)
        or len(set(address.selected_text_run_paths))
        != len(address.selected_text_run_paths)
        or len(set(address.selected_text_run_ordinals))
        != len(address.selected_text_run_ordinals)
    ):
        raise HwpxExactWriterError(
            "stale_paragraph_text_run_address",
            "paragraph selected text-run address set is invalid",
        )
    parent_map = {
        child: parent
        for parent in paragraph.iter()
        for child in list(parent)
    }
    selected = []
    for ordinal, path in zip(
        address.selected_text_run_ordinals,
        address.selected_text_run_paths,
    ):
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or ordinal < 0
            or ordinal >= len(text_nodes)
        ):
            raise HwpxExactWriterError(
                "stale_paragraph_text_run_address",
                "paragraph text-run ordinal is stale",
            )
        try:
            element = _element_at_path(paragraph, path)
        except HwpxExactWriterError as error:
            raise HwpxExactWriterError(
                "stale_paragraph_text_run_address",
                "paragraph text-run path is stale",
            ) from error
        if (
            _local_name(element.tag) != "t"
            or element is not text_nodes[ordinal]
        ):
            raise HwpxExactWriterError(
                "stale_paragraph_text_run_address",
                "paragraph text-run path and ordinal disagree",
            )
        current = element
        nearest_paragraph = None
        while current in parent_map:
            current = parent_map[current]
            if _local_name(current.tag) == "p":
                nearest_paragraph = current
                break
        if nearest_paragraph is not paragraph:
            raise HwpxExactWriterError(
                "stale_paragraph_text_run_address",
                "selected text belongs to a nested paragraph",
            )
        selected.append(element)
    return tuple(selected)


def _paragraph_target_id(
    document_sha256: str,
    address: XmlParagraphAddress,
) -> str:
    payload = {
        "document_sha256": document_sha256,
        "section_path": address.section_path,
        "section_index": address.section_index,
        "paragraph_ordinal": address.paragraph_ordinal,
        "text_sha256": address.selected_text_sha256,
    }
    return "hwpx-paragraph-" + _canonical_sha256(payload)[:24]


def _resolve_paragraph_address(
    root: ElementTree.Element,
    *,
    snapshot: HwpxStructureSnapshot,
    section_path: str,
    operation: ExactHwpxParagraphTextOperation,
    output_mode: bool,
) -> _ResolvedParagraph:
    address = operation.address
    if (
        address.section_path != section_path
        or address.section_child_index
        != (
            address.xml_child_path[0]
            if address.xml_child_path
            else -1
        )
        or address.lineseg_policy
        != "remove_direct_linesegarray_from_this_p_only"
        or not address.recursive_lineseg_removal_forbidden
        or not address.run_deletion_forbidden
    ):
        raise HwpxExactWriterError(
            "paragraph_address_contract_invalid",
            "paragraph address or protected-edit policy is invalid",
        )
    section_matches = tuple(
        section
        for section in snapshot.sections
        if (
            section.path == address.section_path
            and section.section_index == address.section_index
        )
    )
    if len(section_matches) != 1:
        raise HwpxExactWriterError(
            "stale_paragraph_address",
            "paragraph section address is stale",
        )
    paragraphs = tuple(
        element
        for element in root.iter()
        if _local_name(element.tag) == "p"
    )
    if (
        isinstance(address.paragraph_ordinal, bool)
        or not isinstance(address.paragraph_ordinal, int)
        or address.paragraph_ordinal < 0
        or address.paragraph_ordinal >= len(paragraphs)
    ):
        raise HwpxExactWriterError(
            "stale_paragraph_address",
            "paragraph ordinal is stale",
        )
    paragraph = _element_at_path(root, address.xml_child_path)
    if (
        _local_name(paragraph.tag) != "p"
        or paragraph is not paragraphs[address.paragraph_ordinal]
        or _attribute(paragraph, "id") != address.paragraph_xml_id
        or _paragraph_page_break(paragraph) != address.page_break
        or _paragraph_attributes_sha256(paragraph)
        != address.paragraph_attributes_sha256
    ):
        raise HwpxExactWriterError(
            "stale_paragraph_address",
            "paragraph XML path, ordinal, ID, or attributes are stale",
        )
    selected = _selected_paragraph_text_nodes(paragraph, address)
    run_structure = _paragraph_run_structure(paragraph)
    run_sha = _paragraph_run_structure_sha256(paragraph)
    if (
        len(run_structure) != address.direct_run_count
        or run_sha != address.run_structure_sha256
    ):
        raise HwpxExactWriterError(
            "stale_paragraph_run_structure",
            "paragraph run structure changed from the exact audit",
        )
    protected = any(
        kind != "t"
        for row in run_structure
        for kind in row[1]
    )
    if protected != address.protected_structure_present:
        raise HwpxExactWriterError(
            "stale_paragraph_run_structure",
            "paragraph protected-run classification is stale",
        )
    lineseg_rows = _direct_lineseg_rows(paragraph)
    if output_mode:
        if lineseg_rows:
            raise HwpxExactWriterError(
                "output_paragraph_lineseg_not_removed",
                "output paragraph still has a direct linesegarray",
            )
        expected_output = _normalize_paragraph_value(operation.value)
        current = _normalize_paragraph_value(
            "".join(element.text or "" for element in selected)
        )
        if current != expected_output:
            raise HwpxExactWriterError(
                "output_paragraph_text_verification_failed",
                "output paragraph does not contain the planned value",
            )
    else:
        lineseg_indexes = tuple(index for index, _ in lineseg_rows)
        lineseg_hashes = tuple(
            hashlib.sha256(
                ElementTree.tostring(child, encoding="utf-8")
            ).hexdigest()
            for _, child in lineseg_rows
        )
        if (
            not lineseg_rows
            or lineseg_indexes
            != address.direct_linesegarray_child_indexes
            or lineseg_hashes
            != address.direct_linesegarray_sha256s
        ):
            raise HwpxExactWriterError(
                "stale_paragraph_lineseg_structure",
                "paragraph direct linesegarray address or hash is stale",
            )
        normalized_address_text = _normalize_paragraph_value(
            address.selected_text
        )
        normalized_expected = _normalize_paragraph_value(
            operation.expected_current_value
        )
        current = _normalize_paragraph_value(
            "".join(element.text or "" for element in selected)
        )
        expected_hash = _sha256_text(normalized_expected)
        if (
            operation.expected_current_sha256 != expected_hash
            or address.selected_text_sha256
            != _sha256_text(normalized_address_text)
        ):
            raise HwpxExactWriterError(
                "stale_paragraph_expected_hash",
                "paragraph expected value and SHA-256 disagree",
            )
        if (
            current != normalized_expected
            or current != normalized_address_text
            or _sha256_text(current) != expected_hash
        ):
            raise HwpxExactWriterError(
                "stale_paragraph_value",
                "paragraph no longer contains the exact audited text",
            )
        expected_target_id = _paragraph_target_id(
            snapshot.document_sha256,
            address,
        )
        address_payload = {
            "document_sha256": snapshot.document_sha256,
            "section_path": address.section_path,
            "section_index": address.section_index,
            "paragraph_ordinal": address.paragraph_ordinal,
            "xml_child_path": address.xml_child_path,
            "selected_text_sha256": address.selected_text_sha256,
        }
        if (
            operation.target_id != expected_target_id
            or address.address_sha256
            != _canonical_sha256(address_payload)
        ):
            raise HwpxExactWriterError(
                "stale_paragraph_target_id",
                "paragraph target ID or address hash is stale",
            )
    return _ResolvedParagraph(
        operation=operation,
        paragraph=paragraph,
        selected_text_nodes=selected,
        protected_structure_sha256=(
            _paragraph_protected_structure_sha256(paragraph)
        ),
        untargeted_content_sha256=(
            _paragraph_untargeted_content_sha256(
                paragraph,
                selected,
            )
        ),
    )


def _resolve_source_paragraph_operation(
    root: ElementTree.Element,
    *,
    snapshot: HwpxStructureSnapshot,
    section_path: str,
    operation: ExactHwpxParagraphTextOperation,
) -> _ResolvedParagraph:
    return _resolve_paragraph_address(
        root,
        snapshot=snapshot,
        section_path=section_path,
        operation=operation,
        output_mode=False,
    )


def _find_paragraph_text_ranges(
    raw: bytes,
    operations: Sequence[ExactHwpxParagraphTextOperation],
) -> Tuple[
    Mapping[str, Tuple[_TextRange, ...]],
    Tuple[_LineSegRange, ...],
]:
    if not operations:
        return {}, ()
    by_ordinal = {
        operation.address.paragraph_ordinal: operation
        for operation in operations
    }
    if len(by_ordinal) != len(operations):
        raise HwpxExactWriterError(
            "duplicate_paragraph_address",
            "two paragraph operations share one paragraph ordinal",
        )
    parser = expat.ParserCreate(namespace_separator="}")
    stack: List[Dict[str, object]] = []
    paragraph_counter = -1
    found_paragraphs: Dict[str, int] = {
        operation.target_id: 0 for operation in operations
    }
    found_text_paths: Dict[
        str, Dict[Tuple[int, ...], int]
    ] = {
        operation.target_id: {
            tuple(path): 0
            for path in operation.address.selected_text_run_paths
        }
        for operation in operations
    }
    found_linesegs: Dict[str, int] = {
        operation.target_id: 0 for operation in operations
    }
    ranges: Dict[str, List[_TextRange]] = {
        operation.target_id: [] for operation in operations
    }
    lineseg_ranges: List[_LineSegRange] = []

    def start(name: str, _attributes: Mapping[str, str]) -> None:
        nonlocal paragraph_counter
        local = _local_name(name)
        if stack:
            parent = stack[-1]
            child_index = int(parent.get("child_count", 0))
            parent["child_count"] = child_index + 1
            path = tuple(parent["path"]) + (child_index,)
        else:
            path = ()
        frame: Dict[str, object] = {
            "local": local,
            "path": path,
            "child_count": 0,
        }
        for ancestor in reversed(stack):
            if ancestor["local"] == "linesegarray":
                ancestor["element_count"] = (
                    int(ancestor.get("element_count", 1)) + 1
                )
                break
        if local == "p":
            paragraph_counter += 1
            operation = by_ordinal.get(paragraph_counter)
            if operation is not None:
                if path != operation.address.xml_child_path:
                    raise HwpxExactWriterError(
                        "stale_paragraph_address",
                        "paragraph raw XML path is stale",
                    )
                frame["paragraph_operation"] = operation
                found_paragraphs[operation.target_id] += 1
        elif local == "t":
            paragraph_frame = next(
                (
                    candidate
                    for candidate in reversed(stack)
                    if candidate["local"] == "p"
                ),
                None,
            )
            if (
                paragraph_frame is not None
                and paragraph_frame.get("paragraph_operation") is not None
            ):
                operation = paragraph_frame["paragraph_operation"]
                assert isinstance(
                    operation,
                    ExactHwpxParagraphTextOperation,
                )
                paragraph_path = tuple(paragraph_frame["path"])
                relative_path = path[len(paragraph_path) :]
                target_paths = found_text_paths[operation.target_id]
                if relative_path in target_paths:
                    target_paths[relative_path] += 1
                    start_index = parser.CurrentByteIndex
                    content_start = _tag_end(raw, start_index)
                    frame["paragraph_operation"] = operation
                    frame["content_start"] = content_start
                    frame["self_closing"] = (
                        raw[start_index:content_start]
                        .rstrip()
                        .endswith(b"/>")
                    )
        elif local == "linesegarray" and stack:
            parent = stack[-1]
            operation = parent.get("paragraph_operation")
            if (
                parent["local"] == "p"
                and isinstance(
                    operation,
                    ExactHwpxParagraphTextOperation,
                )
            ):
                relative_index = path[-1]
                if (
                    relative_index
                    in operation.address.direct_linesegarray_child_indexes
                ):
                    found_linesegs[operation.target_id] += 1
                    start_index = parser.CurrentByteIndex
                    content_start = _tag_end(raw, start_index)
                    frame["paragraph_operation"] = operation
                    frame["lineseg_start"] = start_index
                    frame["lineseg_open_end"] = content_start
                    frame["element_count"] = 1
                    frame["self_closing"] = (
                        raw[start_index:content_start]
                        .rstrip()
                        .endswith(b"/>")
                    )
        stack.append(frame)

    def end(_name: str) -> None:
        if not stack:
            raise HwpxExactWriterError(
                "xml_traversal_invalid",
                "section XML traversal stack is inconsistent",
            )
        frame = stack.pop()
        operation = frame.get("paragraph_operation")
        if (
            frame["local"] == "t"
            and isinstance(
                operation,
                ExactHwpxParagraphTextOperation,
            )
        ):
            if frame.get("self_closing"):
                raise HwpxExactWriterError(
                    "self_closing_text_run_unsupported",
                    "self-closing paragraph text runs are unsupported",
                )
            content_start = int(frame["content_start"])
            content_end = parser.CurrentByteIndex
            ranges[operation.target_id].extend(
                _text_segments(raw, content_start, content_end)
            )
        elif (
            frame["local"] == "linesegarray"
            and isinstance(
                operation,
                ExactHwpxParagraphTextOperation,
            )
        ):
            if frame.get("self_closing"):
                end_index = int(frame["lineseg_open_end"])
            else:
                end_index = _tag_end(raw, parser.CurrentByteIndex)
            lineseg_ranges.append(
                _LineSegRange(
                    int(frame["lineseg_start"]),
                    end_index,
                    int(frame.get("element_count", 1)),
                )
            )

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        parser.Parse(raw, True)
    except HwpxExactWriterError:
        raise
    except expat.ExpatError as error:
        raise HwpxExactWriterError(
            "section_xml_invalid",
            "target section XML could not be parsed safely",
        ) from error
    if any(count != 1 for count in found_paragraphs.values()):
        raise HwpxExactWriterError(
            "stale_paragraph_address",
            "paragraph ordinal did not resolve exactly once",
        )
    if any(
        count != 1
        for target in found_text_paths.values()
        for count in target.values()
    ):
        raise HwpxExactWriterError(
            "stale_paragraph_text_run_address",
            "paragraph text-run path did not resolve exactly once",
        )
    for operation in operations:
        if found_linesegs[operation.target_id] != len(
            operation.address.direct_linesegarray_child_indexes
        ):
            raise HwpxExactWriterError(
                "stale_paragraph_lineseg_structure",
                "paragraph direct linesegarray did not resolve exactly",
            )
    return (
        {target: tuple(value) for target, value in ranges.items()},
        tuple(lineseg_ranges),
    )


def _find_text_ranges(
    raw: bytes,
    targets: Mapping[Tuple[int, int], ExactHwpxTextOperation],
) -> Tuple[
    Mapping[Tuple[int, int], Tuple[_TextRange, ...]],
    Tuple[_LineSegRange, ...],
]:
    parser = expat.ParserCreate(namespace_separator="}")
    stack: List[Dict[str, object]] = []
    table_counter = -1
    cell_counts: Dict[int, int] = {}
    found_cells: Dict[Tuple[int, int], int] = {}
    ranges: Dict[Tuple[int, int], List[_TextRange]] = {
        target: [] for target in targets
    }
    empty_run_candidates: Dict[
        Tuple[int, int],
        List[_TextRange],
    ] = {target: [] for target in targets}
    targeted_lineseg_ranges: List[_LineSegRange] = []

    def start(name: str, _attributes: Mapping[str, str]) -> None:
        nonlocal table_counter
        local = _local_name(name)
        frame: Dict[str, object] = {"local": local}
        if stack and stack[-1]["local"] == "run":
            stack[-1]["child_element_count"] = (
                int(stack[-1].get("child_element_count", 0)) + 1
            )
        for ancestor in reversed(stack):
            if ancestor["local"] == "linesegarray":
                ancestor["element_count"] = (
                    int(ancestor.get("element_count", 1)) + 1
                )
                break
        if local == "tbl":
            table_counter += 1
            frame["table_index"] = table_counter
            cell_counts[table_counter] = 0
        elif (
            local == "tc"
            and len(stack) >= 2
            and stack[-1]["local"] == "tr"
            and stack[-2]["local"] == "tbl"
        ):
            table_index = int(stack[-2]["table_index"])
            cell_index = cell_counts[table_index]
            cell_counts[table_index] += 1
            frame["cell_key"] = (table_index, cell_index)
            if (table_index, cell_index) in targets:
                found_cells[(table_index, cell_index)] = (
                    found_cells.get((table_index, cell_index), 0) + 1
                )
        elif local == "p":
            cell_key = next(
                (
                    candidate.get("cell_key")
                    for candidate in reversed(stack)
                    if candidate["local"] == "tc"
                    and candidate.get("cell_key") is not None
                ),
                None,
            )
            if cell_key in targets:
                frame["host_target_cell_key"] = cell_key
        elif (
            local == "run"
            and stack
            and stack[-1]["local"] == "p"
            and stack[-1].get("host_target_cell_key") in targets
        ):
            start_index = parser.CurrentByteIndex
            content_start = _tag_end(raw, start_index)
            opening = raw[start_index:content_start]
            match = re.match(
                br"<([A-Za-z_][A-Za-z0-9_.:-]*)",
                opening,
            )
            if match is None:
                raise HwpxExactWriterError(
                    "empty_run_tag_invalid",
                    "an exact target run tag cannot be resolved safely",
                )
            run_qname = match.group(1)
            prefix = (
                run_qname.rsplit(b":", 1)[0] + b":"
                if b":" in run_qname
                else b""
            )
            frame.update(
                {
                    "target_empty_run_cell_key": (
                        stack[-1]["host_target_cell_key"]
                    ),
                    "tag_start": start_index,
                    "content_start": content_start,
                    "self_closing": opening.rstrip().endswith(b"/>"),
                    "child_element_count": 0,
                    "empty_run_text_qname": prefix + b"t",
                }
            )
        elif local == "t":
            cell_key = next(
                (
                    candidate.get("cell_key")
                    for candidate in reversed(stack)
                    if candidate["local"] == "tc"
                    and candidate.get("cell_key") is not None
                ),
                None,
            )
            if cell_key in targets:
                start_index = parser.CurrentByteIndex
                frame["target_cell_key"] = cell_key
                paragraph = next(
                    (
                        candidate
                        for candidate in reversed(stack)
                        if candidate["local"] == "p"
                    ),
                    None,
                )
                if paragraph is None:
                    raise HwpxExactWriterError(
                        "target_paragraph_missing",
                        "target text is not hosted by a paragraph",
                    )
                paragraph["contains_target_text"] = True
                content_start = _tag_end(raw, start_index)
                frame["tag_start"] = start_index
                frame["content_start"] = content_start
                frame["self_closing"] = (
                    raw[start_index:content_start].rstrip().endswith(b"/>")
                )
        elif local == "linesegarray" and stack:
            # HWPX stores editor-computed line placement in linesegarray.
            # Text edits invalidate it.  Record only a *direct* child of the
            # nearest host paragraph; deleting descendant caches would alter
            # untouched decorative/table-cell layout.
            parent = stack[-1]
            if parent["local"] == "p":
                start_index = parser.CurrentByteIndex
                content_start = _tag_end(raw, start_index)
                frame["lineseg_start"] = start_index
                frame["lineseg_open_end"] = content_start
                frame["element_count"] = 1
                frame["self_closing"] = (
                    raw[start_index:content_start].rstrip().endswith(b"/>")
                )
        stack.append(frame)

    def end(_name: str) -> None:
        if not stack:
            raise HwpxExactWriterError(
                "xml_traversal_invalid",
                "section XML traversal stack is inconsistent",
            )
        frame = stack.pop()
        cell_key = frame.get("target_cell_key")
        if cell_key is not None:
            if frame.get("self_closing"):
                ranges[cell_key].append(
                    _TextRange(
                        int(frame["tag_start"]),
                        int(frame["content_start"]),
                        self_closing_tag=True,
                    )
                )
            else:
                content_start = int(frame["content_start"])
                content_end = parser.CurrentByteIndex
                if content_end < content_start:
                    raise HwpxExactWriterError(
                        "text_run_range_invalid",
                        "target text run byte range is invalid",
                    )
                ranges[cell_key].extend(
                    _text_segments(raw, content_start, content_end)
                )
        empty_run_cell_key = frame.get("target_empty_run_cell_key")
        if (
            empty_run_cell_key is not None
            and int(frame.get("child_element_count", 0)) == 0
        ):
            if frame.get("self_closing"):
                candidate = _TextRange(
                    int(frame["tag_start"]),
                    int(frame["content_start"]),
                    empty_run_tag=True,
                    empty_run_text_qname=bytes(
                        frame["empty_run_text_qname"]
                    ),
                )
            else:
                candidate = _TextRange(
                    parser.CurrentByteIndex,
                    parser.CurrentByteIndex,
                    empty_run_tag=True,
                    empty_run_text_qname=bytes(
                        frame["empty_run_text_qname"]
                    ),
                )
            empty_run_candidates[empty_run_cell_key].append(candidate)
            if stack and stack[-1]["local"] == "p":
                stack[-1]["contains_target_empty_run"] = True
        if frame["local"] == "linesegarray":
            start_index = frame.get("lineseg_start")
            if start_index is not None and stack:
                parent = stack[-1]
                if frame.get("self_closing"):
                    end_index = int(frame["lineseg_open_end"])
                else:
                    end_index = _tag_end(raw, parser.CurrentByteIndex)
                parent.setdefault("direct_lineseg_ranges", []).append(
                    _LineSegRange(
                        int(start_index),
                        end_index,
                        int(frame.get("element_count", 1)),
                    )
                )
        elif (
            frame["local"] == "p"
            and (
                frame.get("contains_target_text")
                or frame.get("contains_target_empty_run")
            )
        ):
            targeted_lineseg_ranges.extend(
                frame.get("direct_lineseg_ranges", ())
            )

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        parser.Parse(raw, True)
    except HwpxExactWriterError:
        raise
    except expat.ExpatError as error:
        raise HwpxExactWriterError(
            "section_xml_invalid",
            "target section XML could not be parsed safely",
        ) from error

    if any(found_cells.get(target) != 1 for target in targets):
        raise HwpxExactWriterError(
            "target_cell_not_unique",
            "a text target did not resolve to exactly one direct table cell",
        )
    for target, candidates in empty_run_candidates.items():
        if ranges[target]:
            continue
        if len(candidates) == 1:
            ranges[target].append(candidates[0])
    return (
        {key: tuple(value) for key, value in ranges.items()},
        tuple(targeted_lineseg_ranges),
    )


def _text_segments(
    raw: bytes,
    start: int,
    end: int,
) -> List[_TextRange]:
    payload = raw[start:end]
    if b"<![CDATA[" in payload or b"<!--" in payload or b"<?" in payload:
        raise HwpxExactWriterError(
            "complex_text_run_unsupported",
            "CDATA, comments, and processing instructions are not supported "
            "inside target text runs",
        )
    segments: List[_TextRange] = []
    cursor = start
    text_start = start
    while cursor < end:
        if raw[cursor] != ord("<"):
            cursor += 1
            continue
        if text_start <= cursor:
            segments.append(_TextRange(text_start, cursor))
        cursor = _tag_end(raw, cursor)
        text_start = cursor
    if text_start <= end:
        segments.append(_TextRange(text_start, end))
    return segments


def _tag_end(raw: bytes, start: int) -> int:
    if start < 0 or start >= len(raw) or raw[start] != ord("<"):
        raise HwpxExactWriterError(
            "xml_tag_offset_invalid",
            "XML tag offset is invalid",
        )
    quote: Optional[int] = None
    cursor = start + 1
    while cursor < len(raw):
        value = raw[cursor]
        if quote is not None:
            if value == quote:
                quote = None
        elif value in {ord('"'), ord("'")}:
            quote = value
        elif value == ord(">"):
            return cursor + 1
        cursor += 1
    raise HwpxExactWriterError(
        "xml_tag_unterminated",
        "target XML tag is unterminated",
    )


def _resolve_picture_operations(
    source_archive: zipfile.ZipFile,
    *,
    normalized_infos: Mapping[str, zipfile.ZipInfo],
    snapshot: HwpxStructureSnapshot,
    manifest: Mapping[str, _ManifestItem],
    operations: Sequence[ExactHwpxPictureOperation],
    limits: ExactHwpxWriterLimits,
) -> Tuple[_ResolvedPicture, ...]:
    if not operations:
        return ()
    pictures = {
        picture.target_id: picture for picture in snapshot.pictures
    }
    evidence_cache: Dict[Tuple[str, str], _Evidence] = {}
    total_evidence = 0
    resolved: List[_ResolvedPicture] = []
    member_by_picture: Dict[str, str] = {}

    for operation in operations:
        picture = pictures[operation.target_id]
        if len(picture.binary_item_refs) != 1:
            raise HwpxExactWriterError(
                "picture_reference_ambiguous",
                "picture target must resolve to exactly one binary item",
            )
        ref = picture.binary_item_refs[0]
        if ref != operation.expected_binary_item_ref:
            raise HwpxExactWriterError(
                "picture_reference_stale",
                "picture binary item reference changed",
            )
        item = manifest.get(ref)
        if item is None:
            raise HwpxExactWriterError(
                "picture_manifest_item_missing",
                "picture binary item is missing from content.hpf",
            )
        info = normalized_infos.get(_archive_key(item.package_path))
        if info is None or info.is_dir():
            raise HwpxExactWriterError(
                "picture_package_member_missing",
                "picture package member is missing",
            )
        source_payload = structure._read_member_bounded(
            source_archive,
            info,
            maximum=limits.max_evidence_bytes,
        )
        source_media, _width, _height = _inspect_image_header(
            source_payload,
            limits=_image_limits(limits),
        )
        declared_media = _MEDIA_ALIASES.get(item.declared_media_type)
        if declared_media is None or declared_media != source_media:
            raise HwpxExactWriterError(
                "source_picture_media_invalid",
                "source picture magic does not match its declared media type",
            )
        source_sha = hashlib.sha256(source_payload).hexdigest()
        if source_sha != operation.expected_current_sha256:
            raise HwpxExactWriterError(
                "stale_picture_hash",
                "picture package member no longer matches the expected hash",
            )
        key = (operation.evidence_path, operation.evidence_sha256)
        evidence = evidence_cache.get(key)
        if evidence is None:
            evidence = _read_evidence(operation, limits=limits)
            total_evidence += len(evidence.payload)
            if total_evidence > limits.max_total_evidence_bytes:
                raise HwpxExactWriterError(
                    "evidence_capacity_exceeded",
                    "evidence payloads exceed the total byte limit",
                )
            evidence_cache[key] = evidence
        if evidence.media_type != source_media:
            raise HwpxExactWriterError(
                "evidence_media_mismatch",
                "evidence media type must match the existing picture media",
            )
        member_by_picture[picture.target_id] = item.package_path
        resolved.append(
            _ResolvedPicture(
                operation=operation,
                picture=picture,
                item=item,
                package_member=info.filename,
                source_payload_sha256=source_sha,
                evidence=evidence,
            )
        )

    targeted = {row.picture.target_id for row in resolved}
    evidence_by_member: Dict[str, set[str]] = {}
    for row in resolved:
        evidence_by_member.setdefault(
            row.package_member, set()
        ).add(row.evidence.sha256)
    for member, hashes in evidence_by_member.items():
        if len(hashes) != 1:
            raise HwpxExactWriterError(
                "conflicting_picture_replacement",
                "shared picture member has conflicting evidence payloads",
            )
        affected = {
            picture.target_id
            for picture in snapshot.pictures
            if any(
                manifest.get(ref) is not None
                and _archive_key(manifest[ref].package_path)
                == _archive_key(member)
                for ref in picture.binary_item_refs
            )
        }
        if not affected.issubset(targeted):
            raise HwpxExactWriterError(
                "shared_picture_cross_target_change",
                "replacing this image member would alter an untargeted picture",
            )
    return tuple(resolved)


def _read_evidence(
    operation: ExactHwpxPictureOperation,
    *,
    limits: ExactHwpxWriterLimits,
) -> _Evidence:
    path = _absolute_path(
        operation.evidence_path,
        label="evidence",
        suffix=None,
    )
    structure._assert_no_link_or_reparse_components(path)
    try:
        identity = structure._file_identity(os.lstat(str(path)))
    except OSError as error:
        raise HwpxExactWriterError(
            "evidence_missing",
            "evidence file does not exist",
        ) from error
    if not stat.S_ISREG(identity.mode):
        raise HwpxExactWriterError(
            "evidence_not_regular",
            "evidence path must be a regular file",
        )
    if (
        identity.size_bytes <= 0
        or identity.size_bytes > limits.max_evidence_bytes
    ):
        raise HwpxExactWriterError(
            "evidence_capacity_exceeded",
            "evidence file is empty or exceeds the byte limit",
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError as error:
        raise HwpxExactWriterError(
            "evidence_open_failed",
            "evidence file could not be opened safely",
        ) from error
    with os.fdopen(descriptor, "rb", buffering=0, closefd=True) as source:
        if structure._file_identity(os.fstat(source.fileno())) != identity:
            raise HwpxExactWriterError(
                "evidence_changed",
                "evidence identity changed while opening",
            )
        payload = source.read(limits.max_evidence_bytes + 1)
        if len(payload) != identity.size_bytes:
            raise HwpxExactWriterError(
                "evidence_changed",
                "evidence size changed while reading",
            )
        if structure._file_identity(os.fstat(source.fileno())) != identity:
            raise HwpxExactWriterError(
                "evidence_changed",
                "evidence identity changed while reading",
            )
    digest = hashlib.sha256(payload).hexdigest()
    if (
        operation.evidence_sha256 != digest
        or _SHA256.fullmatch(operation.evidence_sha256) is None
    ):
        raise HwpxExactWriterError(
            "evidence_hash_mismatch",
            "evidence SHA-256 does not match the plan",
        )
    media, width, height = _inspect_image_header(
        payload,
        limits=_image_limits(limits),
    )
    return _Evidence(
        path=path,
        sha256=digest,
        payload=payload,
        media_type=media,
        width=width,
        height=height,
    )


def _parse_manifest(
    raw: bytes,
    *,
    limits: ExactHwpxWriterLimits,
) -> Mapping[str, _ManifestItem]:
    structure._reject_dtd_or_entities(raw)
    root, _ = structure._parse_xml_bounded(
        raw,
        entry_name="Contents/content.hpf",
        limits=limits.structure_limits,
    )
    items: Dict[str, _ManifestItem] = {}
    for element in root.iter():
        if _local_name(element.tag) != "item":
            continue
        item_id = _attribute(element, "id")
        href = _attribute(element, "href")
        declared_media = _attribute(element, "media-type")
        if not item_id or not href or not declared_media:
            raise HwpxExactWriterError(
                "manifest_item_invalid",
                "content.hpf contains an incomplete item",
            )
        if item_id in items:
            raise HwpxExactWriterError(
                "manifest_item_duplicate",
                "content.hpf contains duplicate item IDs",
            )
        if len(items) >= 10_000:
            raise HwpxExactWriterError(
                "manifest_capacity_exceeded",
                "content.hpf contains too many items",
            )
        try:
            package_path = structure._validate_archive_member_name(
                href,
                limits.structure_limits.max_entry_name_chars,
            )
        except Exception as error:
            raise HwpxExactWriterError(
                "manifest_href_unsafe",
                "content.hpf contains an unsafe package href",
            ) from error
        items[item_id] = _ManifestItem(
            item_id=item_id,
            package_path=package_path,
            declared_media_type=declared_media.strip().casefold(),
        )
    return items


def _verify_output_structure(
    source: HwpxStructureSnapshot,
    output: HwpxStructureSnapshot,
    *,
    plan: ExactHwpxWritePlan,
    expected_projection_sha256: str,
    section_element_removals: Mapping[str, int],
) -> None:
    ignored = {
        operation.target_id
        for operation in plan.operations
        if isinstance(operation, ExactHwpxTextOperation)
    }
    source_projection = _structure_projection(
        source,
        ignored_text_targets=ignored,
        section_element_count_adjustments=section_element_removals,
    )
    output_ignored = set()
    source_cells = {
        cell.target_id: cell
        for table in source.tables
        for cell in table.cells
    }
    for target in ignored:
        cell = source_cells[target]
        try:
            output_cell = output.tables[cell.table_index].cells[
                cell.cell_index
            ]
        except IndexError as error:
            raise HwpxExactWriterError(
                "output_target_missing",
                "output cell traversal no longer matches the source",
            ) from error
        output_ignored.add(output_cell.target_id)
    output_projection = _structure_projection(
        output,
        ignored_text_targets=output_ignored,
    )
    source_sha = _canonical_sha256(source_projection)
    output_sha = _canonical_sha256(output_projection)
    if (
        source_sha != expected_projection_sha256
        or source_sha != output_sha
    ):
        raise HwpxExactWriterError(
            "output_structural_drift",
            "output HWPX contains structural or non-target content drift",
        )

    output_by_address = {
        (cell.section_path, cell.table_index, cell.cell_index): cell
        for table in output.tables
        for cell in table.cells
    }
    for operation in plan.operations:
        if not isinstance(operation, ExactHwpxTextOperation):
            continue
        source_cell = source_cells[operation.target_id]
        output_cell = output_by_address.get(
            (
                source_cell.section_path,
                source_cell.table_index,
                source_cell.cell_index,
            )
        )
        expected_output = _normalize_cell_value(operation.value)
        if (
            output_cell is None
            or output_cell.text_sha256 != _sha256_text(expected_output)
        ):
            raise HwpxExactWriterError(
                "output_text_verification_failed",
                "output target cell does not contain the planned value",
            )


def _verify_output_package(
    source_path: Path,
    output_path: Path,
    *,
    snapshot: HwpxStructureSnapshot,
    plan: ExactHwpxWritePlan,
    verifications: Sequence[ExactHwpxTargetVerification],
    limits: ExactHwpxWriterLimits,
) -> None:
    with (
        zipfile.ZipFile(source_path, mode="r") as source_archive,
        zipfile.ZipFile(output_path, mode="r") as output_archive,
    ):
        source_infos = source_archive.infolist()
        output_infos = output_archive.infolist()
        structure._validate_archive_entries(
            output_infos,
            limits.structure_limits,
        )
        if [info.filename for info in source_infos] != [
            info.filename for info in output_infos
        ]:
            raise HwpxExactWriterError(
                "output_member_sequence_drift",
                "output package member order changed",
            )
        if output_infos[0].filename != "mimetype" or (
            output_infos[0].compress_type != zipfile.ZIP_STORED
        ):
            raise HwpxExactWriterError(
                "output_mimetype_contract_failed",
                "output mimetype is not first and stored",
            )
        modified_sections = {
            next(
                cell.section_path
                for table in snapshot.tables
                for cell in table.cells
                if cell.target_id == operation.target_id
            )
            for operation in plan.operations
            if isinstance(operation, ExactHwpxTextOperation)
        }
        modified_sections.update(
            operation.address.section_path
            for operation in plan.operations
            if isinstance(
                operation,
                ExactHwpxParagraphTextOperation,
            )
        )
        modified_images = {
            verification.package_member
            for verification in verifications
            if verification.package_member is not None
        }
        modified = modified_sections | modified_images
        for source_info, output_info in zip(source_infos, output_infos):
            if (
                source_info.date_time != output_info.date_time
                or source_info.external_attr != output_info.external_attr
            ):
                raise HwpxExactWriterError(
                    "output_member_metadata_drift",
                    "output package member metadata changed",
                )
            if output_info.filename in modified:
                if output_info.compress_type != zipfile.ZIP_STORED:
                    raise HwpxExactWriterError(
                        "output_modified_member_compression_invalid",
                        "server-generated package members must be stored",
                    )
            elif source_info.compress_type != output_info.compress_type:
                raise HwpxExactWriterError(
                    "output_member_metadata_drift",
                    "untargeted package member compression changed",
                )
            if source_info.filename not in modified and (
                source_info.CRC != output_info.CRC
                or source_info.file_size != output_info.file_size
            ):
                raise HwpxExactWriterError(
                    "output_untargeted_member_changed",
                    "an untargeted package member changed",
                )
        for verification in verifications:
            if verification.package_member is None:
                continue
            info = next(
                (
                    candidate
                    for candidate in output_infos
                    if candidate.filename == verification.package_member
                ),
                None,
            )
            if info is None:
                raise HwpxExactWriterError(
                    "output_picture_member_missing",
                    "output picture member is missing",
                )
            payload = structure._read_member_bounded(
                output_archive,
                info,
                maximum=limits.max_evidence_bytes,
            )
            if (
                hashlib.sha256(payload).hexdigest()
                != verification.output_value_sha256
            ):
                raise HwpxExactWriterError(
                    "output_picture_hash_mismatch",
                    "output picture payload does not match the evidence hash",
                )

        source_infos_by_key = {
            _archive_key(info.filename): info for info in source_infos
        }
        output_infos_by_key = {
            _archive_key(info.filename): info for info in output_infos
        }
        source_roots: Dict[str, ElementTree.Element] = {}
        output_roots: Dict[str, ElementTree.Element] = {}

        def section_root(
            archive: zipfile.ZipFile,
            infos_by_key: Mapping[str, zipfile.ZipInfo],
            cache: Dict[str, ElementTree.Element],
            section_path: str,
            *,
            output_member: bool,
        ) -> ElementTree.Element:
            key = _archive_key(section_path)
            cached = cache.get(key)
            if cached is not None:
                return cached
            info = infos_by_key.get(key)
            if info is None:
                raise HwpxExactWriterError(
                    (
                        "output_paragraph_section_missing"
                        if output_member
                        else "source_paragraph_section_missing"
                    ),
                    "paragraph section is missing during reopen verification",
                )
            raw = structure._read_member_bounded(
                archive,
                info,
                maximum=limits.structure_limits.max_xml_entry_bytes,
            )
            root, _ = structure._parse_xml_bounded(
                raw,
                entry_name=section_path,
                limits=limits.structure_limits,
            )
            cache[key] = root
            return root

        verification_by_operation = {
            verification.operation_id: verification
            for verification in verifications
        }
        if len(verification_by_operation) != len(verifications):
            raise HwpxExactWriterError(
                "output_verification_duplicate",
                "writer produced duplicate operation verifications",
            )
        for operation in plan.operations:
            if not isinstance(
                operation,
                ExactHwpxParagraphTextOperation,
            ):
                continue
            verification = verification_by_operation.get(
                operation.operation_id
            )
            if (
                verification is None
                or verification.target_id != operation.target_id
                or verification.kind != operation.kind
                or verification.package_member is not None
            ):
                raise HwpxExactWriterError(
                    "output_paragraph_verification_missing",
                    "paragraph operation has no exact output verification",
                )
            expected_output_sha = _sha256_text(
                _normalize_paragraph_value(operation.value)
            )
            if verification.output_value_sha256 != expected_output_sha:
                raise HwpxExactWriterError(
                    "output_paragraph_hash_mismatch",
                    "paragraph verification hash does not match the plan",
                )
            source_root = section_root(
                source_archive,
                source_infos_by_key,
                source_roots,
                operation.address.section_path,
                output_member=False,
            )
            output_root = section_root(
                output_archive,
                output_infos_by_key,
                output_roots,
                operation.address.section_path,
                output_member=True,
            )
            source_resolved = _resolve_source_paragraph_operation(
                source_root,
                snapshot=snapshot,
                section_path=operation.address.section_path,
                operation=operation,
            )
            output_resolved = _resolve_paragraph_address(
                output_root,
                snapshot=snapshot,
                section_path=operation.address.section_path,
                operation=operation,
                output_mode=True,
            )
            if (
                source_resolved.protected_structure_sha256
                != output_resolved.protected_structure_sha256
                or source_resolved.untargeted_content_sha256
                != output_resolved.untargeted_content_sha256
            ):
                raise HwpxExactWriterError(
                    "output_paragraph_structure_drift",
                    (
                        "paragraph runs, tables, controls, or untargeted "
                        "content changed during edit"
                    ),
                )
            output_text = _normalize_paragraph_value(
                "".join(
                    element.text or ""
                    for element in output_resolved.selected_text_nodes
                )
            )
            if _sha256_text(output_text) != expected_output_sha:
                raise HwpxExactWriterError(
                    "output_paragraph_hash_mismatch",
                    "reopened paragraph text hash does not match the plan",
                )


def _structure_projection(
    snapshot: HwpxStructureSnapshot,
    *,
    ignored_text_targets: set[str],
    section_element_count_adjustments: Optional[
        Mapping[str, int]
    ] = None,
) -> Mapping[str, object]:
    adjustments = section_element_count_adjustments or {}
    cell_address_by_target = {
        cell.target_id: (
            cell.section_path,
            cell.table_index,
            cell.cell_index,
            cell.row,
            cell.column,
        )
        for table in snapshot.tables
        for cell in table.cells
    }
    return {
        "archive_entry_count": snapshot.archive_entry_count,
        "xml_entry_count": snapshot.xml_entry_count,
        "sections": [
            {
                "path": section.path,
                "section_index": section.section_index,
                "element_count": (
                    section.element_count
                    - int(adjustments.get(section.path, 0))
                ),
                "table_count": section.table_count,
                "cell_count": section.cell_count,
                "picture_count": section.picture_count,
                "explicit_page_break_count": (
                    section.explicit_page_break_count
                ),
                "named_field_count": section.named_field_count,
                "named_bookmark_count": section.named_bookmark_count,
            }
            for section in snapshot.sections
        ],
        "tables": [
            {
                "section_path": table.section_path,
                "section_index": table.section_index,
                "table_index": table.table_index,
                "xml_id": table.xml_id,
                "rows": table.rows,
                "columns": table.columns,
                "declared_rows": table.declared_rows,
                "declared_columns": table.declared_columns,
                "cells": [
                    {
                        "cell_index": cell.cell_index,
                        "row": cell.row,
                        "column": cell.column,
                        "row_span": cell.row_span,
                        "column_span": cell.column_span,
                        "address_source": cell.address_source,
                        "text_sha256": (
                            None
                            if cell.target_id in ignored_text_targets
                            else cell.text_sha256
                        ),
                    }
                    for cell in table.cells
                ],
            }
            for table in snapshot.tables
        ],
        "pictures": [
            {
                "section_path": picture.section_path,
                "section_index": picture.section_index,
                "picture_index": picture.picture_index,
                "xml_id": picture.xml_id,
                "binary_item_refs": list(picture.binary_item_refs),
                "nearest_table_index": picture.nearest_table_index,
                "nearest_cell_address": (
                    cell_address_by_target.get(
                        picture.nearest_cell_target_id
                    )
                    if picture.nearest_cell_target_id
                    else None
                ),
            }
            for picture in snapshot.pictures
        ],
        "page_breaks": [
            {
                "section_path": page_break.section_path,
                "section_index": page_break.section_index,
                "page_break_index": page_break.page_break_index,
                "source": page_break.source,
                "xml_id": page_break.xml_id,
                "nearest_table_index": (
                    next(
                        (
                            table.table_index
                            for table in snapshot.tables
                            if table.table_id
                            == page_break.nearest_table_id
                        ),
                        None,
                    )
                    if page_break.nearest_table_id
                    else None
                ),
                "nearest_cell_address": (
                    cell_address_by_target.get(
                        page_break.nearest_cell_target_id
                    )
                    if page_break.nearest_cell_target_id
                    else None
                ),
            }
            for page_break in snapshot.page_breaks
        ],
        "field_names": list(snapshot.field_names),
        "field_count": snapshot.field_count,
        "bookmark_names": list(snapshot.bookmark_names),
        "bookmark_count": snapshot.bookmark_count,
        "target_count": snapshot.target_count,
    }


def _validate_paragraph_operation(
    operation: ExactHwpxParagraphTextOperation,
    *,
    source_document_sha256: str,
    limits: ExactHwpxWriterLimits,
) -> None:
    address = operation.address
    if not isinstance(address, XmlParagraphAddress):
        raise HwpxExactWriterError(
            "paragraph_operation_invalid",
            "paragraph operation does not contain an audited XML address",
        )
    try:
        structure._validate_archive_member_name(
            address.section_path,
            limits.structure_limits.max_entry_name_chars,
        )
    except Exception as error:
        raise HwpxExactWriterError(
            "paragraph_operation_invalid",
            "paragraph section path is invalid",
        ) from error
    integer_fields = (
        address.section_index,
        address.paragraph_ordinal,
        address.section_child_index,
        address.direct_run_count,
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        for value in integer_fields
    ):
        raise HwpxExactWriterError(
            "paragraph_operation_invalid",
            "paragraph address contains an invalid integer field",
        )
    if address.direct_run_count < 1:
        raise HwpxExactWriterError(
            "paragraph_operation_invalid",
            "paragraph address must preserve at least one direct run",
        )
    if (
        not isinstance(address.page_break, bool)
        or not isinstance(address.protected_structure_present, bool)
        or not isinstance(address.recursive_lineseg_removal_forbidden, bool)
        or not isinstance(address.run_deletion_forbidden, bool)
        or address.lineseg_policy
        != "remove_direct_linesegarray_from_this_p_only"
        or not address.recursive_lineseg_removal_forbidden
        or not address.run_deletion_forbidden
    ):
        raise HwpxExactWriterError(
            "paragraph_operation_invalid",
            "paragraph protected-edit policy is invalid",
        )
    if (
        not isinstance(address.xml_child_path, tuple)
        or not address.xml_child_path
        or address.xml_child_path[0] != address.section_child_index
        or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for value in address.xml_child_path
        )
    ):
        raise HwpxExactWriterError(
            "paragraph_operation_invalid",
            "paragraph XML child path is invalid",
        )
    if (
        not isinstance(address.selected_text_run_ordinals, tuple)
        or not isinstance(address.selected_text_run_paths, tuple)
        or not address.selected_text_run_ordinals
        or len(address.selected_text_run_ordinals)
        != len(address.selected_text_run_paths)
        or len(set(address.selected_text_run_ordinals))
        != len(address.selected_text_run_ordinals)
        or len(set(address.selected_text_run_paths))
        != len(address.selected_text_run_paths)
        or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for value in address.selected_text_run_ordinals
        )
        or any(
            not isinstance(path, tuple)
            or not path
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for value in path
            )
            for path in address.selected_text_run_paths
        )
    ):
        raise HwpxExactWriterError(
            "paragraph_operation_invalid",
            "paragraph selected text-run addresses are invalid",
        )
    if (
        not isinstance(address.direct_linesegarray_child_indexes, tuple)
        or not isinstance(address.direct_linesegarray_sha256s, tuple)
        or not address.direct_linesegarray_child_indexes
        or len(address.direct_linesegarray_child_indexes)
        != len(address.direct_linesegarray_sha256s)
        or len(set(address.direct_linesegarray_child_indexes))
        != len(address.direct_linesegarray_child_indexes)
        or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for value in address.direct_linesegarray_child_indexes
        )
        or any(
            not isinstance(value, str)
            or _SHA256.fullmatch(value) is None
            for value in address.direct_linesegarray_sha256s
        )
    ):
        raise HwpxExactWriterError(
            "paragraph_operation_invalid",
            "paragraph direct linesegarray address set is invalid",
        )
    hash_fields = (
        address.selected_text_sha256,
        address.paragraph_attributes_sha256,
        address.run_structure_sha256,
        address.address_sha256,
    )
    if any(
        not isinstance(value, str) or _SHA256.fullmatch(value) is None
        for value in hash_fields
    ):
        raise HwpxExactWriterError(
            "paragraph_operation_invalid",
            "paragraph audit hash is invalid",
        )
    if (
        address.paragraph_xml_id is not None
        and (
            not isinstance(address.paragraph_xml_id, str)
            or len(address.paragraph_xml_id) > 256
            or _CONTROL.search(address.paragraph_xml_id)
        )
    ):
        raise HwpxExactWriterError(
            "paragraph_operation_invalid",
            "paragraph XML ID is invalid",
        )
    _validate_text(address.selected_text, limits)
    _validate_text(operation.expected_current_value, limits)
    _validate_text(operation.value, limits)

    normalized_address_text = _normalize_paragraph_value(
        address.selected_text
    )
    normalized_expected = _normalize_paragraph_value(
        operation.expected_current_value
    )
    address_payload = {
        "document_sha256": source_document_sha256,
        "section_path": address.section_path,
        "section_index": address.section_index,
        "paragraph_ordinal": address.paragraph_ordinal,
        "xml_child_path": address.xml_child_path,
        "selected_text_sha256": address.selected_text_sha256,
    }
    if (
        address.selected_text_sha256
        != _sha256_text(normalized_address_text)
        or operation.expected_current_sha256
        != _sha256_text(normalized_expected)
        or normalized_address_text != normalized_expected
        or address.address_sha256 != _canonical_sha256(address_payload)
        or operation.target_id
        != _paragraph_target_id(source_document_sha256, address)
    ):
        raise HwpxExactWriterError(
            "paragraph_operation_stale_contract",
            "paragraph target, expected text, or audit hash is stale",
        )


def _validate_plan(
    plan: ExactHwpxWritePlan,
    limits: ExactHwpxWriterLimits,
) -> None:
    if plan.schema_version != HWPX_EXACT_WRITER_SCHEMA:
        raise HwpxExactWriterError(
            "plan_schema_invalid",
            "exact writer plan schema is unsupported",
        )
    if _OPERATION_ID.fullmatch(plan.plan_id) is None:
        raise HwpxExactWriterError(
            "plan_id_invalid",
            "plan ID is invalid",
        )
    if (
        _SHA256.fullmatch(plan.source_document_sha256) is None
        or _SHA256.fullmatch(plan.source_target_summary_sha256) is None
    ):
        raise HwpxExactWriterError(
            "plan_hash_invalid",
            "plan source hashes are invalid",
        )
    if not plan.incident_source_refs or len(
        plan.incident_source_refs
    ) != len(set(plan.incident_source_refs)):
        raise HwpxExactWriterError(
            "incident_refs_invalid",
            "incident source references must be non-empty and unique",
        )
    if any(
        _INCIDENT_REF.fullmatch(ref) is None
        for ref in plan.incident_source_refs
    ):
        raise HwpxExactWriterError(
            "incident_refs_invalid",
            "incident source references must be opaque incident refs",
        )
    if not plan.operations or len(plan.operations) > limits.max_operations:
        raise HwpxExactWriterError(
            "operation_capacity_exceeded",
            "operation count is empty or exceeds the configured limit",
        )
    operation_ids = [operation.operation_id for operation in plan.operations]
    targets = [operation.target_id for operation in plan.operations]
    if len(operation_ids) != len(set(operation_ids)):
        raise HwpxExactWriterError(
            "duplicate_operation_id",
            "operation IDs must be unique",
        )
    if len(targets) != len(set(targets)):
        raise HwpxExactWriterError(
            "duplicate_target",
            "one exact target cannot appear twice in a plan",
        )
    paragraph_addresses = [
        (
            _archive_key(operation.address.section_path),
            operation.address.section_index,
            operation.address.paragraph_ordinal,
            operation.address.xml_child_path,
        )
        for operation in plan.operations
        if isinstance(operation, ExactHwpxParagraphTextOperation)
        and isinstance(operation.address, XmlParagraphAddress)
    ]
    if len(paragraph_addresses) != len(set(paragraph_addresses)):
        raise HwpxExactWriterError(
            "duplicate_paragraph_address",
            "one audited paragraph cannot appear twice in a plan",
        )
    for operation in plan.operations:
        if (
            _OPERATION_ID.fullmatch(operation.operation_id) is None
            or _TARGET_ID.fullmatch(operation.target_id) is None
        ):
            raise HwpxExactWriterError(
                "operation_identity_invalid",
                "operation ID or exact target ID is invalid",
            )
        if operation.source_ref not in plan.incident_source_refs:
            raise HwpxExactWriterError(
                "cross_incident_source_ref",
                "operation source ref is not part of the current Incident set",
            )
        if _SHA256.fullmatch(operation.expected_current_sha256) is None:
            raise HwpxExactWriterError(
                "operation_hash_invalid",
                "operation expected SHA-256 is invalid",
            )
        if isinstance(operation, ExactHwpxTextOperation):
            if not operation.target_id.startswith("hwpx-cell-"):
                raise HwpxExactWriterError(
                    "operation_target_kind_invalid",
                    "cell text operation must use a cell target",
                )
            _validate_text(operation.expected_current_value, limits)
            _validate_text(operation.value, limits)
        elif isinstance(operation, ExactHwpxParagraphTextOperation):
            if not operation.target_id.startswith("hwpx-paragraph-"):
                raise HwpxExactWriterError(
                    "operation_target_kind_invalid",
                    "paragraph text operation must use a paragraph target",
                )
            _validate_paragraph_operation(
                operation,
                source_document_sha256=plan.source_document_sha256,
                limits=limits,
            )
        elif isinstance(operation, ExactHwpxPictureOperation):
            if not operation.target_id.startswith("hwpx-picture-"):
                raise HwpxExactWriterError(
                    "operation_target_kind_invalid",
                    "picture operation must use a picture target",
                )
            if (
                not operation.expected_binary_item_ref
                or len(operation.expected_binary_item_ref) > 256
                or _CONTROL.search(operation.expected_binary_item_ref)
                or _SHA256.fullmatch(operation.evidence_sha256) is None
            ):
                raise HwpxExactWriterError(
                    "picture_operation_invalid",
                    "picture operation binding or evidence hash is invalid",
                )
        else:
            raise HwpxExactWriterError(
                "unsupported_operation",
                "plan contains an unsupported operation object",
            )


def _validate_text(
    value: str,
    limits: ExactHwpxWriterLimits,
) -> None:
    if not isinstance(value, str) or len(value) > limits.max_text_chars:
        raise HwpxExactWriterError(
            "text_capacity_exceeded",
            "text value exceeds the configured character limit",
        )
    if _CONTROL.search(value):
        raise HwpxExactWriterError(
            "text_control_character",
            "text value contains prohibited control characters",
        )


def _absolute_path(
    value: Union[str, os.PathLike],
    *,
    label: str,
    suffix: Optional[str],
) -> Path:
    try:
        path = Path(value)
    except (TypeError, ValueError) as error:
        raise HwpxExactWriterError(
            f"{label}_path_invalid",
            f"{label} path is invalid",
        ) from error
    if not path.is_absolute() or any(part == ".." for part in path.parts):
        raise HwpxExactWriterError(
            f"{label}_path_invalid",
            f"{label} path must be absolute and traversal-free",
        )
    if suffix is not None and path.suffix.casefold() != suffix:
        raise HwpxExactWriterError(
            f"{label}_path_invalid",
            f"{label} path must use the {suffix} extension",
        )
    return path


def _validate_output_path(source: Path, output: Path) -> None:
    if os.path.normcase(str(source)) == os.path.normcase(str(output)):
        raise HwpxExactWriterError(
            "source_overwrite_forbidden",
            "output must not be the source HWPX",
        )
    if output.exists():
        raise HwpxExactWriterError(
            "output_exists",
            "exact writer never overwrites an existing output",
        )
    parent = output.parent
    structure._assert_no_link_or_reparse_components(parent)
    try:
        parent_stat = os.lstat(str(parent))
    except OSError as error:
        raise HwpxExactWriterError(
            "output_parent_missing",
            "output parent directory does not exist",
        ) from error
    if not stat.S_ISDIR(parent_stat.st_mode):
        raise HwpxExactWriterError(
            "output_parent_invalid",
            "output parent must be a directory",
        )


def _create_output_exclusive(path: Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    try:
        return os.open(str(path), flags, 0o600)
    except FileExistsError as error:
        raise HwpxExactWriterError(
            "output_exists",
            "exact writer never overwrites an existing output",
        ) from error
    except OSError as error:
        raise HwpxExactWriterError(
            "output_create_failed",
            "output HWPX could not be created exclusively",
        ) from error


def _remove_owned_output(path: Path) -> None:
    try:
        if path.exists() and path.is_file():
            path.unlink()
    except OSError:
        # The original exception is more actionable.  A later cleanup job can
        # remove a writer-owned failed artifact if Windows still holds it.
        pass


def _clone_zip_info(source: zipfile.ZipInfo) -> zipfile.ZipInfo:
    clone = zipfile.ZipInfo(source.filename, date_time=source.date_time)
    clone.compress_type = source.compress_type
    clone.comment = source.comment
    clone.extra = source.extra
    clone.create_system = source.create_system
    clone.create_version = source.create_version
    clone.extract_version = source.extract_version
    clone.reserved = source.reserved
    clone.flag_bits = source.flag_bits & ~0x1
    clone.volume = source.volume
    clone.internal_attr = source.internal_attr
    clone.external_attr = source.external_attr
    return clone


def _direct_table_cells(
    table: ElementTree.Element,
) -> List[ElementTree.Element]:
    cells: List[ElementTree.Element] = []
    for row in list(table):
        if _local_name(row.tag) != "tr":
            continue
        cells.extend(
            child
            for child in list(row)
            if _local_name(child.tag) == "tc"
        )
    return cells


def _normalized_cell_text(cell: ElementTree.Element) -> str:
    fragments: List[str] = []

    def visit(element: ElementTree.Element) -> None:
        for child in list(element):
            local = _local_name(child.tag)
            if local == "tbl":
                continue
            if local == "t":
                fragments.append("".join(child.itertext()))
                continue
            visit(child)

    visit(cell)
    return _normalize_cell_value(" ".join(fragments))


def _normalize_cell_value(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def _replace_self_closing_text_tag(
    raw_tag: bytes,
    replacement: bytes,
) -> bytes:
    """Expand one existing self-closing t element only when text is inserted."""

    if not replacement:
        return raw_tag
    match = re.match(br"<([A-Za-z_][A-Za-z0-9_.:-]*)", raw_tag)
    closing_index = raw_tag.rfind(b"/>")
    if (
        match is None
        or closing_index < match.end()
        or raw_tag[closing_index + 2 :].strip()
    ):
        raise HwpxExactWriterError(
            "self_closing_text_tag_invalid",
            "an existing self-closing text element cannot be expanded safely",
        )
    qualified_name = match.group(1)
    opening = raw_tag[:closing_index] + b">"
    return (
        opening
        + replacement
        + b"</"
        + qualified_name
        + b">"
    )


def _replace_self_closing_run_tag(
    raw_tag: bytes,
    replacement: bytes,
    *,
    text_qname: bytes,
) -> bytes:
    """Use one existing empty formatted run as the exact text carrier.

    A zero-length ``raw_tag`` represents insertion immediately before the
    closing tag of an already paired, childless run.  Otherwise the exact
    self-closing run tag is expanded while its qualified name and all
    attributes stay byte-for-byte unchanged.
    """

    if not replacement:
        return raw_tag
    if re.fullmatch(
        br"[A-Za-z_][A-Za-z0-9_.:-]*",
        text_qname,
    ) is None or not text_qname.endswith(b"t"):
        raise HwpxExactWriterError(
            "empty_run_text_tag_invalid",
            "an exact empty run has an invalid text child name",
        )
    text_element = (
        b"<"
        + text_qname
        + b">"
        + replacement
        + b"</"
        + text_qname
        + b">"
    )
    if not raw_tag:
        return text_element
    match = re.match(br"<([A-Za-z_][A-Za-z0-9_.:-]*)", raw_tag)
    closing_index = raw_tag.rfind(b"/>")
    if (
        match is None
        or closing_index < match.end()
        or raw_tag[closing_index + 2 :].strip()
        or not match.group(1).endswith(b"run")
    ):
        raise HwpxExactWriterError(
            "self_closing_run_tag_invalid",
            "an exact self-closing run cannot be expanded safely",
        )
    run_qname = match.group(1)
    run_prefix = (
        run_qname.rsplit(b":", 1)[0] + b":"
        if b":" in run_qname
        else b""
    )
    text_prefix = (
        text_qname.rsplit(b":", 1)[0] + b":"
        if b":" in text_qname
        else b""
    )
    if run_prefix != text_prefix:
        raise HwpxExactWriterError(
            "empty_run_namespace_mismatch",
            "the exact run and inserted text child use different prefixes",
        )
    opening = raw_tag[:closing_index] + b">"
    return opening + text_element + b"</" + run_qname + b">"


def _xml_text_bytes(
    value: str,
    limits: ExactHwpxWriterLimits,
) -> bytes:
    _validate_text(value, limits)
    return html.escape(
        unicodedata.normalize("NFC", value),
        quote=False,
    ).encode("utf-8")


def _assert_disjoint_patches(
    patches: Sequence[Tuple[int, int, bytes]],
) -> None:
    ordered = sorted(patches, key=lambda row: (row[0], row[1]))
    previous_end = -1
    for start, end, _replacement in ordered:
        if start < previous_end or end < start:
            raise HwpxExactWriterError(
                "text_patch_overlap",
                "target text byte ranges overlap or are invalid",
            )
        previous_end = end


def _attribute(
    element: ElementTree.Element,
    local_name: str,
) -> Optional[str]:
    wanted = local_name.casefold()
    for key, value in element.attrib.items():
        if _local_name(key) == wanted:
            return value
    return None


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].casefold()


def _archive_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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


def _image_limits(
    limits: ExactHwpxWriterLimits,
) -> HwpxWeakCaseLimits:
    return HwpxWeakCaseLimits(
        structure_limits=limits.structure_limits,
        max_image_bytes=limits.max_evidence_bytes,
        max_total_image_bytes=limits.max_total_evidence_bytes,
    )


__all__ = [
    "HWPX_EXACT_WRITER_SCHEMA",
    "ExactHwpxParagraphTextOperation",
    "ExactHwpxPictureOperation",
    "ExactHwpxTargetVerification",
    "ExactHwpxTextOperation",
    "ExactHwpxWritePlan",
    "ExactHwpxWriteResult",
    "ExactHwpxWriterLimits",
    "ExactSampleWriterContract",
    "HwpxExactWriterError",
    "inspect_exact_sample_writer_contract",
    "write_exact_hwpx",
]
