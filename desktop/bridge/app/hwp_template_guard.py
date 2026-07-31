"""Fail-closed format routing before any HWP COM activation.

The filename is not trusted as a format oracle.  The guard binds suffix,
magic, a complete file fingerprint, and (for HWPX) the bounded structure
parser.  Exact audited samples are recognized by their pinned SHA-256 and can
never fall through to the legacy field/bookmark COM path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional, Union

from .hwp_com import HwpDocumentFingerprint, fingerprint_document
from .hwpx_sample_layout import (
    SAMPLE_LAYOUT_BASELINES,
    SampleLayoutInspection,
    identify_sample_layout,
    is_protected_sample_document_sha256,
)
from .hwpx_structure import HwpxStructureError, inspect_hwpx_structure


_HWP_OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
_HWPX_ZIP_MAGIC = b"PK\x03\x04"
_AUDITED_SAMPLE_NAMES = frozenset(
    baseline.expected_document_name.casefold()
    for baseline in SAMPLE_LAYOUT_BASELINES
)


class HwpTemplateGuardError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class HwpTemplateClassification:
    kind: Literal["exact_sample", "legacy_hwp", "legacy_hwpx"]
    fingerprint: HwpDocumentFingerprint
    sample_inspection: Optional[SampleLayoutInspection]


def classify_hwp_template(
    source_path: Union[str, Path],
) -> HwpTemplateClassification:
    """Classify an immutable HWP/HWPX source without invoking COM."""

    source = Path(source_path)
    initial = fingerprint_document(source)
    suffix = source.suffix.casefold()
    magic = _read_magic(source)
    after_magic = fingerprint_document(source)
    if not _fingerprints_match(initial, after_magic):
        raise HwpTemplateGuardError(
            "template_changed_during_classification",
            "문서 형식을 확인하는 동안 양식이 변경되었습니다.",
        )

    is_exact_sample = is_protected_sample_document_sha256(initial.sha256)
    if (
        source.name.casefold() in _AUDITED_SAMPLE_NAMES
        and not is_exact_sample
    ):
        raise HwpTemplateGuardError(
            "audited_sample_hash_mismatch",
            "3·4분기 표본 이름과 파일 해시가 일치하지 않습니다.",
        )
    if suffix not in {".hwp", ".hwpx"}:
        raise HwpTemplateGuardError(
            "unsupported_document_suffix",
            "문서 양식은 명시적인 HWP 또는 HWPX 파일이어야 합니다.",
        )

    if suffix == ".hwp":
        if magic.startswith(b"PK") or is_exact_sample:
            raise HwpTemplateGuardError(
                "hwpx_suffix_mismatch",
                "HWPX 내용을 HWP 확장자로 위장한 문서는 열 수 없습니다.",
            )
        if magic != _HWP_OLE_MAGIC:
            raise HwpTemplateGuardError(
                "hwp_magic_mismatch",
                "HWP 확장자와 문서 magic이 일치하지 않습니다.",
            )
        return HwpTemplateClassification(
            kind="legacy_hwp",
            fingerprint=after_magic,
            sample_inspection=None,
        )

    if not magic.startswith(_HWPX_ZIP_MAGIC):
        raise HwpTemplateGuardError(
            "hwpx_magic_mismatch",
            "HWPX 확장자와 문서 magic이 일치하지 않습니다.",
        )
    try:
        inspection = identify_sample_layout(source)
        snapshot = (
            inspect_hwpx_structure(source)
            if inspection is None
            else inspection.snapshot
        )
    except HwpxStructureError as error:
        raise HwpTemplateGuardError(
            "hwpx_structure_invalid",
            "HWPX 구조를 안전하게 검증하지 못해 COM 미리보기를 차단했습니다.",
        ) from error
    final = fingerprint_document(source)
    if not _fingerprints_match(initial, final):
        raise HwpTemplateGuardError(
            "template_changed_during_classification",
            "HWPX 구조를 확인하는 동안 양식이 변경되었습니다.",
        )
    if is_exact_sample:
        if inspection is None:
            raise HwpTemplateGuardError(
                "audited_sample_structure_mismatch",
                "감사된 표본 해시는 확인됐지만 고정 구조가 일치하지 않습니다.",
            )
        return HwpTemplateClassification(
            kind="exact_sample",
            fingerprint=final,
            sample_inspection=inspection,
        )
    if inspection is not None:
        # Defensive impossibility: sample inspection is SHA-pinned.
        raise HwpTemplateGuardError(
            "sample_identity_inconsistent",
            "표본 양식 식별 결과가 파일 해시와 일치하지 않습니다.",
        )
    if any(
        _matches_audited_sample_shape(snapshot, baseline)
        for baseline in SAMPLE_LAYOUT_BASELINES
    ):
        raise HwpTemplateGuardError(
            "audited_sample_hash_mismatch",
            "감사된 표본 구조와 일치하지만 파일 해시가 승인 기준과 다릅니다.",
        )
    return HwpTemplateClassification(
        kind="legacy_hwpx",
        fingerprint=final,
        sample_inspection=None,
    )


def _matches_audited_sample_shape(snapshot, baseline) -> bool:
    return (
        snapshot.section_count == baseline.section_count
        and len(snapshot.tables) == baseline.table_count
        and sum(
            len(table.cells) for table in snapshot.tables
        )
        == baseline.cell_count
        and len(snapshot.pictures) == baseline.picture_count
        and len(snapshot.page_breaks)
        == baseline.explicit_page_break_count
    )


def _read_magic(source: Path) -> bytes:
    try:
        with source.open("rb") as stream:
            return stream.read(8)
    except OSError as error:
        raise HwpTemplateGuardError(
            "template_magic_unreadable",
            "문서 양식의 magic을 안전하게 확인하지 못했습니다.",
        ) from error


def _fingerprints_match(
    actual: HwpDocumentFingerprint,
    expected: HwpDocumentFingerprint,
) -> bool:
    return (
        actual.path == expected.path
        and actual.device == expected.device
        and actual.inode == expected.inode
        and actual.size == expected.size
        and actual.modified_ns == expected.modified_ns
        and actual.sha256 == expected.sha256
    )


__all__ = [
    "HwpTemplateClassification",
    "HwpTemplateGuardError",
    "classify_hwp_template",
]
