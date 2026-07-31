from __future__ import annotations

import hashlib
import json
import stat
import struct
import zipfile
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree

import pytest

from app.hwpx_exact_writer import (
    ExactHwpxParagraphTextOperation,
    ExactHwpxPictureOperation,
    ExactHwpxTextOperation,
    ExactHwpxWritePlan,
    ExactHwpxWriterLimits,
    HwpxExactWriterError,
    _normalized_cell_text,
    _paragraph_protected_structure_sha256,
    _replace_section_text,
    _resolve_paragraph_address,
    inspect_exact_sample_writer_contract,
    write_exact_hwpx,
)
from app.hwpx_structure import inspect_hwpx_structure
from app.hwpx_stale_factual_targets import (
    _paragraph_address,
    audit_stale_factual_targets,
)


WORKSPACE = Path(__file__).resolve().parents[3]
SAMPLE_3Q = WORKSPACE / "sample" / "3분기 점검 내용 정리.hwpx"
SAMPLE_4Q = WORKSPACE / "sample" / "4분기 점검 내용 정리.hwpx"

HEADER = """
 <hp:p id="header-p" pageBreak="1">
  <hp:run charPrIDRef="9"><hp:ctrl><hp:pageNum pos="TOP"/></hp:ctrl></hp:run>
  <hp:run charPrIDRef="10"><hp:t>□ 기존 노선 현황사진</hp:t></hp:run>
  <hp:linesegarray><hp:lineseg textpos="0"/></hp:linesegarray>
 </hp:p>
""".encode("utf-8")

SECTION = """<?xml version="1.0" encoding="UTF-8"?>
<hs:sec xmlns:hs="urn:hancom:hwpml:section"
 xmlns:hp="urn:hancom:hwpml:paragraph"
 xmlns:hc="urn:hancom:hwpml:core">
 <hp:tbl id="table-A" rowCnt="1" colCnt="2">
  <hp:tr>
   <hp:tc>
    <hp:cellAddr rowAddr="0" colAddr="0"/>
    <hp:cellSpan rowSpan="1" colSpan="1"/>
    <hp:subList><hp:p><hp:run charPrIDRef="7">
     <hp:t><hp:markpenBegin color="#FFFFFF"/>기존 노선<markpenEnd/></hp:t>
    </hp:run><hp:linesegarray><hp:lineseg textpos="0"/></hp:linesegarray><hp:pic id="picture-A">
     <hp:offset x="0" y="0"/><hp:orgSz width="640" height="480"/>
     <hc:img binaryItemIDRef="image1"/>
     <hp:sz width="320" height="240"/>
    </hp:pic></hp:p></hp:subList>
   </hp:tc>
   <hp:tc>
    <hp:cellAddr rowAddr="0" colAddr="1"/>
    <hp:cellSpan rowSpan="1" colSpan="1"/>
    <hp:subList><hp:p><hp:run><hp:t>절대 변경 금지</hp:t></hp:run>
     <hp:linesegarray><hp:lineseg textpos="0"/></hp:linesegarray>
    </hp:p></hp:subList>
   </hp:tc>
  </hp:tr>
 </hp:tbl>
</hs:sec>
""".encode("utf-8")

MANIFEST = """<?xml version="1.0" encoding="UTF-8"?>
<opf:package xmlns:opf="http://www.idpf.org/2007/opf/">
 <opf:manifest>
  <opf:item id="image1" href="BinData/image1.bmp"
   media-type="image/bmp" isEmbeded="1"/>
 </opf:manifest>
</opf:package>
""".encode("utf-8")


def _zip_info(
    name: str,
    *,
    compression: int = zipfile.ZIP_DEFLATED,
) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = compression
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    return info


def _bmp(pixel: tuple[int, int, int]) -> bytes:
    # Valid 1x1, 24-bit BMP with one padded BGR row.
    row = bytes((pixel[2], pixel[1], pixel[0], 0))
    size = 54 + len(row)
    header = (
        b"BM"
        + struct.pack("<IHHI", size, 0, 0, 54)
        + struct.pack(
            "<IIIHHIIIIII",
            40,
            1,
            1,
            1,
            24,
            0,
            len(row),
            2835,
            2835,
            0,
            0,
        )
    )
    return header + row


def _png() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", 1, 1)
        + b"\x08\x02\x00\x00\x00"
    )


def _write_source(
    path: Path,
    *,
    second_picture: bool = False,
    standalone_header: bool = False,
    empty_first_run: str | None = None,
) -> Path:
    section = SECTION
    if standalone_header:
        section = section.replace(
            b" <hp:tbl",
            HEADER + b" <hp:tbl",
            1,
        )
    if second_picture:
        protected_text = "<hp:t>절대 변경 금지</hp:t>".encode("utf-8")
        section = section.replace(
            protected_text,
            protected_text
            + (
                b'<hp:pic id="picture-B"><hc:img '
                b'binaryItemIDRef="image1"/></hp:pic>'
            ),
        )
    if empty_first_run is not None:
        populated_run = (
            b'<hp:run charPrIDRef="7">\n'
            b'     <hp:t><hp:markpenBegin color="#FFFFFF"/>'
            + "기존 노선".encode("utf-8")
            + b"<markpenEnd/></hp:t>\n"
            b"    </hp:run>"
        )
        if empty_first_run == "self_closing":
            empty_run = b'<hp:run charPrIDRef="7"/>'
        elif empty_first_run == "paired":
            empty_run = (
                b'<hp:run charPrIDRef="7"></hp:run>'
            )
        else:
            raise ValueError("unsupported empty_first_run fixture mode")
        if populated_run not in section:
            raise AssertionError("fixture populated run was not found")
        section = section.replace(populated_run, empty_run, 1)
    with zipfile.ZipFile(path, mode="w") as archive:
        archive.writestr(
            _zip_info("mimetype", compression=zipfile.ZIP_STORED),
            b"application/hwp+zip",
        )
        archive.writestr(
            _zip_info("Contents/content.hpf"),
            MANIFEST,
        )
        archive.writestr(
            _zip_info("Contents/section0.xml"),
            section,
        )
        archive.writestr(
            _zip_info("BinData/image1.bmp"),
            _bmp((255, 0, 0)),
        )
        archive.writestr(
            _zip_info("Settings.xml"),
            b"<settings keep='exact'/>",
        )
    return path


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _local_name(value: object) -> str:
    return str(value).rsplit("}", 1)[-1].casefold()


def _canonical_sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _element_at(
    root: ElementTree.Element,
    path: tuple[int, ...],
) -> ElementTree.Element:
    current = root
    for index in path:
        current = list(current)[index]
    return current


def _paragraph_operation(
    source: Path,
    *,
    value: str = "□ 현재 영상 노선 현황사진",
) -> ExactHwpxParagraphTextOperation:
    snapshot = inspect_hwpx_structure(source)
    section = snapshot.sections[0]
    with zipfile.ZipFile(source) as archive:
        root = ElementTree.fromstring(archive.read(section.path))
    parent_map = {
        child: parent
        for parent in root.iter()
        for child in list(parent)
    }
    paragraphs = tuple(
        element for element in root.iter() if _local_name(element.tag) == "p"
    )
    paragraph = next(
        element
        for element in paragraphs
        if next(
            (
                candidate
                for key, candidate in element.attrib.items()
                if _local_name(key) == "id"
            ),
            None,
        )
        == "header-p"
    )
    selected = tuple(
        element
        for element in paragraph.iter()
        if _local_name(element.tag) == "t" and element.text is not None
    )
    address = _paragraph_address(
        document_sha256=snapshot.document_sha256,
        section_path=section.path,
        section_index=section.section_index,
        root=root,
        paragraph=paragraph,
        selected_text_nodes=selected,
        paragraph_ordinals={
            element: ordinal
            for ordinal, element in enumerate(paragraphs)
        },
        parent_map=parent_map,
    )
    target_payload = {
        "document_sha256": snapshot.document_sha256,
        "section_path": address.section_path,
        "section_index": address.section_index,
        "paragraph_ordinal": address.paragraph_ordinal,
        "text_sha256": address.selected_text_sha256,
    }
    return ExactHwpxParagraphTextOperation(
        operation_id="op-photo-page-header",
        target_id="hwpx-paragraph-" + _canonical_sha(target_payload)[:24],
        address=address,
        expected_current_value=address.selected_text,
        expected_current_sha256=address.selected_text_sha256,
        value=value,
        source_ref="incident:current-job-approved-1",
    )


def _paragraph_plan(
    source: Path,
    *,
    operation: ExactHwpxParagraphTextOperation | None = None,
) -> ExactHwpxWritePlan:
    snapshot = inspect_hwpx_structure(source)
    paragraph_operation = operation or _paragraph_operation(source)
    return ExactHwpxWritePlan(
        plan_id="plan-current-header-1",
        source_document_sha256=snapshot.document_sha256,
        source_target_summary_sha256=snapshot.target_summary_sha256,
        incident_source_refs=("incident:current-job-approved-1",),
        operations=(paragraph_operation,),
    )


def _plan(
    source: Path,
    evidence: Path,
    *,
    stale_text: bool = False,
) -> ExactHwpxWritePlan:
    snapshot = inspect_hwpx_structure(source)
    cell = snapshot.tables[0].cells[0]
    picture = snapshot.pictures[0]
    old_image = _bmp((255, 0, 0))
    evidence_bytes = evidence.read_bytes()
    expected_text = "다른 값" if stale_text else "기존 노선"
    return ExactHwpxWritePlan(
        plan_id="plan-current-job-1",
        source_document_sha256=snapshot.document_sha256,
        source_target_summary_sha256=snapshot.target_summary_sha256,
        incident_source_refs=("incident:current-job-approved-1",),
        operations=(
            ExactHwpxTextOperation(
                operation_id="op-route",
                target_id=cell.target_id,
                expected_current_value=expected_text,
                expected_current_sha256=_sha(
                    expected_text.encode("utf-8")
                ),
                value="노선명: 현재 영상 노선 & 검증",
                source_ref="incident:current-job-approved-1",
            ),
            ExactHwpxPictureOperation(
                operation_id="op-photo",
                target_id=picture.target_id,
                expected_binary_item_ref="image1",
                expected_current_sha256=_sha(old_image),
                evidence_path=str(evidence.resolve()),
                evidence_sha256=_sha(evidence_bytes),
                source_ref="incident:current-job-approved-1",
            ),
        ),
    )


def _empty_cell_plan(
    source: Path,
    *,
    value: str,
) -> ExactHwpxWritePlan:
    snapshot = inspect_hwpx_structure(source)
    cell = snapshot.tables[0].cells[0]
    assert cell.text_excerpt == ""
    return ExactHwpxWritePlan(
        plan_id="plan-preformatted-empty-cell-1",
        source_document_sha256=snapshot.document_sha256,
        source_target_summary_sha256=snapshot.target_summary_sha256,
        incident_source_refs=("incident:current-job-approved-1",),
        operations=(
            ExactHwpxTextOperation(
                operation_id="op-evaluation-square",
                target_id=cell.target_id,
                expected_current_value="",
                expected_current_sha256=_sha(b""),
                value=value,
                source_ref="incident:current-job-approved-1",
            ),
        ),
    )


def test_writer_creates_owned_output_and_preserves_structure_geometry(
    tmp_path,
):
    source = _write_source(tmp_path / "source.hwpx")
    evidence = tmp_path / "evidence.bmp"
    evidence.write_bytes(_bmp((0, 255, 0)))
    output = tmp_path / "owned-output.hwpx"
    source_before = source.read_bytes()

    result = write_exact_hwpx(
        source.resolve(),
        output.resolve(),
        _plan(source, evidence),
        require_audited_sample=False,
    )

    assert result.state == "writer_completed_com_validation_required"
    assert result.output_render_validation_status == (
        "not_run_com_validation_required"
    )
    assert result.com_invocation_count == 0
    assert result.source_unchanged is True
    assert result.page_contract_proxy_verified is False
    assert source.read_bytes() == source_before
    assert output.exists()

    reopened = inspect_hwpx_structure(output.resolve())
    assert reopened.tables[0].cells[0].text_excerpt == (
        "노선명: 현재 영상 노선 & 검증"
    )
    assert reopened.tables[0].cells[1].text_excerpt == "절대 변경 금지"
    assert len(reopened.tables) == 1
    assert len(reopened.pictures) == 1
    assert reopened.pictures[0].binary_item_refs == ("image1",)

    with zipfile.ZipFile(output) as archive:
        infos = archive.infolist()
        assert infos[0].filename == "mimetype"
        assert infos[0].compress_type == zipfile.ZIP_STORED
        assert archive.read("BinData/image1.bmp") == evidence.read_bytes()
        assert archive.read("Settings.xml") == b"<settings keep='exact'/>"
        section = archive.read("Contents/section0.xml")
        assert b'charPrIDRef="7"' in section
        assert b'<hp:orgSz width="640" height="480"/>' in section
        assert b'<hp:sz width="320" height="240"/>' in section
        assert b"&amp;" in section
        assert section.count(b"<hp:linesegarray>") == 1
        protected_start = section.index("절대 변경 금지".encode("utf-8"))
        assert b"<hp:linesegarray>" in section[protected_start:]


@pytest.mark.parametrize("run_mode", ("self_closing", "paired"))
def test_writer_uses_existing_preformatted_empty_run_for_grade_square(
    tmp_path,
    run_mode,
):
    source = _write_source(
        tmp_path / f"empty-{run_mode}.hwpx",
        empty_first_run=run_mode,
    )
    output = tmp_path / f"empty-{run_mode}-output.hwpx"

    result = write_exact_hwpx(
        source.resolve(),
        output.resolve(),
        _empty_cell_plan(source, value="■"),
        require_audited_sample=False,
    )

    assert result.operation_count == 1
    assert result.target_verifications[0].output_value_sha256 == _sha(
        "■".encode("utf-8")
    )
    reopened = inspect_hwpx_structure(output.resolve())
    assert reopened.tables[0].cells[0].text_excerpt == "■"
    assert reopened.tables[0].cells[1].text_excerpt == "절대 변경 금지"

    with (
        zipfile.ZipFile(source) as source_archive,
        zipfile.ZipFile(output) as output_archive,
    ):
        source_root = ElementTree.fromstring(
            source_archive.read("Contents/section0.xml")
        )
        output_root = ElementTree.fromstring(
            output_archive.read("Contents/section0.xml")
        )
        source_cell = next(
            element
            for element in source_root.iter()
            if _local_name(element.tag) == "tc"
        )
        output_cell = next(
            element
            for element in output_root.iter()
            if _local_name(element.tag) == "tc"
        )
        source_run = next(
            element
            for element in source_cell.iter()
            if _local_name(element.tag) == "run"
        )
        output_run = next(
            element
            for element in output_cell.iter()
            if _local_name(element.tag) == "run"
        )
        assert source_run.attrib == output_run.attrib == {
            "charPrIDRef": "7"
        }
        assert not tuple(
            element
            for element in source_run
            if _local_name(element.tag) == "t"
        )
        output_text_nodes = tuple(
            element
            for element in output_run
            if _local_name(element.tag) == "t"
        )
        assert len(output_text_nodes) == 1
        assert output_text_nodes[0].text == "■"
        assert not tuple(
            element
            for element in output_cell.iter()
            if _local_name(element.tag) == "linesegarray"
        )


def test_writer_does_not_remove_layout_cache_for_exact_empty_noop(
    tmp_path,
):
    source = _write_source(
        tmp_path / "empty-noop.hwpx",
        empty_first_run="self_closing",
    )
    output = tmp_path / "empty-noop-output.hwpx"

    write_exact_hwpx(
        source.resolve(),
        output.resolve(),
        _empty_cell_plan(source, value=""),
        require_audited_sample=False,
    )

    with (
        zipfile.ZipFile(source) as source_archive,
        zipfile.ZipFile(output) as output_archive,
    ):
        assert output_archive.read(
            "Contents/section0.xml"
        ) == source_archive.read("Contents/section0.xml")


def test_writer_replaces_exact_standalone_paragraph_and_preserves_controls(
    tmp_path,
):
    source = _write_source(
        tmp_path / "source-with-header.hwpx",
        standalone_header=True,
    )
    output = tmp_path / "paragraph-output.hwpx"
    source_before = source.read_bytes()
    operation = _paragraph_operation(
        source,
        value="□ 현재 영상 & 노선 현황사진",
    )

    result = write_exact_hwpx(
        source.resolve(),
        output.resolve(),
        _paragraph_plan(source, operation=operation),
        require_audited_sample=False,
    )

    assert source.read_bytes() == source_before
    assert result.operation_count == 1
    assert result.target_verifications[0].kind == "replace_paragraph_text"
    assert result.target_verifications[0].target_id == operation.target_id
    assert result.target_verifications[0].output_value_sha256 == _sha(
        operation.value.encode("utf-8")
    )

    with (
        zipfile.ZipFile(source) as source_archive,
        zipfile.ZipFile(output) as output_archive,
    ):
        source_raw = source_archive.read("Contents/section0.xml")
        output_raw = output_archive.read("Contents/section0.xml")
        source_root = ElementTree.fromstring(source_raw)
        output_root = ElementTree.fromstring(output_raw)
        source_paragraph = _element_at(
            source_root,
            operation.address.xml_child_path,
        )
        output_paragraph = _element_at(
            output_root,
            operation.address.xml_child_path,
        )
        output_text = "".join(
            (_element_at(output_paragraph, path).text or "")
            for path in operation.address.selected_text_run_paths
        )

        assert output_text == operation.value
        assert "&amp;".encode() in output_raw
        assert len(
            [
                child
                for child in list(output_paragraph)
                if _local_name(child.tag) == "linesegarray"
            ]
        ) == 0
        assert len(
            [
                child
                for child in list(source_paragraph)
                if _local_name(child.tag) == "linesegarray"
            ]
        ) == 1
        assert len(
            [
                element
                for element in output_paragraph.iter()
                if _local_name(element.tag) == "run"
            ]
        ) == 2
        assert any(
            _local_name(element.tag) == "ctrl"
            for element in output_paragraph.iter()
        )
        assert any(
            _local_name(element.tag) == "pagenum"
            for element in output_paragraph.iter()
        )
        source_table = next(
            element
            for element in source_root.iter()
            if _local_name(element.tag) == "tbl"
        )
        output_table = next(
            element
            for element in output_root.iter()
            if _local_name(element.tag) == "tbl"
        )
        assert ElementTree.tostring(output_table) == ElementTree.tostring(
            source_table
        )
        assert source_raw.count(b"<hp:linesegarray>") == 3
        assert output_raw.count(b"<hp:linesegarray>") == 2


def _paragraph_operation_with_address(
    source: Path,
    operation: ExactHwpxParagraphTextOperation,
    address,
) -> ExactHwpxParagraphTextOperation:
    snapshot = inspect_hwpx_structure(source)
    payload = {
        "document_sha256": snapshot.document_sha256,
        "section_path": address.section_path,
        "section_index": address.section_index,
        "paragraph_ordinal": address.paragraph_ordinal,
        "xml_child_path": address.xml_child_path,
        "selected_text_sha256": address.selected_text_sha256,
    }
    return replace(
        operation,
        address=replace(
            address,
            address_sha256=_canonical_sha(payload),
        ),
    )


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        ("address", "stale_paragraph_address"),
        ("text_run", "stale_paragraph_text_run_address"),
        ("run_structure", "stale_paragraph_run_structure"),
        ("lineseg", "stale_paragraph_lineseg_structure"),
        ("expected_hash", "paragraph_operation_stale_contract"),
    ),
)
def test_standalone_paragraph_stale_contract_fails_closed(
    tmp_path,
    mutation,
    expected_code,
):
    source = _write_source(
        tmp_path / f"source-{mutation}.hwpx",
        standalone_header=True,
    )
    operation = _paragraph_operation(source)
    address = operation.address
    if mutation == "address":
        stale_address = replace(
            address,
            section_child_index=99,
            xml_child_path=(99,),
        )
        operation = _paragraph_operation_with_address(
            source,
            operation,
            stale_address,
        )
    elif mutation == "text_run":
        operation = replace(
            operation,
            address=replace(
                address,
                selected_text_run_paths=((1, 99),),
            ),
        )
    elif mutation == "run_structure":
        operation = replace(
            operation,
            address=replace(address, run_structure_sha256="0" * 64),
        )
    elif mutation == "lineseg":
        operation = replace(
            operation,
            address=replace(
                address,
                direct_linesegarray_sha256s=("0" * 64,),
            ),
        )
    elif mutation == "expected_hash":
        operation = replace(
            operation,
            expected_current_sha256="0" * 64,
        )
    else:  # pragma: no cover - exhaustive parameter guard
        raise AssertionError(mutation)
    output = tmp_path / f"must-not-exist-{mutation}.hwpx"

    with pytest.raises(HwpxExactWriterError) as caught:
        write_exact_hwpx(
            source.resolve(),
            output.resolve(),
            _paragraph_plan(source, operation=operation),
            require_audited_sample=False,
        )

    assert caught.value.code == expected_code
    assert not output.exists()


def test_stale_expected_text_fails_closed_without_output(tmp_path):
    source = _write_source(tmp_path / "source.hwpx")
    evidence = tmp_path / "evidence.bmp"
    evidence.write_bytes(_bmp((0, 255, 0)))
    output = tmp_path / "must-not-exist.hwpx"

    with pytest.raises(
        HwpxExactWriterError,
        match="expected value",
    ) as caught:
        write_exact_hwpx(
            source.resolve(),
            output.resolve(),
            _plan(source, evidence, stale_text=True),
            require_audited_sample=False,
        )

    assert caught.value.code == "stale_expected_value"
    assert not output.exists()


def test_wrong_source_hash_is_rejected_before_output(tmp_path):
    source = _write_source(tmp_path / "source.hwpx")
    evidence = tmp_path / "evidence.bmp"
    evidence.write_bytes(_bmp((0, 255, 0)))
    plan = replace(
        _plan(source, evidence),
        source_document_sha256="0" * 64,
    )
    output = tmp_path / "must-not-exist.hwpx"

    with pytest.raises(HwpxExactWriterError) as caught:
        write_exact_hwpx(
            source.resolve(),
            output.resolve(),
            plan,
            require_audited_sample=False,
        )

    assert caught.value.code == "source_hash_mismatch"
    assert not output.exists()


def test_evidence_must_use_same_validated_media(tmp_path):
    source = _write_source(tmp_path / "source.hwpx")
    evidence = tmp_path / "evidence.png"
    evidence.write_bytes(_png())
    output = tmp_path / "must-not-exist.hwpx"

    with pytest.raises(HwpxExactWriterError) as caught:
        write_exact_hwpx(
            source.resolve(),
            output.resolve(),
            _plan(source, evidence),
            require_audited_sample=False,
        )

    assert caught.value.code == "evidence_media_mismatch"
    assert not output.exists()


def test_shared_binary_member_cannot_change_untargeted_picture(tmp_path):
    source = _write_source(
        tmp_path / "shared.hwpx",
        second_picture=True,
    )
    evidence = tmp_path / "evidence.bmp"
    evidence.write_bytes(_bmp((0, 255, 0)))
    output = tmp_path / "must-not-exist.hwpx"

    with pytest.raises(HwpxExactWriterError) as caught:
        write_exact_hwpx(
            source.resolve(),
            output.resolve(),
            _plan(source, evidence),
            require_audited_sample=False,
        )

    assert caught.value.code == "shared_picture_cross_target_change"
    assert not output.exists()


def test_existing_output_is_never_overwritten(tmp_path):
    source = _write_source(tmp_path / "source.hwpx")
    evidence = tmp_path / "evidence.bmp"
    evidence.write_bytes(_bmp((0, 255, 0)))
    output = tmp_path / "existing.hwpx"
    output.write_bytes(b"owner data")

    with pytest.raises(HwpxExactWriterError) as caught:
        write_exact_hwpx(
            source.resolve(),
            output.resolve(),
            _plan(source, evidence),
            require_audited_sample=False,
        )

    assert caught.value.code == "output_exists"
    assert output.read_bytes() == b"owner data"


def test_unknown_and_duplicate_exact_targets_fail_closed(tmp_path):
    source = _write_source(tmp_path / "source.hwpx")
    evidence = tmp_path / "evidence.bmp"
    evidence.write_bytes(_bmp((0, 255, 0)))
    valid = _plan(source, evidence)

    unknown_text = replace(
        valid.operations[0],
        target_id="hwpx-cell-" + ("0" * 24),
    )
    unknown_plan = replace(
        valid,
        operations=(unknown_text, valid.operations[1]),
    )
    unknown_output = tmp_path / "unknown.hwpx"
    with pytest.raises(HwpxExactWriterError) as unknown:
        write_exact_hwpx(
            source.resolve(),
            unknown_output.resolve(),
            unknown_plan,
            require_audited_sample=False,
        )
    assert unknown.value.code == "unknown_cell_target"
    assert not unknown_output.exists()

    duplicate_plan = replace(
        valid,
        operations=(
            valid.operations[0],
            replace(valid.operations[0], operation_id="op-route-2"),
        ),
    )
    duplicate_output = tmp_path / "duplicate.hwpx"
    with pytest.raises(HwpxExactWriterError) as duplicate:
        write_exact_hwpx(
            source.resolve(),
            duplicate_output.resolve(),
            duplicate_plan,
            require_audited_sample=False,
        )
    assert duplicate.value.code == "duplicate_target"
    assert not duplicate_output.exists()


@pytest.mark.parametrize(
    ("sample", "quarter", "special_pair_index"),
    (
        (SAMPLE_3Q, 3, 3),
        (SAMPLE_4Q, 4, 21),
    ),
)
def test_real_samples_replace_exact_photo_headers_without_table_drift(
    sample,
    quarter,
    special_pair_index,
):
    audit = audit_stale_factual_targets(sample.resolve())
    snapshot = inspect_hwpx_structure(sample.resolve())
    targets = (
        audit.page_pairs[0].photo_page_header,
        audit.page_pairs[special_pair_index].photo_page_header,
    )
    operations = tuple(
        ExactHwpxParagraphTextOperation(
            operation_id=f"op-real-{quarter}q-header-{target.page_pair_index}",
            target_id=target.target_id,
            address=target.address,
            expected_current_value=target.expected_text,
            expected_current_sha256=target.expected_text_sha256,
            value=(
                f"□ 분석 영상 {quarter}분기 "
                f"{target.page_pair_index + 1}구간 현황사진"
            ),
            source_ref="incident:current-job-approved-1",
        )
        for target in targets
    )
    section_path = operations[0].address.section_path
    with zipfile.ZipFile(sample) as archive:
        source_raw = archive.read(section_path)

    modified_raw, verifications, removed_elements = _replace_section_text(
        source_raw,
        section_path=section_path,
        snapshot=snapshot,
        operations=operations,
        limits=ExactHwpxWriterLimits(),
    )

    source_root = ElementTree.fromstring(source_raw)
    output_root = ElementTree.fromstring(modified_raw)
    assert len(verifications) == 2
    assert {
        verification.target_id for verification in verifications
    } == {operation.target_id for operation in operations}
    assert removed_elements > 0
    assert (
        sum(1 for _ in source_root.iter())
        - sum(1 for _ in output_root.iter())
        == removed_elements
    )
    source_linesegs = [
        element
        for element in source_root.iter()
        if _local_name(element.tag) == "linesegarray"
    ]
    output_linesegs = [
        element
        for element in output_root.iter()
        if _local_name(element.tag) == "linesegarray"
    ]
    assert len(source_linesegs) - len(output_linesegs) == sum(
        len(operation.address.direct_linesegarray_child_indexes)
        for operation in operations
    )

    source_tables = [
        element
        for element in source_root.iter()
        if _local_name(element.tag) == "tbl"
    ]
    output_tables = [
        element
        for element in output_root.iter()
        if _local_name(element.tag) == "tbl"
    ]
    assert len(source_tables) == len(output_tables) == 72
    assert [
        ElementTree.tostring(element) for element in output_tables
    ] == [ElementTree.tostring(element) for element in source_tables]

    for operation in operations:
        source_resolved = _resolve_paragraph_address(
            source_root,
            snapshot=snapshot,
            section_path=section_path,
            operation=operation,
            output_mode=False,
        )
        output_resolved = _resolve_paragraph_address(
            output_root,
            snapshot=snapshot,
            section_path=section_path,
            operation=operation,
            output_mode=True,
        )
        assert "".join(
            element.text or ""
            for element in output_resolved.selected_text_nodes
        ) == operation.value
        assert (
            _paragraph_protected_structure_sha256(
                source_resolved.paragraph
            )
            == _paragraph_protected_structure_sha256(
                output_resolved.paragraph
            )
        )

    special = targets[1]
    assert special.descendant_table_indexes
    for table_index in special.descendant_table_indexes:
        assert ElementTree.tostring(output_tables[table_index]) == (
            ElementTree.tostring(source_tables[table_index])
        )

    # The malformed sample pages host a real photo table inside the same hp:p.
    # A complete atomic report can therefore update a cell and the standalone
    # header together; paragraph reopen verification must not reject that
    # separately authorized cell edit.
    nested_table_index = special.descendant_table_indexes[0]
    nested_cell = next(
        cell
        for cell in snapshot.tables[nested_table_index].cells
        if cell.text_excerpt and not cell.text_truncated
    )
    cell_operation = ExactHwpxTextOperation(
        operation_id=f"op-real-{quarter}q-nested-cell",
        target_id=nested_cell.target_id,
        expected_current_value=nested_cell.text_excerpt,
        expected_current_sha256=nested_cell.text_sha256,
        value=f"동시 셀 변경 검증 {quarter}분기",
        source_ref="incident:current-job-approved-1",
    )
    mixed_raw, _, _ = _replace_section_text(
        source_raw,
        section_path=section_path,
        snapshot=snapshot,
        operations=(operations[1], cell_operation),
        limits=ExactHwpxWriterLimits(),
    )
    mixed_root = ElementTree.fromstring(mixed_raw)
    mixed_source = _resolve_paragraph_address(
        source_root,
        snapshot=snapshot,
        section_path=section_path,
        operation=operations[1],
        output_mode=False,
    )
    mixed_output = _resolve_paragraph_address(
        mixed_root,
        snapshot=snapshot,
        section_path=section_path,
        operation=operations[1],
        output_mode=True,
    )
    assert (
        mixed_source.untargeted_content_sha256
        == mixed_output.untargeted_content_sha256
    )
    mixed_tables = [
        element
        for element in mixed_root.iter()
        if _local_name(element.tag) == "tbl"
    ]
    mixed_cells = [
        row
        for row in mixed_tables[nested_table_index].iter()
        if _local_name(row.tag) == "tc"
    ]
    assert _normalized_cell_text(
        mixed_cells[nested_cell.cell_index]
    ) == cell_operation.value


def test_real_3q_sample_has_exact_writer_72_table_48_page_proxy():
    contract = inspect_exact_sample_writer_contract(SAMPLE_3Q.resolve())

    assert contract.template_id == "sample-3q-v1"
    assert contract.quarter == 3
    assert contract.table_count == 72
    assert contract.expected_rendered_page_count == 48
    assert contract.page_contract_proxy_verified is True
    assert contract.source_read_only is True
    assert contract.no_com is True


def test_exact_writer_capacity_covers_one_complete_48_page_projection():
    limits = ExactHwpxWriterLimits()

    assert limits.max_operations == 5_000
    with pytest.raises(ValueError, match="max_operations"):
        ExactHwpxWriterLimits(max_operations=10_001)
