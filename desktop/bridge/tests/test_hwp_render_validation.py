from __future__ import annotations

from dataclasses import replace
import io
from pathlib import Path
import zipfile

import pytest
from PIL import Image
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

import app.hwp_render_validation as hwp_render_validation
from app.hwp_com import fingerprint_document
from app.hwp_render_validation import (
    ExpectedPagePairSlot,
    RenderValidationLimits,
    ReportBindingManifest,
    validate_saved_hwpx_pdf_render,
)
from app.hwpx_sample_layout import SAMPLE_LAYOUT_BASELINES


WORKSPACE = Path(__file__).resolve().parents[3]
SAMPLE_3Q_HWPX = WORKSPACE / "sample" / "3분기 점검 내용 정리.hwpx"
SAMPLE_3Q_PDF = WORKSPACE / "sample" / "3분기 점검 내용 정리.pdf"
SAMPLE_4Q_HWPX = WORKSPACE / "sample" / "4분기 점검 내용 정리.hwpx"
SAMPLE_4Q_PDF = WORKSPACE / "sample" / "4분기 점검 내용 정리.pdf"

SAMPLE_PHOTO_COUNTS = {
    "sample-3q-v1": (
        4, 4, 7, 6, 5, 4, 4, 4, 6, 6, 1, 3,
        4, 3, 4, 6, 6, 6, 6, 6, 8, 8, 8, 4,
    ),
    "sample-4q-v1": (
        7, 8, 10, 8, 6, 5, 6, 6, 4, 6, 3, 3,
        8, 8, 8, 6, 6, 3, 7, 8, 6, 6, 4, 7,
    ),
}


def _manifest(
    template_id: str = "sample-3q-v1",
    *,
    photo_counts: tuple[int, ...] | None = None,
    expected_text: str | None = None,
    neutral_picture_maximums: tuple[int, ...] | None = None,
    caption_counts: tuple[int, ...] | None = None,
) -> ReportBindingManifest:
    baseline = next(
        item
        for item in SAMPLE_LAYOUT_BASELINES
        if item.template_id == template_id
    )
    counts = photo_counts or ((0,) * 24)
    neutral_maximums = neutral_picture_maximums or ((0,) * 24)
    captions = caption_counts if caption_counts is not None else counts
    slots = tuple(
        ExpectedPagePairSlot(
            page_pair_index=index,
            inspection_page_number=index * 2 - 1,
            photo_page_number=index * 2,
            incident_ids=(),
            expected_photo_count=count,
            expected_caption_count=captions[index - 1],
            expected_text_evidence=(
                (expected_text,)
                if expected_text is not None and index == 1
                else ()
            ),
            maximum_neutral_picture_count=neutral_maximums[index - 1],
        )
        for index, count in enumerate(counts, start=1)
    )
    return ReportBindingManifest(
        template_id=template_id,
        template_sha256=baseline.document_sha256,
        expected_page_pair_slots=slots,
        current_incident_ids=(),
        expected_photo_count=sum(counts),
        expected_caption_count=sum(captions),
    )


def _make_pdf(
    path: Path,
    *,
    page_count: int = 48,
    page_size=A4,
    blank_page: int | None = None,
    overflow_page: int | None = None,
) -> None:
    document = canvas.Canvas(str(path), pagesize=page_size)
    width, height = page_size
    for page_number in range(1, page_count + 1):
        if page_number != blank_page:
            document.drawString(
                72,
                height - 72,
                f"TrafficVisson report page {page_number}",
            )
            document.drawString(72, height - 96, "검증된 점검 의견")
        if page_number == overflow_page:
            document.rect(-40, height / 2, 120, 80, stroke=1, fill=0)
        document.showPage()
    document.save()


def _image_reader(
    *,
    size: tuple[int, int],
    color: tuple[int, int, int],
) -> ImageReader:
    payload = io.BytesIO()
    Image.new("RGB", size, color).save(payload, format="PNG")
    payload.seek(0)
    return ImageReader(payload)


def _make_photo_evidence_pdf(
    path: Path,
    *,
    include_meaningful: bool,
    include_neutral: bool,
    neutral_color: tuple[int, int, int] = (255, 255, 255),
) -> None:
    document = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    meaningful = _image_reader(size=(16, 16), color=(40, 90, 180))
    neutral = _image_reader(size=(1, 1), color=neutral_color)
    for page_number in range(1, 49):
        document.drawString(
            72,
            height - 72,
            f"TrafficVisson report page {page_number}",
        )
        document.drawString(72, height - 96, "검증된 점검 의견")
        if page_number == 2:
            if include_meaningful:
                document.drawImage(
                    meaningful,
                    72,
                    height - 300,
                    width=160,
                    height=120,
                )
                document.drawString(
                    72,
                    height - 318,
                    "current incident caption",
                )
            if include_neutral:
                document.drawImage(
                    neutral,
                    330,
                    height - 300,
                    width=160,
                    height=120,
                )
        document.showPage()
    document.save()


def _make_minimal_hwpx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/hwp+zip")


def _fast_limits() -> RenderValidationLimits:
    return RenderValidationLimits(
        render_dpi=72,
        max_duration_seconds=180.0,
    )


@pytest.mark.parametrize(
    ("template_id", "hwpx_path", "pdf_path"),
    (
        ("sample-3q-v1", SAMPLE_3Q_HWPX, SAMPLE_3Q_PDF),
        ("sample-4q-v1", SAMPLE_4Q_HWPX, SAMPLE_4Q_PDF),
    ),
)
def test_real_sample_pdf_passes_exact_48_page_structure_and_raster_gates(
    template_id,
    hwpx_path,
    pdf_path,
    tmp_path,
):
    result = validate_saved_hwpx_pdf_render(
        output_hwpx_fingerprint=fingerprint_document(hwpx_path),
        exported_pdf_path=pdf_path,
        binding_manifest=_manifest(
            template_id,
            photo_counts=SAMPLE_PHOTO_COUNTS[template_id],
            expected_text="현황사진",
        ),
        owned_render_root=tmp_path.resolve(),
        limits=_fast_limits(),
    )

    assert result.status == "validated_48_page_render"
    assert result.failure_codes == ()
    assert result.passed is True
    assert result.rendered_page_count == 48
    assert len(result.pages) == 48
    assert all(page.png_path.is_file() for page in result.pages)
    assert all(len(page.png_sha256) == 64 for page in result.pages)
    assert result.output_reopen_structure_sha256 is not None
    assert len(result.output_reopen_structure_sha256) == 64
    assert result.raster_artifact_manifest_sha256 is not None
    assert len(result.raster_artifact_manifest_sha256) == 64
    assert result.output_reopen_structure_sha256 != (
        result.raster_artifact_manifest_sha256
    )
    assert result.limitations


@pytest.fixture()
def synthetic_output(tmp_path):
    path = (tmp_path / "owned-output.hwpx").resolve()
    _make_minimal_hwpx(path)
    return fingerprint_document(path)


@pytest.fixture()
def bypass_structure_reopen(monkeypatch):
    monkeypatch.setattr(
        "app.hwp_render_validation._reopen_and_verify_hwpx_structure",
        lambda output, manifest: "a" * 64,
    )


def test_47_pages_fails_page_count_and_never_claims_completed(
    tmp_path,
    synthetic_output,
    bypass_structure_reopen,
):
    pdf = (tmp_path / "47-pages.pdf").resolve()
    _make_pdf(pdf, page_count=47)

    result = validate_saved_hwpx_pdf_render(
        output_hwpx_fingerprint=synthetic_output,
        exported_pdf_path=pdf,
        binding_manifest=_manifest(),
        owned_render_root=(tmp_path / "renders").resolve(),
        limits=_fast_limits(),
    )

    assert result.status == "page_count_mismatch"
    assert result.failure_codes == ("page_count_mismatch",)
    assert result.passed is False
    assert result.pages == ()
    assert result.raster_artifact_manifest_sha256 is None


def test_one_blank_page_keeps_all_raster_hashes_but_fails_whole_result(
    tmp_path,
    synthetic_output,
    bypass_structure_reopen,
):
    pdf = (tmp_path / "one-blank.pdf").resolve()
    _make_pdf(pdf, blank_page=24)

    result = validate_saved_hwpx_pdf_render(
        output_hwpx_fingerprint=synthetic_output,
        exported_pdf_path=pdf,
        binding_manifest=_manifest(),
        owned_render_root=(tmp_path / "renders").resolve(),
        limits=_fast_limits(),
    )

    assert result.status == "blank_page"
    assert "blank_page" in result.failure_codes
    assert result.passed is False
    assert len(result.pages) == 48
    assert result.raster_artifact_manifest_sha256 is not None
    assert any("blank_page=24" == item for item in result.diagnostics)


def test_changed_early_raster_is_detected_after_all_48_pages_render(
    tmp_path,
    synthetic_output,
    bypass_structure_reopen,
    monkeypatch,
):
    pdf = (tmp_path / "tampered-render.pdf").resolve()
    _make_pdf(pdf)
    original_render_page = hwp_render_validation._render_page

    def render_and_tamper_first_page(**kwargs):
        artifact = original_render_page(**kwargs)
        if artifact.page_number == 48:
            first_page = (
                kwargs["render_directory"] / "page-01.png"
            )
            first_page.write_bytes(b"changed-after-first-hash")
        return artifact

    monkeypatch.setattr(
        hwp_render_validation,
        "_render_page",
        render_and_tamper_first_page,
    )

    result = validate_saved_hwpx_pdf_render(
        output_hwpx_fingerprint=synthetic_output,
        exported_pdf_path=pdf,
        binding_manifest=_manifest(),
        owned_render_root=(tmp_path / "renders").resolve(),
        limits=_fast_limits(),
    )

    assert result.status == "render_failed"
    assert result.passed is False
    assert len(result.pages) == 48
    assert "rendered_page_artifact_changed=1" in result.diagnostics


def test_out_of_page_object_is_overflow_not_partial_success(
    tmp_path,
    synthetic_output,
    bypass_structure_reopen,
):
    pdf = (tmp_path / "overflow.pdf").resolve()
    _make_pdf(pdf, overflow_page=17)

    result = validate_saved_hwpx_pdf_render(
        output_hwpx_fingerprint=synthetic_output,
        exported_pdf_path=pdf,
        binding_manifest=_manifest(),
        owned_render_root=(tmp_path / "renders").resolve(),
        limits=_fast_limits(),
    )

    assert result.status == "overflow_suspected"
    assert result.failure_codes == ("overflow_suspected",)
    assert result.passed is False
    assert len(result.pages) == 48


def test_wrong_page_size_and_missing_expected_evidence_are_explicit(
    tmp_path,
    synthetic_output,
    bypass_structure_reopen,
):
    pdf = (tmp_path / "letter.pdf").resolve()
    _make_pdf(pdf, page_size=LETTER)
    counts = (1,) + ((0,) * 23)

    result = validate_saved_hwpx_pdf_render(
        output_hwpx_fingerprint=synthetic_output,
        exported_pdf_path=pdf,
        binding_manifest=_manifest(
            photo_counts=counts,
            expected_text="존재하지 않는 점검 문구",
        ),
        owned_render_root=(tmp_path / "renders").resolve(),
        limits=_fast_limits(),
    )

    assert result.status == "page_size_mismatch"
    assert "page_size_mismatch" in result.failure_codes
    assert "evidence_count_mismatch" in result.failure_codes
    assert result.passed is False


def test_large_1x1_white_neutral_picture_is_excluded_with_bound_allowance(
    tmp_path,
    synthetic_output,
    bypass_structure_reopen,
):
    pdf = (tmp_path / "meaningful-plus-neutral.pdf").resolve()
    _make_photo_evidence_pdf(
        pdf,
        include_meaningful=True,
        include_neutral=True,
    )
    counts = (1,) + ((0,) * 23)
    neutral_maximums = (1,) + ((0,) * 23)

    result = validate_saved_hwpx_pdf_render(
        output_hwpx_fingerprint=synthetic_output,
        exported_pdf_path=pdf,
        binding_manifest=_manifest(
            photo_counts=counts,
            neutral_picture_maximums=neutral_maximums,
        ),
        owned_render_root=(tmp_path / "renders").resolve(),
        limits=_fast_limits(),
    )

    assert result.status == "validated_48_page_render"
    assert result.pages[1].significant_photo_count == 1
    assert result.pages[1].neutral_picture_count == 1
    assert result.pages[1].caption_proxy_count == 1


def test_unbound_neutral_picture_fails_closed(
    tmp_path,
    synthetic_output,
    bypass_structure_reopen,
):
    pdf = (tmp_path / "unbound-neutral.pdf").resolve()
    _make_photo_evidence_pdf(
        pdf,
        include_meaningful=True,
        include_neutral=True,
    )
    counts = (1,) + ((0,) * 23)

    result = validate_saved_hwpx_pdf_render(
        output_hwpx_fingerprint=synthetic_output,
        exported_pdf_path=pdf,
        binding_manifest=_manifest(photo_counts=counts),
        owned_render_root=(tmp_path / "renders").resolve(),
        limits=_fast_limits(),
    )

    assert result.status == "evidence_count_mismatch"
    assert any(
        diagnostic
        == "neutral_picture_count_pair=1;maximum=0;actual=1"
        for diagnostic in result.diagnostics
    )


def test_neutral_picture_cannot_satisfy_meaningful_photo_expectation(
    tmp_path,
    synthetic_output,
    bypass_structure_reopen,
):
    pdf = (tmp_path / "neutral-only.pdf").resolve()
    _make_photo_evidence_pdf(
        pdf,
        include_meaningful=False,
        include_neutral=True,
    )
    counts = (1,) + ((0,) * 23)
    neutral_maximums = (1,) + ((0,) * 23)
    no_captions = (0,) * 24

    result = validate_saved_hwpx_pdf_render(
        output_hwpx_fingerprint=synthetic_output,
        exported_pdf_path=pdf,
        binding_manifest=_manifest(
            photo_counts=counts,
            neutral_picture_maximums=neutral_maximums,
            caption_counts=no_captions,
        ),
        owned_render_root=(tmp_path / "renders").resolve(),
        limits=_fast_limits(),
    )

    assert result.status == "evidence_count_mismatch"
    assert any(
        diagnostic == "photo_count_pair=1;expected=1;actual=0"
        for diagnostic in result.diagnostics
    )


def test_large_1x1_nonwhite_picture_aborts_validation(
    tmp_path,
    synthetic_output,
    bypass_structure_reopen,
):
    pdf = (tmp_path / "nonwhite-1x1.pdf").resolve()
    _make_photo_evidence_pdf(
        pdf,
        include_meaningful=False,
        include_neutral=True,
        neutral_color=(255, 0, 0),
    )

    result = validate_saved_hwpx_pdf_render(
        output_hwpx_fingerprint=synthetic_output,
        exported_pdf_path=pdf,
        binding_manifest=_manifest(),
        owned_render_root=(tmp_path / "renders").resolve(),
        limits=_fast_limits(),
    )

    assert result.status == "render_failed"
    assert any(
        diagnostic == "page_render_failed:RuntimeError"
        for diagnostic in result.diagnostics
    )


def test_changed_output_fingerprint_fails_before_pdf_or_com_work(
    tmp_path,
    synthetic_output,
):
    wrong = replace(synthetic_output, sha256="0" * 64)
    missing_pdf = (tmp_path / "never-opened.pdf").resolve()

    result = validate_saved_hwpx_pdf_render(
        output_hwpx_fingerprint=wrong,
        exported_pdf_path=missing_pdf,
        binding_manifest=_manifest(),
        owned_render_root=(tmp_path / "renders").resolve(),
        limits=_fast_limits(),
    )

    assert result.status == "output_changed"
    assert result.failure_codes == ("output_changed",)
    assert result.exported_pdf_sha256 is None
    assert result.render_directory is None


def test_unreadable_reopened_hwpx_is_render_failed_and_never_validated(
    tmp_path,
    synthetic_output,
):
    pdf = (tmp_path / "valid-looking.pdf").resolve()
    _make_pdf(pdf)

    result = validate_saved_hwpx_pdf_render(
        output_hwpx_fingerprint=synthetic_output,
        exported_pdf_path=pdf,
        binding_manifest=_manifest(),
        owned_render_root=(tmp_path / "renders").resolve(),
        limits=_fast_limits(),
    )

    assert result.status == "render_failed"
    assert result.failure_codes == ("render_failed",)
    assert result.output_reopen_structure_sha256 is None
    assert result.exported_pdf_sha256 is None
    assert result.pages == ()
