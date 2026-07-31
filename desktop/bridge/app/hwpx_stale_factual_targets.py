"""Read-only audit of stale factual targets omitted by the report binder.

The audited 3Q/4Q HWPX samples are completed historical reports.  Reusing
their layout therefore requires more than replacing incident photos,
captions, route/date cells, opinions, and evaluation markers.  Five additional
historical surfaces exist in every one of the 24 page pairs:

* the group cell in the title table;
* the route-bearing title cell;
* the inspector cell;
* the photo-page ``□ ... 현황사진`` paragraph; and
* the signature picture embedded in the inspector cell.

This module maps those surfaces without writing a document, extracting an
archive member, invoking COM, or accepting renderer-supplied addresses.  It
also records the minimum ``hp:p`` that owns every text run.  A later exact
writer must remove only that paragraph's *direct* ``linesegarray`` children
after changing text; it must never delete title/complex runs or recurse into
unmodified child paragraphs.

The custom standalone-paragraph target ID is deterministic over the exact
document SHA-256, section address, paragraph ordinal, and expected text hash.
Cell and picture targets reuse the structure inspector's exact IDs.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple, Union
from xml.etree import ElementTree

from . import hwpx_structure as structure
from .hwpx_sample_layout import (
    EXPECTED_PAGE_PAIR_COUNT,
    SampleLayoutInspection,
    inspect_sample_layout,
)
from .hwpx_structure import (
    CellSnapshot,
    HwpxFormatError,
    HwpxLimitExceeded,
    HwpxSecurityError,
    HwpxStructureLimits,
    PictureSnapshot,
)


HWPX_STALE_FACTUAL_TARGET_SCHEMA = (
    "trafficvisson.hwpx-stale-factual-target-audit.v1"
)
HWPX_STALE_FACTUAL_COVERAGE_SCHEMA = (
    "trafficvisson.hwpx-stale-factual-target-coverage.v1"
)
_PARAGRAPH_TARGET_PREFIX = "hwpx-paragraph-"
_PHOTO_HEADER_TERM = "현황사진"
_MAX_MANIFEST_BYTES = 2 * 1024**2
_MAX_SIGNATURE_BINARY_BYTES = 64 * 1024**2
_MAX_TOTAL_SIGNATURE_BINARY_BYTES = 512 * 1024**2


class HwpxStaleFactualTargetError(RuntimeError):
    """Fail-closed stale-target audit or coverage failure."""

    provider_called = False
    com_invoked = False
    writer_invoked = False
    apply_blocked = True

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class XmlParagraphAddress:
    """Exact source address and minimal-layout-cache contract for one hp:p."""

    section_path: str
    section_index: int
    paragraph_ordinal: int
    section_child_index: int
    xml_child_path: Tuple[int, ...]
    paragraph_xml_id: Optional[str]
    page_break: bool
    selected_text_run_ordinals: Tuple[int, ...]
    selected_text_run_paths: Tuple[Tuple[int, ...], ...]
    selected_text: str
    selected_text_sha256: str
    paragraph_attributes_sha256: str
    direct_linesegarray_child_indexes: Tuple[int, ...]
    direct_linesegarray_sha256s: Tuple[str, ...]
    direct_run_count: int
    run_structure_sha256: str
    protected_structure_present: bool
    address_sha256: str
    lineseg_policy: str = "remove_direct_linesegarray_from_this_p_only"
    recursive_lineseg_removal_forbidden: bool = True
    run_deletion_forbidden: bool = True


@dataclass(frozen=True)
class FactualCellTarget:
    role: str
    target_id: str
    page_pair_index: int
    page_number: int
    table_id: str
    table_index: int
    cell_index: int
    row: int
    column: int
    row_span: int
    column_span: int
    expected_text: str
    expected_text_sha256: str
    host_paragraphs: Tuple[XmlParagraphAddress, ...]
    operation_requirement: str = "replace_or_explicitly_neutralize"
    run_deletion_forbidden: bool = True


@dataclass(frozen=True)
class FactualParagraphTarget:
    role: str
    target_id: str
    page_pair_index: int
    page_number: int
    expected_text: str
    expected_text_sha256: str
    address: XmlParagraphAddress
    descendant_table_indexes: Tuple[int, ...]
    operation_requirement: str = "replace_or_explicitly_neutralize"
    run_deletion_forbidden: bool = True


@dataclass(frozen=True)
class EmbeddedBinaryFingerprint:
    binary_item_ref: str
    package_member: str
    declared_media_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class FactualPictureTarget:
    role: str
    target_id: str
    page_pair_index: int
    page_number: int
    picture_index: int
    picture_xml_id: Optional[str]
    table_index: int
    cell_target_id: str
    cell_row: int
    cell_column: int
    binary_item_refs: Tuple[str, ...]
    binaries: Tuple[EmbeddedBinaryFingerprint, ...]
    host_paragraph: XmlParagraphAddress
    expected_binding_sha256: str
    operation_requirement: str = "replace_or_explicitly_neutralize"
    run_deletion_forbidden: bool = True


@dataclass(frozen=True)
class ExistingBinderRouteReference:
    """Inspection-route cell already covered by the current binder."""

    target_id: str
    page_pair_index: int
    page_number: int
    table_index: int
    expected_text: str
    expected_text_sha256: str


@dataclass(frozen=True)
class StaleFactualPagePair:
    page_pair_index: int
    inspection_page_number: int
    photo_page_number: int
    title_group: FactualCellTarget
    title_route_heading: FactualCellTarget
    inspection_inspector: FactualCellTarget
    photo_page_header: FactualParagraphTarget
    inspector_signature: FactualPictureTarget
    existing_binder_route: ExistingBinderRouteReference
    required_target_ids: Tuple[str, ...]
    complete_route_surface_target_ids: Tuple[str, ...]
    page_pair_manifest_sha256: str


@dataclass(frozen=True)
class SharedSignatureBinaryGroup:
    binary_sha256: str
    package_members: Tuple[str, ...]
    binary_item_refs: Tuple[str, ...]
    picture_target_ids: Tuple[str, ...]
    page_pair_indices: Tuple[int, ...]


@dataclass(frozen=True)
class StaleFactualTargetCounts:
    page_pair_count: int
    required_target_count: int
    cell_target_count: int
    paragraph_target_count: int
    picture_target_count: int
    title_group_count: int
    title_route_heading_count: int
    inspector_cell_count: int
    photo_page_header_count: int
    inspector_signature_picture_count: int
    existing_binder_route_reference_count: int
    complete_route_surface_count: int
    unique_signature_binary_count: int
    unclassified_external_factual_paragraph_count: int
    unclassified_non_photo_picture_count: int


@dataclass(frozen=True)
class HwpxStaleFactualTargetAudit:
    schema_version: str
    template_id: str
    quarter: int
    document_name: str
    document_snapshot_id: str
    document_sha256: str
    structure_target_summary_sha256: str
    counts: StaleFactualTargetCounts
    page_pairs: Tuple[StaleFactualPagePair, ...]
    required_target_ids: Tuple[str, ...]
    signature_binary_groups: Tuple[SharedSignatureBinaryGroup, ...]
    audit_sha256: str
    text_edit_policy: str = (
        "replace_exact_t_nodes_and_remove_only_host_p_direct_linesegarray"
    )
    title_and_complex_run_deletion_forbidden: bool = True
    pre_save_full_residual_scan_required: bool = True
    residual_scan_mode: str = (
        "exact_target_address_expected_value_and_signature_binding"
    )
    complete_page_pair_coverage: bool = True
    read_only: bool = True
    no_archive_extraction: bool = True
    no_provider: bool = True
    no_com: bool = True
    no_document_write: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StaleFactualTargetCoverage:
    schema_version: str
    document_snapshot_id: str
    required_target_count: int
    supplied_operation_count: int
    unique_supplied_target_count: int
    covered_target_count: int
    missing_target_ids: Tuple[str, ...]
    duplicate_target_ids: Tuple[str, ...]
    unknown_target_ids: Tuple[str, ...]
    stale_route_surface_target_ids: Tuple[str, ...]
    stale_inspector_target_ids: Tuple[str, ...]
    stale_signature_picture_target_ids: Tuple[str, ...]
    complete: bool
    pre_save_full_residual_scan_required: bool = True


@dataclass(frozen=True)
class _ManifestItem:
    item_id: str
    package_member: str
    declared_media_type: str


@dataclass(frozen=True)
class _ParsedPackage:
    roots_by_section: Mapping[str, ElementTree.Element]
    binaries_by_ref: Mapping[str, EmbeddedBinaryFingerprint]


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _local_name(value: object) -> str:
    return structure._local_name(value).casefold()  # type: ignore[attr-defined]


def _xml_attribute(
    element: ElementTree.Element,
    name: str,
) -> Optional[str]:
    wanted = name.casefold()
    matches = [
        value
        for key, value in element.attrib.items()
        if _local_name(key) == wanted
    ]
    if len(matches) > 1:
        raise HwpxStaleFactualTargetError(
            "duplicate_local_xml_attribute",
            "an audited HWPX element has duplicate local attribute names",
        )
    return matches[0] if matches else None


def _archive_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _manifest_items(
    raw: bytes,
    *,
    limits: HwpxStructureLimits,
) -> Mapping[str, _ManifestItem]:
    structure._reject_dtd_or_entities(raw)  # type: ignore[attr-defined]
    root, _ = structure._parse_xml_bounded(  # type: ignore[attr-defined]
        raw,
        entry_name="Contents/content.hpf",
        limits=limits,
    )
    items: Dict[str, _ManifestItem] = {}
    for element in root.iter():
        if _local_name(element.tag) != "item":
            continue
        item_id = _xml_attribute(element, "id")
        href = _xml_attribute(element, "href")
        media_type = _xml_attribute(element, "media-type")
        if not item_id or not href or not media_type:
            raise HwpxStaleFactualTargetError(
                "signature_manifest_item_invalid",
                "a signature manifest item is incomplete",
            )
        if item_id in items:
            raise HwpxStaleFactualTargetError(
                "signature_manifest_item_duplicate",
                "the HWPX manifest contains a duplicate item ID",
            )
        try:
            package_member = structure._validate_archive_member_name(  # type: ignore[attr-defined]
                href,
                limits.max_entry_name_chars,
            )
        except (HwpxLimitExceeded, HwpxSecurityError) as error:
            raise HwpxStaleFactualTargetError(
                "signature_manifest_member_unsafe",
                "a signature manifest member path is unsafe",
            ) from error
        items[item_id] = _ManifestItem(
            item_id=item_id,
            package_member=package_member,
            declared_media_type=media_type.casefold(),
        )
    return items


def _read_exact_package(
    source: Path,
    inspection: SampleLayoutInspection,
    *,
    signature_refs: Sequence[str],
) -> _ParsedPackage:
    limits = HwpxStructureLimits()
    try:
        with source.open("rb") as stream:
            with zipfile.ZipFile(stream, mode="r") as archive:
                infos = archive.infolist()
                normalized_infos = structure._validate_archive_entries(  # type: ignore[attr-defined]
                    infos,
                    limits,
                )
                roots: Dict[str, ElementTree.Element] = {}
                for section in inspection.snapshot.sections:
                    info = normalized_infos.get(_archive_key(section.path))
                    if info is None:
                        raise HwpxStaleFactualTargetError(
                            "section_disappeared",
                            "an audited HWPX section disappeared",
                        )
                    raw = structure._read_member_bounded(  # type: ignore[attr-defined]
                        archive,
                        info,
                        maximum=limits.max_xml_entry_bytes,
                    )
                    structure._reject_dtd_or_entities(raw)  # type: ignore[attr-defined]
                    root, _ = structure._parse_xml_bounded(  # type: ignore[attr-defined]
                        raw,
                        entry_name=section.path,
                        limits=limits,
                    )
                    roots[section.path] = root

                manifest_info = normalized_infos.get(
                    _archive_key("Contents/content.hpf")
                )
                if manifest_info is None:
                    raise HwpxStaleFactualTargetError(
                        "signature_manifest_missing",
                        "the audited HWPX content manifest is missing",
                    )
                manifest_raw = structure._read_member_bounded(  # type: ignore[attr-defined]
                    archive,
                    manifest_info,
                    maximum=_MAX_MANIFEST_BYTES,
                )
                manifest = _manifest_items(
                    manifest_raw,
                    limits=limits,
                )
                binaries: Dict[str, EmbeddedBinaryFingerprint] = {}
                total_bytes = 0
                for item_ref in sorted(set(signature_refs)):
                    item = manifest.get(item_ref)
                    if item is None:
                        raise HwpxStaleFactualTargetError(
                            "signature_binary_ref_unresolved",
                            "an inspector signature binary ref is unresolved",
                        )
                    info = normalized_infos.get(
                        _archive_key(item.package_member)
                    )
                    if info is None or info.is_dir():
                        raise HwpxStaleFactualTargetError(
                            "signature_binary_member_missing",
                            "an inspector signature package member is missing",
                        )
                    payload = structure._read_member_bounded(  # type: ignore[attr-defined]
                        archive,
                        info,
                        maximum=_MAX_SIGNATURE_BINARY_BYTES,
                    )
                    total_bytes += len(payload)
                    if total_bytes > _MAX_TOTAL_SIGNATURE_BINARY_BYTES:
                        raise HwpxStaleFactualTargetError(
                            "signature_binary_total_limit_exceeded",
                            "inspector signature bytes exceed the audit limit",
                        )
                    binaries[item_ref] = EmbeddedBinaryFingerprint(
                        binary_item_ref=item_ref,
                        package_member=item.package_member,
                        declared_media_type=item.declared_media_type,
                        size_bytes=len(payload),
                        sha256=hashlib.sha256(payload).hexdigest(),
                    )
    except HwpxStaleFactualTargetError:
        raise
    except (
        HwpxFormatError,
        HwpxLimitExceeded,
        HwpxSecurityError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        OSError,
    ) as error:
        raise HwpxStaleFactualTargetError(
            "stale_factual_package_read_failed",
            "the audited HWPX package could not be read safely",
        ) from error

    after = inspect_sample_layout(source)
    if (
        after.snapshot.document_sha256
        != inspection.snapshot.document_sha256
        or after.snapshot.target_summary_sha256
        != inspection.snapshot.target_summary_sha256
        or after.document_snapshot_id != inspection.document_snapshot_id
    ):
        raise HwpxStaleFactualTargetError(
            "sample_changed_during_stale_target_audit",
            "the audited HWPX source changed during stale-target mapping",
        )
    return _ParsedPackage(
        roots_by_section=roots,
        binaries_by_ref=binaries,
    )


def _direct_table_cells(
    table: ElementTree.Element,
) -> Tuple[ElementTree.Element, ...]:
    cells = []
    for row in list(table):
        if _local_name(row.tag) != "tr":
            continue
        cells.extend(
            child
            for child in list(row)
            if _local_name(child.tag) == "tc"
        )
    return tuple(cells)


def _element_path(
    root: ElementTree.Element,
    element: ElementTree.Element,
    parent_map: Mapping[ElementTree.Element, ElementTree.Element],
) -> Tuple[int, ...]:
    reverse = []
    current = element
    while current is not root:
        parent = parent_map.get(current)
        if parent is None:
            raise HwpxStaleFactualTargetError(
                "xml_address_unresolved",
                "an exact HWPX XML element address is unresolved",
            )
        reverse.append(list(parent).index(current))
        current = parent
    return tuple(reversed(reverse))


def _nearest_paragraph(
    element: ElementTree.Element,
    parent_map: Mapping[ElementTree.Element, ElementTree.Element],
) -> ElementTree.Element:
    current = element
    while current in parent_map:
        current = parent_map[current]
        if _local_name(current.tag) == "p":
            return current
    raise HwpxStaleFactualTargetError(
        "host_paragraph_unresolved",
        "an exact factual target has no host paragraph",
    )


def _selected_text_outside_tables(
    paragraph: ElementTree.Element,
    parent_map: Mapping[ElementTree.Element, ElementTree.Element],
) -> Tuple[ElementTree.Element, ...]:
    selected = []
    for element in paragraph.iter():
        if _local_name(element.tag) != "t" or element.text is None:
            continue
        current = element
        inside_table = False
        while current is not paragraph:
            current = parent_map[current]
            if _local_name(current.tag) == "tbl":
                inside_table = True
                break
        if not inside_table:
            selected.append(element)
    return tuple(selected)


def _paragraph_address(
    *,
    document_sha256: str,
    section_path: str,
    section_index: int,
    root: ElementTree.Element,
    paragraph: ElementTree.Element,
    selected_text_nodes: Sequence[ElementTree.Element],
    paragraph_ordinals: Mapping[ElementTree.Element, int],
    parent_map: Mapping[ElementTree.Element, ElementTree.Element],
) -> XmlParagraphAddress:
    paragraph_path = _element_path(root, paragraph, parent_map)
    if not paragraph_path:
        raise HwpxStaleFactualTargetError(
            "paragraph_address_invalid",
            "the section root itself cannot be a paragraph target",
        )
    text_elements = [
        element
        for element in paragraph.iter()
        if _local_name(element.tag) == "t"
    ]
    text_ordinals = []
    text_paths = []
    for selected in selected_text_nodes:
        try:
            text_ordinals.append(text_elements.index(selected))
        except ValueError as error:
            raise HwpxStaleFactualTargetError(
                "paragraph_text_address_invalid",
                "a selected text run is outside its host paragraph",
            ) from error
        text_paths.append(_element_path(paragraph, selected, parent_map))
    if not text_ordinals:
        raise HwpxStaleFactualTargetError(
            "paragraph_text_address_empty",
            "a factual paragraph target has no exact text run",
        )
    selected_text = "".join(element.text or "" for element in selected_text_nodes)
    selected_sha = _text_sha256(selected_text)
    children = list(paragraph)
    lineseg_rows = [
        (index, child)
        for index, child in enumerate(children)
        if _local_name(child.tag) == "linesegarray"
    ]
    run_rows = [
        child for child in children if _local_name(child.tag) == "run"
    ]
    run_structure = [
        {
            "run_index": index,
            "child_kinds": [
                _local_name(child.tag) for child in list(run)
            ],
            "attributes": sorted(
                (_local_name(key), value)
                for key, value in run.attrib.items()
            ),
        }
        for index, run in enumerate(run_rows)
    ]
    protected = any(
        kind not in {"t"}
        for row in run_structure
        for kind in row["child_kinds"]
    )
    attributes_payload = sorted(
        (_local_name(key), value)
        for key, value in paragraph.attrib.items()
    )
    address_payload = {
        "document_sha256": document_sha256,
        "section_path": section_path,
        "section_index": section_index,
        "paragraph_ordinal": paragraph_ordinals[paragraph],
        "xml_child_path": paragraph_path,
        "selected_text_sha256": selected_sha,
    }
    return XmlParagraphAddress(
        section_path=section_path,
        section_index=section_index,
        paragraph_ordinal=paragraph_ordinals[paragraph],
        section_child_index=paragraph_path[0],
        xml_child_path=paragraph_path,
        paragraph_xml_id=_xml_attribute(paragraph, "id"),
        page_break=(
            (_xml_attribute(paragraph, "pageBreak") or "0").casefold()
            in {"1", "true", "yes"}
        ),
        selected_text_run_ordinals=tuple(text_ordinals),
        selected_text_run_paths=tuple(text_paths),
        selected_text=selected_text,
        selected_text_sha256=selected_sha,
        paragraph_attributes_sha256=_canonical_sha256(attributes_payload),
        direct_linesegarray_child_indexes=tuple(
            index for index, _ in lineseg_rows
        ),
        direct_linesegarray_sha256s=tuple(
            hashlib.sha256(
                ElementTree.tostring(child, encoding="utf-8")
            ).hexdigest()
            for _, child in lineseg_rows
        ),
        direct_run_count=len(run_rows),
        run_structure_sha256=_canonical_sha256(run_structure),
        protected_structure_present=protected,
        address_sha256=_canonical_sha256(address_payload),
    )


def _host_paragraphs_for_cell(
    *,
    document_sha256: str,
    section_path: str,
    section_index: int,
    root: ElementTree.Element,
    cell_element: ElementTree.Element,
    paragraph_ordinals: Mapping[ElementTree.Element, int],
    parent_map: Mapping[ElementTree.Element, ElementTree.Element],
) -> Tuple[XmlParagraphAddress, ...]:
    grouped: Dict[ElementTree.Element, list[ElementTree.Element]] = {}
    for element in cell_element.iter():
        if (
            _local_name(element.tag) == "t"
            and element.text is not None
            and element.text != ""
        ):
            grouped.setdefault(
                _nearest_paragraph(element, parent_map),
                [],
            ).append(element)
    if not grouped:
        raise HwpxStaleFactualTargetError(
            "factual_cell_text_unresolved",
            "a factual cell has no exact text-bearing paragraph",
        )
    return tuple(
        _paragraph_address(
            document_sha256=document_sha256,
            section_path=section_path,
            section_index=section_index,
            root=root,
            paragraph=paragraph,
            selected_text_nodes=selected,
            paragraph_ordinals=paragraph_ordinals,
            parent_map=parent_map,
        )
        for paragraph, selected in sorted(
            grouped.items(),
            key=lambda item: paragraph_ordinals[item[0]],
        )
    )


def _factual_cell_target(
    *,
    role: str,
    cell: CellSnapshot,
    page_pair_index: int,
    page_number: int,
    root: ElementTree.Element,
    table_elements: Sequence[ElementTree.Element],
    paragraph_ordinals: Mapping[ElementTree.Element, int],
    parent_map: Mapping[ElementTree.Element, ElementTree.Element],
) -> FactualCellTarget:
    try:
        cell_element = _direct_table_cells(
            table_elements[cell.table_index]
        )[cell.cell_index]
    except IndexError as error:
        raise HwpxStaleFactualTargetError(
            "factual_cell_xml_address_changed",
            "the exact factual cell XML traversal changed",
        ) from error
    return FactualCellTarget(
        role=role,
        target_id=cell.target_id,
        page_pair_index=page_pair_index,
        page_number=page_number,
        table_id=cell.table_id,
        table_index=cell.table_index,
        cell_index=cell.cell_index,
        row=cell.row,
        column=cell.column,
        row_span=cell.row_span,
        column_span=cell.column_span,
        expected_text=cell.text_excerpt,
        expected_text_sha256=cell.text_sha256,
        host_paragraphs=_host_paragraphs_for_cell(
            document_sha256=cell.document_sha256,
            section_path=cell.section_path,
            section_index=cell.section_index,
            root=root,
            cell_element=cell_element,
            paragraph_ordinals=paragraph_ordinals,
            parent_map=parent_map,
        ),
    )


def _normalized_role_text(value: str) -> str:
    return " ".join(value.replace("：", ":").split()).replace(" :", ":")


def _one_prefixed_cell(table: Any, prefix: str) -> CellSnapshot:
    cells = tuple(
        cell
        for cell in table.cells
        if _normalized_role_text(cell.text_excerpt).startswith(prefix)
    )
    if len(cells) != 1:
        raise HwpxStaleFactualTargetError(
            "factual_role_cell_ambiguous",
            "an exact factual role cell is missing or ambiguous",
        )
    return cells[0]


def _title_cells(table: Any) -> Tuple[CellSnapshot, CellSnapshot]:
    group = tuple(
        cell
        for cell in table.cells
        if cell.row == 0 and cell.column == 0 and cell.text_excerpt.strip()
    )
    heading = tuple(
        cell
        for cell in table.cells
        if cell.row == 0 and cell.column == 2 and cell.text_excerpt.strip()
    )
    if (
        len(group) != 1
        or len(heading) != 1
        or not group[0].text_excerpt.strip().startswith("그룹")
        or "분기 일반평가" not in heading[0].text_excerpt
        or "양식(" not in heading[0].text_excerpt
    ):
        raise HwpxStaleFactualTargetError(
            "title_factual_cells_invalid",
            "the exact title group/route heading cells cannot be mapped",
        )
    return group[0], heading[0]


def _top_level_paragraph(
    element: ElementTree.Element,
    *,
    root: ElementTree.Element,
    parent_map: Mapping[ElementTree.Element, ElementTree.Element],
) -> ElementTree.Element:
    current = element
    candidate = None
    while current is not root:
        if _local_name(current.tag) == "p":
            candidate = current
        current = parent_map[current]
    if candidate is None or parent_map.get(candidate) is not root:
        raise HwpxStaleFactualTargetError(
            "top_level_paragraph_unresolved",
            "an audited table has no section-level host paragraph",
        )
    return candidate


def _map_photo_header_paragraphs(
    *,
    inspection: SampleLayoutInspection,
    root: ElementTree.Element,
    table_elements: Sequence[ElementTree.Element],
    paragraph_ordinals: Mapping[ElementTree.Element, int],
    parent_map: Mapping[ElementTree.Element, ElementTree.Element],
) -> Tuple[FactualParagraphTarget, ...]:
    section_path = inspection.snapshot.sections[0].path
    section_index = inspection.snapshot.sections[0].section_index
    top_level_paragraphs = tuple(
        child for child in list(root) if _local_name(child.tag) == "p"
    )
    top_level_positions = {
        paragraph: index
        for index, paragraph in enumerate(top_level_paragraphs)
    }
    candidates = []
    for paragraph in top_level_paragraphs:
        selected = _selected_text_outside_tables(
            paragraph,
            parent_map,
        )
        text = "".join(element.text or "" for element in selected)
        if text.strip() and _PHOTO_HEADER_TERM not in text:
            raise HwpxStaleFactualTargetError(
                "unclassified_external_factual_paragraph",
                "an external factual paragraph is outside the audited header map",
            )
        if _PHOTO_HEADER_TERM in text:
            candidates.append((paragraph, selected, text))
    if len(candidates) != EXPECTED_PAGE_PAIR_COUNT:
        raise HwpxStaleFactualTargetError(
            "photo_header_paragraph_coverage_invalid",
            "the 24 photo-page header paragraphs cannot be mapped",
        )

    targets = []
    used = set()
    for pair, (paragraph, selected, text) in enumerate(candidates):
        photo_table_index = pair * 3 + 2
        wrapper = _top_level_paragraph(
            table_elements[photo_table_index],
            root=root,
            parent_map=parent_map,
        )
        header_position = top_level_positions[paragraph]
        wrapper_position = top_level_positions[wrapper]
        if (
            header_position > wrapper_position
            or wrapper_position - header_position > 1
        ):
            raise HwpxStaleFactualTargetError(
                "photo_header_paragraph_sequence_invalid",
                "a photo-page header is not adjacent to its photo table",
            )
        address = _paragraph_address(
            document_sha256=inspection.snapshot.document_sha256,
            section_path=section_path,
            section_index=section_index,
            root=root,
            paragraph=paragraph,
            selected_text_nodes=selected,
            paragraph_ordinals=paragraph_ordinals,
            parent_map=parent_map,
        )
        target_payload = {
            "document_sha256": inspection.snapshot.document_sha256,
            "section_path": section_path,
            "section_index": section_index,
            "paragraph_ordinal": address.paragraph_ordinal,
            "text_sha256": address.selected_text_sha256,
        }
        target_id = (
            _PARAGRAPH_TARGET_PREFIX
            + _canonical_sha256(target_payload)[:24]
        )
        if target_id in used:
            raise HwpxStaleFactualTargetError(
                "duplicate_paragraph_target_id",
                "two photo-page headers resolve to one target ID",
            )
        used.add(target_id)
        descendant_tables = tuple(
            index
            for index, table in enumerate(table_elements)
            if paragraph is _top_level_paragraph(
                table,
                root=root,
                parent_map=parent_map,
            )
        )
        if descendant_tables not in {(), (photo_table_index,)}:
            raise HwpxStaleFactualTargetError(
                "photo_header_descendant_table_invalid",
                "a photo-page header contains an unexpected table",
            )
        targets.append(
            FactualParagraphTarget(
                role="photo_page_header",
                target_id=target_id,
                page_pair_index=pair,
                page_number=pair * 2 + 2,
                expected_text=text,
                expected_text_sha256=_text_sha256(text),
                address=address,
                descendant_table_indexes=descendant_tables,
            )
        )
    return tuple(targets)


def _signature_picture_target(
    *,
    picture: PictureSnapshot,
    inspector: CellSnapshot,
    page_pair_index: int,
    root: ElementTree.Element,
    picture_elements: Sequence[ElementTree.Element],
    paragraph_ordinals: Mapping[ElementTree.Element, int],
    parent_map: Mapping[ElementTree.Element, ElementTree.Element],
    binaries_by_ref: Mapping[str, EmbeddedBinaryFingerprint],
) -> FactualPictureTarget:
    if (
        picture.nearest_table_index != inspector.table_index
        or picture.nearest_cell_target_id != inspector.target_id
        or picture.nearest_cell_row != inspector.row
        or picture.nearest_cell_column != inspector.column
        or not picture.binary_item_refs
    ):
        raise HwpxStaleFactualTargetError(
            "inspector_signature_binding_invalid",
            "a fixed picture is not bound to the exact inspector cell",
        )
    try:
        picture_element = picture_elements[picture.picture_index]
    except IndexError as error:
        raise HwpxStaleFactualTargetError(
            "inspector_signature_xml_address_changed",
            "the signature picture XML traversal changed",
        ) from error
    paragraph = _nearest_paragraph(picture_element, parent_map)
    selected = tuple(
        element
        for element in paragraph.iter()
        if (
            _local_name(element.tag) == "t"
            and element.text is not None
            and element.text != ""
        )
    )
    if not selected:
        raise HwpxStaleFactualTargetError(
            "inspector_signature_host_text_missing",
            "the signature host paragraph has no inspector text",
        )
    host = _paragraph_address(
        document_sha256=picture.document_sha256,
        section_path=picture.section_path,
        section_index=picture.section_index,
        root=root,
        paragraph=paragraph,
        selected_text_nodes=selected,
        paragraph_ordinals=paragraph_ordinals,
        parent_map=parent_map,
    )
    binaries = tuple(
        binaries_by_ref[item_ref] for item_ref in picture.binary_item_refs
    )
    payload = {
        "target_id": picture.target_id,
        "picture_index": picture.picture_index,
        "binary_item_refs": picture.binary_item_refs,
        "binaries": [asdict(item) for item in binaries],
        "host_paragraph_address_sha256": host.address_sha256,
    }
    return FactualPictureTarget(
        role="inspector_signature",
        target_id=picture.target_id,
        page_pair_index=page_pair_index,
        page_number=page_pair_index * 2 + 1,
        picture_index=picture.picture_index,
        picture_xml_id=picture.xml_id,
        table_index=inspector.table_index,
        cell_target_id=inspector.target_id,
        cell_row=inspector.row,
        cell_column=inspector.column,
        binary_item_refs=picture.binary_item_refs,
        binaries=binaries,
        host_paragraph=host,
        expected_binding_sha256=_canonical_sha256(payload),
    )


def _signature_binary_groups(
    page_pairs: Sequence[StaleFactualPagePair],
) -> Tuple[SharedSignatureBinaryGroup, ...]:
    grouped: Dict[str, Dict[str, set[Any]]] = {}
    for pair in page_pairs:
        picture = pair.inspector_signature
        for binary in picture.binaries:
            row = grouped.setdefault(
                binary.sha256,
                {
                    "members": set(),
                    "refs": set(),
                    "targets": set(),
                    "pairs": set(),
                },
            )
            row["members"].add(binary.package_member)
            row["refs"].add(binary.binary_item_ref)
            row["targets"].add(picture.target_id)
            row["pairs"].add(pair.page_pair_index)
    return tuple(
        SharedSignatureBinaryGroup(
            binary_sha256=digest,
            package_members=tuple(sorted(row["members"])),
            binary_item_refs=tuple(sorted(row["refs"])),
            picture_target_ids=tuple(sorted(row["targets"])),
            page_pair_indices=tuple(sorted(row["pairs"])),
        )
        for digest, row in sorted(grouped.items())
    )


def audit_stale_factual_targets(
    source_path: Union[str, Path],
) -> HwpxStaleFactualTargetAudit:
    """Map all 24 omitted stale-factual target sets in one exact sample."""

    source = Path(source_path)
    inspection = inspect_sample_layout(source)
    if inspection.snapshot.section_count != 1:
        raise HwpxStaleFactualTargetError(
            "stale_factual_section_contract_invalid",
            "the audited sample must contain exactly one section",
        )
    fixed_pictures = tuple(
        picture
        for picture in inspection.snapshot.pictures
        if (
            picture.nearest_table_index is not None
            and picture.nearest_table_index % 3 == 1
        )
    )
    if len(fixed_pictures) != EXPECTED_PAGE_PAIR_COUNT:
        raise HwpxStaleFactualTargetError(
            "fixed_picture_coverage_invalid",
            "the 24 fixed inspector signature pictures cannot be mapped",
        )
    signature_refs = tuple(
        item_ref
        for picture in fixed_pictures
        for item_ref in picture.binary_item_refs
    )
    package = _read_exact_package(
        source,
        inspection,
        signature_refs=signature_refs,
    )
    section = inspection.snapshot.sections[0]
    root = package.roots_by_section[section.path]
    parent_map = {
        child: parent for parent in root.iter() for child in list(parent)
    }
    paragraphs = tuple(
        element for element in root.iter() if _local_name(element.tag) == "p"
    )
    paragraph_ordinals = {
        paragraph: index for index, paragraph in enumerate(paragraphs)
    }
    table_elements = tuple(
        element
        for element in root.iter()
        if _local_name(element.tag) == "tbl"
    )
    picture_elements = tuple(
        element
        for element in root.iter()
        if _local_name(element.tag) == "pic"
    )
    if (
        len(table_elements) != len(inspection.snapshot.tables)
        or len(picture_elements) != len(inspection.snapshot.pictures)
    ):
        raise HwpxStaleFactualTargetError(
            "stale_factual_xml_traversal_changed",
            "the audited table/picture XML traversal changed",
        )
    photo_headers = _map_photo_header_paragraphs(
        inspection=inspection,
        root=root,
        table_elements=table_elements,
        paragraph_ordinals=paragraph_ordinals,
        parent_map=parent_map,
    )
    fixed_by_pair = {
        picture.nearest_table_index // 3: picture
        for picture in fixed_pictures
        if picture.nearest_table_index is not None
    }
    if len(fixed_by_pair) != EXPECTED_PAGE_PAIR_COUNT:
        raise HwpxStaleFactualTargetError(
            "fixed_picture_pair_coverage_invalid",
            "the fixed signature pictures do not cover every page pair",
        )

    page_pairs = []
    all_required_ids = []
    for pair in range(EXPECTED_PAGE_PAIR_COUNT):
        title_table = inspection.snapshot.tables[pair * 3]
        detail_table = inspection.snapshot.tables[pair * 3 + 1]
        group_cell, heading_cell = _title_cells(title_table)
        inspector_cell = _one_prefixed_cell(detail_table, "점검자:")
        route_cell = _one_prefixed_cell(detail_table, "노선명:")
        inspection_page = pair * 2 + 1
        title_group = _factual_cell_target(
            role="title_group",
            cell=group_cell,
            page_pair_index=pair,
            page_number=inspection_page,
            root=root,
            table_elements=table_elements,
            paragraph_ordinals=paragraph_ordinals,
            parent_map=parent_map,
        )
        title_heading = _factual_cell_target(
            role="title_route_heading",
            cell=heading_cell,
            page_pair_index=pair,
            page_number=inspection_page,
            root=root,
            table_elements=table_elements,
            paragraph_ordinals=paragraph_ordinals,
            parent_map=parent_map,
        )
        inspector_target = _factual_cell_target(
            role="inspection_inspector",
            cell=inspector_cell,
            page_pair_index=pair,
            page_number=inspection_page,
            root=root,
            table_elements=table_elements,
            paragraph_ordinals=paragraph_ordinals,
            parent_map=parent_map,
        )
        signature_target = _signature_picture_target(
            picture=fixed_by_pair[pair],
            inspector=inspector_cell,
            page_pair_index=pair,
            root=root,
            picture_elements=picture_elements,
            paragraph_ordinals=paragraph_ordinals,
            parent_map=parent_map,
            binaries_by_ref=package.binaries_by_ref,
        )
        header_target = photo_headers[pair]
        required = (
            title_group.target_id,
            title_heading.target_id,
            inspector_target.target_id,
            header_target.target_id,
            signature_target.target_id,
        )
        if len(required) != len(set(required)):
            raise HwpxStaleFactualTargetError(
                "duplicate_page_pair_factual_target",
                "one page pair contains duplicate stale factual targets",
            )
        route_reference = ExistingBinderRouteReference(
            target_id=route_cell.target_id,
            page_pair_index=pair,
            page_number=inspection_page,
            table_index=route_cell.table_index,
            expected_text=route_cell.text_excerpt,
            expected_text_sha256=route_cell.text_sha256,
        )
        payload = {
            "page_pair_index": pair,
            "required_target_ids": required,
            "complete_route_surface_target_ids": (
                title_heading.target_id,
                route_reference.target_id,
                header_target.target_id,
            ),
            "title_group": asdict(title_group),
            "title_route_heading": asdict(title_heading),
            "inspection_inspector": asdict(inspector_target),
            "photo_page_header": asdict(header_target),
            "inspector_signature": asdict(signature_target),
            "existing_binder_route": asdict(route_reference),
        }
        page_pairs.append(
            StaleFactualPagePair(
                page_pair_index=pair,
                inspection_page_number=inspection_page,
                photo_page_number=pair * 2 + 2,
                title_group=title_group,
                title_route_heading=title_heading,
                inspection_inspector=inspector_target,
                photo_page_header=header_target,
                inspector_signature=signature_target,
                existing_binder_route=route_reference,
                required_target_ids=required,
                complete_route_surface_target_ids=(
                    title_heading.target_id,
                    route_reference.target_id,
                    header_target.target_id,
                ),
                page_pair_manifest_sha256=_canonical_sha256(payload),
            )
        )
        all_required_ids.extend(required)

    if len(all_required_ids) != len(set(all_required_ids)):
        raise HwpxStaleFactualTargetError(
            "duplicate_document_factual_target",
            "the stale factual target set contains duplicate IDs",
        )
    non_photo_pictures = tuple(
        picture
        for picture in inspection.snapshot.pictures
        if (
            picture.nearest_table_index is None
            or picture.nearest_table_index % 3 != 2
        )
    )
    mapped_non_photo_ids = {
        pair.inspector_signature.target_id for pair in page_pairs
    }
    unclassified_non_photo = tuple(
        picture
        for picture in non_photo_pictures
        if picture.target_id not in mapped_non_photo_ids
    )
    if unclassified_non_photo:
        raise HwpxStaleFactualTargetError(
            "unclassified_non_photo_picture",
            "a non-photo-grid picture is outside the signature target map",
        )

    groups = _signature_binary_groups(page_pairs)
    counts = StaleFactualTargetCounts(
        page_pair_count=len(page_pairs),
        required_target_count=len(all_required_ids),
        cell_target_count=len(page_pairs) * 3,
        paragraph_target_count=len(photo_headers),
        picture_target_count=len(page_pairs),
        title_group_count=len(page_pairs),
        title_route_heading_count=len(page_pairs),
        inspector_cell_count=len(page_pairs),
        photo_page_header_count=len(photo_headers),
        inspector_signature_picture_count=len(page_pairs),
        existing_binder_route_reference_count=len(page_pairs),
        complete_route_surface_count=len(page_pairs) * 3,
        unique_signature_binary_count=len(groups),
        unclassified_external_factual_paragraph_count=0,
        unclassified_non_photo_picture_count=0,
    )
    audit_payload = {
        "schema_version": HWPX_STALE_FACTUAL_TARGET_SCHEMA,
        "template_id": inspection.baseline.template_id,
        "document_snapshot_id": inspection.document_snapshot_id,
        "document_sha256": inspection.snapshot.document_sha256,
        "structure_target_summary_sha256": (
            inspection.snapshot.target_summary_sha256
        ),
        "counts": asdict(counts),
        "page_pairs": [asdict(pair) for pair in page_pairs],
        "required_target_ids": all_required_ids,
        "signature_binary_groups": [asdict(group) for group in groups],
        "text_edit_policy": (
            "replace_exact_t_nodes_and_remove_only_host_p_direct_linesegarray"
        ),
        "title_and_complex_run_deletion_forbidden": True,
        "pre_save_full_residual_scan_required": True,
        "residual_scan_mode": (
            "exact_target_address_expected_value_and_signature_binding"
        ),
    }
    return HwpxStaleFactualTargetAudit(
        schema_version=HWPX_STALE_FACTUAL_TARGET_SCHEMA,
        template_id=inspection.baseline.template_id,
        quarter=inspection.baseline.quarter,
        document_name=inspection.snapshot.document_name,
        document_snapshot_id=inspection.document_snapshot_id,
        document_sha256=inspection.snapshot.document_sha256,
        structure_target_summary_sha256=(
            inspection.snapshot.target_summary_sha256
        ),
        counts=counts,
        page_pairs=tuple(page_pairs),
        required_target_ids=tuple(all_required_ids),
        signature_binary_groups=groups,
        audit_sha256=_canonical_sha256(audit_payload),
    )


def check_stale_factual_target_coverage(
    audit: HwpxStaleFactualTargetAudit,
    operation_target_ids: Iterable[str],
) -> StaleFactualTargetCoverage:
    """Check that a future writer explicitly handles every audited surface."""

    supplied = tuple(operation_target_ids)
    seen = set()
    duplicates = set()
    for target_id in supplied:
        if target_id in seen:
            duplicates.add(target_id)
        seen.add(target_id)
    required = set(audit.required_target_ids)
    unknown = tuple(sorted(seen - required))
    missing = tuple(
        target_id
        for target_id in audit.required_target_ids
        if target_id not in seen
    )
    role_by_target = {
        target_id: role
        for pair in audit.page_pairs
        for role, target_id in (
            ("title_group", pair.title_group.target_id),
            ("title_route_heading", pair.title_route_heading.target_id),
            ("inspection_inspector", pair.inspection_inspector.target_id),
            ("photo_page_header", pair.photo_page_header.target_id),
            ("inspector_signature", pair.inspector_signature.target_id),
        )
    }
    route_missing = tuple(
        target_id
        for target_id in missing
        if role_by_target[target_id]
        in {"title_route_heading", "photo_page_header"}
    )
    inspector_missing = tuple(
        target_id
        for target_id in missing
        if role_by_target[target_id] == "inspection_inspector"
    )
    signature_missing = tuple(
        target_id
        for target_id in missing
        if role_by_target[target_id] == "inspector_signature"
    )
    duplicate_rows = tuple(sorted(duplicates))
    complete = not missing and not duplicate_rows and not unknown
    return StaleFactualTargetCoverage(
        schema_version=HWPX_STALE_FACTUAL_COVERAGE_SCHEMA,
        document_snapshot_id=audit.document_snapshot_id,
        required_target_count=len(audit.required_target_ids),
        supplied_operation_count=len(supplied),
        unique_supplied_target_count=len(seen),
        covered_target_count=len(required & seen),
        missing_target_ids=missing,
        duplicate_target_ids=duplicate_rows,
        unknown_target_ids=unknown,
        stale_route_surface_target_ids=route_missing,
        stale_inspector_target_ids=inspector_missing,
        stale_signature_picture_target_ids=signature_missing,
        complete=complete,
    )


def require_stale_factual_target_coverage(
    audit: HwpxStaleFactualTargetAudit,
    operation_target_ids: Iterable[str],
) -> StaleFactualTargetCoverage:
    """Fail closed before save when any legacy factual target can persist."""

    coverage = check_stale_factual_target_coverage(
        audit,
        operation_target_ids,
    )
    if coverage.duplicate_target_ids:
        raise HwpxStaleFactualTargetError(
            "duplicate_stale_factual_operation_target",
            "a stale factual target is handled more than once",
        )
    if coverage.unknown_target_ids:
        raise HwpxStaleFactualTargetError(
            "unknown_stale_factual_operation_target",
            "a stale factual operation target is not in the exact audit",
        )
    if coverage.missing_target_ids:
        raise HwpxStaleFactualTargetError(
            "stale_factual_target_coverage_incomplete",
            "historical route, inspector, signature, or header content can persist",
        )
    return coverage


__all__ = [
    "EmbeddedBinaryFingerprint",
    "ExistingBinderRouteReference",
    "FactualCellTarget",
    "FactualParagraphTarget",
    "FactualPictureTarget",
    "HWPX_STALE_FACTUAL_COVERAGE_SCHEMA",
    "HWPX_STALE_FACTUAL_TARGET_SCHEMA",
    "HwpxStaleFactualTargetAudit",
    "HwpxStaleFactualTargetError",
    "SharedSignatureBinaryGroup",
    "StaleFactualPagePair",
    "StaleFactualTargetCounts",
    "StaleFactualTargetCoverage",
    "XmlParagraphAddress",
    "audit_stale_factual_targets",
    "check_stale_factual_target_coverage",
    "require_stale_factual_target_coverage",
]
