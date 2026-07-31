"""Project-scoped approval ledger for sample HWPX reference cases.

The read-only weak-case extractor never promotes historical report evidence.
This module provides the separate user-confirmation boundary.  Every approval
is bound to the exact document, weak-case manifest, embedded image, HWPML
photo slot, and square-marker address set returned by the server-owned
catalog.  It creates neither a road-video Annotation nor an Incident.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence
from uuid import UUID

from .hwpx_sample_layout import (
    HWPX_SAMPLE_LAYOUT_SCHEMA,
    SAMPLE_LAYOUT_BINDING_REVISION,
    SAMPLE_REFERENCE_BINDING_REVISION,
)
from .weak_case_catalog import (
    WeakCaseCatalog,
    WeakCaseCatalogError,
    semantic_attestation_sha256,
)


APPROVED_REFERENCE_SCHEMA = "trafficvisson.approved-reference.v2"
APPROVED_REFERENCE_LIST_SCHEMA = (
    "trafficvisson.approved-reference-list.v2"
)
APPROVAL_ID_PATTERN = re.compile(
    r"^approved-reference-[a-f0-9]{24}$"
)
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
EVIDENCE_GROUP_ID_PATTERN = re.compile(
    r"^sample-evidence-group-[a-f0-9]{24}$"
)
DUPLICATE_CASE_REF_PATTERN = re.compile(
    r"^sample-hwpx-[34]q-[a-f0-9]{16}/"
    r"hwpx-weak-case-[a-f0-9]{32}$"
)
PICTURE_TARGET_ID_PATTERN = re.compile(
    r"^hwpx-picture-[a-f0-9]{24}$"
)
CELL_TARGET_ID_PATTERN = re.compile(
    r"^hwpx-cell-[a-f0-9]{24}$"
)
PHOTO_SLOT_ID_PATTERN = re.compile(
    r"^sample-photo-slot-[a-f0-9]{24}$"
)
SECTION_PATH_PATTERN = re.compile(
    r"^Contents/section[0-9]{1,5}\.xml$"
)
MAX_RECORD_BYTES = 256 * 1024
_WINDOWS_REPARSE_ATTRIBUTE = 0x400
_SEMANTIC_CELL_ROLES = (
    "photo_caption",
    "inspection_route",
    "inspection_opinion_label",
    "inspection_opinion_body",
)


class WeakCaseApprovalError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_optional_text(
    value: object,
    *,
    maximum: int,
) -> bool:
    return value is None or (
        isinstance(value, str)
        and len(value) <= maximum
    )


def _non_boolean_int(
    value: object,
    *,
    minimum: int,
    maximum: int,
) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and minimum <= value <= maximum
    )


def _approval_boundary_and_provenance_valid(
    record: Mapping[str, Any],
) -> bool:
    """Recheck the historical-seed boundary before internal reuse.

    API response models validate these fields too, but training and retrieval
    services consume the store directly.  A durable approval therefore has to
    prove its exact sample address and remain an auxiliary reference with zero
    current-video truth even when it is loaded outside an HTTP response.
    """

    required_hashes = (
        "document_sha256",
        "structure_target_summary_sha256",
        "index_manifest_sha256",
        "image_sha256",
        "sample_layout_binding_sha256",
        "semantic_attestation_sha256",
        "semantic_sha256",
    )
    if any(
        not isinstance(record.get(field), str)
        or SHA256_PATTERN.fullmatch(record[field]) is None
        for field in required_hashes
    ):
        return False

    selected = {
        "issue_type": record.get("issue_type"),
        "maintenance_action": record.get("maintenance_action"),
        "caption": record.get("caption"),
        "route": record.get("route"),
        "location": record.get("location"),
        "evaluation": record.get("evaluation"),
        "opinion": record.get("opinion"),
    }
    optional_texts = (
        selected["caption"],
        selected["route"],
        selected["location"],
        selected["evaluation"],
        selected["opinion"],
    )
    weak_candidates = record.get("weak_candidates")
    semantic_cells = record.get("semantic_cells")
    square_markers = record.get("square_markers")
    expected_roles = {
        "photo_caption",
        "inspection_route",
        "inspection_opinion_label",
        "inspection_opinion_body",
    }
    if (
        record.get("reference_state") != "approved_reference"
        or record.get("approved_reference") is not True
        or record.get("training_eligible") is not True
        or record.get("training_scope")
        != "approved_reference_only"
        or record.get("annotation_created") is not False
        or record.get("incident_created") is not False
        or record.get("user_confirmed") is not True
        or record.get("quarter") not in {3, 4}
        or not isinstance(record.get("document_name"), str)
        or not record["document_name"]
        or len(record["document_name"]) > 120
        or not isinstance(record.get("section_path"), str)
        or SECTION_PATH_PATTERN.fullmatch(record["section_path"])
        is None
        or not _non_boolean_int(
            record.get("section_index"),
            minimum=0,
            maximum=100_000,
        )
        or not isinstance(record.get("picture_target_id"), str)
        or PICTURE_TARGET_ID_PATTERN.fullmatch(
            record["picture_target_id"]
        )
        is None
        or not _non_boolean_int(
            record.get("picture_index"),
            minimum=0,
            maximum=100_000,
        )
        or not isinstance(record.get("table_id"), str)
        or not record["table_id"]
        or len(record["table_id"]) > 120
        or not _non_boolean_int(
            record.get("table_index"),
            minimum=0,
            maximum=100_000,
        )
        or not isinstance(record.get("cell_target_id"), str)
        or CELL_TARGET_ID_PATTERN.fullmatch(
            record["cell_target_id"]
        )
        is None
        or not _non_boolean_int(
            record.get("cell_row"),
            minimum=0,
            maximum=100_000,
        )
        or not _non_boolean_int(
            record.get("cell_column"),
            minimum=0,
            maximum=100_000,
        )
        or not isinstance(record.get("photo_slot_id"), str)
        or PHOTO_SLOT_ID_PATTERN.fullmatch(
            record["photo_slot_id"]
        )
        is None
        or record.get("photo_page_number")
        not in set(range(2, 49, 2))
        or record.get("inspection_page_number")
        != record.get("photo_page_number") - 1
        or not _non_boolean_int(
            record.get("page_hint"),
            minimum=1,
            maximum=10_000,
        )
        or not isinstance(record.get("page_hint_source"), str)
        or not record["page_hint_source"]
        or len(record["page_hint_source"]) > 120
        or record.get("image_media_type")
        not in {
            "image/bmp",
            "image/png",
            "image/jpeg",
            "image/gif",
            "image/webp",
        }
        or not _non_boolean_int(
            record.get("image_width"),
            minimum=1,
            maximum=65_535,
        )
        or not _non_boolean_int(
            record.get("image_height"),
            minimum=1,
            maximum=65_535,
        )
        or not isinstance(selected["issue_type"], str)
        or not selected["issue_type"]
        or len(selected["issue_type"]) > 128
        or not isinstance(selected["maintenance_action"], str)
        or not selected["maintenance_action"]
        or selected["maintenance_action"]
        != selected["maintenance_action"].strip()
        or len(selected["maintenance_action"]) > 500
        or any(
            not _bounded_optional_text(value, maximum=320)
            for value in optional_texts
        )
        or _canonical_sha256(selected)
        != record.get("semantic_sha256")
        or not isinstance(weak_candidates, Mapping)
        or set(weak_candidates) != {
            "caption",
            "route",
            "location",
            "evaluation",
            "opinion",
        }
        or any(
            not _bounded_optional_text(value, maximum=320)
            for value in weak_candidates.values()
        )
        or not isinstance(semantic_cells, list)
        or len(semantic_cells) != 4
        or any(
            not isinstance(cell, Mapping)
            for cell in semantic_cells
        )
        or {
            cell.get("role")
            for cell in semantic_cells
            if isinstance(cell, Mapping)
        }
        != expected_roles
        or not isinstance(square_markers, list)
        or not square_markers
        or any(
            not isinstance(marker, Mapping)
            or marker.get("marker") != "■"
            for marker in square_markers
        )
    ):
        return False
    return True


def _record_matches_current_attestation(
    record: Mapping[str, Any],
    attestation: Mapping[str, Any],
) -> bool:
    """Bind a durable approval back to the current server-owned sample case.

    A record SHA detects accidental changes, but an attacker or repair tool
    that can rewrite SQLite could reseal a modified JSON row.  The immutable
    sample catalog is the independent authority for the historical document,
    photo, page/table/cell addresses, marker cells and weak source text.  This
    comparison therefore runs every time an approval is reused.
    """

    sample_binding = attestation.get("sample_layout_binding")
    if not isinstance(sample_binding, Mapping):
        return False
    semantic_cells = [
        sample_binding.get(role) for role in _SEMANTIC_CELL_ROLES
    ]
    if any(not isinstance(cell, Mapping) for cell in semantic_cells):
        return False
    square_markers = sample_binding.get("square_markers")
    duplicate_case_refs = attestation.get(
        "duplicate_image_case_refs"
    )
    if (
        not isinstance(square_markers, list)
        or not square_markers
        or not isinstance(duplicate_case_refs, list)
        or attestation.get("duplicate_image_registry_complete")
        is not True
    ):
        return False

    exact_fields = (
        "source_id",
        "case_id",
        "document_name",
        "document_sha256",
        "structure_target_summary_sha256",
        "index_manifest_sha256",
        "image_sha256",
        "image_media_type",
        "image_width",
        "image_height",
        "quarter",
        "section_path",
        "section_index",
        "picture_target_id",
        "picture_index",
        "picture_xml_id",
        "table_id",
        "table_index",
        "cell_target_id",
        "cell_row",
        "cell_column",
        "page_hint",
        "page_hint_source",
        "semantic_attestation_sha256",
        "duplicate_image_occurrence_count",
        "duplicate_image_review_required",
        "duplicate_image_case_refs",
    )
    if any(
        record.get(field) != attestation.get(field)
        for field in exact_fields
    ):
        return False

    binding_pairs = (
        ("photo_slot_id", "photo_slot_id"),
        ("photo_page_number", "photo_page_number"),
        ("inspection_page_number", "inspection_page_number"),
        (
            "sample_layout_binding_schema",
            "sample_layout_binding_schema",
        ),
        (
            "sample_layout_binding_revision",
            "sample_layout_binding_revision",
        ),
        (
            "semantic_projection_revision",
            "semantic_projection_revision",
        ),
        (
            "sample_evidence_group_id",
            "sample_evidence_group_id",
        ),
        ("group_photo_index", "group_photo_index"),
        ("group_photo_count", "group_photo_count"),
        ("sample_layout_binding_sha256", "binding_sha256"),
    )
    if any(
        record.get(record_field)
        != sample_binding.get(binding_field)
        for record_field, binding_field in binding_pairs
    ):
        return False

    weak_candidates = record.get("weak_candidates")
    expected_weak_candidates = {
        "caption": attestation.get("caption"),
        "route": attestation.get("route"),
        "location": attestation.get("location"),
        "evaluation": attestation.get("evaluation"),
        "opinion": attestation.get("opinion"),
    }
    return (
        record.get("semantic_cells") == semantic_cells
        and record.get("square_markers") == square_markers
        and weak_candidates == expected_weak_candidates
    )


def _is_link_or_reparse(path: Path) -> bool:
    result = os.lstat(str(path))
    return stat.S_ISLNK(result.st_mode) or bool(
        int(getattr(result, "st_file_attributes", 0))
        & _WINDOWS_REPARSE_ATTRIBUTE
    )


def _ensure_real_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("weak-case approval data_root must be absolute")
    path.mkdir(parents=True, exist_ok=True)
    resolved = path.resolve()
    if resolved != path:
        raise ValueError("weak-case approval data_root cannot be a link")
    for component in (resolved, *resolved.parents):
        if str(component) == component.anchor:
            continue
        if _is_link_or_reparse(component):
            raise ValueError(
                "weak-case approval data_root cannot contain links"
            )
    return resolved


class WeakCaseApprovalStore:
    """Durable exact-provenance approval state, isolated per project."""

    def __init__(
        self,
        data_root: Path,
        catalog: WeakCaseCatalog,
    ) -> None:
        root = _ensure_real_directory(Path(data_root))
        store_root = root / "approved-reference-cases"
        store_root.mkdir(mode=0o700, exist_ok=True)
        if _is_link_or_reparse(store_root):
            raise ValueError(
                "weak-case approval store cannot be a link"
            )
        self._database_path = store_root / "approvals.sqlite3"
        self._catalog = catalog
        self._lock = threading.RLock()
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self._database_path),
            timeout=10.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _migrate(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS approved_reference_cases (
                    project_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    approval_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    payload_json TEXT NOT NULL,
                    record_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, source_id, case_id),
                    UNIQUE(project_id, approval_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                approved_reference_cases_project_source
                ON approved_reference_cases(project_id, source_id)
                """
            )
            connection.execute("COMMIT")

    def _revalidate_record(
        self,
        record: Dict[str, Any],
        *,
        attestation: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        try:
            current = (
                attestation
                if attestation is not None
                else self._catalog.case_attestation(
                    record["source_id"],
                    record["case_id"],
                )
            )
        except WeakCaseCatalogError as error:
            raise WeakCaseApprovalError(
                "approval_source_changed",
                "승인 사례의 현재 표본 근거를 재검증하지 못했습니다.",
            ) from error
        if not _record_matches_current_attestation(record, current):
            raise WeakCaseApprovalError(
                "approval_source_changed",
                "승인 사례의 문서·사진·페이지·셀 근거가 변경되었습니다.",
            )
        return record

    def approve(
        self,
        project_id: UUID,
        request: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Approve one exact weak case after explicit user confirmation."""

        if request.get("user_confirmed") is not True:
            raise WeakCaseApprovalError(
                "confirmation_required",
                "사용자 확인 없이는 표본 사례를 승인할 수 없습니다.",
            )
        source_id = str(request.get("source_id", ""))
        case_id = str(request.get("case_id", ""))
        try:
            attestation = self._catalog.case_attestation(
                source_id,
                case_id,
            )
        except WeakCaseCatalogError as error:
            raise WeakCaseApprovalError(
                error.code,
                str(error),
            ) from error
        for field in (
            "expected_document_sha256",
            "expected_index_manifest_sha256",
            "expected_image_sha256",
        ):
            value = request.get(field)
            if not isinstance(value, str) or SHA256_PATTERN.fullmatch(
                value
            ) is None:
                raise WeakCaseApprovalError(
                    "invalid_expected_hash",
                    "승인 요청의 예상 해시가 올바르지 않습니다.",
                )
        expected_pairs = (
            (
                "expected_document_sha256",
                "document_sha256",
                "document_changed",
            ),
            (
                "expected_index_manifest_sha256",
                "index_manifest_sha256",
                "case_index_changed",
            ),
            (
                "expected_image_sha256",
                "image_sha256",
                "image_changed",
            ),
        )
        for expected_field, actual_field, code in expected_pairs:
            if request[expected_field] != attestation[actual_field]:
                raise WeakCaseApprovalError(
                    code,
                    "미리보기 이후 표본 사례 근거가 변경되었습니다.",
                )
        sample_binding = attestation.get("sample_layout_binding")
        if not isinstance(sample_binding, Mapping):
            raise WeakCaseApprovalError(
                "sample_layout_binding_required",
                "감사된 3·4분기 사진 슬롯과 네모 위치가 없는 사례는 승인할 수 없습니다.",
            )
        square_markers = sample_binding.get("square_markers")
        semantic_roles = (
            "photo_caption",
            "inspection_route",
            "inspection_opinion_label",
            "inspection_opinion_body",
        )
        if (
            not isinstance(square_markers, list)
            or not square_markers
            or not isinstance(sample_binding.get("photo_slot_id"), str)
            or sample_binding.get("sample_layout_binding_schema")
            != HWPX_SAMPLE_LAYOUT_SCHEMA
            or sample_binding.get("sample_layout_binding_revision")
            != SAMPLE_LAYOUT_BINDING_REVISION
            or sample_binding.get("semantic_projection_revision")
            != SAMPLE_REFERENCE_BINDING_REVISION
            or not isinstance(
                sample_binding.get("sample_evidence_group_id"),
                str,
            )
            or isinstance(
                sample_binding.get("group_photo_index"), bool
            )
            or not isinstance(
                sample_binding.get("group_photo_index"), int
            )
            or isinstance(
                sample_binding.get("group_photo_count"), bool
            )
            or not isinstance(
                sample_binding.get("group_photo_count"), int
            )
            or not (
                1
                <= sample_binding["group_photo_index"]
                <= sample_binding["group_photo_count"]
            )
            or any(
                not isinstance(sample_binding.get(role), Mapping)
                for role in semantic_roles
            )
        ):
            raise WeakCaseApprovalError(
                "sample_marker_binding_required",
                "표본 사례의 사진 슬롯 또는 네모 위치 결속이 불완전합니다.",
            )
        attested_semantic_sha256 = attestation.get(
            "semantic_attestation_sha256"
        )
        try:
            current_semantic_attestation_sha256 = (
                semantic_attestation_sha256(attestation)
            )
        except (TypeError, ValueError) as error:
            raise WeakCaseApprovalError(
                "semantic_attestation_invalid",
                "표본 사례의 의미 결속을 재검증하지 못했습니다.",
            ) from error
        if (
            not isinstance(attested_semantic_sha256, str)
            or SHA256_PATTERN.fullmatch(
                attested_semantic_sha256
            )
            is None
            or attested_semantic_sha256
            != current_semantic_attestation_sha256
        ):
            raise WeakCaseApprovalError(
                "semantic_attestation_changed",
                "표본 사례의 캡션·사진 묶음 결속이 변경되었습니다.",
            )
        duplicate_image_registry_complete = attestation.get(
            "duplicate_image_registry_complete"
        )
        duplicate_image_review_required = attestation.get(
            "duplicate_image_review_required"
        )
        duplicate_image_occurrence_count = attestation.get(
            "duplicate_image_occurrence_count"
        )
        duplicate_image_case_refs = attestation.get(
            "duplicate_image_case_refs"
        )
        if (
            duplicate_image_registry_complete is not True
            or not isinstance(
                duplicate_image_review_required, bool
            )
            or isinstance(
                duplicate_image_occurrence_count, bool
            )
            or not isinstance(
                duplicate_image_occurrence_count, int
            )
            or duplicate_image_occurrence_count < 1
            or not isinstance(duplicate_image_case_refs, list)
            or len(duplicate_image_case_refs)
            != duplicate_image_occurrence_count
            or f"{source_id}/{case_id}"
            not in duplicate_image_case_refs
        ):
            raise WeakCaseApprovalError(
                "duplicate_image_registry_incomplete",
                "표본 중복 사진 상태를 재검증하지 못했습니다.",
            )
        duplicate_image_review_confirmed = request.get(
            "duplicate_image_review_confirmed",
            False,
        )
        if (
            not isinstance(
                duplicate_image_review_confirmed, bool
            )
            or (
                duplicate_image_review_required
                and duplicate_image_review_confirmed is not True
            )
        ):
            raise WeakCaseApprovalError(
                "duplicate_image_confirmation_required",
                "다른 노선·쪽에 재사용된 동일 사진임을 확인해야 승인할 수 있습니다.",
            )

        selected = {
            "issue_type": request.get("issue_type"),
            "maintenance_action": request.get("maintenance_action"),
            "caption": request.get("caption"),
            "route": request.get("route"),
            "location": request.get("location"),
            "evaluation": request.get("evaluation"),
            "opinion": request.get("opinion"),
        }
        semantic_sha256 = _canonical_sha256(selected)
        project_text = str(project_id)
        approval_id = (
            "approved-reference-"
            + _canonical_sha256(
                {
                    "project_id": project_text,
                    "source_id": source_id,
                    "case_id": case_id,
                }
            )[:24]
        )
        expected_revision = request.get("expected_revision")
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise WeakCaseApprovalError(
                "invalid_expected_revision",
                "승인 사례 revision이 올바르지 않습니다.",
            )

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_row = connection.execute(
                """
                SELECT * FROM approved_reference_cases
                WHERE project_id = ? AND source_id = ? AND case_id = ?
                """,
                (project_text, source_id, case_id),
            ).fetchone()
            if existing_row is not None:
                existing = self._revalidate_record(
                    self._row_record(existing_row),
                    attestation=attestation,
                )
                if (
                    existing["semantic_sha256"] == semantic_sha256
                    and existing["document_sha256"]
                    == attestation["document_sha256"]
                    and existing["index_manifest_sha256"]
                    == attestation["index_manifest_sha256"]
                    and existing["image_sha256"]
                    == attestation["image_sha256"]
                    and existing["sample_layout_binding_sha256"]
                    == sample_binding["binding_sha256"]
                    and existing["semantic_attestation_sha256"]
                    == current_semantic_attestation_sha256
                    and existing[
                        "duplicate_image_review_confirmed"
                    ]
                    == duplicate_image_review_confirmed
                ):
                    connection.execute("COMMIT")
                    return {
                        **existing,
                        "idempotent_replay": True,
                    }
                if expected_revision != existing["revision"]:
                    connection.execute("ROLLBACK")
                    raise WeakCaseApprovalError(
                        "approval_revision_conflict",
                        "승인 사례가 이미 변경되었습니다. 다시 확인하세요.",
                    )
                revision = existing["revision"] + 1
                created_at = existing["approved_at"]
            else:
                if expected_revision is not None:
                    connection.execute("ROLLBACK")
                    raise WeakCaseApprovalError(
                        "approval_revision_conflict",
                        "새 승인 사례에는 기존 revision을 지정할 수 없습니다.",
                    )
                revision = 1
                created_at = _utc_now()

            updated_at = _utc_now()
            record_without_hash: Dict[str, Any] = {
                "schema_version": APPROVED_REFERENCE_SCHEMA,
                "approval_id": approval_id,
                "project_id": project_text,
                "source_id": source_id,
                "case_id": case_id,
                "revision": revision,
                "reference_state": "approved_reference",
                "approved_reference": True,
                "training_eligible": True,
                "training_scope": "approved_reference_only",
                "annotation_created": False,
                "incident_created": False,
                "document_name": attestation["document_name"],
                "document_sha256": attestation["document_sha256"],
                "structure_target_summary_sha256": attestation[
                    "structure_target_summary_sha256"
                ],
                "index_manifest_sha256": attestation[
                    "index_manifest_sha256"
                ],
                "image_sha256": attestation["image_sha256"],
                "image_media_type": attestation["image_media_type"],
                "image_width": attestation["image_width"],
                "image_height": attestation["image_height"],
                "quarter": attestation["quarter"],
                "section_path": attestation["section_path"],
                "section_index": attestation["section_index"],
                "picture_target_id": attestation["picture_target_id"],
                "picture_index": attestation["picture_index"],
                "picture_xml_id": attestation["picture_xml_id"],
                "table_id": attestation["table_id"],
                "table_index": attestation["table_index"],
                "cell_target_id": attestation["cell_target_id"],
                "cell_row": attestation["cell_row"],
                "cell_column": attestation["cell_column"],
                "page_hint": attestation["page_hint"],
                "page_hint_source": attestation["page_hint_source"],
                "photo_slot_id": sample_binding["photo_slot_id"],
                "sample_layout_binding_schema": sample_binding[
                    "sample_layout_binding_schema"
                ],
                "sample_layout_binding_revision": sample_binding[
                    "sample_layout_binding_revision"
                ],
                "semantic_projection_revision": sample_binding[
                    "semantic_projection_revision"
                ],
                "semantic_attestation_sha256": (
                    current_semantic_attestation_sha256
                ),
                "sample_evidence_group_id": sample_binding[
                    "sample_evidence_group_id"
                ],
                "group_photo_index": sample_binding[
                    "group_photo_index"
                ],
                "group_photo_count": sample_binding[
                    "group_photo_count"
                ],
                "duplicate_image_occurrence_count": (
                    duplicate_image_occurrence_count
                ),
                "duplicate_image_review_required": (
                    duplicate_image_review_required
                ),
                "duplicate_image_review_confirmed": (
                    duplicate_image_review_confirmed
                ),
                "duplicate_image_case_refs": list(
                    duplicate_image_case_refs
                ),
                "photo_page_number": sample_binding[
                    "photo_page_number"
                ],
                "inspection_page_number": sample_binding[
                    "inspection_page_number"
                ],
                "sample_layout_binding_sha256": sample_binding[
                    "binding_sha256"
                ],
                "square_markers": square_markers,
                "semantic_cells": [
                    sample_binding[role] for role in semantic_roles
                ],
                "weak_candidates": {
                    "caption": attestation["caption"],
                    "route": attestation["route"],
                    "location": attestation["location"],
                    "evaluation": attestation["evaluation"],
                    "opinion": attestation["opinion"],
                },
                "issue_type": selected["issue_type"],
                "maintenance_action": selected[
                    "maintenance_action"
                ],
                "caption": selected["caption"],
                "route": selected["route"],
                "location": selected["location"],
                "evaluation": selected["evaluation"],
                "opinion": selected["opinion"],
                "semantic_sha256": semantic_sha256,
                "approved_at": created_at,
                "updated_at": updated_at,
                "user_confirmed": True,
            }
            if (
                not _approval_boundary_and_provenance_valid(
                    record_without_hash
                )
                or not _record_matches_current_attestation(
                    record_without_hash,
                    attestation,
                )
            ):
                connection.execute("ROLLBACK")
                raise WeakCaseApprovalError(
                    "invalid_approval_record",
                    "승인 사례의 문서·사진·페이지·셀 결속이 올바르지 않습니다.",
                )
            record_sha256 = _canonical_sha256(record_without_hash)
            record = {
                **record_without_hash,
                "record_sha256": record_sha256,
            }
            payload = json.dumps(
                record,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if len(payload.encode("utf-8")) > MAX_RECORD_BYTES:
                connection.execute("ROLLBACK")
                raise WeakCaseApprovalError(
                    "approval_record_too_large",
                    "승인 사례 기록이 허용 크기를 초과했습니다.",
                )
            connection.execute(
                """
                INSERT INTO approved_reference_cases(
                    project_id, source_id, case_id, approval_id,
                    revision, payload_json, record_sha256,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, source_id, case_id)
                DO UPDATE SET
                    approval_id = excluded.approval_id,
                    revision = excluded.revision,
                    payload_json = excluded.payload_json,
                    record_sha256 = excluded.record_sha256,
                    updated_at = excluded.updated_at
                """,
                (
                    project_text,
                    source_id,
                    case_id,
                    approval_id,
                    revision,
                    payload,
                    record_sha256,
                    created_at,
                    updated_at,
                ),
            )
            connection.execute("COMMIT")
        return {**record, "idempotent_replay": False}

    def list_project(self, project_id: UUID) -> Dict[str, Any]:
        project_text = str(project_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM approved_reference_cases
                WHERE project_id = ?
                ORDER BY updated_at ASC, source_id ASC, case_id ASC
                """,
                (project_text,),
            ).fetchall()
        records = [
            self._revalidate_record(self._row_record(row))
            for row in rows
        ]
        approved_reference_event_count = len(
            {
                record["sample_evidence_group_id"]
                for record in records
            }
        )
        return {
            "schema_version": APPROVED_REFERENCE_LIST_SCHEMA,
            "project_id": project_text,
            "approved_reference_count": len(records),
            "approved_reference_event_count": (
                approved_reference_event_count
            ),
            "training_eligible_count": len(records),
            "duplicate_image_reference_count": sum(
                bool(record["duplicate_image_review_required"])
                for record in records
            ),
            "annotation_count": 0,
            "incident_count": 0,
            "records": records,
        }

    def list_by_approval_ids(
        self,
        project_id: UUID,
        approval_ids: Sequence[str],
    ) -> list[Dict[str, Any]]:
        """Load only plan-referenced approvals, never the whole project set."""

        if isinstance(approval_ids, (str, bytes)):
            raise WeakCaseApprovalError(
                "invalid_approval_ids",
                "승인 사례 식별자 목록이 올바르지 않습니다.",
            )
        unique_ids = tuple(sorted(set(approval_ids)))
        if len(unique_ids) > 100 or any(
            not isinstance(value, str)
            or APPROVAL_ID_PATTERN.fullmatch(value) is None
            for value in unique_ids
        ):
            raise WeakCaseApprovalError(
                "invalid_approval_ids",
                "편집 계획의 승인 사례 식별자가 올바르지 않습니다.",
            )
        if not unique_ids:
            return []
        placeholders = ",".join("?" for _ in unique_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM approved_reference_cases
                WHERE project_id = ?
                  AND approval_id IN ({placeholders})
                ORDER BY approval_id ASC
                """,
                (str(project_id), *unique_ids),
            ).fetchall()
        return [
            self._revalidate_record(self._row_record(row))
            for row in rows
        ]

    def decorate_source_list(
        self,
        project_id: UUID,
        snapshot: Mapping[str, Any],
    ) -> Dict[str, Any]:
        result = dict(snapshot)
        records = self.list_project(project_id)["records"]
        ready_source_ids = {
            source.get("source_id")
            for source in result.get("sources", ())
            if source.get("status") == "ready"
        }
        approved_by_source: Dict[str, int] = {}
        for record in records:
            if record["source_id"] not in ready_source_ids:
                continue
            approved_by_source[record["source_id"]] = (
                approved_by_source.get(record["source_id"], 0) + 1
            )
        sources = []
        for source in result.get("sources", ()):
            decorated = dict(source)
            approved = (
                approved_by_source.get(
                    decorated.get("source_id"), 0
                )
                if decorated.get("status") == "ready"
                else 0
            )
            decorated["approved_reference_count"] = approved
            decorated["training_eligible_count"] = approved
            if decorated.get("status") == "ready":
                decorated["review_required_count"] = max(
                    0,
                    int(decorated.get("case_count", 0)) - approved,
                )
            sources.append(decorated)
        result["sources"] = sources
        result["catalog_role"] = (
            "sample_hwpx_project_scoped_reference"
        )
        result["capability_state"] = (
            "approved_reference_available"
            if approved_by_source
            else result.get("capability_state", "unavailable")
        )
        return result

    def decorate_search(
        self,
        project_id: UUID,
        snapshot: Mapping[str, Any],
    ) -> Dict[str, Any]:
        result = dict(snapshot)
        records = self.list_project(project_id)["records"]
        by_identity = {
            (record["source_id"], record["case_id"]): record
            for record in records
        }
        cases = []
        approved_count = 0
        for case in result.get("cases", ()):
            decorated = dict(case)
            approved = by_identity.get(
                (
                    decorated.get("source_id"),
                    decorated.get("case_id"),
                )
            )
            if approved is not None:
                approved_count += 1
                decorated.update(
                    {
                        "reference_state": "approved_reference",
                        "review_required": False,
                        "approved_reference": True,
                        "training_eligible": True,
                        "approval_id": approved["approval_id"],
                        "approval_revision": approved["revision"],
                        "approval_record_sha256": approved[
                            "record_sha256"
                        ],
                        "approved_issue_type": approved["issue_type"],
                        "approved_action": approved[
                            "maintenance_action"
                        ],
                    }
                )
            cases.append(decorated)
        result["cases"] = cases
        result["approved_reference_count"] = approved_count
        result["training_eligible_count"] = approved_count
        result["capability_state"] = (
            "approved_reference_available"
            if approved_count
            else result.get("capability_state", "unavailable")
        )
        return result

    @staticmethod
    def _row_record(row: sqlite3.Row) -> Dict[str, Any]:
        payload = row["payload_json"]
        if not isinstance(payload, str) or len(
            payload.encode("utf-8")
        ) > MAX_RECORD_BYTES:
            raise WeakCaseApprovalError(
                "approval_store_corrupt",
                "승인 사례 저장소 레코드 크기가 올바르지 않습니다.",
            )
        try:
            record = json.loads(payload)
        except (TypeError, ValueError) as error:
            raise WeakCaseApprovalError(
                "approval_store_corrupt",
                "승인 사례 저장소 JSON이 손상되었습니다.",
            ) from error
        if not isinstance(record, dict):
            raise WeakCaseApprovalError(
                "approval_store_corrupt",
                "승인 사례 저장소 레코드가 올바르지 않습니다.",
            )
        record_sha256 = record.pop("record_sha256", None)
        calculated = _canonical_sha256(record)
        stored = row["record_sha256"]
        if (
            record_sha256 != stored
            or calculated != stored
            or record.get("schema_version") != APPROVED_REFERENCE_SCHEMA
            or record.get("project_id") != row["project_id"]
            or record.get("source_id") != row["source_id"]
            or record.get("case_id") != row["case_id"]
            or record.get("approval_id") != row["approval_id"]
            or record.get("revision") != row["revision"]
            or APPROVAL_ID_PATTERN.fullmatch(
                str(record.get("approval_id", ""))
            )
            is None
            or record.get("sample_layout_binding_schema")
            != HWPX_SAMPLE_LAYOUT_SCHEMA
            or record.get("sample_layout_binding_revision")
            != SAMPLE_LAYOUT_BINDING_REVISION
            or record.get("semantic_projection_revision")
            != SAMPLE_REFERENCE_BINDING_REVISION
            or not _approval_boundary_and_provenance_valid(record)
            or not isinstance(
                record.get("semantic_attestation_sha256"), str
            )
            or SHA256_PATTERN.fullmatch(
                record["semantic_attestation_sha256"]
            )
            is None
            or not isinstance(
                record.get("sample_evidence_group_id"), str
            )
            or EVIDENCE_GROUP_ID_PATTERN.fullmatch(
                record["sample_evidence_group_id"]
            )
            is None
            or isinstance(record.get("group_photo_index"), bool)
            or not isinstance(record.get("group_photo_index"), int)
            or isinstance(record.get("group_photo_count"), bool)
            or not isinstance(record.get("group_photo_count"), int)
            or not (
                1
                <= record["group_photo_index"]
                <= record["group_photo_count"]
            )
            or isinstance(
                record.get("duplicate_image_occurrence_count"),
                bool,
            )
            or not isinstance(
                record.get("duplicate_image_occurrence_count"),
                int,
            )
            or record["duplicate_image_occurrence_count"] < 1
            or not isinstance(
                record.get("duplicate_image_review_required"),
                bool,
            )
            or not isinstance(
                record.get("duplicate_image_review_confirmed"),
                bool,
            )
            or record["duplicate_image_review_required"]
            != (
                record["duplicate_image_occurrence_count"] > 1
            )
            or (
                record["duplicate_image_review_required"]
                and not record["duplicate_image_review_confirmed"]
            )
            or not isinstance(
                record.get("duplicate_image_case_refs"), list
            )
            or len(record["duplicate_image_case_refs"])
            != record["duplicate_image_occurrence_count"]
            or (
                f"{record.get('source_id')}/{record.get('case_id')}"
                not in record["duplicate_image_case_refs"]
            )
            or record["duplicate_image_case_refs"]
            != sorted(set(record["duplicate_image_case_refs"]))
            or any(
                not isinstance(value, str)
                or DUPLICATE_CASE_REF_PATTERN.fullmatch(value)
                is None
                for value in record[
                    "duplicate_image_case_refs"
                ]
            )
        ):
            raise WeakCaseApprovalError(
                "approval_store_corrupt",
                "승인 사례 저장소 무결성 검사가 실패했습니다.",
            )
        return {**record, "record_sha256": stored}


__all__ = [
    "APPROVED_REFERENCE_LIST_SCHEMA",
    "APPROVED_REFERENCE_SCHEMA",
    "WeakCaseApprovalError",
    "WeakCaseApprovalStore",
]
