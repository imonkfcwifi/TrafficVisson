import hashlib
import stat
import struct
import zipfile
from pathlib import Path

import pytest

from app.hwpx_structure import HwpxLimitExceeded
from app.hwpx_weak_cases import (
    HWPX_WEAK_CASE_PURPOSE,
    HWPX_WEAK_CASE_SCHEMA,
    HwpxWeakCaseAuthorization,
    HwpxWeakCaseAuthorizationError,
    HwpxWeakCaseLimits,
    HwpxWeakCaseSecurityError,
    inspect_hwpx_weak_cases,
)


SECTION_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hs:sec
  xmlns:hs="urn:hancom:hwpml:section"
  xmlns:hp="urn:hancom:hwpml:paragraph"
  xmlns:hc="urn:hancom:hwpml:core">
  <hp:p id="page-2" pageBreak="1">
    <hp:run><hp:t>두 번째 보고서 페이지</hp:t></hp:run>
  </hp:p>
  <hp:tbl id="case-table" rowCnt="4" colCnt="2">
    <hp:tr>
      <hp:tc>
        <hp:cellAddr rowAddr="0" colAddr="0"/>
        <hp:cellSpan rowSpan="1" colSpan="1"/>
        <hp:p><hp:run><hp:t>노선</hp:t></hp:run></hp:p>
      </hp:tc>
      <hp:tc>
        <hp:cellAddr rowAddr="0" colAddr="1"/>
        <hp:cellSpan rowSpan="1" colSpan="1"/>
        <hp:p><hp:run><hp:t>인천국제공항고속도로</hp:t></hp:run></hp:p>
      </hp:tc>
    </hp:tr>
    <hp:tr>
      <hp:tc>
        <hp:cellAddr rowAddr="1" colAddr="0"/>
        <hp:cellSpan rowSpan="1" colSpan="1"/>
        <hp:p><hp:run><hp:t>점검위치</hp:t></hp:run></hp:p>
      </hp:tc>
      <hp:tc>
        <hp:cellAddr rowAddr="1" colAddr="1"/>
        <hp:cellSpan rowSpan="1" colSpan="1"/>
        <hp:p><hp:run><hp:t>영종대교 상행 12.3km</hp:t></hp:run></hp:p>
      </hp:tc>
    </hp:tr>
    <hp:tr>
      <hp:tc>
        <hp:cellAddr rowAddr="2" colAddr="0"/>
        <hp:cellSpan rowSpan="1" colSpan="1"/>
        <hp:p><hp:run><hp:t>현장사진</hp:t></hp:run></hp:p>
      </hp:tc>
      <hp:tc>
        <hp:cellAddr rowAddr="2" colAddr="1"/>
        <hp:cellSpan rowSpan="1" colSpan="1"/>
        <hp:p>
          <hp:run><hp:t>포트홀 발생</hp:t></hp:run>
          <hp:pic id="case-picture">
            <hc:img binaryItemIDRef="image1"/>
          </hp:pic>
        </hp:p>
      </hp:tc>
    </hp:tr>
    <hp:tr>
      <hp:tc>
        <hp:cellAddr rowAddr="3" colAddr="0"/>
        <hp:cellSpan rowSpan="1" colSpan="1"/>
        <hp:p><hp:run><hp:t>점검의견</hp:t></hp:run></hp:p>
      </hp:tc>
      <hp:tc>
        <hp:cellAddr rowAddr="3" colAddr="1"/>
        <hp:cellSpan rowSpan="1" colSpan="1"/>
        <hp:p><hp:run><hp:t>긴급 보수 필요</hp:t></hp:run></hp:p>
      </hp:tc>
    </hp:tr>
  </hp:tbl>
</hs:sec>
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


def _bmp(width: int = 2, height: int = 3) -> bytes:
    row_bytes = ((width * 3 + 3) // 4) * 4
    pixels = bytes(row_bytes * height)
    file_size = 54 + len(pixels)
    return (
        b"BM"
        + struct.pack("<IHHI", file_size, 0, 0, 54)
        + struct.pack(
            "<IIIHHIIIIII",
            40,
            width,
            height,
            1,
            24,
            0,
            len(pixels),
            2835,
            2835,
            0,
            0,
        )
        + pixels
    )


def _manifest(items, *, title="3분기 도로 점검 사례") -> bytes:
    item_xml = "".join(
        (
            f'<opf:item id="{item_id}" href="{href}" '
            f'media-type="{media_type}" isEmbeded="1"/>'
        )
        for item_id, href, media_type in items
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<opf:package xmlns:opf="http://www.idpf.org/2007/opf/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<opf:metadata><opf:title>{title}</opf:title>"
        '<opf:meta name="creator" content="KOTI"/>'
        "</opf:metadata>"
        f"<opf:manifest>{item_xml}</opf:manifest>"
        "</opf:package>"
    ).encode("utf-8")


def _write_hwpx(
    path: Path,
    *,
    section_xml: bytes = SECTION_XML,
    items=None,
    images=None,
    manifest_title: str = "3분기 도로 점검 사례",
) -> Path:
    active_items = items or [
        ("image1", "BinData/image1.bmp", "image/bmp")
    ]
    active_images = images or {"BinData/image1.bmp": _bmp()}
    with zipfile.ZipFile(path, mode="w") as archive:
        archive.writestr(
            _zip_info("mimetype", compression=zipfile.ZIP_STORED),
            b"application/hwp+zip",
        )
        archive.writestr(
            _zip_info("Contents/content.hpf"),
            _manifest(active_items, title=manifest_title),
        )
        archive.writestr(
            _zip_info("Contents/section0.xml"),
            section_xml,
        )
        for name, payload in active_images.items():
            archive.writestr(_zip_info(name), payload)
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _authorization(source: Path) -> HwpxWeakCaseAuthorization:
    return HwpxWeakCaseAuthorization(
        document_name=source.name,
        expected_document_sha256=_sha256(source),
        purpose=HWPX_WEAK_CASE_PURPOSE,
        user_confirmed_read_only=True,
    )


def test_associates_image_with_exact_cell_context_and_weak_fields(tmp_path):
    source = _write_hwpx(tmp_path / "3분기 사례.hwpx")
    before_names = sorted(path.name for path in tmp_path.iterdir())

    result = inspect_hwpx_weak_cases(
        source,
        authorization=_authorization(source),
    )

    assert result.schema_version == HWPX_WEAK_CASE_SCHEMA
    assert result.document_sha256 == _sha256(source)
    assert result.quarter == 3
    assert result.quarter_source == "filename"
    assert result.case_count == 1
    assert result.resolved_case_count == 1
    assert result.unresolved_case_count == 0
    assert result.unique_image_count == 1
    assert len(result.index_manifest_sha256) == 64

    case = result.cases[0]
    assert case.case_id.startswith("hwpx-weak-case-")
    assert case.source.section_path == "Contents/section0.xml"
    assert case.source.section_index == 0
    assert case.source.picture_index == 0
    assert case.source.picture_xml_id == "case-picture"
    assert case.source.binary_item_refs == ("image1",)
    assert case.source.package_paths == ("BinData/image1.bmp",)
    assert case.source.table_index == 0
    assert case.source.cell_row == 2
    assert case.source.cell_column == 1
    assert case.source.preceding_explicit_page_break_count == 1
    assert case.source.page_hint == 2
    assert "not_rendered_page" in case.source.page_hint_source

    assert case.containing_cell_text is not None
    assert case.containing_cell_text.text == "포트홀 발생"
    assert case.containing_cell_text.relationship == "containing_cell"
    assert len(case.nearby_cell_texts) == 7
    assert case.caption_candidate is not None
    assert case.caption_candidate.text == "포트홀 발생"
    assert case.caption_candidate.confidence == "weak_unconfirmed"
    assert case.caption_candidate.confirmed is False

    assert case.route_candidates[0].text == "인천국제공항고속도로"
    assert case.route_candidates[0].derivation == (
        "adjacent_cell_after_explicit_label"
    )
    assert case.location_candidates[0].text == "영종대교 상행 12.3km"
    assert any(
        candidate.text == "포트홀 발생"
        for candidate in case.evaluation_candidates
    )
    assert case.opinion_candidates[0].text == "긴급 보수 필요"

    image = case.images[0]
    assert image.image_sha256 == hashlib.sha256(_bmp()).hexdigest()
    assert image.media_type == "image/bmp"
    assert image.declared_media_type == "image/bmp"
    assert image.size_bytes == len(_bmp())
    assert (image.width, image.height) == (2, 3)
    assert image.header_validation == "magic_and_bounded_dimensions"
    assert image.bytes_retained is False

    assert sorted(path.name for path in tmp_path.iterdir()) == before_names


def test_duplicate_image_bytes_keep_both_structure_cases(tmp_path):
    second_picture = """
      <hp:tr>
        <hp:tc>
          <hp:cellAddr rowAddr="4" colAddr="0"/>
          <hp:cellSpan rowSpan="1" colSpan="1"/>
          <hp:p><hp:run><hp:t>추가 사례</hp:t></hp:run></hp:p>
        </hp:tc>
        <hp:tc>
          <hp:cellAddr rowAddr="4" colAddr="1"/>
          <hp:cellSpan rowSpan="1" colSpan="1"/>
          <hp:p><hp:pic id="case-picture-2">
            <hc:img binaryItemIDRef="image2"/>
          </hp:pic></hp:p>
        </hp:tc>
      </hp:tr>
    """.encode("utf-8")
    section = SECTION_XML.replace(
        b'rowCnt="4"',
        b'rowCnt="5"',
    ).replace(
        b"</hp:tbl>",
        second_picture + b"</hp:tbl>",
    )
    duplicate = _bmp(4, 2)
    source = _write_hwpx(
        tmp_path / "4분기 중복 사례.hwpx",
        section_xml=section,
        items=[
            ("image1", "BinData/image1.bmp", "image/bmp"),
            ("image2", "BinData/image2.bmp", "image/bmp"),
        ],
        images={
            "BinData/image1.bmp": duplicate,
            "BinData/image2.bmp": duplicate,
        },
    )

    result = inspect_hwpx_weak_cases(
        source,
        authorization=_authorization(source),
    )

    assert result.quarter == 4
    assert result.case_count == 2
    assert result.unique_image_count == 1
    assert result.duplicate_image_group_count == 1
    assert len({case.case_id for case in result.cases}) == 2
    first, second = (case.images[0] for case in result.cases)
    assert first.image_sha256 == second.image_sha256
    assert first.duplicate_group_id == second.duplicate_group_id
    assert first.duplicate_occurrence_count == 2
    assert second.duplicate_occurrence_count == 2
    assert result.cases[0].source.cell_target_id != (
        result.cases[1].source.cell_target_id
    )


def test_quarter_falls_back_to_bounded_package_metadata(tmp_path):
    source = _write_hwpx(
        tmp_path / "분기 표기 없는 사례.hwpx",
        manifest_title="2025년 4분기 도로 점검 사례",
    )

    result = inspect_hwpx_weak_cases(
        source,
        authorization=_authorization(source),
    )

    assert result.quarter == 4
    assert result.quarter_source == "package_metadata"
    assert "2025년 4분기 도로 점검 사례" in result.metadata_excerpt


def test_unsafe_manifest_href_and_bad_image_magic_fail_closed(tmp_path):
    unsafe_href = _write_hwpx(
        tmp_path / "unsafe-href.hwpx",
        items=[("image1", "../escape.bmp", "image/bmp")],
    )
    with pytest.raises(
        HwpxWeakCaseSecurityError,
        match="href is unsafe",
    ):
        inspect_hwpx_weak_cases(
            unsafe_href,
            authorization=_authorization(unsafe_href),
        )

    bad_magic = _write_hwpx(
        tmp_path / "bad-magic.hwpx",
        images={"BinData/image1.bmp": b"not-a-real-image"},
    )
    with pytest.raises(
        HwpxWeakCaseSecurityError,
        match="unknown or invalid magic",
    ):
        inspect_hwpx_weak_cases(
            bad_magic,
            authorization=_authorization(bad_magic),
        )


def test_image_size_bound_is_enforced_before_member_read(tmp_path):
    source = _write_hwpx(tmp_path / "bounded.hwpx")

    with pytest.raises(HwpxLimitExceeded, match="image exceeds"):
        inspect_hwpx_weak_cases(
            source,
            authorization=_authorization(source),
            limits=HwpxWeakCaseLimits(max_image_bytes=40),
        )


def test_exact_document_authorization_is_required(tmp_path):
    source = _write_hwpx(tmp_path / "authorized.hwpx")
    wrong = HwpxWeakCaseAuthorization(
        document_name=source.name,
        expected_document_sha256="0" * 64,
        purpose=HWPX_WEAK_CASE_PURPOSE,
        user_confirmed_read_only=True,
    )

    with pytest.raises(
        HwpxWeakCaseAuthorizationError,
        match="does not match",
    ):
        inspect_hwpx_weak_cases(source, authorization=wrong)


def test_index_never_auto_promotes_cases_and_manifest_is_stable(tmp_path):
    source = _write_hwpx(tmp_path / "3분기 검토 전.hwpx")
    authorization = _authorization(source)

    first = inspect_hwpx_weak_cases(
        source,
        authorization=authorization,
    )
    second = inspect_hwpx_weak_cases(
        source,
        authorization=authorization,
    )

    assert first.index_manifest_sha256 == second.index_manifest_sha256
    assert [case.case_id for case in first.cases] == [
        case.case_id for case in second.cases
    ]
    assert first.review_required_count == first.case_count
    assert first.training_eligible_count == 0
    assert first.annotation_count == 0
    assert first.incident_count == 0
    assert first.safety.review_required_before_promotion is True
    assert first.safety.automatic_annotation_creation is False
    assert first.safety.automatic_incident_creation is False
    assert first.safety.automatic_training_eligibility is False
    assert first.safety.no_archive_extraction is True
    assert first.safety.image_bytes_persisted is False
    assert all(
        case.review_required is True
        and case.review_state == "unconfirmed"
        and case.training_eligible is False
        and case.annotation_created is False
        and case.incident_created is False
        for case in first.cases
    )
