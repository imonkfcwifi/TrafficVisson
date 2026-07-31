from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import struct
import zipfile
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.hwpx_weak_cases import HWPX_WEAK_CASE_PURPOSE
from app.main import create_app
from app.pipeline_jobs import BridgePipelineRuntime, PipelineJobService
from app.store import DEMO_PROJECT_ID
from app.weak_case_approvals import (
    WeakCaseApprovalError,
    WeakCaseApprovalStore,
)
from app.weak_case_catalog import (
    WeakCaseCatalog,
    WeakCaseCatalogError,
    WeakCaseSourceDefinition,
    semantic_attestation_sha256,
)
from test_post_review_auto_refresh_contract import (
    _ExactPreviewDouble,
    _PostReviewContractRuntime,
    _review_completion_id,
    _wait_for_status,
)
from test_project_catalog_v2 import _write_completed_analysis


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


def test_high_resolution_reference_uses_bounded_preview_but_keeps_source_hash(
    tmp_path,
):
    blank = _bmp(width=800, height=800)
    original = (
        blank[:54]
        + hashlib.shake_256(b"trafficvisson-preview-test").digest(
            len(blank) - 54
        )
    )
    source = _write_source(
        tmp_path / DOCUMENT_NAME,
        image=original,
    )
    catalog = _catalog(
        source,
        maximum_stream_image_bytes=128 * 1024,
    )
    result = catalog.search(
        issue_type="roadside_mowing_required",
        query_text="삭초",
        limit=3,
    )
    case = result["cases"][0]

    preview = catalog.resolve_case_preview(
        case["source_id"],
        case["case_id"],
    )

    assert preview.derived_thumbnail is True
    assert preview.media_type == "image/jpeg"
    assert preview.size_bytes <= 128 * 1024
    assert preview.sha256 == hashlib.sha256(preview.body).hexdigest()
    assert preview.source_sha256 == hashlib.sha256(original).hexdigest()
    assert preview.source_size_bytes == len(original)
    assert preview.sha256 != preview.source_sha256


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
        listed_payload = listed.json()
        assert listed_payload["ready_source_count"] == 1
        companion = listed_payload["sources"][0]["companion_pdf"]
        assert companion["quarter"] == 3
        assert companion["status"] == "pdf_missing"
        assert companion["pdf_images_added_as_cases"] is False
        assert companion["automatic_training_ingest"] is False

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
        assert payload["source_states"][0]["companion_pdf"][
            "status"
        ] == "pdf_missing"
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
        assert image.headers[
            "x-trafficvisson-derived-thumbnail"
        ] == "0"
        assert image.headers[
            "x-trafficvisson-source-sha256"
        ] == case["image_sha256"]

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
    bridge_root = (tmp_path / "bridge-data").resolve()
    selected_fixture = _write_completed_analysis(
        bridge_root,
        project_id=DEMO_PROJECT_ID,
        source_seed="approved-reference-bootstrap",
        candidate_count=1,
    )

    class AuditedFixtureCatalog(WeakCaseCatalog):
        def case_attestation(self, source_id, case_id):
            result = super().case_attestation(source_id, case_id)
            result["sample_layout_binding"] = {
                "sample_layout_binding_schema": (
                    "trafficvisson.hwpx-sample-layout.v2"
                ),
                "sample_layout_binding_revision": 2,
                "semantic_projection_revision": (
                    "audited-photo-caption-evidence-group-v2"
                ),
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
                "sample_evidence_group_id": (
                    "sample-evidence-group-" + "9" * 24
                ),
                "group_photo_index": 1,
                "group_photo_count": 1,
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
            result["semantic_attestation_sha256"] = (
                semantic_attestation_sha256(result)
            )
            return result

    catalog = AuditedFixtureCatalog(
        source.parent.resolve(),
        definitions=(_definition(source),),
    )
    app = create_app(
        TOKEN,
        fixture_mode=True,
        allow_test_client=True,
        bridge_data_root=str(bridge_root),
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
        searched_payload = searched.json()
        assert searched_payload["approved_reference_count"] == 0
        assert searched_payload["training_eligible_count"] == 0
        assert searched_payload["annotation_count"] == 0
        assert searched_payload["incident_count"] == 0
        case = searched_payload["cases"][0]
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
        assert approval["structure_target_summary_sha256"] == case[
            "structure_target_summary_sha256"
        ]
        assert approval["index_manifest_sha256"] == case[
            "index_manifest_sha256"
        ]
        assert approval["image_sha256"] == case["image_sha256"]
        assert approval["image_media_type"] == case["image_media_type"]
        assert approval["image_width"] == case["image_width"]
        assert approval["image_height"] == case["image_height"]
        assert approval["caption"] == case["caption"]
        assert approval["route"] == case["route"]
        assert approval["opinion"] == case["opinion"]
        assert approval["weak_candidates"] == {
            "caption": case["caption"],
            "route": case["route"],
            "location": case["location"],
            "evaluation": case["evaluation"],
            "opinion": case["opinion"],
        }
        assert approval["quarter"] == 3
        assert approval["section_path"] == case["section_path"]
        assert approval["section_index"] == case["section_index"]
        assert approval["picture_target_id"] == case["picture_target_id"]
        assert approval["picture_index"] == case["picture_index"]
        assert approval["photo_page_number"] == 2
        assert approval["inspection_page_number"] == 1
        assert approval["table_id"].startswith("hwpx-table-")
        assert approval["table_index"] >= 0
        assert approval["cell_target_id"] == case["cell_target_id"]
        assert approval["cell_row"] >= 0
        assert approval["cell_column"] >= 0
        assert approval["idempotent_replay"] is False
        selected = (
            client.app.state.weak_case_approvals.list_by_approval_ids(
                DEMO_PROJECT_ID,
                [approval["approval_id"]],
            )
        )
        assert [record["approval_id"] for record in selected] == [
            approval["approval_id"]
        ]
        # Internal consumers do not pass through the HTTP response model.
        # Even a self-digested durable row must therefore fail closed if it
        # tries to turn this historical seed into current-video truth or drops
        # its exact audited photo-cell address.
        for changed_field, changed_value in (
            ("incident_created", True),
            ("training_scope", "current_video_confirmed"),
            ("cell_row", None),
        ):
            tampered = {
                key: value
                for key, value in approval.items()
                if key not in {"record_sha256", "idempotent_replay"}
            }
            tampered[changed_field] = changed_value
            tampered_sha256 = hashlib.sha256(
                json.dumps(
                    tampered,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            tampered["record_sha256"] = tampered_sha256
            with pytest.raises(
                WeakCaseApprovalError,
                match="무결성 검사가 실패",
            ):
                WeakCaseApprovalStore._row_record(
                    {
                        "payload_json": json.dumps(
                            tampered,
                            ensure_ascii=False,
                            allow_nan=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "record_sha256": tampered_sha256,
                        "project_id": str(DEMO_PROJECT_ID),
                        "source_id": approval["source_id"],
                        "case_id": approval["case_id"],
                        "approval_id": approval["approval_id"],
                        "revision": approval["revision"],
                    }
                )
        assert (
            client.app.state.weak_case_approvals.list_by_approval_ids(
                DEMO_PROJECT_ID,
                [],
            )
            == []
        )

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
        assert client.app.state.store.list_current_annotations(
            DEMO_PROJECT_ID
        ) == []
        assert client.app.state.store.list_incidents(
            DEMO_PROJECT_ID
        ) == []
        assert client.app.state.store.list_dataset_versions(
            DEMO_PROJECT_ID
        ) == []

        # Exercise the real review-complete HTTP boundary against the same
        # project-scoped approval ledger. All media/model/report integrations
        # remain deterministic fixture doubles; no installed sample or user
        # data is read or changed.
        exact_preview = _ExactPreviewDouble()
        client.app.state.derived_outputs._report_preparer = exact_preview
        bridge_runtime = BridgePipelineRuntime(
            object(),
            data_root=bridge_root,
            candidate_labels=client.app.state.candidate_labels,
            derived_outputs=client.app.state.derived_outputs,
        )
        runtime = _PostReviewContractRuntime(
            bridge_runtime=bridge_runtime,
            store=client.app.state.store,
            selected_analysis=selected_fixture["payload"],
            labels=client.app.state.candidate_labels,
            semantic_map_sha256=hashlib.sha256(
                b"fixture-approved-reference-bootstrap-map"
            ).hexdigest(),
        )
        client.app.state.pipeline_jobs.shutdown()
        pipeline = PipelineJobService(runtime, data_root=bridge_root)
        client.app.state.pipeline_jobs = pipeline

        pipeline.configure_profile(
            DEMO_PROJECT_ID,
            question_policy="learning_assist",
            review_mode="precision",
            default_template_id="sample-q4-v1",
            default_template_sha256=hashlib.sha256(
                b"fixture-sample-q4-template"
            ).hexdigest(),
            detector_pack_id="fixture-road-detector-v1",
            ocr_pack_id="fixture-korean-ocr-v1",
            sam2_pack_id="fixture-sam2-v1",
            report_provider="codex",
            report_transmission_consent=False,
            template_semantic_map_id="sample-q4-semantic-v1",
            report_route_name="부트스트랩 계약 노선",
            report_inspector_name="계약 점검자",
            report_evidence_mode="internal_original",
            output_location_id="project-default",
            candidate_rescan_fps=8,
        )
        started, replayed = pipeline.start(
            DEMO_PROJECT_ID,
            snapshot_id=selected_fixture["snapshot_id"],
            file_name="20260730_bootstrap_drive.mp4",
            rights_confirmed=True,
            idempotency_key="approved-reference-bootstrap-pipeline-v1",
        )
        assert replayed is False
        waiting = _wait_for_status(
            pipeline,
            DEMO_PROJECT_ID,
            UUID(str(started["job_id"])),
            {"awaiting_review"},
        )
        assert waiting["review_completion_id"] is None

        candidate = selected_fixture["payload"]["result"]["candidates"][0]
        answered = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/"
                f"{selected_fixture['job_id']}/candidates/"
                f"{candidate['candidate_id']}/answer"
            ),
            headers=_headers(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": str(selected_fixture["job_id"]),
                "answer": "yes",
                "selected_issue_type": "road_debris",
                "severity": "major",
                "maintenance_action": "낙하물 수거",
            },
        )
        assert answered.status_code == 201, answered.text

        # The append-only answer is still audit-only until the exact queue is
        # completed. Sample approval also remains auxiliary training evidence:
        # neither action may create current Incident/report truth on its own.
        pre_completion_incidents = client.get(
            f"/projects/{DEMO_PROJECT_ID}/incidents",
            headers=_headers(),
        )
        assert pre_completion_incidents.status_code == 200
        assert pre_completion_incidents.json()["count"] == 0
        assert client.app.state.store.list_current_annotations(
            DEMO_PROJECT_ID
        ) == []
        assert client.app.state.store.list_dataset_versions(
            DEMO_PROJECT_ID
        ) == []

        completion_id = _review_completion_id(answered.json())
        completed_request = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/pipeline/jobs/"
                f"{started['job_id']}/review-complete"
            ),
            headers=_headers(),
            json={"review_completion_id": completion_id},
        )
        assert completed_request.status_code == 202, completed_request.text
        completed = _wait_for_status(
            pipeline,
            DEMO_PROJECT_ID,
            UUID(str(started["job_id"])),
            {"completed", "blocked"},
        )
        assert completed["status"] == "completed"
        assert completed["review_completion_id"] == completion_id

        annotations = client.app.state.store.list_current_annotations(
            DEMO_PROJECT_ID
        )
        assert len(annotations) == 1
        assert annotations[0]["analysis_job_id"] == str(
            selected_fixture["job_id"]
        )
        incidents = client.get(
            f"/projects/{DEMO_PROJECT_ID}/incidents",
            headers=_headers(),
        )
        assert incidents.status_code == 200, incidents.text
        # This is the bootstrap/training-video review. It may create a
        # user-confirmed Annotation and immutable DatasetVersion, but it is not
        # an independently verified promoted-model road survey. Incident,
        # map/report truth therefore remains empty until that later survey
        # crosses its own full-journey QA boundary.
        assert incidents.json()["count"] == 0
        datasets = client.get(
            f"/projects/{DEMO_PROJECT_ID}/datasets",
            headers=_headers(),
        )
        assert datasets.status_code == 200, datasets.text
        dataset_rows = datasets.json()["versions"]
        assert len(dataset_rows) == 1
        dataset = dataset_rows[0]
        assert dataset["training_gate_member_count"] == 1
        assert dataset["approved_reference_member_count"] == 1
        assert dataset["approved_reference_training_event_count"] == 1
        assert dataset["approved_reference_members"][0][
            "approval_id"
        ] == approval["approval_id"]
        assert (
            dataset["approved_references_count_toward_training_gate"]
            is False
        )
        assert (
            dataset["approved_references_count_toward_validation"]
            is False
        )
        # Training-only bootstrap evidence must not be projected into the HWPX
        # plan either. The exact preview is prepared only after a promoted
        # model's independent survey receives user QA authority.
        assert exact_preview.calls == []

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
        bridge_data_root=str(bridge_root),
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

    # A locally resealed SQLite row must still be compared with the immutable
    # server-owned sample catalog before DatasetVersion reuse. Merely updating
    # record_sha256 cannot move a marker/caption cell to another address.
    database_path = (
        bridge_root
        / "approved-reference-cases"
        / "approvals.sqlite3"
    )
    with sqlite3.connect(str(database_path)) as connection:
        row = connection.execute(
            """
            SELECT payload_json
            FROM approved_reference_cases
            WHERE project_id = ? AND approval_id = ?
            """,
            (str(DEMO_PROJECT_ID), approval["approval_id"]),
        ).fetchone()
        assert row is not None
        resealed = json.loads(row[0])
        resealed["semantic_cells"][0]["row"] += 1
        without_hash = {
            key: value
            for key, value in resealed.items()
            if key != "record_sha256"
        }
        record_sha256 = hashlib.sha256(
            json.dumps(
                without_hash,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        resealed["record_sha256"] = record_sha256
        connection.execute(
            """
            UPDATE approved_reference_cases
            SET payload_json = ?, record_sha256 = ?
            WHERE project_id = ? AND approval_id = ?
            """,
            (
                json.dumps(
                    resealed,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                record_sha256,
                str(DEMO_PROJECT_ID),
                approval["approval_id"],
            ),
        )
        connection.commit()

    with pytest.raises(
        WeakCaseApprovalError,
        match="문서·사진·페이지·셀 근거가 변경",
    ):
        restarted.state.weak_case_approvals.list_project(
            DEMO_PROJECT_ID
        )
