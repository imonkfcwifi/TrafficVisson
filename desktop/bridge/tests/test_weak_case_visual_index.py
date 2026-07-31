from __future__ import annotations

import hashlib
import json
import math
import os
import struct
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.vision_inference import VisionModelPack
from app.weak_case_catalog import (
    ResolvedWeakCaseImage,
    WEAK_CASE_VISUAL_SOURCE_SCHEMA,
)
from app.weak_case_visual_index import (
    CandidateFrameEvidence,
    IMAGE_EMBEDDING_MODEL_SCHEMA,
    VerifiedLocalClipImageEmbedder,
    WeakCaseVisualIndexError,
    WeakCaseVisualIndexLimits,
    WeakCaseVisualIndexService,
)


def _bmp(marker: int, width: int = 3, height: int = 3) -> bytes:
    row_bytes = ((width * 3 + 3) // 4) * 4
    pixels = bytes([marker]) + bytes(row_bytes * height - 1)
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


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _case(index: int, image: bytes) -> dict:
    quarter = 3 if index <= 3 else 4
    document_sha = (
        hashlib.sha256(f"document-{quarter}".encode()).hexdigest()
    )
    source_id = (
        f"sample-hwpx-{quarter}q-"
        + hashlib.sha256(f"source-{quarter}".encode()).hexdigest()[:16]
    )
    case_id = (
        "hwpx-weak-case-"
        + hashlib.sha256(f"case-{index}".encode()).hexdigest()[:32]
    )
    picture_target = (
        "hwpx-picture-"
        + hashlib.sha256(f"picture-{index}".encode()).hexdigest()[:24]
    )
    table_id = (
        "hwpx-table-"
        + hashlib.sha256(f"table-{index}".encode()).hexdigest()[:24]
    )
    cell_id = (
        "hwpx-cell-"
        + hashlib.sha256(f"cell-{index}".encode()).hexdigest()[:24]
    )
    caption = (
        "표지판 주변 수풀 및 삭초 필요"
        if index == 4
        else f"도로 점검 사진 {index}"
    )
    return {
        "source_id": source_id,
        "case_id": case_id,
        "quarter": quarter,
        "document_name": f"{quarter}분기 점검 내용 정리.hwpx",
        "document_sha256": document_sha,
        "structure_target_summary_sha256": hashlib.sha256(
            f"structure-{quarter}".encode()
        ).hexdigest(),
        "index_manifest_sha256": hashlib.sha256(
            f"index-{quarter}".encode()
        ).hexdigest(),
        "section_path": "Contents/section0.xml",
        "section_index": 0,
        "page_hint": index * 2,
        "page_hint_source": (
            "explicit_page_break_sequence_hint_not_rendered_page"
        ),
        "inspection_page_number": index * 2 - 1,
        "photo_page_number": index * 2,
        "page_pair_index": index - 1,
        "photo_slot_id": (
            "sample-photo-slot-"
            + hashlib.sha256(f"slot-{index}".encode()).hexdigest()[:24]
        ),
        "sample_layout_binding_schema": (
            "trafficvisson.hwpx-sample-layout.v2"
        ),
        "sample_layout_binding_revision": 2,
        "semantic_projection_revision": (
            "audited-photo-caption-evidence-group-v2"
        ),
        "sample_evidence_group_id": (
            "sample-evidence-group-"
            + hashlib.sha256(f"group-{index}".encode()).hexdigest()[:24]
        ),
        "group_photo_index": 1,
        "group_photo_count": 1,
        "sample_layout_binding_sha256": hashlib.sha256(
            f"binding-{index}".encode()
        ).hexdigest(),
        "semantic_attestation_sha256": hashlib.sha256(
            f"semantic-{index}".encode()
        ).hexdigest(),
        "table_id": table_id,
        "table_index": index * 3 - 1,
        "cell_target_id": cell_id,
        "cell_row": 0,
        "cell_column": index % 2,
        "picture_target_id": picture_target,
        "picture_index": index,
        "picture_xml_id": f"picture-{index}",
        "picture_address_sha256": hashlib.sha256(
            f"address-{index}".encode()
        ).hexdigest(),
        "image_sha256": _sha(image),
        "image_media_type": "image/bmp",
        "image_size_bytes": len(image),
        "image_width": 3,
        "image_height": 3,
        "duplicate_image_occurrence_count": 1,
        "duplicate_image_review_required": False,
        "duplicate_image_registry_complete": True,
        "duplicate_image_case_refs": [
            f"{source_id}/{case_id}"
        ],
        "caption": caption,
        "route": "시험고속도로",
        "location": f"{index}.0km",
        "evaluation": "도로 유지관리",
        "opinion": "현장 확인 후 조치 필요",
        "search_texts": [
            caption,
            "시험고속도로",
            "현장 확인 후 조치 필요",
        ],
        "reference_state": "weak_unconfirmed",
        "review_required": True,
        "approved_reference": False,
        "training_eligible": False,
        "annotation_created": False,
        "incident_created": False,
    }


class FakeCatalog:
    def __init__(self) -> None:
        self.images = {
            index: _bmp(index)
            for index in range(1, 6)
        }
        self.cases = [
            _case(index, self.images[index])
            for index in range(1, 6)
        ]
        self.stale = False

    def visual_index_snapshot(self) -> dict:
        cases = [dict(case) for case in self.cases]
        if self.stale:
            cases[0]["image_sha256"] = "f" * 64
        sources = []
        for quarter in (3, 4):
            rows = [case for case in cases if case["quarter"] == quarter]
            sources.append(
                {
                    "source_id": rows[0]["source_id"],
                    "quarter": quarter,
                    "document_name": rows[0]["document_name"],
                    "document_sha256": rows[0]["document_sha256"],
                    "structure_target_summary_sha256": rows[0][
                        "structure_target_summary_sha256"
                    ],
                    "index_manifest_sha256": rows[0][
                        "index_manifest_sha256"
                    ],
                    "photo_case_count": len(rows),
                    "excluded_fixed_picture_count": 24,
                }
            )
        for source in sources:
            source["companion_pdf"] = _pdf_companion(source, cases)
        return {
            "schema_version": WEAK_CASE_VISUAL_SOURCE_SCHEMA,
            "catalog_role": (
                "sample_hwpx_audited_photo_slots_weak_unconfirmed"
            ),
            "case_count": len(cases),
            "evidence_group_count": len(cases),
            "duplicate_image_group_count": 0,
            "duplicate_image_case_count": 0,
            "source_count": len(sources),
            "sources": sources,
            "cases": cases,
            "review_required_before_promotion": True,
            "automatic_annotation_creation": False,
            "automatic_incident_creation": False,
            "automatic_training_ingest": False,
            "annotation_count": 0,
            "incident_count": 0,
            "training_eligible_count": 0,
        }

    def resolve_case_image_for_visual_index(
        self,
        source_id: str,
        case_id: str,
    ) -> ResolvedWeakCaseImage:
        record = next(
            row
            for row in self.cases
            if row["source_id"] == source_id
            and row["case_id"] == case_id
        )
        index = self.cases.index(record) + 1
        body = self.images[index]
        return ResolvedWeakCaseImage(
            body=body,
            media_type="image/bmp",
            size_bytes=len(body),
            sha256=_sha(body),
        )


def _pdf_companion(source: dict, cases: list[dict]) -> dict:
    page_rows = [
        {
            "page_number": page_number,
            "text_sha256": hashlib.sha256(
                f"page-{page_number}".encode()
            ).hexdigest(),
            "text_char_count": len(f"page-{page_number}"),
            "text_extraction_status": "extracted",
        }
        for page_number in range(1, 49)
    ]
    render_rows = [
        {
            "page_number": page_number,
            "width": 298,
            "height": 421,
            "size_bytes": 1024 + page_number,
            "sha256": hashlib.sha256(
                f"render-{page_number}".encode()
            ).hexdigest(),
            "media_type": "image/png",
        }
        for page_number in (1, 24, 48)
    ]
    payload = {
        "schema_version": "trafficvisson.pdf-companion-audit.v1",
        "role": "official_render_cross_validation_only",
        "quarter": source["quarter"],
        "document_name": (
            f"{source['quarter']}분기 점검 내용 정리.pdf"
        ),
        "pdf_sha256": hashlib.sha256(
            f"pdf-{source['quarter']}".encode()
        ).hexdigest(),
        "expected_pdf_sha256": hashlib.sha256(
            f"pdf-{source['quarter']}".encode()
        ).hexdigest(),
        "size_bytes": 4096,
        "status": "ready",
        "page_count": 48,
        "expected_page_count": 48,
        "pages": page_rows,
        "full_text_sha256": hashlib.sha256(
            f"full-text-{source['quarter']}".encode()
        ).hexdigest(),
        "full_text_char_count": sum(
            row["text_char_count"] for row in page_rows
        ),
        "text_hash_method": (
            "sha256(nfc_lf_normalized_pypdf_text_utf8)"
        ),
        "full_text_hash_method": (
            "sha256(form_feed_joined_normalized_page_text_utf8)"
        ),
        "parser": "pypdf",
        "parser_version": "6.5.0",
        "strict_parsing": True,
        "render": {
            "status": "smoke_passed",
            "renderer": "pdftoppm",
            "renderer_version": "pdftoppm version 25.06.0",
            "render_dpi": 36,
            "requested_pages": [1, 24, 48],
            "rendered_pages": render_rows,
        },
        "pdf_images_added_as_cases": False,
        "case_source": "paired_hwpx_photo_slots_only",
        "automatic_annotation_creation": False,
        "automatic_incident_creation": False,
        "automatic_training_ingest": False,
    }
    payload["audit_sha256"] = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    source_cases = [
        case
        for case in cases
        if case["source_id"] == source["source_id"]
    ]
    page_counts = {}
    for case in source_cases:
        page_number = case["photo_page_number"]
        page_counts[page_number] = page_counts.get(page_number, 0) + 1
    payload["hwpx_photo_case_correspondence"] = {
        "status": "verified",
        "hwpx_photo_case_count": len(source_cases),
        "mapped_hwpx_photo_case_count": len(source_cases),
        "unverified_hwpx_photo_case_count": 0,
        "pdf_photo_case_count_added": 0,
        "page_count": len(page_counts),
        "pages": [
            {
                "pdf_page_number": page_number,
                "mapped_hwpx_photo_case_count": count,
                "correspondence_status": (
                    "verified_pdf_page_address"
                ),
                "page_text_sha256": page_rows[
                    page_number - 1
                ]["text_sha256"],
                "render_validation": (
                    "document_render_smoke_passed_page_not_sampled"
                ),
            }
            for page_number, count in sorted(page_counts.items())
        ],
    }
    return payload


class PdfFakeCatalog(FakeCatalog):
    def __init__(self, *, pdf_status: str = "ready") -> None:
        super().__init__()
        self.pdf_status = pdf_status

    def visual_index_snapshot(self) -> dict:
        snapshot = super().visual_index_snapshot()
        for source in snapshot["sources"]:
            companion = _pdf_companion(source, snapshot["cases"])
            if self.pdf_status != "ready":
                companion["status"] = self.pdf_status
            source["companion_pdf"] = companion
        return snapshot


class MissingPdfCatalog(FakeCatalog):
    def visual_index_snapshot(self) -> dict:
        snapshot = super().visual_index_snapshot()
        snapshot["sources"][0].pop("companion_pdf")
        return snapshot


class FakeEmbedder:
    def __init__(self) -> None:
        self.changed = False
        self.embedding_calls = 0
        self.vectors = {
            _sha(_bmp(1)): (1.0, 0.0, 0.0),
            _sha(_bmp(2)): (0.9, 0.1, 0.0),
            _sha(_bmp(3)): (0.8, 0.2, 0.0),
            _sha(_bmp(4)): (0.0, 1.0, 0.0),
            _sha(_bmp(5)): (-1.0, 0.0, 0.0),
            _sha(_bmp(99)): (1.0, 0.0, 0.0),
        }

    def model_identity(self) -> dict:
        marker = "changed" if self.changed else "stable"
        return {
            "schema_version": IMAGE_EMBEDDING_MODEL_SCHEMA,
            "model_id": "fake-clip",
            "model_version": "1.0.0",
            "engine": "deterministic_test_embedding",
            "model_sha256": hashlib.sha256(
                f"model-{marker}".encode()
            ).hexdigest(),
            "manifest_sha256": hashlib.sha256(
                f"manifest-{marker}".encode()
            ).hexdigest(),
            "local_only": True,
            "network_download_allowed": False,
        }

    def embed_image(self, body: bytes, media_type: str):
        assert media_type == "image/bmp"
        self.embedding_calls += 1
        return self.vectors[_sha(body)]


class BatchFakeEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.batch_calls: list[tuple[str, ...]] = []

    def embed_images(self, images):
        self.batch_calls.append(
            tuple(_sha(body) for body, _media_type in images)
        )
        # Make the vector observably depend on physical batch width.  The
        # service must use one fixed padded width for query() and query_many()
        # or the exact public response parity assertion below will fail.
        width_marker = len(images) / 1_000.0
        return tuple(
            (
                self.vectors[_sha(body)][0],
                self.vectors[_sha(body)][1] + width_marker,
                self.vectors[_sha(body)][2],
            )
            for body, media_type in images
            if media_type == "image/bmp"
        )


class OomBatchFakeEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.batch_attempts = 0
        self.oom_enabled = False

    def embed_images(self, images):
        self.batch_attempts += 1
        if not self.oom_enabled:
            return tuple(
                self.vectors[_sha(body)]
                for body, _media_type in images
            )
        raise WeakCaseVisualIndexError(
            "embedding_batch_oom",
            "bounded test OOM",
        )


class FakeApprovals:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def list_project(self, project_id: UUID) -> dict:
        return {
            "project_id": str(project_id),
            "records": [dict(record) for record in self.records],
        }


def _candidate(project_id: UUID) -> CandidateFrameEvidence:
    body = _bmp(99)
    return CandidateFrameEvidence(
        project_id=project_id,
        candidate_id="candidate-0001",
        body=body,
        media_type="image/bmp",
        expected_sha256=_sha(body),
    )


def _candidate_with_id(
    project_id: UUID,
    candidate_id: str,
) -> CandidateFrameEvidence:
    body = _bmp(99)
    return CandidateFrameEvidence(
        project_id=project_id,
        candidate_id=candidate_id,
        body=body,
        media_type="image/bmp",
        expected_sha256=_sha(body),
    )


def _approval(project_id: UUID, case: dict) -> dict:
    return {
        "project_id": str(project_id),
        "source_id": case["source_id"],
        "case_id": case["case_id"],
        "approval_id": (
            "approved-reference-"
            + hashlib.sha256(case["case_id"].encode()).hexdigest()[:24]
        ),
        "revision": 1,
        "record_sha256": hashlib.sha256(
            f"approval-{case['case_id']}".encode()
        ).hexdigest(),
        "reference_state": "approved_reference",
        "approved_reference": True,
        "user_confirmed": True,
        "training_eligible": True,
        "annotation_created": False,
        "incident_created": False,
        "document_sha256": case["document_sha256"],
        "index_manifest_sha256": case["index_manifest_sha256"],
        "image_sha256": case["image_sha256"],
        "sample_layout_binding_sha256": case[
            "sample_layout_binding_sha256"
        ],
        "sample_layout_binding_schema": case[
            "sample_layout_binding_schema"
        ],
        "sample_layout_binding_revision": case[
            "sample_layout_binding_revision"
        ],
        "semantic_projection_revision": case[
            "semantic_projection_revision"
        ],
        "semantic_attestation_sha256": case[
            "semantic_attestation_sha256"
        ],
        "sample_evidence_group_id": case[
            "sample_evidence_group_id"
        ],
        "group_photo_index": case["group_photo_index"],
        "group_photo_count": case["group_photo_count"],
        "duplicate_image_occurrence_count": case[
            "duplicate_image_occurrence_count"
        ],
        "duplicate_image_review_required": case[
            "duplicate_image_review_required"
        ],
        "duplicate_image_case_refs": case[
            "duplicate_image_case_refs"
        ],
        "issue_type": "roadside_mowing_required",
        "maintenance_action": "삭초 필요",
    }


def test_builds_content_addressed_project_index_and_queries_three_to_five(
    tmp_path,
):
    project_id = uuid4()
    catalog = FakeCatalog()
    embedder = FakeEmbedder()
    service = WeakCaseVisualIndexService(
        tmp_path.resolve(),
        catalog=catalog,
        embedder=embedder,
    )

    first = service.build(project_id)
    assert embedder.embedding_calls == 5
    second = service.build(project_id)
    assert embedder.embedding_calls == 5

    assert first["state"] == "ready"
    assert first["case_count"] == 5
    assert first["weak_unconfirmed_count"] == 5
    assert first["approved_reference_count"] == 0
    assert first["annotation_count"] == 0
    assert first["incident_count"] == 0
    assert first["training_eligible_count"] == 0
    assert first["index_manifest_sha256"] == second[
        "index_manifest_sha256"
    ]
    assert first["idempotent_replay"] is False
    assert second["idempotent_replay"] is True
    project_directory = tmp_path / str(project_id)
    index_files = list(
        project_directory.glob("weak-reference-index-*.json")
    )
    assert len(index_files) == 1
    persisted = json.loads(index_files[0].read_text("utf-8"))
    assert persisted["project_id"] == str(project_id)
    assert persisted["case_count"] == 5
    assert persisted["annotation_count"] == 0
    assert persisted["incident_count"] == 0
    assert persisted["training_eligible_count"] == 0
    assert all(
        math.isclose(
            math.sqrt(sum(value * value for value in row["embedding"])),
            1.0,
        )
        for row in persisted["cases"]
    )

    result = service.query(
        project_id,
        _candidate(project_id),
        issue_type="roadside_mowing_required",
        query_text="삭초",
        limit=3,
    )

    assert result["semantic_image_embedding_used"] is True
    assert result["search_mode"] == (
        "local_image_cosine_plus_bounded_text_structure"
    )
    assert result["result_count"] == 3
    assert result["weak_unconfirmed_count"] == 3
    assert result["approved_reference_count"] == 0
    assert result["annotation_count"] == 0
    assert result["incident_count"] == 0
    assert result["cases"][0]["case_id"] == catalog.cases[0]["case_id"]
    assert result["cases"][0]["visual_cosine_score"] == 1.0
    assert all(
        "embedding" not in row and "search_texts" not in row
        for row in result["cases"]
    )
    assert all(
        row["reference_state"] == "weak_unconfirmed"
        and row["review_required"] is True
        and row["training_eligible"] is False
        for row in result["cases"]
    )


def test_query_many_is_exact_query_parity_in_fixed_bounded_batches(
    tmp_path,
):
    project_id = uuid4()
    catalog = FakeCatalog()
    approvals = FakeApprovals()
    embedder = BatchFakeEmbedder()
    service = WeakCaseVisualIndexService(
        tmp_path.resolve(),
        catalog=catalog,
        embedder=embedder,
        approvals=approvals,
    )
    service.build(project_id)
    assert len(embedder.batch_calls) == 1
    assert len(embedder.batch_calls[0]) == 8
    embedder.batch_calls.clear()
    approvals.records = [_approval(project_id, catalog.cases[0])]
    candidates = tuple(
        _candidate_with_id(project_id, f"candidate-{index:04d}")
        for index in range(17)
    )
    issue_types = {
        candidate.candidate_id: "roadside_mowing_required"
        for candidate in candidates
    }
    query_texts = {
        candidate.candidate_id: "삭초"
        for candidate in candidates
    }

    legacy = {
        candidate.candidate_id: service.query(
            project_id,
            candidate,
            issue_type=issue_types[candidate.candidate_id],
            query_text=query_texts[candidate.candidate_id],
            limit=5,
        )
        for candidate in candidates
    }
    embedder.batch_calls.clear()

    batched = service.query_many(
        project_id,
        candidates,
        issue_types_by_candidate=issue_types,
        query_texts_by_candidate=query_texts,
        limit=5,
    )

    assert batched == legacy
    assert list(batched) == [
        candidate.candidate_id for candidate in candidates
    ]
    assert len(embedder.batch_calls) == 3
    assert {
        len(call) for call in embedder.batch_calls
    } == {service._limits.max_query_batch_size}
    assert all(
        result["annotation_count"] == 0
        and result["incident_count"] == 0
        and result["automatic_annotation_creation"] is False
        and result["automatic_incident_creation"] is False
        and result["automatic_training_ingest"] is False
        for result in batched.values()
    )


def test_query_many_legacy_embedder_fallback_preserves_exact_results(
    tmp_path,
):
    project_id = uuid4()
    embedder = FakeEmbedder()
    service = WeakCaseVisualIndexService(
        tmp_path.resolve(),
        catalog=FakeCatalog(),
        embedder=embedder,
    )
    service.build(project_id)
    candidates = tuple(
        _candidate_with_id(project_id, f"candidate-{index:04d}")
        for index in range(3)
    )
    expected = {
        candidate.candidate_id: service.query(
            project_id,
            candidate,
            issue_type=None,
            query_text=None,
            limit=3,
        )
        for candidate in candidates
    }

    actual = service.query_many(
        project_id,
        candidates,
        issue_types_by_candidate={
            candidate.candidate_id: None
            for candidate in candidates
        },
        limit=3,
    )

    assert actual == expected


def test_query_many_prevalidates_all_evidence_before_embedding(
    tmp_path,
):
    project_id = uuid4()
    embedder = BatchFakeEmbedder()
    service = WeakCaseVisualIndexService(
        tmp_path.resolve(),
        catalog=FakeCatalog(),
        embedder=embedder,
    )
    service.build(project_id)
    valid = _candidate_with_id(project_id, "candidate-valid")
    invalid = _candidate_with_id(project_id, "candidate-invalid")
    invalid = CandidateFrameEvidence(
        project_id=invalid.project_id,
        candidate_id=invalid.candidate_id,
        body=invalid.body,
        media_type=invalid.media_type,
        expected_sha256="0" * 64,
    )
    embedder.batch_calls.clear()

    with pytest.raises(WeakCaseVisualIndexError) as captured:
        service.query_many(
            project_id,
            (valid, invalid),
            issue_types_by_candidate={
                valid.candidate_id: None,
                invalid.candidate_id: None,
            },
            limit=3,
        )

    assert captured.value.code == "candidate_image_hash_mismatch"
    assert embedder.batch_calls == []


def test_query_many_batch_oom_fails_closed_without_single_fallback(
    tmp_path,
):
    project_id = uuid4()
    embedder = OomBatchFakeEmbedder()
    service = WeakCaseVisualIndexService(
        tmp_path.resolve(),
        catalog=FakeCatalog(),
        embedder=embedder,
    )
    service.build(project_id)
    singles_after_build = embedder.embedding_calls
    attempts_after_build = embedder.batch_attempts
    embedder.oom_enabled = True
    candidate = _candidate_with_id(project_id, "candidate-oom")

    with pytest.raises(WeakCaseVisualIndexError) as captured:
        service.query_many(
            project_id,
            (candidate,),
            issue_types_by_candidate={candidate.candidate_id: None},
            limit=3,
        )

    assert captured.value.code == "embedding_batch_oom"
    assert embedder.batch_attempts == attempts_after_build + 1
    assert embedder.embedding_calls == singles_after_build


def test_query_many_enforces_candidate_identity_and_batch_bounds(
    tmp_path,
):
    project_id = uuid4()
    service = WeakCaseVisualIndexService(
        tmp_path.resolve(),
        catalog=FakeCatalog(),
        embedder=FakeEmbedder(),
    )
    service.build(project_id)
    candidate = _candidate_with_id(project_id, "candidate-duplicate")

    for candidates, issue_types in (
        (
            (candidate, candidate),
            {candidate.candidate_id: None},
        ),
        (
            tuple(
                _candidate_with_id(
                    project_id,
                    f"candidate-{index:04d}",
                )
                for index in range(129)
            ),
            {},
        ),
    ):
        with pytest.raises(WeakCaseVisualIndexError) as captured:
            service.query_many(
                project_id,
                candidates,
                issue_types_by_candidate=issue_types,
                limit=3,
            )
        assert captured.value.code == "visual_query_batch_invalid"

    with pytest.raises(ValueError):
        WeakCaseVisualIndexLimits(max_query_batch_size=7)


def test_duplicate_image_hash_cannot_be_silently_treated_as_independent(
    tmp_path,
):
    catalog = FakeCatalog()
    catalog.cases[1]["image_sha256"] = catalog.cases[0][
        "image_sha256"
    ]
    service = WeakCaseVisualIndexService(
        tmp_path.resolve(),
        catalog=catalog,
        embedder=FakeEmbedder(),
    )

    with pytest.raises(WeakCaseVisualIndexError) as caught:
        service.build(uuid4())

    assert caught.value.code == "visual_reference_case_invalid"


def test_pdf_companion_is_source_provenance_without_duplicate_cases(
    tmp_path,
):
    project_id = uuid4()
    catalog = PdfFakeCatalog()
    service = WeakCaseVisualIndexService(
        tmp_path.resolve(),
        catalog=catalog,
        embedder=FakeEmbedder(),
    )

    result = service.build(project_id)

    assert result["case_count"] == 5
    index_path = next(
        (tmp_path / str(project_id)).glob(
            "weak-reference-index-*.json"
        )
    )
    persisted = json.loads(index_path.read_text("utf-8"))
    assert persisted["case_count"] == 5
    assert len(persisted["sources"]) == 2
    assert all(
        "companion_pdf" not in case
        for case in persisted["cases"]
    )
    assert all(
        source["companion_pdf"]["status"] == "ready"
        and source["companion_pdf"][
            "pdf_images_added_as_cases"
        ]
        is False
        and source["companion_pdf"][
            "hwpx_photo_case_correspondence"
        ]["mapped_hwpx_photo_case_count"]
        == source["photo_case_count"]
        and source["companion_pdf"][
            "hwpx_photo_case_correspondence"
        ]["pdf_photo_case_count_added"]
        == 0
        for source in persisted["sources"]
    )


@pytest.mark.parametrize(
    "status",
    (
        "pdf_missing",
        "pdf_sha256_mismatch",
        "pdf_malformed",
    ),
)
def test_pdf_companion_failure_state_blocks_visual_index(
    tmp_path,
    status,
):
    service = WeakCaseVisualIndexService(
        tmp_path.resolve(),
        catalog=PdfFakeCatalog(pdf_status=status),
        embedder=FakeEmbedder(),
    )

    with pytest.raises(WeakCaseVisualIndexError) as captured:
        service.build(uuid4())

    assert captured.value.code == f"visual_reference_{status}"


def test_missing_pdf_provenance_field_blocks_visual_index(tmp_path):
    service = WeakCaseVisualIndexService(
        tmp_path.resolve(),
        catalog=MissingPdfCatalog(),
        embedder=FakeEmbedder(),
    )

    with pytest.raises(WeakCaseVisualIndexError) as captured:
        service.build(uuid4())

    assert captured.value.code == "visual_reference_source_invalid"


def test_project_approval_is_decorated_but_never_creates_truth(tmp_path):
    project_id = uuid4()
    catalog = FakeCatalog()
    approvals = FakeApprovals()
    service = WeakCaseVisualIndexService(
        tmp_path.resolve(),
        catalog=catalog,
        embedder=FakeEmbedder(),
        approvals=approvals,
    )
    service.build(project_id)
    approvals.records = [_approval(project_id, catalog.cases[0])]

    result = service.query(
        project_id,
        _candidate(project_id),
        issue_type=None,
        query_text=None,
        limit=5,
    )

    approved = next(
        row
        for row in result["cases"]
        if row["case_id"] == catalog.cases[0]["case_id"]
    )
    assert approved["reference_state"] == "approved_reference"
    assert approved["approved_reference"] is True
    assert approved["review_required"] is False
    assert approved["training_eligible"] is True
    assert approved["approval_record_sha256"] == approvals.records[0][
        "record_sha256"
    ]
    assert result["approved_reference_count"] == 1
    assert result["annotation_count"] == 0
    assert result["incident_count"] == 0
    assert result["automatic_annotation_creation"] is False
    assert result["automatic_incident_creation"] is False
    assert result["automatic_training_ingest"] is False


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        ("cross_project", "cross_project_approval_rejected"),
        ("stale_image", "stale_approval_reference"),
    ),
)
def test_cross_project_and_stale_approvals_fail_closed(
    tmp_path,
    mutation,
    expected_code,
):
    project_id = uuid4()
    catalog = FakeCatalog()
    approvals = FakeApprovals()
    service = WeakCaseVisualIndexService(
        tmp_path.resolve(),
        catalog=catalog,
        embedder=FakeEmbedder(),
        approvals=approvals,
    )
    service.build(project_id)
    record = _approval(project_id, catalog.cases[0])
    if mutation == "cross_project":
        record["project_id"] = str(uuid4())
    else:
        record["image_sha256"] = "0" * 64
    approvals.records = [record]

    with pytest.raises(WeakCaseVisualIndexError) as captured:
        service.query(
            project_id,
            _candidate(project_id),
            issue_type=None,
            query_text=None,
            limit=3,
        )
    assert captured.value.code == expected_code


def test_stale_source_model_tamper_and_candidate_hash_fail_closed(
    tmp_path,
):
    project_id = uuid4()
    catalog = FakeCatalog()
    embedder = FakeEmbedder()
    service = WeakCaseVisualIndexService(
        tmp_path.resolve(),
        catalog=catalog,
        embedder=embedder,
    )
    service.build(project_id)

    catalog.stale = True
    with pytest.raises(WeakCaseVisualIndexError) as stale_source:
        service.query(
            project_id,
            _candidate(project_id),
            issue_type=None,
            query_text=None,
            limit=3,
        )
    assert stale_source.value.code == "visual_reference_source_changed"
    catalog.stale = False

    embedder.changed = True
    with pytest.raises(WeakCaseVisualIndexError) as changed_model:
        service.query(
            project_id,
            _candidate(project_id),
            issue_type=None,
            query_text=None,
            limit=3,
        )
    assert changed_model.value.code == "embedding_model_changed"
    embedder.changed = False

    candidate = _candidate(project_id)
    bad_hash = CandidateFrameEvidence(
        project_id=project_id,
        candidate_id=candidate.candidate_id,
        body=candidate.body,
        media_type=candidate.media_type,
        expected_sha256="0" * 64,
    )
    with pytest.raises(WeakCaseVisualIndexError) as hash_error:
        service.query(
            project_id,
            bad_hash,
            issue_type=None,
            query_text=None,
            limit=3,
        )
    assert hash_error.value.code == "candidate_image_hash_mismatch"

    index_path = next(
        (tmp_path / str(project_id)).glob(
            "weak-reference-index-*.json"
        )
    )
    tampered = json.loads(index_path.read_text("utf-8"))
    tampered["cases"][0]["embedding"][0] = 0.125
    index_path.write_text(
        json.dumps(tampered, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(WeakCaseVisualIndexError) as tamper:
        service.query(
            project_id,
            _candidate(project_id),
            issue_type=None,
            query_text=None,
            limit=3,
        )
    assert tamper.value.code == "visual_index_invalid"


def test_cross_project_candidate_and_symlink_project_are_rejected(
    tmp_path,
):
    project_id = uuid4()
    catalog = FakeCatalog()
    service = WeakCaseVisualIndexService(
        tmp_path.resolve(),
        catalog=catalog,
        embedder=FakeEmbedder(),
    )
    service.build(project_id)
    with pytest.raises(WeakCaseVisualIndexError) as cross_project:
        service.query(
            project_id,
            _candidate(uuid4()),
            issue_type=None,
            query_text=None,
            limit=3,
        )
    assert cross_project.value.code == "cross_project_candidate_rejected"

    linked_project = uuid4()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / str(linked_project)
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")
    with pytest.raises(WeakCaseVisualIndexError) as linked:
        service.build(linked_project)
    assert linked.value.code == "visual_index_path_rejected"

    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")
    with pytest.raises(WeakCaseVisualIndexError) as parent_link:
        WeakCaseVisualIndexService(
            linked_parent / "visual-index",
            catalog=catalog,
            embedder=FakeEmbedder(),
        )
    assert parent_link.value.code == "visual_index_path_rejected"


def test_verified_local_clip_adapter_requires_verified_clip_pack(
    monkeypatch,
    tmp_path,
):
    manifest = tmp_path / "trafficsupervision-model-pack.json"
    manifest.write_text("{}", encoding="utf-8")
    clip_pack = VisionModelPack(
        manifest_path=manifest,
        root=tmp_path,
        manifest_sha256="a" * 64,
        pack_id="local-clip",
        pack_version="1.0.0",
        engine="transformers_clip_zero_shot",
        capabilities=("candidate_classification",),
        runtime_spdx="MIT",
        weights_spdx="MIT",
        redistribution_allowed=False,
        manifest={
            "model": {
                "directory": "clip",
                "device": "cpu",
            }
        },
        artifacts={},
    )
    monkeypatch.setattr(
        "app.weak_case_visual_index.load_model_pack",
        lambda _path: clip_pack,
    )

    adapter = VerifiedLocalClipImageEmbedder(manifest)
    identity = adapter.model_identity()

    assert identity["schema_version"] == IMAGE_EMBEDDING_MODEL_SCHEMA
    assert identity["model_id"] == "local-clip"
    assert identity["engine"] == (
        "transformers_clip_image_features_fixed_batch8"
    )
    assert identity["local_only"] is True
    assert identity["network_download_allowed"] is False
    assert identity["manifest_sha256"] == "a" * 64

    non_clip = VisionModelPack(
        **{
            **clip_pack.__dict__,
            "engine": "onnx_yolo_v8",
            "capabilities": ("defect_detection",),
        }
    )
    monkeypatch.setattr(
        "app.weak_case_visual_index.load_model_pack",
        lambda _path: non_clip,
    )
    with pytest.raises(WeakCaseVisualIndexError) as unsupported:
        VerifiedLocalClipImageEmbedder(manifest)
    assert unsupported.value.code == "embedding_model_pack_unsupported"
