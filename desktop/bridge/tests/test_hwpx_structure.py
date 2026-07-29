import hashlib
import os
import shutil
import stat
import warnings
import zipfile
from pathlib import Path

import pytest

import app.hwpx_structure as hwpx_structure
from app.hwpx_structure import (
    HWPX_STRUCTURE_SCHEMA,
    HwpxFormatError,
    HwpxLimitExceeded,
    HwpxSecurityError,
    HwpxSourceChanged,
    HwpxStructureLimits,
    inspect_hwpx_structure,
)


SECTION_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hs:sec
  xmlns:hs="urn:hancom:hwpml:section"
  xmlns:hp="urn:hancom:hwpml:paragraph"
  xmlns:hc="urn:hancom:hwpml:core">
  <hp:p id="p0" pageBreak="1">
    <hp:run><hp:t>  first   page  </hp:t></hp:run>
  </hp:p>
  <hp:tbl id="table-A" rowCnt="2" colCnt="3">
    <hp:tr>
      <hp:tc>
        <hp:cellAddr rowAddr="0" colAddr="0"/>
        <hp:cellSpan rowSpan="1" colSpan="1"/>
        <hp:subList>
          <hp:p>
            <hp:run><hp:t>  삭초\\n필요  </hp:t></hp:run>
            <hp:fieldBegin name="보고서_제목"/>
            <hp:bookmarkStart name="현장사진"/>
            <hp:pic id="picture-A">
              <hc:img binaryItemIDRef="image1"/>
            </hp:pic>
          </hp:p>
        </hp:subList>
      </hp:tc>
    </hp:tr>
    <hp:tr>
      <hp:tc>
        <hp:cellAddr rowAddr="1" colAddr="1"/>
        <hp:cellSpan rowSpan="1" colSpan="2"/>
        <hp:subList>
          <hp:p><hp:run><hp:t>차선 재도색</hp:t></hp:run></hp:p>
        </hp:subList>
      </hp:tc>
    </hp:tr>
  </hp:tbl>
  <hp:break type="page" id="break-2"/>
  <hp:pic id="picture-outside">
    <hc:img binaryItemIDRef="image2"/>
    <hc:img binaryItemIDRef="image2"/>
  </hp:pic>
</hs:sec>
""".encode("utf-8")


def _zip_info(
    name: str,
    *,
    compression: int = zipfile.ZIP_DEFLATED,
    mode: int = stat.S_IFREG | 0o600,
) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = compression
    info.create_system = 3
    info.external_attr = mode << 16
    return info


def _write_hwpx(
    path: Path,
    *,
    sections=None,
    extra_entries=None,
    mimetype: bytes = b"application/hwp+zip",
) -> Path:
    section_rows = (
        {"Contents/section0.xml": SECTION_XML}
        if sections is None
        else sections
    )
    with zipfile.ZipFile(path, mode="w") as archive:
        archive.writestr(
            _zip_info("mimetype", compression=zipfile.ZIP_STORED),
            mimetype,
        )
        for name, payload in section_rows.items():
            archive.writestr(_zip_info(name), payload)
        for name, payload in (extra_entries or {}).items():
            archive.writestr(_zip_info(name), payload)
    return path


def _sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.sha256(source.read()).hexdigest()


def test_namespace_insensitive_snapshot_has_exact_document_bound_targets(
    tmp_path,
):
    source = _write_hwpx(tmp_path / "inspection.hwpx")

    result = inspect_hwpx_structure(source)

    assert result.schema_version == HWPX_STRUCTURE_SCHEMA
    assert result.document_name == source.name
    assert result.document_sha256 == _sha256(source)
    assert result.sha256_before == result.sha256_after
    assert result.identity_before == result.identity_after
    assert result.archive_entry_count == 2
    assert result.xml_entry_count == 1
    assert result.section_count == 1
    assert result.target_count == 4
    assert len(result.tables) == 1
    assert len(result.pictures) == 2
    assert len(result.page_breaks) == 2
    assert result.field_names == ("보고서_제목",)
    assert result.field_count == 1
    assert result.bookmark_names == ("현장사진",)
    assert result.bookmark_count == 1

    table = result.tables[0]
    assert table.table_index == 0
    assert table.xml_id == "table-A"
    assert table.rows == 2
    assert table.columns == 3
    assert table.declared_rows == 2
    assert table.declared_columns == 3
    assert table.cell_count == 2

    first, second = table.cells
    assert (first.row, first.column, first.row_span, first.column_span) == (
        0,
        0,
        1,
        1,
    )
    assert first.address_source == "direct_cellAddr"
    assert first.text_excerpt == "삭초\\n필요"
    assert first.text_sha256 == hashlib.sha256(
        "삭초\\n필요".encode("utf-8")
    ).hexdigest()
    assert first.text_truncated is False
    assert (second.row, second.column, second.row_span, second.column_span) == (
        1,
        1,
        1,
        2,
    )
    assert second.text_excerpt == "차선 재도색"

    inside, outside = result.pictures
    assert inside.xml_id == "picture-A"
    assert inside.binary_item_refs == ("image1",)
    assert inside.nearest_table_id == table.table_id
    assert inside.nearest_table_index == 0
    assert inside.nearest_cell_target_id == first.target_id
    assert (inside.nearest_cell_row, inside.nearest_cell_column) == (0, 0)
    assert outside.binary_item_refs == ("image2",)
    assert outside.nearest_table_id is None
    assert outside.nearest_cell_target_id is None

    target_ids = [
        *(cell.target_id for cell in table.cells),
        *(picture.target_id for picture in result.pictures),
    ]
    assert len(target_ids) == len(set(target_ids))
    assert all(
        target.startswith(("hwpx-cell-", "hwpx-picture-"))
        and len(target.rsplit("-", 1)[-1]) == 24
        for target in target_ids
    )
    assert len(result.target_summary_sha256) == 64
    assert all(
        cell.document_sha256 == result.document_sha256
        and cell.target_summary_sha256 == result.target_summary_sha256
        for cell in table.cells
    )
    assert all(
        picture.document_sha256 == result.document_sha256
        and picture.target_summary_sha256
        == result.target_summary_sha256
        for picture in result.pictures
    )
    assert result.safety.read_only is True
    assert result.safety.no_edit is True
    assert result.safety.no_external_transfer is True
    assert result.safety.no_extraction is True
    assert result.safety.no_com is True
    assert result.safety.no_writes is True
    assert result.safety.automatic_document_edit is False


def test_cell_excerpt_is_bounded_but_hash_covers_full_normalized_text(
    tmp_path,
):
    text = " ".join(["삭초"] * 40)
    xml = SECTION_XML.replace(
        "  삭초\\n필요  ".encode("utf-8"),
        text.encode("utf-8"),
    )
    source = _write_hwpx(
        tmp_path / "excerpt.hwpx",
        sections={"Contents/section0.xml": xml},
    )
    limits = HwpxStructureLimits(text_excerpt_chars=20)

    result = inspect_hwpx_structure(source, limits=limits)

    cell = result.tables[0].cells[0]
    assert cell.text_excerpt == text[:20]
    assert cell.text_truncated is True
    assert cell.text_sha256 == hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()


def test_missing_cell_address_uses_bounded_row_order_fallback(tmp_path):
    xml = SECTION_XML.replace(
        b'<hp:cellAddr rowAddr="0" colAddr="0"/>',
        b"",
    )
    source = _write_hwpx(
        tmp_path / "fallback.hwpx",
        sections={"Contents/section0.xml": xml},
    )

    result = inspect_hwpx_structure(source)

    cell = result.tables[0].cells[0]
    assert (cell.row, cell.column) == (0, 0)
    assert cell.address_source == "row_order_fallback"


def test_nested_table_text_is_not_folded_into_outer_cell(tmp_path):
    nested = b"""
    <hp:tbl id="nested" rowCnt="1" colCnt="1">
      <hp:tr><hp:tc>
        <hp:cellAddr rowAddr="0" colAddr="0"/>
        <hp:cellSpan rowSpan="1" colSpan="1"/>
        <hp:p><hp:run><hp:t>nested only</hp:t></hp:run></hp:p>
      </hp:tc></hp:tr>
    </hp:tbl>
    """
    xml = SECTION_XML.replace(
        b"</hp:subList>\n      </hp:tc>",
        b"</hp:subList>" + nested + b"</hp:tc>",
        1,
    )
    source = _write_hwpx(
        tmp_path / "nested.hwpx",
        sections={"Contents/section0.xml": xml},
    )

    result = inspect_hwpx_structure(source)

    assert len(result.tables) == 2
    assert "nested only" not in result.tables[0].cells[0].text_excerpt
    assert result.tables[1].cells[0].text_excerpt == "nested only"


def test_target_ids_are_stable_for_a_byte_identical_copy_and_document_bound(
    tmp_path,
):
    first = _write_hwpx(tmp_path / "first.hwpx")
    copy = tmp_path / "copy.hwpx"
    shutil.copyfile(first, copy)
    changed = _write_hwpx(
        tmp_path / "changed.hwpx",
        extra_entries={"meta/change.txt": b"different exact document"},
    )

    first_result = inspect_hwpx_structure(first)
    copy_result = inspect_hwpx_structure(copy)
    changed_result = inspect_hwpx_structure(changed)

    assert first_result.document_sha256 == copy_result.document_sha256
    assert first_result.target_summary_sha256 == (
        copy_result.target_summary_sha256
    )
    assert first_result.tables[0].cells[0].target_id == (
        copy_result.tables[0].cells[0].target_id
    )
    assert changed_result.document_sha256 != first_result.document_sha256
    assert changed_result.target_summary_sha256 != (
        first_result.target_summary_sha256
    )
    assert changed_result.tables[0].cells[0].target_id != (
        first_result.tables[0].cells[0].target_id
    )


def test_inspection_does_not_write_or_extract_any_file(tmp_path):
    source = _write_hwpx(tmp_path / "read-only.hwpx")
    before_hash = _sha256(source)
    before_stat = source.stat()
    before_names = sorted(path.name for path in tmp_path.iterdir())

    inspect_hwpx_structure(source)

    after_stat = source.stat()
    assert _sha256(source) == before_hash
    assert after_stat.st_size == before_stat.st_size
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert sorted(path.name for path in tmp_path.iterdir()) == before_names


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "../section0.xml",
        "/absolute/section0.xml",
        "C:/drive/section0.xml",
        "Contents//section0.xml",
        "Contents/./section0.xml",
    ],
)
def test_zip_path_traversal_and_unsafe_names_are_rejected(
    tmp_path,
    unsafe_name,
):
    source = _write_hwpx(
        tmp_path / "unsafe-name.hwpx",
        extra_entries={unsafe_name: b"payload"},
    )

    with pytest.raises(HwpxSecurityError, match="entry path"):
        inspect_hwpx_structure(source)


def test_zip_backslash_path_is_rejected(tmp_path):
    source = _write_hwpx(
        tmp_path / "backslash.hwpx",
        extra_entries={"Contents/unsafe0.xml": b"<unsafe/>"},
    )
    payload = bytearray(source.read_bytes())
    original = b"Contents/unsafe0.xml"
    replacement = b"Contents\\unsafe0.xml"
    assert len(original) == len(replacement)
    assert payload.count(original) == 2
    payload[:] = payload.replace(original, replacement)
    source.write_bytes(payload)

    with pytest.raises(HwpxSecurityError, match="entry path"):
        inspect_hwpx_structure(source)


def test_duplicate_normalized_zip_entry_is_rejected(tmp_path):
    source = tmp_path / "duplicate.hwpx"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(source, mode="w") as archive:
            archive.writestr(
                _zip_info("mimetype", compression=zipfile.ZIP_STORED),
                b"application/hwp+zip",
            )
            archive.writestr(
                _zip_info("Contents/section0.xml"),
                SECTION_XML,
            )
            archive.writestr(
                _zip_info("contents/SECTION0.XML"),
                SECTION_XML,
            )

    with pytest.raises(HwpxSecurityError, match="duplicate entry"):
        inspect_hwpx_structure(source)


def test_duplicate_section_index_is_rejected(tmp_path):
    source = _write_hwpx(
        tmp_path / "duplicate-section.hwpx",
        sections={
            "Contents/section0.xml": SECTION_XML,
            "Contents/section00.xml": SECTION_XML,
        },
    )

    with pytest.raises(HwpxSecurityError, match="section indexes"):
        inspect_hwpx_structure(source)


def test_encrypted_zip_flag_is_rejected_before_member_read(tmp_path):
    source = _write_hwpx(tmp_path / "encrypted-flag.hwpx")
    payload = bytearray(source.read_bytes())
    local = payload.find(b"PK\x03\x04")
    central = payload.find(b"PK\x01\x02")
    assert local >= 0 and central >= 0
    local_flags = int.from_bytes(payload[local + 6 : local + 8], "little")
    central_flags = int.from_bytes(
        payload[central + 8 : central + 10],
        "little",
    )
    payload[local + 6 : local + 8] = (local_flags | 1).to_bytes(2, "little")
    payload[central + 8 : central + 10] = (
        central_flags | 1
    ).to_bytes(2, "little")
    source.write_bytes(payload)

    with pytest.raises(HwpxSecurityError, match="Encrypted"):
        inspect_hwpx_structure(source)


def test_symlink_archive_entry_is_rejected(tmp_path):
    source = tmp_path / "linked-entry.hwpx"
    with zipfile.ZipFile(source, mode="w") as archive:
        archive.writestr(
            _zip_info("mimetype", compression=zipfile.ZIP_STORED),
            b"application/hwp+zip",
        )
        archive.writestr(
            _zip_info("Contents/section0.xml"),
            SECTION_XML,
        )
        archive.writestr(
            _zip_info("Contents/link", mode=stat.S_IFLNK | 0o777),
            b"../../outside",
        )

    with pytest.raises(HwpxSecurityError, match="links or special"):
        inspect_hwpx_structure(source)


@pytest.mark.parametrize(
    "declaration",
    [
        b'<!DOCTYPE sec SYSTEM "file:///C:/secret">',
        b'<!DOCTYPE sec [<!ENTITY steal SYSTEM "file:///C:/secret">]>',
    ],
)
def test_doctype_and_entity_declarations_are_rejected(
    tmp_path,
    declaration,
):
    xml = b'<?xml version="1.0"?>' + declaration + b"<sec/>"
    source = _write_hwpx(
        tmp_path / "entity.hwpx",
        sections={"Contents/section0.xml": xml},
    )

    with pytest.raises(HwpxSecurityError, match="DOCTYPE|entity"):
        inspect_hwpx_structure(source)


def test_malformed_section_xml_is_rejected(tmp_path):
    source = _write_hwpx(
        tmp_path / "malformed.hwpx",
        sections={"Contents/section0.xml": b"<sec><p></sec>"},
    )

    with pytest.raises(HwpxFormatError, match="malformed"):
        inspect_hwpx_structure(source)


def test_all_xml_entries_are_checked_not_only_sections(tmp_path):
    source = _write_hwpx(
        tmp_path / "bad-header.hwpx",
        extra_entries={
            "Contents/header.xml": b'<!DOCTYPE header><header/>',
        },
    )

    with pytest.raises(HwpxSecurityError, match="DOCTYPE"):
        inspect_hwpx_structure(source)


def test_xml_depth_element_size_and_compression_bounds(tmp_path):
    deep_xml = (
        b'<?xml version="1.0"?>'
        + b"<n>" * 20
        + b"</n>" * 20
    )
    deep_source = _write_hwpx(
        tmp_path / "deep.hwpx",
        sections={"Contents/section0.xml": deep_xml},
    )
    with pytest.raises(HwpxLimitExceeded, match="nesting depth"):
        inspect_hwpx_structure(
            deep_source,
            limits=HwpxStructureLimits(max_xml_depth=8),
        )

    many_xml = b"<sec>" + b"<p/>" * 30 + b"</sec>"
    many_source = _write_hwpx(
        tmp_path / "many-elements.hwpx",
        sections={"Contents/section0.xml": many_xml},
    )
    with pytest.raises(HwpxLimitExceeded, match="element count"):
        inspect_hwpx_structure(
            many_source,
            limits=HwpxStructureLimits(max_elements_per_xml=10),
        )

    with pytest.raises(HwpxLimitExceeded, match="XML entry"):
        inspect_hwpx_structure(
            _write_hwpx(tmp_path / "large-xml.hwpx"),
            limits=HwpxStructureLimits(
                max_xml_entry_bytes=256,
                max_total_xml_bytes=4096,
            ),
        )

    with pytest.raises(HwpxLimitExceeded, match="compression ratio"):
        inspect_hwpx_structure(
            _write_hwpx(tmp_path / "ratio.hwpx"),
            limits=HwpxStructureLimits(max_compression_ratio=1.1),
        )


def test_xml_and_section_count_bounds_are_enforced(tmp_path):
    source = _write_hwpx(
        tmp_path / "counts.hwpx",
        sections={
            "Contents/section0.xml": b"<sec/>",
            "Contents/section1.xml": b"<sec/>",
        },
        extra_entries={"Contents/header.xml": b"<header/>"},
    )

    with pytest.raises(HwpxLimitExceeded, match="XML entry count"):
        inspect_hwpx_structure(
            source,
            limits=HwpxStructureLimits(max_xml_entries=2),
        )
    with pytest.raises(HwpxLimitExceeded, match="section count"):
        inspect_hwpx_structure(
            source,
            limits=HwpxStructureLimits(max_sections=1),
        )


def test_invalid_mimetype_and_missing_section_are_rejected(tmp_path):
    wrong_mimetype = _write_hwpx(
        tmp_path / "wrong-mimetype.hwpx",
        mimetype=b"application/zip",
    )
    with pytest.raises(HwpxFormatError, match="mimetype"):
        inspect_hwpx_structure(wrong_mimetype)

    no_section = _write_hwpx(
        tmp_path / "no-section.hwpx",
        sections={},
        extra_entries={"Contents/header.xml": b"<header/>"},
    )
    with pytest.raises(HwpxFormatError, match="no section"):
        inspect_hwpx_structure(no_section)


def test_invalid_cell_grid_and_duplicate_direct_metadata_are_rejected(
    tmp_path,
):
    outside_grid = SECTION_XML.replace(
        b'rowAddr="1" colAddr="1"',
        b'rowAddr="9" colAddr="1"',
    )
    source = _write_hwpx(
        tmp_path / "outside-grid.hwpx",
        sections={"Contents/section0.xml": outside_grid},
    )
    with pytest.raises(HwpxFormatError, match="row count"):
        inspect_hwpx_structure(source)

    duplicate = SECTION_XML.replace(
        b'<hp:cellAddr rowAddr="0" colAddr="0"/>',
        (
            b'<hp:cellAddr rowAddr="0" colAddr="0"/>'
            b'<hp:cellAddr rowAddr="0" colAddr="0"/>'
        ),
    )
    source = _write_hwpx(
        tmp_path / "duplicate-cell-address.hwpx",
        sections={"Contents/section0.xml": duplicate},
    )
    with pytest.raises(HwpxFormatError, match="multiple direct cellAddr"):
        inspect_hwpx_structure(source)


def test_relative_non_hwpx_directory_and_source_symlink_are_rejected(
    tmp_path,
):
    with pytest.raises(HwpxSecurityError, match="absolute"):
        inspect_hwpx_structure(Path("relative.hwpx"))

    wrong_suffix = tmp_path / "document.zip"
    wrong_suffix.write_bytes(b"not used")
    with pytest.raises(HwpxFormatError, match="extension"):
        inspect_hwpx_structure(wrong_suffix)

    directory = tmp_path / "directory.hwpx"
    directory.mkdir()
    with pytest.raises(HwpxSecurityError, match="regular file"):
        inspect_hwpx_structure(directory)

    source = _write_hwpx(tmp_path / "source.hwpx")
    link = tmp_path / "link.hwpx"
    try:
        os.symlink(source, link)
    except (OSError, NotImplementedError):
        pytest.skip("source symlinks are unavailable on this Windows host")
    with pytest.raises(HwpxSecurityError, match="links or reparse"):
        inspect_hwpx_structure(link)


def test_same_size_source_tamper_is_detected_after_archive_read(
    tmp_path,
    monkeypatch,
):
    source = _write_hwpx(
        tmp_path / "tampered.hwpx",
        extra_entries={"BinData/image1.bin": b"A" * 128},
    )
    original_bytes = source.read_bytes()
    original_reader = hwpx_structure._read_archive

    def mutating_reader(handle, *, document_sha256, limits):
        result = original_reader(
            handle,
            document_sha256=document_sha256,
            limits=limits,
        )
        changed = bytearray(original_bytes)
        marker = changed.find(b"application/hwp+zip")
        assert marker >= 0
        changed[marker] ^= 1
        source.write_bytes(changed)
        return result

    monkeypatch.setattr(
        hwpx_structure,
        "_read_archive",
        mutating_reader,
    )

    with pytest.raises(HwpxSourceChanged, match="bytes changed"):
        inspect_hwpx_structure(source)


def test_limit_configuration_rejects_incoherent_values():
    with pytest.raises(ValueError, match="max_total_xml_bytes"):
        HwpxStructureLimits(
            max_xml_entry_bytes=1024,
            max_total_xml_bytes=128,
        )
    with pytest.raises(ValueError, match="text_excerpt_chars"):
        HwpxStructureLimits(
            max_cell_text_chars=10,
            text_excerpt_chars=20,
        )
