from __future__ import annotations

import hashlib
import stat
import struct
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.hwpx_weak_cases import HWPX_WEAK_CASE_PURPOSE
from app.main import create_app
from app.store import DEMO_PROJECT_ID
from app.weak_case_catalog import (
    WeakCaseCatalog,
    WeakCaseCatalogError,
    WeakCaseSourceDefinition,
)


TOKEN = "w" * 64
SOURCE_ID = "sample-hwpx-3q-0123456789abcdef"
DOCUMENT_NAME = "3분기 점검 내용 정리.hwpx"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _zip_info(name: str, *, stored: bool = False) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = (
        zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    )
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    return info


def _bmp(width: int = 3, height: int = 3) -> bytes:
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


def _write_source(path: Path, *, image: bytes | None = None) -> Path:
    section = """<?xml version="1.0" encoding="UTF-8"?>
<hs:sec xmlns:hs="urn:hancom:hwpml:section"
 xmlns:hp="urn:hancom:hwpml:paragraph"
 xmlns:hc="urn:hancom:hwpml:core">
 <hp:tbl id="case-table" rowCnt="3" colCnt="2">
  <hp:tr>
   <hp:tc><hp:cellAddr rowAddr="0" colAddr="0"/><hp:cellSpan rowSpan="1" colSpan="1"/>
    <hp:p><hp:run><hp:t>노선</hp:t></hp:run></hp:p></hp:tc>
   <hp:tc><hp:cellAddr rowAddr="0" colAddr="1"/><hp:cellSpan rowSpan="1" colSpan="1"/>
    <hp:p><hp:run><hp:t>시험고속도로</hp:t></hp:run></hp:p></hp:tc>
  </hp:tr>
  <hp:tr>
   <hp:tc><hp:cellAddr rowAddr="1" colAddr="0"/><hp:cellSpan rowSpan="1" colSpan="1"/>
    <hp:p><hp:run><hp:t>현장사진</hp:t></hp:run></hp:p></hp:tc>
   <hp:tc><hp:cellAddr rowAddr="1" colAddr="1"/><hp:cellSpan rowSpan="1" colSpan="1"/>
    <hp:p><hp:run><hp:t>표지판 주변 수풀</hp:t></hp:run>
     <hp:pic id="case-picture"><hc:img binaryItemIDRef="image1"/></hp:pic>
    </hp:p></hp:tc>
  </hp:tr>
  <hp:tr>
   <hp:tc><hp:cellAddr rowAddr="2" colAddr="0"/><hp:cellSpan rowSpan="1" colSpan="1"/>
    <hp:p><hp:run><hp:t>점검의견</hp:t></hp:run></hp:p></hp:tc>
   <hp:tc><hp:cellAddr rowAddr="2" colAddr="1"/><hp:cellSpan rowSpan="1" colSpan="1"/>
    <hp:p><hp:run><hp:t>삭초 및 수목 제거 필요</hp:t></hp:run></hp:p></hp:tc>
  </hp:tr>
 </hp:tbl>
</hs:sec>""".encode("utf-8")
    manifest = """<?xml version="1.0" encoding="UTF-8"?>
<opf:package xmlns:opf="http://www.idpf.org/2007/opf/">
 <opf:metadata><opf:title>3분기 도로 점검</opf:title></opf:metadata>
 <opf:manifest>
  <opf:item id="image1" href="BinData/image1.bmp"
   media-type="image/bmp" isEmbeded="1"/>
 </opf:manifest>
</opf:package>""".encode("utf-8")
    with zipfile.ZipFile(path, mode="w") as archive:
        archive.writestr(
            _zip_info("mimetype", stored=True),
            b"application/hwp+zip",
        )
        archive.writestr(_zip_info("Contents/content.hpf"), manifest)
        archive.writestr(_zip_info("Contents/section0.xml"), section)
        archive.writestr(
            _zip_info("BinData/image1.bmp"),
            image if image is not None else _bmp(),
        )
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _definition(path: Path, *, sha256: str | None = None):
    return WeakCaseSourceDefinition(
        source_id=SOURCE_ID,
        document_name=DOCUMENT_NAME,
        expected_document_sha256=sha256 or _sha256(path),
        quarter=3,
        purpose=HWPX_WEAK_CASE_PURPOSE,
    )


def _catalog(
    source: Path,
    *,
    sha256: str | None = None,
    maximum_stream_image_bytes: int = 8 * 1024**2,
) -> WeakCaseCatalog:
    return WeakCaseCatalog(
        source.parent.resolve(),
        definitions=(_definition(source, sha256=sha256),),
        maximum_stream_image_bytes=maximum_stream_image_bytes,
    )


def test_catalog_registers_exact_name_sha_and_never_promotes(tmp_path):
    source = _write_source(tmp_path / DOCUMENT_NAME)
    catalog = _catalog(source)

    sources = catalog.list_sources()
    assert sources["ready_source_count"] == 1
    assert sources["sources"][0]["document_sha256"] == _sha256(source)
    assert sources["sources"][0]["authorization_purpose"] == (
        HWPX_WEAK_CASE_PURPOSE
    )

    result = catalog.search(
        issue_type="roadside_mowing_required",
        query_text="수풀 삭초",
        limit=5,
    )
    assert result["result_count"] == 1
    case = result["cases"][0]
    assert case["reference_state"] == "weak_unconfirmed"
    assert case["review_required"] is True
    assert case["approved_reference"] is False
    assert case["training_eligible"] is False
    assert case["annotation_created"] is False
    assert case["incident_created"] is False
    assert result["approved_reference_count"] == 0
    assert result["annotation_count"] == 0
    assert result["incident_count"] == 0
    assert result["training_eligible_count"] == 0
    serialized = str(result)
    assert str(source) not in serialized
    assert "BinData" not in serialized


def test_sha_mismatch_is_not_registered_as_ready(tmp_path):
    source = _write_source(tmp_path / DOCUMENT_NAME)
    catalog = _catalog(source, sha256="0" * 64)

    snapshot = catalog.list_sources()
    assert snapshot["ready_source_count"] == 0
    assert snapshot["capability_state"] == "unavailable"
    assert snapshot["sources"][0]["status"] == "source_sha256_mismatch"
    assert snapshot["sources"][0]["case_count"] == 0
    result = catalog.search(
        issue_type="roadside_mowing_required",
        query_text=None,
        limit=3,
    )
    assert result["cases"] == []
    assert result["source_states"][0]["status"] == (
        "source_sha256_mismatch"
    )


def test_image_resolution_requires_opaque_ids_and_enforces_bound(tmp_path):
    source = _write_source(tmp_path / DOCUMENT_NAME)
    catalog = _catalog(source)
    result = catalog.search(
        issue_type="roadside_mowing_required",
        query_text="삭초",
        limit=3,
    )
    case = result["cases"][0]

    image = catalog.resolve_case_image(
        case["source_id"],
        case["case_id"],
    )
    assert image.body == _bmp()
    assert image.media_type == "image/bmp"
    assert image.size_bytes == len(_bmp())
    with pytest.raises(WeakCaseCatalogError, match="not server-registered"):
        catalog.resolve_case_image(
            "sample-hwpx-3q-ffffffffffffffff",
            case["case_id"],
        )
    with pytest.raises(WeakCaseCatalogError, match="identifier is invalid"):
        catalog.resolve_case_image(
            case["source_id"],
            "../../BinData/image1.bmp",
        )

    bounded = _catalog(source, maximum_stream_image_bytes=64)
    bounded_result = bounded.search(
        issue_type="roadside_mowing_required",
        query_text="삭초",
        limit=3,
    )
    assert bounded_result["cases"][0]["image_available"] is False
    with pytest.raises(WeakCaseCatalogError) as caught:
        bounded.resolve_case_image(
            bounded_result["cases"][0]["source_id"],
            bounded_result["cases"][0]["case_id"],
        )
    assert caught.value.code == "image_too_large"


def test_cached_source_change_fails_closed(tmp_path):
    source = _write_source(tmp_path / DOCUMENT_NAME)
    catalog = _catalog(source)
    result = catalog.search(
        issue_type="roadside_mowing_required",
        query_text="삭초",
        limit=3,
    )
    case = result["cases"][0]
    original = source.read_bytes()
    source.write_bytes(original + b"changed")

    with pytest.raises(WeakCaseCatalogError) as caught:
        catalog.resolve_case_image(
            case["source_id"],
            case["case_id"],
        )
    assert caught.value.code == "source_changed"


def test_bridge_api_is_project_scoped_path_free_and_bounded(tmp_path):
    source = _write_source(tmp_path / DOCUMENT_NAME)
    app = create_app(
        TOKEN,
        fixture_mode=True,
        allow_test_client=True,
        bridge_data_root=str(tmp_path / "bridge-data"),
        media_stability_min_interval_seconds=0,
        weak_case_catalog=_catalog(source),
    )
    with TestClient(app) as client:
        listed = client.get(
            f"/projects/{DEMO_PROJECT_ID}/reference-cases/sources",
            headers=_headers(),
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()["ready_source_count"] == 1

        searched = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reference-cases/search",
            headers=_headers(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "issue_type": "roadside_mowing_required",
                "query_text": "표지판 주변 수풀 삭초",
                "limit": 5,
            },
        )
        assert searched.status_code == 200, searched.text
        payload = searched.json()
        assert payload["result_count"] == 1
        assert payload["annotation_count"] == 0
        assert payload["incident_count"] == 0
        assert str(source) not in searched.text
        assert "BinData" not in searched.text

        rejected_path = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reference-cases/search",
            headers=_headers(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "issue_type": "roadside_mowing_required",
                "limit": 5,
                "source_path": str(source),
            },
        )
        assert rejected_path.status_code == 422

        case = payload["cases"][0]
        image = client.get(
            (
                f"/projects/{DEMO_PROJECT_ID}/reference-cases/sources/"
                f"{case['source_id']}/cases/{case['case_id']}/image"
            ),
            headers=_headers(),
        )
        assert image.status_code == 200
        assert image.content == _bmp()
        assert image.headers["content-type"] == "image/bmp"
        assert image.headers["cache-control"] == "no-store"
        assert int(image.headers["content-length"]) <= (
            app.state.weak_case_catalog.maximum_stream_image_bytes
        )

        arbitrary = client.get(
            (
                f"/projects/{DEMO_PROJECT_ID}/reference-cases/sources/"
                f"sample-hwpx-3q-ffffffffffffffff/cases/"
                f"{case['case_id']}/image"
            ),
            headers=_headers(),
        )
        assert arbitrary.status_code == 404
