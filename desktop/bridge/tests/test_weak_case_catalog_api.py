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


def test_user_confirmation_promotes_exact_case_to_project_reference(
    tmp_path,
):
    source = _write_source(tmp_path / DOCUMENT_NAME)

    class AuditedFixtureCatalog(WeakCaseCatalog):
        def case_attestation(self, source_id, case_id):
            result = super().case_attestation(source_id, case_id)
            result["sample_layout_binding"] = {
                "picture_target_id": result["picture_target_id"],
                "picture_index": result["picture_index"],
                "page_pair_index": 0,
                "inspection_page_number": 1,
                "photo_page_number": 2,
                "photo_table_index": result["table_index"],
                "photo_cell_target_id": result["cell_target_id"],
                "photo_cell_row": result["cell_row"],
                "photo_cell_column": result["cell_column"],
                "photo_slot_id": (
                    "sample-photo-slot-" + "a" * 24
                ),
                "photo_caption": {
                    "role": "photo_caption",
                    "cell_target_id": "hwpx-cell-" + "c" * 24,
                    "table_id": "photo-grid",
                    "table_index": result["table_index"],
                    "page_number": 2,
                    "row": result["cell_row"] + 1,
                    "column": result["cell_column"],
                    "row_span": 1,
                    "column_span": 1,
                    "text_excerpt": "삭초 필요",
                    "text_sha256": hashlib.sha256(
                        "삭초 필요".encode("utf-8")
                    ).hexdigest(),
                },
                "inspection_route": {
                    "role": "inspection_route",
                    "cell_target_id": "hwpx-cell-" + "d" * 24,
                    "table_id": "inspection-detail",
                    "table_index": 1,
                    "page_number": 1,
                    "row": 32,
                    "column": 4,
                    "row_span": 1,
                    "column_span": 1,
                    "text_excerpt": "노선명 : 검증 노선",
                    "text_sha256": hashlib.sha256(
                        "노선명 : 검증 노선".encode("utf-8")
                    ).hexdigest(),
                },
                "inspection_opinion_label": {
                    "role": "inspection_opinion_label",
                    "cell_target_id": "hwpx-cell-" + "e" * 24,
                    "table_id": "inspection-detail",
                    "table_index": 1,
                    "page_number": 1,
                    "row": 29,
                    "column": 1,
                    "row_span": 1,
                    "column_span": 1,
                    "text_excerpt": "점검의견",
                    "text_sha256": hashlib.sha256(
                        "점검의견".encode("utf-8")
                    ).hexdigest(),
                },
                "inspection_opinion_body": {
                    "role": "inspection_opinion_body",
                    "cell_target_id": "hwpx-cell-" + "f" * 24,
                    "table_id": "inspection-detail",
                    "table_index": 1,
                    "page_number": 1,
                    "row": 30,
                    "column": 1,
                    "row_span": 1,
                    "column_span": 1,
                    "text_excerpt": "ㅇ 검증 점검의견",
                    "text_sha256": hashlib.sha256(
                        "ㅇ 검증 점검의견".encode("utf-8")
                    ).hexdigest(),
                },
                "square_markers": [
                    {
                        "cell_target_id": (
                            "hwpx-cell-" + "b" * 24
                        ),
                        "table_id": "inspection-detail",
                        "table_index": 1,
                        "page_number": 1,
                        "row": 3,
                        "column": 6,
                        "marker": "■",
                        "text_sha256": hashlib.sha256(
                            "■".encode("utf-8")
                        ).hexdigest(),
                    }
                ],
                "binding_sha256": hashlib.sha256(
                    b"audited-fixture-binding"
                ).hexdigest(),
            }
            return result

    catalog = AuditedFixtureCatalog(
        source.parent.resolve(),
        definitions=(_definition(source),),
    )
    app = create_app(
        TOKEN,
        fixture_mode=True,
        allow_test_client=True,
        bridge_data_root=str(tmp_path / "bridge-data"),
        media_stability_min_interval_seconds=0,
        weak_case_catalog=catalog,
    )
    with TestClient(app) as client:
        searched = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reference-cases/search",
            headers=_headers(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "issue_type": "roadside_mowing_required",
                "query_text": "수풀 삭초",
                "limit": 3,
            },
        )
        assert searched.status_code == 200, searched.text
        case = searched.json()["cases"][0]
        assert case["approved_reference"] is False
        assert case["image_sha256"] == hashlib.sha256(
            _bmp()
        ).hexdigest()
        assert case["picture_target_id"].startswith("hwpx-picture-")
        assert case["cell_target_id"].startswith("hwpx-cell-")

        request = {
            "project_id": str(DEMO_PROJECT_ID),
            "source_id": case["source_id"],
            "case_id": case["case_id"],
            "expected_document_sha256": case["document_sha256"],
            "expected_index_manifest_sha256": case[
                "index_manifest_sha256"
            ],
            "expected_image_sha256": case["image_sha256"],
            "issue_type": "roadside_mowing_required",
            "maintenance_action": "삭초 및 수목 제거",
            "caption": case["caption"],
            "route": case["route"],
            "location": case["location"],
            "evaluation": case["evaluation"],
            "opinion": case["opinion"],
            "user_confirmed": True,
        }
        approved = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reference-cases/approvals",
            headers=_headers(),
            json=request,
        )
        assert approved.status_code == 200, approved.text
        approval = approved.json()
        assert approval["reference_state"] == "approved_reference"
        assert approval["training_eligible"] is True
        assert approval["training_scope"] == "approved_reference_only"
        assert approval["annotation_created"] is False
        assert approval["incident_created"] is False
        assert approval["photo_slot_id"] == (
            "sample-photo-slot-" + "a" * 24
        )
        assert approval["square_markers"][0]["marker"] == "■"
        assert {
            cell["role"] for cell in approval["semantic_cells"]
        } == {
            "photo_caption",
            "inspection_route",
            "inspection_opinion_label",
            "inspection_opinion_body",
        }
        assert approval["document_sha256"] == case["document_sha256"]
        assert approval["image_sha256"] == case["image_sha256"]
        assert approval["idempotent_replay"] is False

        replay = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reference-cases/approvals",
            headers=_headers(),
            json=request,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["record_sha256"] == approval["record_sha256"]

        promoted_search = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reference-cases/search",
            headers=_headers(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "issue_type": "roadside_mowing_required",
                "query_text": "수풀 삭초",
                "limit": 3,
            },
        )
        assert promoted_search.status_code == 200, promoted_search.text
        promoted = promoted_search.json()
        assert promoted["capability_state"] == (
            "approved_reference_available"
        )
        assert promoted["approved_reference_count"] == 1
        assert promoted["training_eligible_count"] == 1
        assert promoted["annotation_count"] == 0
        assert promoted["incident_count"] == 0
        assert promoted["cases"][0]["review_required"] is False
        assert promoted["cases"][0]["approval_record_sha256"] == (
            approval["record_sha256"]
        )

        listed = client.get(
            f"/projects/{DEMO_PROJECT_ID}/reference-cases/approved",
            headers=_headers(),
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()["approved_reference_count"] == 1
        assert listed.json()["annotation_count"] == 0
        assert listed.json()["incident_count"] == 0

        stale = {
            **request,
            "expected_image_sha256": "0" * 64,
        }
        rejected = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reference-cases/approvals",
            headers=_headers(),
            json=stale,
        )
        assert rejected.status_code == 409

    restarted = create_app(
        TOKEN,
        fixture_mode=True,
        allow_test_client=True,
        bridge_data_root=str(tmp_path / "bridge-data"),
        media_stability_min_interval_seconds=0,
        weak_case_catalog=catalog,
    )
    with TestClient(restarted) as client:
        recovered = client.get(
            f"/projects/{DEMO_PROJECT_ID}/reference-cases/approved",
            headers=_headers(),
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["approved_reference_count"] == 1
        assert recovered.json()["records"][0]["record_sha256"] == (
            approval["record_sha256"]
        )
