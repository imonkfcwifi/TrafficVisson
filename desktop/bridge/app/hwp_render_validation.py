"""Bounded, fail-closed validation of a post-save 48-page HWPX PDF render.

This module deliberately does not automate Hangul or write an HWPX document.
It consumes:

* a trusted fingerprint of the already-applied, TrafficVisson-owned HWPX;
* a PDF that an approval-gated COM adapter has already exported; and
* the server-owned report binding manifest for the current PipelineJob.

The validator proves a narrow set of mechanical facts: the files did not
change during validation, the PDF is 48 portrait A4 pages, every page can be
rasterized and is nonblank, page-pair photo/caption counts match the manifest,
required extractable text is present, and extracted object boxes stay inside
the page.  It cannot prove that a photograph depicts the right incident or
that the report is semantically perfect; callers must not upgrade this result
into such a claim.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import stat as stat_module
import time
import unicodedata
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Optional, Protocol, Sequence, Tuple

from .hwp_com import (
    HwpAutomationError,
    HwpDocumentFingerprint,
    fingerprint_document,
)
from .hwpx_sample_layout import SAMPLE_LAYOUT_BASELINES
from .hwpx_structure import HwpxStructureError, inspect_hwpx_structure


EXPECTED_PAGE_COUNT = 48
EXPECTED_PAGE_PAIR_COUNT = 24
EXPECTED_A4_WIDTH_POINTS = 595.2756
EXPECTED_A4_HEIGHT_POINTS = 841.8898
HWPX_ZIP_MAGIC = b"PK\x03\x04"
PDF_MAGIC = b"%PDF-"

ValidationFailureCode = Literal[
    "page_count_mismatch",
    "page_size_mismatch",
    "blank_page",
    "render_failed",
    "evidence_count_mismatch",
    "overflow_suspected",
    "output_changed",
]
ValidationStatus = Literal[
    "validated_48_page_render",
    "page_count_mismatch",
    "page_size_mismatch",
    "blank_page",
    "render_failed",
    "evidence_count_mismatch",
    "overflow_suspected",
    "output_changed",
]

_FAILURE_PRIORITY: Tuple[ValidationFailureCode, ...] = (
    "output_changed",
    "render_failed",
    "page_count_mismatch",
    "page_size_mismatch",
    "blank_page",
    "evidence_count_mismatch",
    "overflow_suspected",
)


@dataclass(frozen=True)
class ExpectedPagePairSlot:
    """Expected current-report evidence for one audited odd/even page pair."""

    page_pair_index: int
    inspection_page_number: int
    photo_page_number: int
    incident_ids: Tuple[str, ...]
    expected_photo_count: int
    expected_caption_count: int
    expected_text_evidence: Tuple[str, ...] = ()
    maximum_neutral_picture_count: int = 0


@dataclass(frozen=True)
class ReportBindingManifest:
    """Server-owned expectations produced by the exact Incident binder."""

    template_id: str
    template_sha256: str
    expected_page_pair_slots: Tuple[ExpectedPagePairSlot, ...]
    current_incident_ids: Tuple[str, ...]
    expected_photo_count: int
    expected_caption_count: int
    schema_version: str = "trafficvisson.report-render-binding.v2"


@dataclass(frozen=True)
class RenderValidationLimits:
    """Resource and false-positive bounds for untrusted exported PDFs."""

    max_pdf_bytes: int = 512 * 1024 * 1024
    max_rendered_total_bytes: int = 512 * 1024 * 1024
    max_duration_seconds: float = 120.0
    render_dpi: int = 120
    max_page_pixels: int = 6_000_000
    max_objects_per_document: int = 500_000
    a4_tolerance_points: float = 3.0
    bounds_tolerance_points: float = 0.5
    min_photo_area_ratio: float = 0.02
    caption_max_gap_points: float = 30.0
    caption_min_horizontal_overlap: float = 0.15
    blank_nonwhite_ratio: float = 0.0008
    edge_ink_ratio_limit: float = 0.60


@dataclass(frozen=True)
class RenderedPageEvidence:
    page_number: int
    png_path: Path
    png_sha256: str
    width_pixels: int
    height_pixels: int
    size_bytes: int
    nonwhite_ratio: float
    edge_ink_ratio: float
    text_character_count: int
    significant_photo_count: int
    neutral_picture_count: int
    caption_proxy_count: int
    extracted_object_count: int


@dataclass(frozen=True)
class HwpRenderValidationResult:
    status: ValidationStatus
    failure_codes: Tuple[ValidationFailureCode, ...]
    output_hwpx_sha256: str
    output_reopen_structure_sha256: Optional[str]
    exported_pdf_sha256: Optional[str]
    binding_manifest_sha256: str
    raster_artifact_manifest_sha256: Optional[str]
    rendered_page_count: Optional[int]
    render_directory: Optional[Path]
    pages: Tuple[RenderedPageEvidence, ...]
    diagnostics: Tuple[str, ...]
    limitations: Tuple[str, ...]
    elapsed_ms: int

    @property
    def passed(self) -> bool:
        return self.status == "validated_48_page_render"


@dataclass(frozen=True)
class ExplicitApplyApproval:
    """Opaque approval proof passed by the trusted approval ledger."""

    approval_id: str
    output_hwpx_sha256: str
    granted: bool


class ApprovalGatedPdfExporter(Protocol):
    """COM adapter boundary; this module provides no COM implementation."""

    def export_applied_hwpx_to_pdf(
        self,
        *,
        approval_id: str,
        source_hwpx: Path,
        destination_pdf: Path,
    ) -> Path:
        """Export the already-applied owned document to a new PDF path."""


@dataclass(frozen=True)
class _FileFingerprint:
    path: Path
    device: int
    inode: int
    size: int
    modified_ns: int
    sha256: str


@dataclass(frozen=True)
class _PageScan:
    page_number: int
    text: str
    text_blocks: Tuple[Tuple[float, float, float, float, str], ...]
    photo_boxes: Tuple[Tuple[float, float, float, float], ...]
    neutral_picture_boxes: Tuple[Tuple[float, float, float, float], ...]
    object_count: int
    bounds_invalid: bool
    page_size_invalid: bool


_LIMITATIONS = (
    "검증은 48쪽 기계적 구조·렌더·증거 수량을 확인하며 사진 내용의 의미적 정답을 보증하지 않습니다.",
    "사진은 PDF의 큰 이미지 배치로 판정하되 바인딩된 상한 안의 내재 1×1 순백 이미지만 중립화 대상으로 제외합니다.",
    "캡션은 의미 사진 아래 추출 텍스트의 근접성으로 판정하는 보수적 프록시입니다.",
    "글꼴·표·문단의 미세한 시각 차이는 별도의 전 페이지 시각 QA와 사용자 미리보기 승인이 필요합니다.",
)


def export_pdf_after_explicit_apply_approval(
    *,
    exporter: ApprovalGatedPdfExporter,
    approval: ExplicitApplyApproval,
    output_hwpx_fingerprint: HwpDocumentFingerprint,
    destination_pdf: Path,
) -> Path:
    """Invoke an injected exporter only after an explicit apply approval.

    Tests for the render validator never call this helper.  The caller remains
    responsible for obtaining ``approval`` from the trusted approval ledger.
    """

    if (
        approval.granted is not True
        or not approval.approval_id.strip()
        or not hmac.compare_digest(
            approval.output_hwpx_sha256,
            output_hwpx_fingerprint.sha256,
        )
    ):
        raise PermissionError("명시적인 HWP 적용 승인 없이는 PDF를 내보낼 수 없습니다.")
    current = fingerprint_document(output_hwpx_fingerprint.path)
    if not _hwp_fingerprints_match(current, output_hwpx_fingerprint):
        raise HwpAutomationError("승인된 HWPX 출력이 변경되어 PDF 내보내기를 중단했습니다.")
    destination = Path(destination_pdf)
    if (
        not destination.is_absolute()
        or destination.suffix.casefold() != ".pdf"
        or destination.exists()
    ):
        raise ValueError("PDF 출력은 존재하지 않는 절대 .pdf 경로여야 합니다.")
    exported = Path(
        exporter.export_applied_hwpx_to_pdf(
            approval_id=approval.approval_id,
            source_hwpx=current.path,
            destination_pdf=destination,
        )
    )
    if exported != destination:
        raise RuntimeError("PDF exporter가 승인된 출력 경로를 반환하지 않았습니다.")
    _fingerprint_pdf(exported, 512 * 1024 * 1024)
    return exported


def validate_saved_hwpx_pdf_render(
    *,
    output_hwpx_fingerprint: HwpDocumentFingerprint,
    exported_pdf_path: Path,
    binding_manifest: ReportBindingManifest,
    owned_render_root: Path,
    limits: RenderValidationLimits = RenderValidationLimits(),
) -> HwpRenderValidationResult:
    """Validate and rasterize all 48 pages without invoking COM."""

    started = time.monotonic()
    manifest_sha = _manifest_sha256(binding_manifest)
    artifacts: list[RenderedPageEvidence] = []
    render_directory: Optional[Path] = None
    pdf_fingerprint: Optional[_FileFingerprint] = None
    page_count: Optional[int] = None
    diagnostics: list[str] = []
    structure_sha: Optional[str] = None

    manifest_errors = _validate_manifest(binding_manifest)
    if manifest_errors:
        return _result(
            started,
            output_hwpx_fingerprint,
            None,
            manifest_sha,
            None,
            None,
            artifacts,
            ("evidence_count_mismatch",),
            manifest_errors,
            structure_sha=None,
        )

    if not _limits_are_valid(limits):
        return _result(
            started,
            output_hwpx_fingerprint,
            None,
            manifest_sha,
            None,
            None,
            artifacts,
            ("render_failed",),
            ("render_limits_invalid",),
            structure_sha=None,
        )

    if not _owned_hwpx_matches(output_hwpx_fingerprint):
        return _result(
            started,
            output_hwpx_fingerprint,
            None,
            manifest_sha,
            None,
            None,
            artifacts,
            ("output_changed",),
            ("owned_hwpx_fingerprint_or_magic_mismatch",),
            structure_sha=None,
        )

    try:
        structure_sha = _reopen_and_verify_hwpx_structure(
            output_hwpx_fingerprint,
            binding_manifest,
        )
    except HwpxStructureError as error:
        return _result(
            started,
            output_hwpx_fingerprint,
            None,
            manifest_sha,
            None,
            None,
            artifacts,
            ("render_failed",),
            (_safe_error("output_hwpx_reopen_failed", error),),
            structure_sha=None,
        )
    except ValueError as error:
        return _result(
            started,
            output_hwpx_fingerprint,
            None,
            manifest_sha,
            None,
            None,
            artifacts,
            ("render_failed",),
            (_safe_error("output_hwpx_structure_mismatch", error),),
            structure_sha=None,
        )

    try:
        pdf_fingerprint = _fingerprint_pdf(
            Path(exported_pdf_path),
            limits.max_pdf_bytes,
        )
        fitz, Image = _load_pdf_dependencies()
        document = fitz.open(pdf_fingerprint.path)
    except Exception as error:  # dependency/parser failures are caller-safe
        return _result(
            started,
            output_hwpx_fingerprint,
            pdf_fingerprint,
            manifest_sha,
            None,
            None,
            artifacts,
            ("render_failed",),
            (_safe_error("pdf_open_failed", error),),
            structure_sha=structure_sha,
        )

    failure_codes: set[ValidationFailureCode] = set()
    try:
        page_count = len(document)
        if page_count != EXPECTED_PAGE_COUNT:
            failure_codes.add("page_count_mismatch")
            diagnostics.append(
                f"expected_pages={EXPECTED_PAGE_COUNT};actual_pages={page_count}"
            )
        else:
            render_directory = _create_owned_render_directory(
                Path(owned_render_root)
            )
            scans, scan_failures, scan_diagnostics = _scan_pages(
                document=document,
                fitz=fitz,
                limits=limits,
                started=started,
            )
            failure_codes.update(scan_failures)
            diagnostics.extend(scan_diagnostics)

            if not scan_failures.intersection(
                {"render_failed", "page_count_mismatch"}
            ):
                evidence_failures, evidence_diagnostics = _verify_evidence(
                    scans,
                    binding_manifest,
                    limits,
                )
                failure_codes.update(evidence_failures)
                diagnostics.extend(evidence_diagnostics)

            rendered_bytes = 0
            if "render_failed" not in failure_codes:
                for scan in scans:
                    _assert_deadline(started, limits)
                    page = document[scan.page_number - 1]
                    artifact = _render_page(
                        page=page,
                        scan=scan,
                        fitz=fitz,
                        Image=Image,
                        render_directory=render_directory,
                        limits=limits,
                    )
                    artifacts.append(artifact)
                    rendered_bytes += artifact.size_bytes
                    if rendered_bytes > limits.max_rendered_total_bytes:
                        raise RuntimeError("rendered_png_size_limit_exceeded")
                    if artifact.nonwhite_ratio < limits.blank_nonwhite_ratio:
                        failure_codes.add("blank_page")
                        diagnostics.append(
                            f"blank_page={artifact.page_number}"
                        )
                    if artifact.edge_ink_ratio > limits.edge_ink_ratio_limit:
                        failure_codes.add("overflow_suspected")
                        diagnostics.append(
                            "edge_ink_overflow_proxy_page="
                            f"{artifact.page_number}"
                        )
    except Exception as error:
        failure_codes.add("render_failed")
        diagnostics.append(_safe_error("page_render_failed", error))
    finally:
        document.close()

    if not _owned_hwpx_matches(output_hwpx_fingerprint):
        failure_codes.add("output_changed")
        diagnostics.append("owned_hwpx_changed_during_validation")
    try:
        final_pdf = _fingerprint_pdf(
            Path(exported_pdf_path),
            limits.max_pdf_bytes,
        )
        if (
            pdf_fingerprint is None
            or not _file_fingerprints_match(final_pdf, pdf_fingerprint)
        ):
            failure_codes.add("output_changed")
            diagnostics.append("exported_pdf_changed_during_validation")
    except Exception as error:
        failure_codes.add("output_changed")
        diagnostics.append(_safe_error("exported_pdf_recheck_failed", error))

    raster_diagnostics = _verify_rendered_page_artifacts(
        render_directory=render_directory,
        artifacts=artifacts,
    )
    if raster_diagnostics:
        failure_codes.add("render_failed")
        diagnostics.extend(raster_diagnostics)

    return _result(
        started,
        output_hwpx_fingerprint,
        pdf_fingerprint,
        manifest_sha,
        page_count,
        render_directory,
        artifacts,
        tuple(failure_codes),
        tuple(diagnostics),
        structure_sha=structure_sha,
    )


def _reopen_and_verify_hwpx_structure(
    output: HwpDocumentFingerprint,
    manifest: ReportBindingManifest,
) -> str:
    """Reopen the owned HWPX read-only and attest its preserved structure."""

    baseline = next(
        candidate
        for candidate in SAMPLE_LAYOUT_BASELINES
        if candidate.template_id == manifest.template_id
    )
    snapshot = inspect_hwpx_structure(output.path)
    cell_count = sum(len(table.cells) for table in snapshot.tables)
    observed = {
        "schema_version": snapshot.schema_version,
        "document_sha256": snapshot.document_sha256,
        "target_summary_sha256": snapshot.target_summary_sha256,
        "archive_entry_count": snapshot.archive_entry_count,
        "xml_entry_count": snapshot.xml_entry_count,
        "section_count": snapshot.section_count,
        "table_count": len(snapshot.tables),
        "cell_count": cell_count,
        "picture_count": len(snapshot.pictures),
        "explicit_page_break_count": len(snapshot.page_breaks),
        "field_count": snapshot.field_count,
        "bookmark_count": snapshot.bookmark_count,
        "template_id": manifest.template_id,
        "template_sha256": manifest.template_sha256,
    }
    expected_counts = (
        baseline.section_count,
        baseline.table_count,
        baseline.cell_count,
        baseline.picture_count,
        baseline.explicit_page_break_count,
    )
    actual_counts = (
        snapshot.section_count,
        len(snapshot.tables),
        cell_count,
        len(snapshot.pictures),
        len(snapshot.page_breaks),
    )
    if (
        actual_counts != expected_counts
        or snapshot.field_count != 0
        or snapshot.bookmark_count != 0
        or not hmac.compare_digest(
            snapshot.document_sha256,
            output.sha256,
        )
    ):
        raise ValueError("preserved_48_page_hwpx_structure_mismatch")
    return hashlib.sha256(
        json.dumps(
            observed,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _scan_pages(
    *,
    document: Any,
    fitz: Any,
    limits: RenderValidationLimits,
    started: float,
) -> Tuple[
    Tuple[_PageScan, ...],
    set[ValidationFailureCode],
    Tuple[str, ...],
]:
    scans: list[_PageScan] = []
    failures: set[ValidationFailureCode] = set()
    diagnostics: list[str] = []
    total_objects = 0
    for page_index in range(EXPECTED_PAGE_COUNT):
        _assert_deadline(started, limits)
        page = document[page_index]
        media = page.mediabox
        crop = page.cropbox
        rect = page.rect
        size_invalid = (
            page.rotation != 0
            or not _is_a4_portrait(media, limits)
            or not _is_a4_portrait(crop, limits)
            or not _is_a4_portrait(rect, limits)
        )
        if size_invalid:
            failures.add("page_size_mismatch")
            diagnostics.append(f"page_size_mismatch={page_index + 1}")

        text_blocks: list[Tuple[float, float, float, float, str]] = []
        object_boxes: list[Tuple[float, float, float, float]] = []
        for block in page.get_text(
            "blocks",
            flags=getattr(fitz, "TEXTFLAGS_TEXT", 3),
        ):
            if len(block) < 7 or int(block[6]) != 0:
                continue
            box = tuple(float(value) for value in block[:4])
            text = str(block[4]).strip()
            if text:
                text_blocks.append((*box, text))
            object_boxes.append(box)

        page_area = max(1.0, float(rect.width * rect.height))
        photo_boxes: list[Tuple[float, float, float, float]] = []
        neutral_picture_boxes: list[
            Tuple[float, float, float, float]
        ] = []
        for image in page.get_image_info(xrefs=True):
            box = tuple(float(value) for value in image["bbox"])
            object_boxes.append(box)
            if _box_area(box) >= page_area * limits.min_photo_area_ratio:
                if _is_exact_writer_neutral_picture(
                    document=document,
                    fitz=fitz,
                    image=image,
                ):
                    neutral_picture_boxes.append(box)
                else:
                    photo_boxes.append(box)

        for drawing in page.get_drawings():
            drawing_rect = drawing.get("rect")
            if drawing_rect is not None:
                object_boxes.append(
                    tuple(float(value) for value in drawing_rect)
                )
        for link in page.get_links():
            link_rect = link.get("from")
            if link_rect is not None:
                object_boxes.append(tuple(float(value) for value in link_rect))

        total_objects += len(object_boxes)
        if total_objects > limits.max_objects_per_document:
            failures.add("overflow_suspected")
            diagnostics.append("extracted_object_budget_exceeded")
            break

        bounds_invalid = any(
            not _box_inside_page(box, rect, limits.bounds_tolerance_points)
            for box in object_boxes
        )
        if bounds_invalid:
            failures.add("overflow_suspected")
            diagnostics.append(f"object_bounds_invalid_page={page_index + 1}")
        scans.append(
            _PageScan(
                page_number=page_index + 1,
                text="\n".join(block[4] for block in text_blocks),
                text_blocks=tuple(text_blocks),
                photo_boxes=tuple(photo_boxes),
                neutral_picture_boxes=tuple(neutral_picture_boxes),
                object_count=len(object_boxes),
                bounds_invalid=bounds_invalid,
                page_size_invalid=size_invalid,
            )
        )
    if len(scans) != EXPECTED_PAGE_COUNT:
        failures.add("render_failed")
        diagnostics.append(
            f"page_scan_incomplete={len(scans)}/{EXPECTED_PAGE_COUNT}"
        )
    return tuple(scans), failures, tuple(diagnostics)


def _verify_evidence(
    scans: Sequence[_PageScan],
    manifest: ReportBindingManifest,
    limits: RenderValidationLimits,
) -> Tuple[set[ValidationFailureCode], Tuple[str, ...]]:
    failures: set[ValidationFailureCode] = set()
    diagnostics: list[str] = []
    photos_total = 0
    captions_total = 0
    for slot in manifest.expected_page_pair_slots:
        inspection = scans[slot.inspection_page_number - 1]
        photo = scans[slot.photo_page_number - 1]
        actual_photos = len(photo.photo_boxes)
        actual_neutral_pictures = len(photo.neutral_picture_boxes)
        actual_captions = _caption_proxy_count(
            photo.photo_boxes,
            photo.text_blocks,
            limits,
        )
        photos_total += actual_photos
        captions_total += actual_captions
        if actual_photos != slot.expected_photo_count:
            failures.add("evidence_count_mismatch")
            diagnostics.append(
                "photo_count_pair="
                f"{slot.page_pair_index};expected={slot.expected_photo_count};"
                f"actual={actual_photos}"
            )
        if (
            actual_neutral_pictures
            > slot.maximum_neutral_picture_count
        ):
            failures.add("evidence_count_mismatch")
            diagnostics.append(
                "neutral_picture_count_pair="
                f"{slot.page_pair_index};"
                f"maximum={slot.maximum_neutral_picture_count};"
                f"actual={actual_neutral_pictures}"
            )
        if actual_captions != slot.expected_caption_count:
            failures.add("evidence_count_mismatch")
            diagnostics.append(
                "caption_count_pair="
                f"{slot.page_pair_index};expected={slot.expected_caption_count};"
                f"actual={actual_captions}"
            )
        pair_text = _normalize_text(inspection.text + "\n" + photo.text)
        expected_counts = Counter(
            _normalize_text(value) for value in slot.expected_text_evidence
        )
        for evidence, required_count in expected_counts.items():
            if not evidence or pair_text.count(evidence) < required_count:
                failures.add("evidence_count_mismatch")
                diagnostics.append(
                    f"text_evidence_missing_pair={slot.page_pair_index}"
                )
                break
    if photos_total != manifest.expected_photo_count:
        failures.add("evidence_count_mismatch")
        diagnostics.append(
            "photo_count_total="
            f"expected={manifest.expected_photo_count};actual={photos_total}"
        )
    if captions_total != manifest.expected_caption_count:
        failures.add("evidence_count_mismatch")
        diagnostics.append(
            "caption_count_total="
            f"expected={manifest.expected_caption_count};actual={captions_total}"
        )
    return failures, tuple(diagnostics)


def _render_page(
    *,
    page: Any,
    scan: _PageScan,
    fitz: Any,
    Image: Any,
    render_directory: Path,
    limits: RenderValidationLimits,
) -> RenderedPageEvidence:
    scale = limits.render_dpi / 72.0
    width = int(math.ceil(float(page.rect.width) * scale))
    height = int(math.ceil(float(page.rect.height) * scale))
    if width <= 0 or height <= 0 or width * height > limits.max_page_pixels:
        raise RuntimeError("rendered_page_pixel_limit_exceeded")
    png_path = render_directory / f"page-{scan.page_number:02d}.png"
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(scale, scale),
        colorspace=fitz.csRGB,
        alpha=False,
    )
    if pixmap.width * pixmap.height > limits.max_page_pixels:
        raise RuntimeError("rendered_page_pixel_limit_exceeded")
    pixmap.save(png_path)
    size = png_path.stat().st_size
    with Image.open(png_path) as image:
        image.load()
        actual_dimensions = image.size
        gray = image.convert("L")
        gray.thumbnail((192, 192))
        histogram = gray.histogram()
        total = max(1, sum(histogram))
        nonwhite = sum(histogram[:250]) / total
        edge = _edge_ink_ratio(gray)
    if actual_dimensions != (pixmap.width, pixmap.height):
        raise RuntimeError("rendered_png_dimensions_mismatch")
    return RenderedPageEvidence(
        page_number=scan.page_number,
        png_path=png_path,
        png_sha256=_sha256_path(png_path),
        width_pixels=pixmap.width,
        height_pixels=pixmap.height,
        size_bytes=size,
        nonwhite_ratio=nonwhite,
        edge_ink_ratio=edge,
        text_character_count=len(_normalize_text(scan.text)),
        significant_photo_count=len(scan.photo_boxes),
        neutral_picture_count=len(scan.neutral_picture_boxes),
        caption_proxy_count=_caption_proxy_count(
            scan.photo_boxes,
            scan.text_blocks,
            limits,
        ),
        extracted_object_count=scan.object_count,
    )


def _verify_rendered_page_artifacts(
    *,
    render_directory: Optional[Path],
    artifacts: Sequence[RenderedPageEvidence],
) -> Tuple[str, ...]:
    """Re-fingerprint the complete 48-page raster set before success.

    Each page is hashed when it is first written.  This second pass closes the
    gap where an earlier PNG could be replaced while later pages are still
    rendering.  No filesystem path is included in diagnostics.
    """

    if render_directory is None:
        return (
            ()
            if not artifacts
            else ("rendered_page_directory_missing",)
        )
    if len(artifacts) != EXPECTED_PAGE_COUNT:
        return (
            "rendered_page_artifact_count_mismatch="
            f"{len(artifacts)}/{EXPECTED_PAGE_COUNT}",
        )
    directory = Path(
        os.path.abspath(os.fspath(render_directory))
    )
    try:
        directory_value = os.lstat(directory)
    except OSError:
        return ("rendered_page_directory_unavailable",)
    if (
        not stat_module.S_ISDIR(directory_value.st_mode)
        or stat_module.S_ISLNK(directory_value.st_mode)
        or _is_reparse_point(directory_value)
    ):
        return ("rendered_page_directory_invalid",)

    diagnostics: list[str] = []
    for expected_page_number, artifact in enumerate(
        artifacts,
        start=1,
    ):
        expected_path = directory / (
            f"page-{expected_page_number:02d}.png"
        )
        actual_path = Path(
            os.path.abspath(os.fspath(artifact.png_path))
        )
        if (
            artifact.page_number != expected_page_number
            or actual_path != expected_path
        ):
            diagnostics.append(
                "rendered_page_address_mismatch="
                f"{expected_page_number}"
            )
            continue
        try:
            before_value = os.lstat(actual_path)
            before = (
                int(before_value.st_dev),
                int(before_value.st_ino),
                int(before_value.st_size),
                int(before_value.st_mtime_ns),
            )
            if (
                not stat_module.S_ISREG(before_value.st_mode)
                or stat_module.S_ISLNK(before_value.st_mode)
                or _is_reparse_point(before_value)
                or before[2] != artifact.size_bytes
            ):
                raise ValueError(
                    "rendered page is not the expected regular file"
                )
            digest = _sha256_path(actual_path)
            after_value = os.lstat(actual_path)
            after = (
                int(after_value.st_dev),
                int(after_value.st_ino),
                int(after_value.st_size),
                int(after_value.st_mtime_ns),
            )
            if (
                before != after
                or not hmac.compare_digest(
                    digest,
                    artifact.png_sha256,
                )
            ):
                raise ValueError(
                    "rendered page changed while being verified"
                )
        except (OSError, ValueError):
            diagnostics.append(
                "rendered_page_artifact_changed="
                f"{expected_page_number}"
            )
    return tuple(diagnostics)


def _is_reparse_point(value: os.stat_result) -> bool:
    return bool(
        getattr(value, "st_file_attributes", 0)
        & getattr(
            stat_module,
            "FILE_ATTRIBUTE_REPARSE_POINT",
            0x400,
        )
    )


def _validate_manifest(manifest: ReportBindingManifest) -> Tuple[str, ...]:
    errors: list[str] = []
    if manifest.schema_version != "trafficvisson.report-render-binding.v2":
        errors.append("binding_manifest_schema_invalid")
    baseline = next(
        (
            candidate
            for candidate in SAMPLE_LAYOUT_BASELINES
            if candidate.template_id == manifest.template_id
        ),
        None,
    )
    if (
        baseline is None
        or not hmac.compare_digest(
            baseline.document_sha256,
            manifest.template_sha256,
        )
    ):
        errors.append("template_id_or_hash_mismatch")
    slots = manifest.expected_page_pair_slots
    if len(slots) != EXPECTED_PAGE_PAIR_COUNT:
        errors.append("expected_page_pair_slot_count_mismatch")
    expected_incidents: list[str] = []
    expected_photos = 0
    expected_captions = 0
    seen_slots: set[int] = set()
    for offset, slot in enumerate(slots, start=1):
        if (
            slot.page_pair_index != offset
            or slot.page_pair_index in seen_slots
            or slot.inspection_page_number != (offset * 2 - 1)
            or slot.photo_page_number != offset * 2
        ):
            errors.append("odd_even_page_pair_pattern_mismatch")
            break
        seen_slots.add(slot.page_pair_index)
        if (
            not _bounded_count(slot.expected_photo_count)
            or not _bounded_count(slot.expected_caption_count)
            or not _bounded_count(slot.maximum_neutral_picture_count)
            or slot.expected_caption_count > slot.expected_photo_count
        ):
            errors.append("page_pair_evidence_count_invalid")
            break
        if len(slot.incident_ids) != len(set(slot.incident_ids)):
            errors.append("duplicate_incident_in_page_pair")
            break
        if any(not _bounded_identifier(value) for value in slot.incident_ids):
            errors.append("incident_id_invalid")
            break
        if (
            len(slot.expected_text_evidence) > 512
            or any(
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 500
                for value in slot.expected_text_evidence
            )
        ):
            errors.append("text_evidence_invalid")
            break
        expected_incidents.extend(slot.incident_ids)
        expected_photos += slot.expected_photo_count
        expected_captions += slot.expected_caption_count
    if len(expected_incidents) != len(set(expected_incidents)):
        errors.append("incident_bound_to_multiple_page_pairs")
    if (
        tuple(sorted(expected_incidents))
        != tuple(sorted(manifest.current_incident_ids))
        or len(manifest.current_incident_ids)
        != len(set(manifest.current_incident_ids))
    ):
        errors.append("current_incident_set_mismatch")
    if any(
        not _bounded_identifier(value)
        for value in manifest.current_incident_ids
    ):
        errors.append("current_incident_id_invalid")
    if (
        expected_photos != manifest.expected_photo_count
        or expected_captions != manifest.expected_caption_count
    ):
        errors.append("aggregate_evidence_count_mismatch")
    return tuple(dict.fromkeys(errors))


def _manifest_sha256(manifest: ReportBindingManifest) -> str:
    return hashlib.sha256(
        json.dumps(
            asdict(manifest),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _owned_hwpx_matches(
    expected: HwpDocumentFingerprint,
) -> bool:
    try:
        if (
            expected.path.suffix.casefold() != ".hwpx"
            or not expected.path.is_absolute()
            or not _read_prefix(expected.path, 4).startswith(HWPX_ZIP_MAGIC)
        ):
            return False
        actual = fingerprint_document(expected.path)
        return _hwp_fingerprints_match(actual, expected)
    except (OSError, HwpAutomationError, ValueError):
        return False


def _hwp_fingerprints_match(
    actual: HwpDocumentFingerprint,
    expected: HwpDocumentFingerprint,
) -> bool:
    return (
        actual.path == expected.path
        and actual.device == expected.device
        and actual.inode == expected.inode
        and actual.size == expected.size
        and actual.modified_ns == expected.modified_ns
        and hmac.compare_digest(actual.sha256, expected.sha256)
    )


def _fingerprint_pdf(path: Path, maximum_bytes: int) -> _FileFingerprint:
    if (
        not path.is_absolute()
        or path.suffix.casefold() != ".pdf"
        or "\x00" in os.fspath(path)
    ):
        raise ValueError("exported_pdf_path_invalid")
    absolute = Path(os.path.abspath(os.fspath(path)))
    value = os.lstat(absolute)
    if (
        not stat_module.S_ISREG(value.st_mode)
        or stat_module.S_ISLNK(value.st_mode)
        or value.st_size <= 0
        or value.st_size > maximum_bytes
    ):
        raise ValueError("exported_pdf_file_invalid")
    if not _read_prefix(absolute, 5).startswith(PDF_MAGIC):
        raise ValueError("exported_pdf_magic_invalid")
    before = (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
    )
    digest = _sha256_path(absolute)
    after_value = os.lstat(absolute)
    after = (
        int(after_value.st_dev),
        int(after_value.st_ino),
        int(after_value.st_size),
        int(after_value.st_mtime_ns),
    )
    if before != after:
        raise RuntimeError("exported_pdf_changed_while_hashing")
    return _FileFingerprint(
        path=absolute,
        device=before[0],
        inode=before[1],
        size=before[2],
        modified_ns=before[3],
        sha256=digest,
    )


def _file_fingerprints_match(
    left: _FileFingerprint,
    right: _FileFingerprint,
) -> bool:
    return (
        left.path == right.path
        and left.device == right.device
        and left.inode == right.inode
        and left.size == right.size
        and left.modified_ns == right.modified_ns
        and hmac.compare_digest(left.sha256, right.sha256)
    )


def _create_owned_render_directory(root: Path) -> Path:
    if not root.is_absolute():
        raise ValueError("owned_render_root_must_be_absolute")
    root = Path(os.path.abspath(os.fspath(root)))
    root.mkdir(parents=True, exist_ok=True)
    value = os.lstat(root)
    if not stat_module.S_ISDIR(value.st_mode) or stat_module.S_ISLNK(
        value.st_mode
    ):
        raise ValueError("owned_render_root_invalid")
    output = root / f"trafficvisson-render-{uuid.uuid4().hex}"
    output.mkdir(mode=0o700)
    return output


def _caption_proxy_count(
    photo_boxes: Sequence[Tuple[float, float, float, float]],
    text_blocks: Sequence[Tuple[float, float, float, float, str]],
    limits: RenderValidationLimits,
) -> int:
    count = 0
    for photo in photo_boxes:
        width = max(1.0, photo[2] - photo[0])
        for text in text_blocks:
            horizontal_overlap = max(
                0.0,
                min(photo[2], text[2]) - max(photo[0], text[0]),
            )
            vertical_gap = text[1] - photo[3]
            if (
                -1.0 <= vertical_gap <= limits.caption_max_gap_points
                and horizontal_overlap / width
                >= limits.caption_min_horizontal_overlap
                and _normalize_text(text[4])
            ):
                count += 1
                break
    return count


def _box_inside_page(
    box: Sequence[float],
    page_rect: Any,
    tolerance: float,
) -> bool:
    if len(box) != 4 or not all(math.isfinite(value) for value in box):
        return False
    x0, y0, x1, y1 = box
    if x1 < x0 or y1 < y0:
        return False
    return (
        x0 >= float(page_rect.x0) - tolerance
        and y0 >= float(page_rect.y0) - tolerance
        and x1 <= float(page_rect.x1) + tolerance
        and y1 <= float(page_rect.y1) + tolerance
    )


def _box_area(box: Sequence[float]) -> float:
    if len(box) != 4 or not all(math.isfinite(value) for value in box):
        return -1.0
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _is_exact_writer_neutral_picture(
    *,
    document: Any,
    fitz: Any,
    image: Mapping[str, Any],
) -> bool:
    """Recognize only the exact writer's deterministic 1x1 white payload.

    A large placement box is not evidence that the underlying image carries
    useful pixels.  Conversely, intrinsic 1x1 images must never be accepted as
    current Incident evidence.  If such an image cannot be proven to be the
    exact white neutral representation, abort the whole render validation.
    """

    width = image.get("width")
    height = image.get("height")
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or not isinstance(height, int)
        or isinstance(height, bool)
        or width <= 0
        or height <= 0
    ):
        raise RuntimeError("image_intrinsic_dimensions_invalid")
    if (width, height) != (1, 1):
        return False

    xref = image.get("xref")
    if (
        not isinstance(xref, int)
        or isinstance(xref, bool)
        or xref <= 0
    ):
        raise RuntimeError("neutral_picture_xref_invalid")
    try:
        source = fitz.Pixmap(document, xref)
        if (
            source.width != 1
            or source.height != 1
            or source.colorspace is None
        ):
            raise RuntimeError("neutral_picture_pixmap_invalid")
        rgb = fitz.Pixmap(fitz.csRGB, source)
        samples = bytes(rgb.samples)
        if (
            rgb.width != 1
            or rgb.height != 1
            or rgb.n != 3
            or samples != b"\xff\xff\xff"
        ):
            raise RuntimeError("intrinsic_1x1_nonwhite_picture")
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError("neutral_picture_decode_failed") from error
    return True


def _is_a4_portrait(rect: Any, limits: RenderValidationLimits) -> bool:
    width = float(rect.width)
    height = float(rect.height)
    return (
        math.isfinite(width)
        and math.isfinite(height)
        and abs(width - EXPECTED_A4_WIDTH_POINTS)
        <= limits.a4_tolerance_points
        and abs(height - EXPECTED_A4_HEIGHT_POINTS)
        <= limits.a4_tolerance_points
    )


def _edge_ink_ratio(gray: Any) -> float:
    width, height = gray.size
    if width < 2 or height < 2:
        return 1.0
    pixels = gray.load()
    samples = []
    for x in range(width):
        samples.append(pixels[x, 0])
        samples.append(pixels[x, height - 1])
    for y in range(1, height - 1):
        samples.append(pixels[0, y])
        samples.append(pixels[width - 1, y])
    return sum(value < 245 for value in samples) / max(1, len(samples))


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return "".join(
        character
        for character in normalized
        if not character.isspace() and not unicodedata.category(character).startswith("C")
    )


def _load_pdf_dependencies() -> Tuple[Any, Any]:
    import fitz
    from PIL import Image

    return fitz, Image


def _limits_are_valid(limits: RenderValidationLimits) -> bool:
    return (
        1 <= limits.max_pdf_bytes <= 2 * 1024**3
        and 1 <= limits.max_rendered_total_bytes <= 2 * 1024**3
        and 1.0 <= limits.max_duration_seconds <= 600.0
        and 72 <= limits.render_dpi <= 300
        and 100_000 <= limits.max_page_pixels <= 30_000_000
        and 48 <= limits.max_objects_per_document <= 2_000_000
        and 0.0 <= limits.a4_tolerance_points <= 10.0
        and 0.0 <= limits.bounds_tolerance_points <= 2.0
        and 0.001 <= limits.min_photo_area_ratio <= 0.25
        and 1.0 <= limits.caption_max_gap_points <= 100.0
        and 0.01 <= limits.caption_min_horizontal_overlap <= 1.0
        and 0.0 <= limits.blank_nonwhite_ratio <= 0.05
        and 0.0 <= limits.edge_ink_ratio_limit <= 1.0
    )


def _bounded_count(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= 2_048
    )


def _bounded_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= 128
        and not any(ord(character) < 32 for character in value)
    )


def _assert_deadline(started: float, limits: RenderValidationLimits) -> None:
    if time.monotonic() - started > limits.max_duration_seconds:
        raise TimeoutError("render_validation_time_limit_exceeded")


def _read_prefix(path: Path, count: int) -> bytes:
    with path.open("rb") as stream:
        return stream.read(count)


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _safe_error(prefix: str, error: Exception) -> str:
    return f"{prefix}:{type(error).__name__}"


def _result(
    started: float,
    output: HwpDocumentFingerprint,
    pdf: Optional[_FileFingerprint],
    manifest_sha: str,
    page_count: Optional[int],
    render_directory: Optional[Path],
    artifacts: Sequence[RenderedPageEvidence],
    failures: Sequence[ValidationFailureCode],
    diagnostics: Sequence[str],
    *,
    structure_sha: Optional[str],
) -> HwpRenderValidationResult:
    ordered = tuple(
        code for code in _FAILURE_PRIORITY if code in set(failures)
    )
    status: ValidationStatus = (
        ordered[0] if ordered else "validated_48_page_render"
    )
    return HwpRenderValidationResult(
        status=status,
        failure_codes=ordered,
        output_hwpx_sha256=output.sha256,
        output_reopen_structure_sha256=structure_sha,
        exported_pdf_sha256=None if pdf is None else pdf.sha256,
        binding_manifest_sha256=manifest_sha,
        raster_artifact_manifest_sha256=_raster_manifest_sha256(artifacts),
        rendered_page_count=page_count,
        render_directory=render_directory,
        pages=tuple(artifacts),
        diagnostics=tuple(dict.fromkeys(diagnostics))[:256],
        limitations=_LIMITATIONS,
        elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
    )


def rendered_page_manifest_sha256(
    artifacts: Sequence[RenderedPageEvidence],
) -> Optional[str]:
    """Return the path-free manifest hash for rendered page evidence."""

    if not artifacts:
        return None
    rows = [
        {
            "page_number": item.page_number,
            "png_sha256": item.png_sha256,
            "width_pixels": item.width_pixels,
            "height_pixels": item.height_pixels,
            "size_bytes": item.size_bytes,
            "significant_photo_count": item.significant_photo_count,
            "neutral_picture_count": item.neutral_picture_count,
            "caption_proxy_count": item.caption_proxy_count,
        }
        for item in artifacts
    ]
    return hashlib.sha256(
        json.dumps(
            {
                "schema_version": (
                    "trafficvisson.report-raster-artifacts.v2"
                ),
                "rendered_page_count": len(rows),
                "pages": rows,
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _raster_manifest_sha256(
    artifacts: Sequence[RenderedPageEvidence],
) -> Optional[str]:
    """Compatibility wrapper for the original private helper."""

    return rendered_page_manifest_sha256(artifacts)


__all__ = [
    "ApprovalGatedPdfExporter",
    "ExplicitApplyApproval",
    "ExpectedPagePairSlot",
    "HwpRenderValidationResult",
    "RenderValidationLimits",
    "RenderedPageEvidence",
    "ReportBindingManifest",
    "export_pdf_after_explicit_apply_approval",
    "rendered_page_manifest_sha256",
    "validate_saved_hwpx_pdf_render",
]
