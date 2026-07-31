from __future__ import annotations

import hashlib
from pathlib import Path

from pypdf import PdfWriter

from app.contracts import PdfCompanionAuditResponse
from app.pdf_companion_audit import (
    PDF_COMPANION_ROLE,
    PDF_COMPANION_SCHEMA,
    PdfCompanionCatalog,
    PdfCompanionDefinition,
)


def _write_pdf(path: Path, page_count: int) -> str:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=595, height=842)
    with path.open("wb") as stream:
        writer.write(stream)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _definition(
    *,
    digest: str,
    page_count: int = 3,
) -> PdfCompanionDefinition:
    return PdfCompanionDefinition(
        quarter=3,
        document_name="3분기 점검 내용 정리.pdf",
        expected_pdf_sha256=digest,
        expected_page_count=page_count,
    )


def test_audits_exact_pdf_text_hashes_without_creating_photo_cases(
    tmp_path,
):
    root = tmp_path.resolve()
    path = root / "3분기 점검 내용 정리.pdf"
    digest = _write_pdf(path, 3)
    catalog = PdfCompanionCatalog(
        root,
        definitions=(_definition(digest=digest),),
        pdftoppm_path=root / "not-installed-pdftoppm.exe",
    )

    result = catalog.snapshot(3)

    empty_hash = hashlib.sha256(b"").hexdigest()
    form_feed_hash = hashlib.sha256(b"\f\f").hexdigest()
    assert result["schema_version"] == PDF_COMPANION_SCHEMA
    assert result["role"] == PDF_COMPANION_ROLE
    assert result["status"] == "ready"
    assert result["pdf_sha256"] == digest
    assert result["expected_pdf_sha256"] == digest
    assert result["page_count"] == 3
    assert [page["page_number"] for page in result["pages"]] == [
        1,
        2,
        3,
    ]
    assert {
        page["text_sha256"] for page in result["pages"]
    } == {empty_hash}
    assert result["full_text_sha256"] == form_feed_hash
    assert result["render"]["status"] == "renderer_unavailable"
    assert result["pdf_images_added_as_cases"] is False
    assert result["case_source"] == "paired_hwpx_photo_slots_only"
    assert result["automatic_annotation_creation"] is False
    assert result["automatic_incident_creation"] is False
    assert result["automatic_training_ingest"] is False
    response = PdfCompanionAuditResponse.model_validate(result)
    assert response.status == "ready"
    assert response.page_count == 3


def test_catalog_distinguishes_missing_changed_and_malformed_pdf(
    tmp_path,
):
    root = tmp_path.resolve()
    path = root / "3분기 점검 내용 정리.pdf"
    missing = PdfCompanionCatalog(
        root,
        definitions=(_definition(digest="0" * 64),),
        pdftoppm_path=root / "not-installed-pdftoppm.exe",
    )
    assert missing.snapshot(3)["status"] == "pdf_missing"

    _write_pdf(path, 3)
    assert missing.snapshot(3)["status"] == "pdf_sha256_mismatch"

    path.write_bytes(b"%PDF-1.7\nnot-a-pdf\n%%EOF\n")
    malformed_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    malformed = PdfCompanionCatalog(
        root,
        definitions=(_definition(digest=malformed_digest),),
        pdftoppm_path=root / "not-installed-pdftoppm.exe",
    )
    assert malformed.snapshot(3)["status"] == "pdf_malformed"


def test_catalog_distinguishes_expected_page_count_mismatch(tmp_path):
    root = tmp_path.resolve()
    path = root / "3분기 점검 내용 정리.pdf"
    digest = _write_pdf(path, 3)
    catalog = PdfCompanionCatalog(
        root,
        definitions=(
            _definition(digest=digest, page_count=4),
        ),
        pdftoppm_path=root / "not-installed-pdftoppm.exe",
    )

    result = catalog.snapshot(3)

    assert result["status"] == "pdf_page_count_mismatch"
    assert result["expected_page_count"] == 4
    assert result["page_count"] is None
