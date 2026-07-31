"""Compile one current-Incident binding manifest into an exact HWPX plan.

This is the deterministic boundary between
``CurrentIncidentReportBindingManifest`` and ``ExactHwpxWritePlan``.  It does
not call a Provider, HWP COM, or the writer itself.

The compiler is intentionally strict:

* the byte-exact audited sample is reopened read-only;
* every replacement, neutralization, and evaluation target is consumed;
* one exact target receives one final mutation, with only identical stale
  evaluation-marker clears allowed to consolidate;
* all current evidence is resolved from a server-owned absolute path and
  verified against the selected reference and SHA-256 in the binding manifest;
* evidence is deterministically transcoded to the existing target picture
  media, while removed pictures receive a deterministic same-media neutral
  payload so their HWPML geometry is left intact; and
* the complete plan is emitted atomically only when it fits the exact writer's
  own operation limit.  It is never split or truncated to the historical
  Provider-facing 100-operation limit.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import zipfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple, Union

from . import hwpx_structure as structure
from .current_incident_report_binder import (
    CURRENT_INCIDENT_REPORT_BINDING_SCHEMA,
    CurrentIncidentReportBindingManifest,
    EvaluationRowBinding,
    ReportTargetMutation,
)
from .hwpx_exact_writer import (
    ExactHwpxOperation,
    ExactHwpxParagraphTextOperation,
    ExactHwpxPictureOperation,
    ExactHwpxTextOperation,
    ExactHwpxWritePlan,
    ExactHwpxWriterLimits,
    HwpxExactWriterError,
    _MEDIA_ALIASES,
    _archive_key,
    _direct_table_cells,
    _image_limits,
    _normalized_cell_text,
    _parse_manifest,
    _validate_plan,
)
from .hwpx_stale_factual_targets import (
    FactualParagraphTarget,
    FactualPictureTarget,
    HwpxStaleFactualTargetAudit,
    HwpxStaleFactualTargetError,
    XmlParagraphAddress,
    audit_stale_factual_targets,
    require_stale_factual_target_coverage,
)
from .hwpx_sample_layout import (
    HwpxSampleLayoutError,
    SampleLayoutInspection,
    inspect_sample_layout,
)
from .hwpx_structure import CellSnapshot, PictureSnapshot
from .hwpx_weak_cases import _inspect_image_header


CURRENT_INCIDENT_HWPX_PLAN_SCHEMA = (
    "trafficvisson.current-incident-hwpx-plan.v1"
)
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SUPPORTED_TARGET_MEDIA = {
    "image/bmp": ("BMP", ".bmp"),
    "image/png": ("PNG", ".png"),
    "image/jpeg": ("JPEG", ".jpg"),
}
_MAX_TRANSCODE_DIMENSION = 1_024


class CurrentIncidentHwpxPlanError(RuntimeError):
    """Fail-closed binding-to-writer compilation error."""

    provider_called = False
    com_invoked = False
    writer_invoked = False
    apply_blocked = True

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ServerOwnedEvidenceFile:
    """One immutable server-catalog evidence path selected by the binder."""

    evidence_ref: str
    absolute_path: str
    sha256: str


@dataclass(frozen=True)
class _TrustedEvidence:
    evidence_ref: str
    path: Path
    sha256: str
    payload: bytes


@dataclass(frozen=True)
class _PictureSource:
    target_id: str
    binary_item_ref: str
    package_member: str
    current_sha256: str
    media_type: str


@dataclass(frozen=True)
class _PendingText:
    target_id: str
    expected_text: str
    expected_sha256: str
    value: str
    source_ref: str
    roles: Tuple[str, ...]
    consumed_tokens: Tuple[str, ...]
    evaluation_keys: Tuple[str, ...] = ()
    paragraph_address: Optional[XmlParagraphAddress] = None


@dataclass(frozen=True)
class _PendingPicture:
    target_id: str
    evidence_ref: Optional[str]
    evidence_sha256: Optional[str]
    source_ref: str
    role: str
    consumed_token: str
    neutralize: bool


def build_exact_hwpx_write_plan(
    *,
    source_path: Union[str, os.PathLike],
    binding_manifest: CurrentIncidentReportBindingManifest,
    evidence_by_ref: Mapping[str, ServerOwnedEvidenceFile],
    server_evidence_root: Union[str, os.PathLike],
    transcode_root: Union[str, os.PathLike],
    limits: Optional[ExactHwpxWriterLimits] = None,
) -> ExactHwpxWritePlan:
    """Build one complete exact-writer plan without writing an HWPX.

    ``server_evidence_root`` and ``transcode_root`` must already exist as real
    absolute directories.  The former is the authority boundary for original
    or verified-deidentified evidence.  The latter receives only
    content-addressed deterministic derived image payloads.
    """

    active_limits = limits or ExactHwpxWriterLimits()
    _validate_manifest_header(binding_manifest, active_limits)
    _validate_incident_picture_bindings(binding_manifest)
    source = _absolute_file(source_path, suffix=".hwpx", label="source")
    evidence_root = _trusted_directory(
        server_evidence_root, label="server evidence root"
    )
    staging_root = _trusted_directory(
        transcode_root, label="transcode root"
    )
    trusted_evidence = _resolve_trusted_evidence(
        binding_manifest,
        evidence_by_ref=evidence_by_ref,
        evidence_root=evidence_root,
        limits=active_limits,
    )

    try:
        inspection = inspect_sample_layout(source)
    except HwpxSampleLayoutError as error:
        raise CurrentIncidentHwpxPlanError(
            "audited_sample_required",
            "the exact audited 3Q/4Q HWPX sample is required",
        ) from error
    _validate_source_binding(binding_manifest, inspection)
    stale_factual_audit = _load_stale_factual_audit(
        source,
        inspection=inspection,
    )
    paragraph_targets = {
        pair.photo_page_header.target_id: pair.photo_page_header
        for pair in stale_factual_audit.page_pairs
    }
    signature_targets = {
        pair.inspector_signature.target_id: pair.inspector_signature
        for pair in stale_factual_audit.page_pairs
    }
    _validate_stale_factual_manifest(
        binding_manifest,
        audit=stale_factual_audit,
        paragraph_targets=paragraph_targets,
        signature_targets=signature_targets,
    )

    requested_text_targets = _requested_text_targets(binding_manifest)
    requested_cell_targets = tuple(
        target_id
        for target_id in requested_text_targets
        if target_id not in paragraph_targets
    )
    requested_paragraph_targets = {
        target_id: paragraph_targets[target_id]
        for target_id in requested_text_targets
        if target_id in paragraph_targets
    }
    cells = _resolve_exact_cells(
        source,
        inspection=inspection,
        requested_targets=requested_cell_targets,
        limits=active_limits,
    )
    if (
        set(cells) | set(requested_paragraph_targets)
        != set(requested_text_targets)
    ):
        raise CurrentIncidentHwpxPlanError(
            "exact_text_resolution_incomplete",
            "not every requested text target resolved from the exact sample",
        )
    pictures = {
        picture.target_id: picture
        for picture in inspection.snapshot.pictures
    }
    incident_refs = tuple(
        f"incident:{incident_id}"
        for incident_id in binding_manifest.approved_incident_ids
    )
    text_targets, picture_targets, consumed = _compile_target_mutations(
        binding_manifest,
        cells=cells,
        paragraphs=requested_paragraph_targets,
        pictures=pictures,
        signature_targets=signature_targets,
        incident_refs=incident_refs,
    )
    operation_count = len(text_targets) + len(picture_targets)
    if operation_count != binding_manifest.required_writer_operation_count:
        raise CurrentIncidentHwpxPlanError(
            "binding_operation_count_mismatch",
            "the complete exact operation count does not match the binding manifest",
        )
    if operation_count > active_limits.max_operations:
        raise CurrentIncidentHwpxPlanError(
            "operation_capacity_exceeded",
            "the complete atomic HWPX plan exceeds the exact writer limit",
        )
    expected_tokens = _expected_consumption_tokens(binding_manifest)
    if consumed != expected_tokens:
        raise CurrentIncidentHwpxPlanError(
            "binding_consumption_incomplete",
            "not every binding replacement, neutralization, and evaluation target was consumed",
        )

    picture_sources = _resolve_picture_sources(
        source,
        inspection=inspection,
        requested_targets=tuple(picture_targets),
        limits=active_limits,
    )
    operations: list[ExactHwpxOperation] = []
    for pending in text_targets.values():
        if pending.paragraph_address is None:
            operations.append(
                ExactHwpxTextOperation(
                    operation_id="pending",
                    target_id=pending.target_id,
                    expected_current_value=pending.expected_text,
                    expected_current_sha256=pending.expected_sha256,
                    value=pending.value,
                    source_ref=pending.source_ref,
                )
            )
        else:
            operations.append(
                ExactHwpxParagraphTextOperation(
                    operation_id="pending",
                    target_id=pending.target_id,
                    address=pending.paragraph_address,
                    expected_current_value=pending.expected_text,
                    expected_current_sha256=pending.expected_sha256,
                    value=pending.value,
                    source_ref=pending.source_ref,
                )
            )

    staged_payloads: Dict[
        Tuple[str, str], Tuple[Path, str, int]
    ] = {}
    total_staged_bytes = 0
    for pending in picture_targets.values():
        source_picture = picture_sources[pending.target_id]
        if pending.neutralize:
            payload = _neutral_payload(source_picture.media_type)
            payload_key = ("neutral", source_picture.media_type)
        else:
            if pending.evidence_ref is None:
                raise CurrentIncidentHwpxPlanError(
                    "incident_evidence_missing",
                    "an incident photo mutation has no selected evidence reference",
                )
            selected = trusted_evidence[pending.evidence_ref]
            if selected.sha256 != pending.evidence_sha256:
                raise CurrentIncidentHwpxPlanError(
                    "incident_evidence_hash_mismatch",
                    "selected evidence no longer matches the binding manifest",
                )
            payload = _transcode_payload(
                selected.payload,
                target_media=source_picture.media_type,
                limits=active_limits,
            )
            payload_key = (selected.sha256, source_picture.media_type)
        staged = staged_payloads.get(payload_key)
        if staged is None:
            staged_path, staged_sha256 = _stage_payload(
                staging_root,
                payload,
                media_type=source_picture.media_type,
            )
            staged = (
                staged_path,
                staged_sha256,
                len(payload),
            )
            staged_payloads[payload_key] = staged
            total_staged_bytes += len(payload)
            if total_staged_bytes > active_limits.max_total_evidence_bytes:
                raise CurrentIncidentHwpxPlanError(
                    "evidence_capacity_exceeded",
                    "transcoded report evidence exceeds the exact writer byte limit",
                )
        staged_path, staged_sha256, _staged_size = staged
        operations.append(
            ExactHwpxPictureOperation(
                operation_id="pending",
                target_id=pending.target_id,
                expected_binary_item_ref=(
                    source_picture.binary_item_ref
                ),
                expected_current_sha256=(
                    source_picture.current_sha256
                ),
                evidence_path=str(staged_path),
                evidence_sha256=staged_sha256,
                source_ref=pending.source_ref,
            )
        )

    finalized = tuple(
        replace(
            operation,
            operation_id=(
                f"op-{index:04d}-"
                + hashlib.sha256(
                    (
                        f"{operation.kind}:{operation.target_id}:"
                        f"{operation.source_ref}"
                    ).encode("utf-8")
                ).hexdigest()[:16]
            ),
        )
        for index, operation in enumerate(operations)
    )
    if len(finalized) != operation_count:
        raise CurrentIncidentHwpxPlanError(
            "binding_consumption_incomplete",
            "the final exact operation set changed during compilation",
        )
    plan = ExactHwpxWritePlan(
        plan_id=(
            "current-report-"
            + binding_manifest.binding_manifest_sha256[:32]
        ),
        source_document_sha256=inspection.snapshot.document_sha256,
        source_target_summary_sha256=(
            inspection.snapshot.target_summary_sha256
        ),
        incident_source_refs=incident_refs,
        operations=finalized,
    )
    try:
        _validate_plan(plan, active_limits)
    except HwpxExactWriterError as error:
        raise CurrentIncidentHwpxPlanError(
            error.code,
            "the compiled current-report plan is not accepted by the exact writer",
        ) from error
    return plan


def _validate_manifest_header(
    manifest: CurrentIncidentReportBindingManifest,
    limits: ExactHwpxWriterLimits,
) -> None:
    if not isinstance(manifest, CurrentIncidentReportBindingManifest):
        raise CurrentIncidentHwpxPlanError(
            "binding_manifest_invalid",
            "a server-owned current-Incident binding manifest is required",
        )
    if (
        manifest.schema_version
        != CURRENT_INCIDENT_REPORT_BINDING_SCHEMA
        or manifest.state
        != "current_incidents_bound_writer_required"
        or not manifest.approved_incident_bound
        or not manifest.content_provenance_bound
        or not manifest.writer_required
        or manifest.apply_eligible
        or manifest.provider_called
        or manifest.com_invoked
        or manifest.document_written
        or manifest.weak_reference_approval_required
    ):
        raise CurrentIncidentHwpxPlanError(
            "binding_manifest_state_invalid",
            "the current-Incident binding manifest is not writer-ready",
        )
    hashes = (
        manifest.source_sha256,
        manifest.template_sha256,
        manifest.template_semantic_map_sha256,
        manifest.structure_target_summary_sha256,
        manifest.survey_incident_authority_sha256,
        manifest.approved_incident_authority_set_sha256,
        manifest.input_sha256,
        manifest.content_sha256,
        manifest.slot_manifest_sha256,
        manifest.binding_manifest_sha256,
    )
    if any(
        not isinstance(value, str) or _SHA256.fullmatch(value) is None
        for value in hashes
    ):
        raise CurrentIncidentHwpxPlanError(
            "binding_manifest_hash_invalid",
            "the binding manifest contains an invalid SHA-256",
        )
    content_payload = {
        "incidents": [
            asdict(item) for item in manifest.incident_bindings
        ],
        "replacements": [
            asdict(item) for item in manifest.replacements
        ],
        "neutralizations": [
            asdict(item) for item in manifest.neutralizations
        ],
        "evaluation_rows": [
            asdict(item) for item in manifest.evaluation_rows
        ],
    }
    if _canonical_sha256(content_payload) != manifest.content_sha256:
        raise CurrentIncidentHwpxPlanError(
            "binding_content_integrity_invalid",
            "the binding manifest content hash does not verify",
        )
    incident_ids = manifest.approved_incident_ids
    binding_ids = tuple(
        row.incident_id for row in manifest.incident_bindings
    )
    if (
        not incident_ids
        or len(incident_ids) != len(set(incident_ids))
        or manifest.approved_incident_count != len(incident_ids)
        or binding_ids != incident_ids
        or len(binding_ids) != len(set(binding_ids))
    ):
        raise CurrentIncidentHwpxPlanError(
            "approved_incident_binding_incomplete",
            "the complete ordered approved-Incident set is required",
        )
    authority_digests = [
        row.approved_incident_authority_sha256
        for row in manifest.incident_bindings
    ]
    if (
        any(
            not isinstance(value, str)
            or _SHA256.fullmatch(value) is None
            for value in authority_digests
        )
        or _canonical_sha256(authority_digests)
        != manifest.approved_incident_authority_set_sha256
    ):
        raise CurrentIncidentHwpxPlanError(
            "approved_incident_authority_invalid",
            "the approved-Incident authority set does not verify",
        )
    if (
        isinstance(manifest.required_writer_operation_count, bool)
        or not isinstance(
            manifest.required_writer_operation_count, int
        )
        or manifest.required_writer_operation_count <= 0
    ):
        raise CurrentIncidentHwpxPlanError(
            "binding_operation_count_invalid",
            "the binding manifest operation count is invalid",
        )
    if manifest.required_writer_operation_count > limits.max_operations:
        raise CurrentIncidentHwpxPlanError(
            "operation_capacity_exceeded",
            "the complete atomic HWPX plan exceeds the exact writer limit",
        )
    payload = {
        "schema_version": manifest.schema_version,
        "project_id": manifest.project_id,
        "pipeline_job_id": manifest.pipeline_job_id,
        "analysis_job_id": manifest.analysis_job_id,
        "source_sha256": manifest.source_sha256,
        "template_id": manifest.template_id,
        "document_snapshot_id": manifest.document_snapshot_id,
        "template_sha256": manifest.template_sha256,
        "template_semantic_map_sha256": (
            manifest.template_semantic_map_sha256
        ),
        "structure_target_summary_sha256": (
            manifest.structure_target_summary_sha256
        ),
        "incident_capacity": manifest.incident_capacity,
        "approved_incident_ids": manifest.approved_incident_ids,
        "occupied_page_pair_indices": (
            manifest.occupied_page_pair_indices
        ),
        "survey_incident_authority_sha256": (
            manifest.survey_incident_authority_sha256
        ),
        "approved_incident_authority_set_sha256": (
            manifest.approved_incident_authority_set_sha256
        ),
        "input_sha256": manifest.input_sha256,
        "content_sha256": manifest.content_sha256,
        "slot_manifest_sha256": manifest.slot_manifest_sha256,
        "required_writer_operation_count": (
            manifest.required_writer_operation_count
        ),
        "legacy_document_edit_plan_operation_limit": (
            manifest.legacy_document_edit_plan_operation_limit
        ),
        "exact_hwpx_writer_operation_limit": (
            manifest.exact_hwpx_writer_operation_limit
        ),
        "legacy_plan_expansion_required": (
            manifest.legacy_plan_expansion_required
        ),
        "writer_batching_required": manifest.writer_batching_required,
        "approved_incident_bound": True,
        "content_provenance_bound": True,
        "writer_required": True,
    }
    if _canonical_sha256(payload) != manifest.binding_manifest_sha256:
        raise CurrentIncidentHwpxPlanError(
            "binding_manifest_integrity_invalid",
            "the binding manifest identity hash does not verify",
        )


def _validate_incident_picture_bindings(
    manifest: CurrentIncidentReportBindingManifest,
) -> None:
    expected: Dict[str, Tuple[str, str, str]] = {}
    for row in manifest.incident_bindings:
        if row.primary_picture_target_id in expected:
            raise CurrentIncidentHwpxPlanError(
                "approved_incident_binding_incomplete",
                "one primary picture target is reused across Incidents",
            )
        expected[row.primary_picture_target_id] = (
            row.incident_id,
            row.selected_evidence_ref,
            row.selected_evidence_sha256,
        )
    actual: Dict[str, Tuple[str, str]] = {}
    for mutation in manifest.replacements:
        if mutation.operation != "replace_picture":
            continue
        evidence_ref = mutation.current_value.get("evidence_ref")
        evidence_sha = mutation.current_value.get("evidence_sha256")
        actual[mutation.target_id] = (evidence_ref, evidence_sha)
    if set(actual) != set(expected):
        raise CurrentIncidentHwpxPlanError(
            "incident_picture_binding_incomplete",
            "every current Incident must have exactly one primary picture replacement",
        )
    for target_id, (_incident_id, evidence_ref, evidence_sha) in (
        expected.items()
    ):
        if actual[target_id] != (evidence_ref, evidence_sha):
            raise CurrentIncidentHwpxPlanError(
                "incident_picture_binding_conflict",
                "an incident picture replacement crossed its selected evidence",
            )


def _validate_source_binding(
    manifest: CurrentIncidentReportBindingManifest,
    inspection: SampleLayoutInspection,
) -> None:
    snapshot = inspection.snapshot
    if (
        manifest.template_id != inspection.baseline.template_id
        or manifest.document_snapshot_id
        != inspection.document_snapshot_id
        or manifest.template_sha256 != snapshot.document_sha256
        or manifest.structure_target_summary_sha256
        != snapshot.target_summary_sha256
    ):
        raise CurrentIncidentHwpxPlanError(
            "sample_template_provenance_conflict",
            "the binding manifest does not match the exact audited sample",
        )


def _load_stale_factual_audit(
    source: Path,
    *,
    inspection: SampleLayoutInspection,
) -> HwpxStaleFactualTargetAudit:
    try:
        audit = audit_stale_factual_targets(source)
    except Exception as error:
        raise CurrentIncidentHwpxPlanError(
            "stale_factual_audit_unavailable",
            "the exact stale-factual target audit could not be reopened",
        ) from error
    expected_pairs = inspection.baseline.page_pair_count
    if (
        audit.template_id != inspection.baseline.template_id
        or audit.document_sha256 != inspection.snapshot.document_sha256
        or audit.document_snapshot_id != inspection.document_snapshot_id
        or audit.structure_target_summary_sha256
        != inspection.snapshot.target_summary_sha256
        or audit.counts.page_pair_count != expected_pairs
        or audit.counts.required_target_count != expected_pairs * 5
        or len(audit.required_target_ids) != expected_pairs * 5
        or not audit.complete_page_pair_coverage
    ):
        raise CurrentIncidentHwpxPlanError(
            "stale_factual_audit_mismatch",
            "the stale-factual audit does not match the exact source",
        )
    return audit


def _validate_stale_factual_manifest(
    manifest: CurrentIncidentReportBindingManifest,
    *,
    audit: HwpxStaleFactualTargetAudit,
    paragraph_targets: Mapping[str, FactualParagraphTarget],
    signature_targets: Mapping[str, FactualPictureTarget],
) -> None:
    required = set(audit.required_target_ids)
    mutations = (
        *manifest.replacements,
        *manifest.neutralizations,
    )
    static_mutations = tuple(
        mutation
        for mutation in mutations
        if mutation.target_id in required
    )
    try:
        coverage = require_stale_factual_target_coverage(
            audit,
            (mutation.target_id for mutation in static_mutations),
        )
    except HwpxStaleFactualTargetError as error:
        raise CurrentIncidentHwpxPlanError(
            error.code,
            "the binding does not cover every exact stale-factual target",
        ) from error
    if (
        not coverage.complete
        or coverage.covered_target_count
        != audit.counts.required_target_count
    ):
        raise CurrentIncidentHwpxPlanError(
            "stale_factual_target_coverage_incomplete",
            "historical factual content can persist in the output",
        )
    mutation_by_target = {
        mutation.target_id: mutation for mutation in static_mutations
    }
    expected_text_roles: Dict[str, Tuple[str, str, str]] = {}
    for pair in audit.page_pairs:
        for role, target in (
            ("current_title_group", pair.title_group),
            ("current_title_route_heading", pair.title_route_heading),
            (
                "current_inspection_inspector",
                pair.inspection_inspector,
            ),
            ("current_photo_page_header", pair.photo_page_header),
        ):
            expected_text_roles[target.target_id] = (
                role,
                target.expected_text,
                target.expected_text_sha256,
            )
    if set(paragraph_targets) != {
        pair.photo_page_header.target_id for pair in audit.page_pairs
    } or set(signature_targets) != {
        pair.inspector_signature.target_id for pair in audit.page_pairs
    }:
        raise CurrentIncidentHwpxPlanError(
            "stale_factual_target_map_invalid",
            "paragraph or signature target map is incomplete",
        )
    for target_id, (
        expected_role,
        expected_text,
        expected_sha,
    ) in expected_text_roles.items():
        mutation = mutation_by_target[target_id]
        if (
            mutation.role != expected_role
            or mutation.operation != "replace_text"
            or mutation.expected_value.get("text") != expected_text
            or mutation.expected_value.get("text_sha256")
            != expected_sha
        ):
            raise CurrentIncidentHwpxPlanError(
                "stale_factual_text_guard_invalid",
                "a stale-factual text target lacks its exact source guard",
            )
        _current_text(mutation.current_value)
    for target_id, target in signature_targets.items():
        mutation = mutation_by_target[target_id]
        raw_binary_refs = mutation.expected_value.get(
            "binary_item_refs"
        )
        raw_binary_shas = mutation.expected_value.get(
            "binary_sha256s"
        )
        if (
            isinstance(raw_binary_refs, (str, bytes, bytearray))
            or not isinstance(raw_binary_refs, Sequence)
            or isinstance(raw_binary_shas, (str, bytes, bytearray))
            or not isinstance(raw_binary_shas, Sequence)
            or mutation.role
            != "neutralized_stale_inspector_signature"
            or mutation.operation != "remove_picture"
            or mutation.expected_value.get("picture_target_id")
            != target_id
            or mutation.expected_value.get("template_sha256")
            != audit.document_sha256
            or mutation.expected_value.get("expected_binding_sha256")
            != target.expected_binding_sha256
            or tuple(raw_binary_refs) != target.binary_item_refs
            or tuple(raw_binary_shas)
            != tuple(binary.sha256 for binary in target.binaries)
            or mutation.current_value.get("neutralized") is not True
        ):
            raise CurrentIncidentHwpxPlanError(
                "stale_signature_guard_invalid",
                "an old inspector signature is not explicitly neutralized",
            )


def _resolve_trusted_evidence(
    manifest: CurrentIncidentReportBindingManifest,
    *,
    evidence_by_ref: Mapping[str, ServerOwnedEvidenceFile],
    evidence_root: Path,
    limits: ExactHwpxWriterLimits,
) -> Dict[str, _TrustedEvidence]:
    selected: Dict[str, str] = {}
    for row in manifest.incident_bindings:
        existing = selected.setdefault(
            row.selected_evidence_ref,
            row.selected_evidence_sha256,
        )
        if existing != row.selected_evidence_sha256:
            raise CurrentIncidentHwpxPlanError(
                "incident_evidence_provenance_conflict",
                "one evidence reference resolves to conflicting selected hashes",
            )
    if (
        not isinstance(evidence_by_ref, Mapping)
        or set(evidence_by_ref) != set(selected)
    ):
        raise CurrentIncidentHwpxPlanError(
            "incident_evidence_mapping_incomplete",
            "the server evidence mapping must exactly match selected references",
        )
    resolved: Dict[str, _TrustedEvidence] = {}
    total_bytes = 0
    for evidence_ref in sorted(selected):
        descriptor = evidence_by_ref[evidence_ref]
        if (
            not isinstance(descriptor, ServerOwnedEvidenceFile)
            or descriptor.evidence_ref != evidence_ref
            or descriptor.sha256 != selected[evidence_ref]
            or _SHA256.fullmatch(descriptor.sha256) is None
        ):
            raise CurrentIncidentHwpxPlanError(
                "incident_evidence_mapping_invalid",
                "a server evidence descriptor does not match the selected reference",
            )
        path = _absolute_file(
            descriptor.absolute_path,
            suffix=None,
            label="evidence",
        )
        if not _is_within(path, evidence_root):
            raise CurrentIncidentHwpxPlanError(
                "incident_evidence_outside_server_root",
                "selected evidence is outside the server-owned evidence root",
            )
        payload = _read_stable_file(
            path,
            maximum=limits.max_evidence_bytes,
            label="evidence",
        )
        digest = hashlib.sha256(payload).hexdigest()
        if digest != descriptor.sha256:
            raise CurrentIncidentHwpxPlanError(
                "incident_evidence_hash_mismatch",
                "selected evidence SHA-256 does not match its server catalog",
            )
        try:
            _inspect_image_header(
                payload, limits=_image_limits(limits)
            )
        except Exception as error:
            raise CurrentIncidentHwpxPlanError(
                "incident_evidence_image_invalid",
                "selected evidence is not a supported bounded image",
            ) from error
        total_bytes += len(payload)
        if total_bytes > limits.max_total_evidence_bytes:
            raise CurrentIncidentHwpxPlanError(
                "evidence_capacity_exceeded",
                "selected evidence exceeds the exact writer byte limit",
            )
        resolved[evidence_ref] = _TrustedEvidence(
            evidence_ref=evidence_ref,
            path=path,
            sha256=digest,
            payload=payload,
        )
    return resolved


def _compile_target_mutations(
    manifest: CurrentIncidentReportBindingManifest,
    *,
    cells: Mapping[str, CellSnapshot],
    paragraphs: Optional[
        Mapping[str, FactualParagraphTarget]
    ] = None,
    pictures: Mapping[str, PictureSnapshot],
    signature_targets: Optional[
        Mapping[str, FactualPictureTarget]
    ] = None,
    incident_refs: Tuple[str, ...],
) -> Tuple[
    Dict[str, _PendingText],
    Dict[str, _PendingPicture],
    set[str],
]:
    text_targets: Dict[str, _PendingText] = {}
    picture_targets: Dict[str, _PendingPicture] = {}
    consumed: set[str] = set()
    paragraph_map = paragraphs or {}
    signature_map = signature_targets or {}

    for group, mutations in (
        ("replacement", manifest.replacements),
        ("neutralization", manifest.neutralizations),
    ):
        for index, mutation in enumerate(mutations):
            token = f"{group}:{index}"
            if token in consumed:
                raise CurrentIncidentHwpxPlanError(
                    "binding_consumption_duplicate",
                    "one binding mutation was consumed more than once",
                )
            _add_report_mutation(
                mutation,
                token=token,
                cells=cells,
                paragraphs=paragraph_map,
                pictures=pictures,
                signature_targets=signature_map,
                incident_refs=incident_refs,
                text_targets=text_targets,
                picture_targets=picture_targets,
            )
            consumed.add(token)

    seen_evaluation_rows: set[Tuple[int, int, int]] = set()
    evaluation_target_owner: Dict[str, str] = {}
    for row_index, row in enumerate(manifest.evaluation_rows):
        row_key = (
            row.page_pair_index,
            row.row_order,
            row.row,
        )
        if row_key in seen_evaluation_rows:
            raise CurrentIncidentHwpxPlanError(
                "evaluation_row_duplicate",
                "an evaluation row appears more than once",
            )
        seen_evaluation_rows.add(row_key)
        _add_evaluation_row(
            row,
            row_index=row_index,
            cells=cells,
            incident_refs=incident_refs,
            text_targets=text_targets,
            target_owner=evaluation_target_owner,
            consumed=consumed,
        )
    return text_targets, picture_targets, consumed


def _requested_text_targets(
    manifest: CurrentIncidentReportBindingManifest,
) -> Tuple[str, ...]:
    targets = {
        mutation.target_id
        for mutation in (
            *manifest.replacements,
            *manifest.neutralizations,
        )
        if mutation.operation in {"replace_text", "clear_text"}
    }
    targets.update(
        target["target_id"]
        for row in manifest.evaluation_rows
        for target in row.grade_targets
        if isinstance(target, Mapping)
        and isinstance(target.get("target_id"), str)
    )
    return tuple(sorted(targets))


def _resolve_exact_cells(
    source: Path,
    *,
    inspection: SampleLayoutInspection,
    requested_targets: Sequence[str],
    limits: ExactHwpxWriterLimits,
) -> Dict[str, CellSnapshot]:
    snapshot_cells = {
        cell.target_id: cell
        for table in inspection.snapshot.tables
        for cell in table.cells
    }
    unknown = set(requested_targets).difference(snapshot_cells)
    if unknown:
        raise CurrentIncidentHwpxPlanError(
            "unknown_cell_target",
            "a requested text target is absent from the exact sample",
        )
    grouped: Dict[str, list[CellSnapshot]] = {}
    for target_id in requested_targets:
        cell = snapshot_cells[target_id]
        grouped.setdefault(cell.section_path, []).append(cell)
    resolved: Dict[str, CellSnapshot] = {}
    try:
        with zipfile.ZipFile(source, mode="r") as archive:
            normalized = structure._validate_archive_entries(
                archive.infolist(), limits.structure_limits
            )
            for section_path, target_cells in grouped.items():
                info = normalized.get(_archive_key(section_path))
                if info is None:
                    raise CurrentIncidentHwpxPlanError(
                        "section_missing",
                        "a target HWPX section is missing",
                    )
                raw = structure._read_member_bounded(
                    archive,
                    info,
                    maximum=(
                        limits.structure_limits.max_xml_entry_bytes
                    ),
                )
                root, _count = structure._parse_xml_bounded(
                    raw,
                    entry_name=section_path,
                    limits=limits.structure_limits,
                )
                tables = tuple(
                    element
                    for element in root.iter()
                    if structure._local_name(element.tag) == "tbl"
                )
                for cell in target_cells:
                    try:
                        table = tables[cell.table_index]
                        direct_cells = _direct_table_cells(table)
                        element = direct_cells[cell.cell_index]
                    except IndexError as error:
                        raise CurrentIncidentHwpxPlanError(
                            "exact_cell_address_stale",
                            "an exact table/cell address no longer resolves",
                        ) from error
                    full_text = _normalized_cell_text(element)
                    digest = hashlib.sha256(
                        full_text.encode("utf-8")
                    ).hexdigest()
                    if digest != cell.text_sha256:
                        raise CurrentIncidentHwpxPlanError(
                            "exact_cell_hash_mismatch",
                            "full exact cell text does not match its structure hash",
                        )
                    resolved[cell.target_id] = replace(
                        cell,
                        text_excerpt=full_text,
                        text_truncated=False,
                    )
    except CurrentIncidentHwpxPlanError:
        raise
    except Exception as error:
        raise CurrentIncidentHwpxPlanError(
            "exact_cell_resolution_failed",
            "full exact cell text could not be resolved safely",
        ) from error
    return resolved


def _add_report_mutation(
    mutation: ReportTargetMutation,
    *,
    token: str,
    cells: Mapping[str, CellSnapshot],
    paragraphs: Mapping[str, FactualParagraphTarget],
    pictures: Mapping[str, PictureSnapshot],
    signature_targets: Mapping[str, FactualPictureTarget],
    incident_refs: Tuple[str, ...],
    text_targets: Dict[str, _PendingText],
    picture_targets: Dict[str, _PendingPicture],
) -> None:
    if not isinstance(mutation, ReportTargetMutation):
        raise CurrentIncidentHwpxPlanError(
            "binding_mutation_invalid",
            "the binding manifest contains an invalid mutation",
        )
    source_ref = _operation_source_ref(
        mutation.current_source_refs, incident_refs
    )
    if mutation.operation in {"replace_text", "clear_text"}:
        cell = cells.get(mutation.target_id)
        paragraph = paragraphs.get(mutation.target_id)
        if (cell is None) == (paragraph is None):
            raise CurrentIncidentHwpxPlanError(
                "unknown_or_ambiguous_text_target",
                (
                    "a text mutation does not resolve to exactly one "
                    "complete cell or audited paragraph"
                ),
            )
        expected_text, expected_hash = _expected_text(
            mutation.expected_value
        )
        if cell is not None:
            if cell.text_truncated or (
                expected_hash != cell.text_sha256
                or not _expected_excerpt_matches(
                    expected_text, cell.text_excerpt
                )
            ):
                raise CurrentIncidentHwpxPlanError(
                    "stale_expected_text",
                    "a binding text guard does not match the exact sample cell",
                )
            exact_text = cell.text_excerpt
            paragraph_address = None
        else:
            assert paragraph is not None
            if (
                expected_text != paragraph.expected_text
                or expected_hash != paragraph.expected_text_sha256
                or paragraph.address.selected_text
                != paragraph.expected_text
                or paragraph.address.selected_text_sha256
                != paragraph.expected_text_sha256
            ):
                raise CurrentIncidentHwpxPlanError(
                    "stale_expected_paragraph",
                    (
                        "a binding text guard does not match the exact "
                        "audited paragraph"
                    ),
                )
            exact_text = paragraph.expected_text
            paragraph_address = paragraph.address
        if mutation.operation == "replace_text":
            value = _current_text(mutation.current_value)
        else:
            if (
                mutation.current_value.get("text") != ""
                or mutation.current_value.get("neutralized") is not True
            ):
                raise CurrentIncidentHwpxPlanError(
                    "clear_text_contract_invalid",
                    "a clear-text mutation is not an explicit neutralization",
                )
            value = ""
        pending = _PendingText(
            target_id=mutation.target_id,
            expected_text=exact_text,
            expected_sha256=expected_hash,
            value=value,
            source_ref=source_ref,
            roles=(mutation.role,),
            consumed_tokens=(token,),
            paragraph_address=paragraph_address,
        )
        existing = text_targets.get(mutation.target_id)
        if existing is None:
            text_targets[mutation.target_id] = pending
            return
        if _can_consolidate_identical_text(existing, pending):
            text_targets[mutation.target_id] = replace(
                existing,
                roles=tuple(
                    dict.fromkeys((*existing.roles, mutation.role))
                ),
                consumed_tokens=(*existing.consumed_tokens, token),
            )
            return
        raise CurrentIncidentHwpxPlanError(
            "conflicting_target_mutation",
            "one exact text target has conflicting current-report mutations",
        )

    if mutation.operation in {"replace_picture", "remove_picture"}:
        if mutation.target_id not in pictures:
            raise CurrentIncidentHwpxPlanError(
                "unknown_picture_target",
                "a picture mutation does not resolve to the exact sample",
            )
        if (
            mutation.expected_value.get("picture_target_id")
            != mutation.target_id
            or mutation.expected_value.get("template_sha256")
            != pictures[mutation.target_id].document_sha256
        ):
            raise CurrentIncidentHwpxPlanError(
                "stale_expected_picture",
                "a binding picture guard does not match the exact sample",
            )
        if mutation.target_id in picture_targets:
            raise CurrentIncidentHwpxPlanError(
                "conflicting_target_mutation",
                "one exact picture target appears more than once",
            )
        if mutation.operation == "replace_picture":
            evidence_ref = mutation.current_value.get("evidence_ref")
            evidence_sha = mutation.current_value.get(
                "evidence_sha256"
            )
            if (
                not isinstance(evidence_ref, str)
                or not evidence_ref
                or _CONTROL.search(evidence_ref)
                or not isinstance(evidence_sha, str)
                or _SHA256.fullmatch(evidence_sha) is None
            ):
                raise CurrentIncidentHwpxPlanError(
                    "incident_evidence_binding_invalid",
                    "an incident photo has an invalid evidence binding",
                )
            neutralize = False
        else:
            if mutation.current_value.get("neutralized") is not True:
                raise CurrentIncidentHwpxPlanError(
                    "remove_picture_contract_invalid",
                    "a removed picture is not an explicit neutralization",
                )
            evidence_ref = None
            evidence_sha = None
            neutralize = True
            signature = signature_targets.get(mutation.target_id)
            if signature is not None and (
                mutation.role
                != "neutralized_stale_inspector_signature"
                or mutation.expected_value.get(
                    "expected_binding_sha256"
                )
                != signature.expected_binding_sha256
            ):
                raise CurrentIncidentHwpxPlanError(
                    "stale_signature_guard_invalid",
                    "an old inspector signature lacks its exact source guard",
                )
        picture_targets[mutation.target_id] = _PendingPicture(
            target_id=mutation.target_id,
            evidence_ref=evidence_ref,
            evidence_sha256=evidence_sha,
            source_ref=source_ref,
            role=mutation.role,
            consumed_token=token,
            neutralize=neutralize,
        )
        return
    raise CurrentIncidentHwpxPlanError(
        "binding_operation_unsupported",
        "the binding manifest contains an unsupported operation",
    )


def _add_evaluation_row(
    row: EvaluationRowBinding,
    *,
    row_index: int,
    cells: Mapping[str, CellSnapshot],
    incident_refs: Tuple[str, ...],
    text_targets: Dict[str, _PendingText],
    target_owner: Dict[str, str],
    consumed: set[str],
) -> None:
    if not isinstance(row, EvaluationRowBinding):
        raise CurrentIncidentHwpxPlanError(
            "evaluation_row_invalid",
            "the binding manifest contains an invalid evaluation row",
        )
    source_ref = _operation_source_ref(
        row.current_source_refs, incident_refs
    )
    unique: Dict[str, Mapping[str, object]] = {}
    for grade_index, raw_target in enumerate(row.grade_targets):
        if not isinstance(raw_target, Mapping):
            raise CurrentIncidentHwpxPlanError(
                "evaluation_target_invalid",
                "an evaluation grade target is invalid",
            )
        target_id = raw_target.get("target_id")
        grade = raw_target.get("grade")
        if not isinstance(target_id, str) or not isinstance(grade, str):
            raise CurrentIncidentHwpxPlanError(
                "evaluation_target_invalid",
                "an evaluation grade target identity is invalid",
            )
        token = f"evaluation:{row_index}:{grade_index}"
        consumed.add(token)
        prior = unique.get(target_id)
        if prior is not None:
            if (
                prior.get("expected_value")
                != raw_target.get("expected_value")
                or prior.get("target_id") != target_id
            ):
                raise CurrentIncidentHwpxPlanError(
                    "evaluation_target_conflict",
                    "a merged evaluation target has conflicting guards",
                )
            continue
        unique[target_id] = raw_target

    if row.selected_grade == "clear_unassessed":
        if (
            row.selected_target_id is not None
            or row.selected_anchor_column is not None
        ):
            raise CurrentIncidentHwpxPlanError(
                "evaluation_selection_invalid",
                "an unassessed row cannot select a grade target",
            )
        selected_target = None
    else:
        if not row.five_grade_grid_available:
            raise CurrentIncidentHwpxPlanError(
                "evaluation_grid_unavailable",
                "a merged evaluation row cannot select a five-grade target",
            )
        matches = tuple(
            item
            for item in row.grade_targets
            if (
                item.get("grade") == row.selected_grade
                and item.get("target_id") == row.selected_target_id
                and item.get("anchor_column")
                == row.selected_anchor_column
            )
        )
        if len(matches) != 1:
            raise CurrentIncidentHwpxPlanError(
                "evaluation_selection_invalid",
                "the selected current grade does not match one exact target",
            )
        selected_target = row.selected_target_id

    evaluation_key = (
        f"{row.page_pair_index}:{row.row_order}:{row.row}"
    )
    for target_id, raw_target in unique.items():
        owner = target_owner.setdefault(target_id, evaluation_key)
        if owner != evaluation_key:
            raise CurrentIncidentHwpxPlanError(
                "evaluation_target_reused",
                "one exact grade target is reused across evaluation rows",
            )
        cell = cells.get(target_id)
        if cell is None or cell.text_truncated:
            raise CurrentIncidentHwpxPlanError(
                "evaluation_target_unknown",
                "an evaluation target does not resolve to one exact cell",
            )
        expected_text, expected_hash = _expected_text(
            raw_target.get("expected_value")
        )
        if (
            expected_hash != cell.text_sha256
            or not _expected_excerpt_matches(
                expected_text, cell.text_excerpt
            )
            or expected_text not in {"", "■"}
        ):
            raise CurrentIncidentHwpxPlanError(
                "evaluation_target_stale",
                "an evaluation target guard does not match the exact sample",
            )
        value = "■" if target_id == selected_target else ""
        pending = _PendingText(
            target_id=target_id,
            expected_text=cell.text_excerpt,
            expected_sha256=expected_hash,
            value=value,
            source_ref=source_ref,
            roles=("current_evaluation_grade",),
            consumed_tokens=(),
            evaluation_keys=(evaluation_key,),
        )
        existing = text_targets.get(target_id)
        if existing is None:
            text_targets[target_id] = pending
            continue
        if not _is_stale_marker_clear(existing):
            raise CurrentIncidentHwpxPlanError(
                "conflicting_target_mutation",
                "an evaluation target conflicts with another report mutation",
            )
        if (
            existing.expected_text != pending.expected_text
            or existing.expected_sha256 != pending.expected_sha256
        ):
            raise CurrentIncidentHwpxPlanError(
                "evaluation_target_conflict",
                "a historical marker clear and current rating have different guards",
            )
        text_targets[target_id] = replace(
            existing,
            value=value,
            source_ref=source_ref,
            roles=(*existing.roles, "current_evaluation_grade"),
            evaluation_keys=(*existing.evaluation_keys, evaluation_key),
        )


def _resolve_picture_sources(
    source: Path,
    *,
    inspection: SampleLayoutInspection,
    requested_targets: Sequence[str],
    limits: ExactHwpxWriterLimits,
) -> Dict[str, _PictureSource]:
    snapshot = inspection.snapshot
    pictures = {
        picture.target_id: picture for picture in snapshot.pictures
    }
    requested_target_set = set(requested_targets)
    if len(requested_target_set) != len(tuple(requested_targets)):
        raise CurrentIncidentHwpxPlanError(
            "duplicate_picture_target",
            "a picture target is requested more than once",
        )
    identity_before = structure._file_identity(
        os.lstat(str(source))
    )
    resolved: Dict[str, _PictureSource] = {}
    try:
        with zipfile.ZipFile(source, mode="r") as archive:
            infos = archive.infolist()
            normalized = structure._validate_archive_entries(
                infos, limits.structure_limits
            )
            manifest_info = normalized.get("contents/content.hpf")
            if manifest_info is None:
                raise CurrentIncidentHwpxPlanError(
                    "picture_manifest_missing",
                    "Contents/content.hpf is required for exact picture binding",
                )
            manifest_raw = structure._read_member_bounded(
                archive,
                manifest_info,
                maximum=limits.max_manifest_bytes,
            )
            package_manifest = _parse_manifest(
                manifest_raw, limits=limits
            )
            member_targets: Dict[str, set[str]] = {}
            for picture in snapshot.pictures:
                for ref in picture.binary_item_refs:
                    item = package_manifest.get(ref)
                    if item is not None:
                        member_targets.setdefault(
                            _archive_key(item.package_path), set()
                        ).add(picture.target_id)
            for target_id in requested_targets:
                picture = pictures[target_id]
                if len(picture.binary_item_refs) != 1:
                    raise CurrentIncidentHwpxPlanError(
                        "picture_reference_ambiguous",
                        "a picture target must resolve to exactly one binary item",
                    )
                binary_ref = picture.binary_item_refs[0]
                item = package_manifest.get(binary_ref)
                if item is None:
                    raise CurrentIncidentHwpxPlanError(
                        "picture_manifest_item_missing",
                        "a picture binary item is absent from content.hpf",
                    )
                member_key = _archive_key(item.package_path)
                affected = member_targets.get(member_key, set())
                if not affected or not affected.issubset(
                    requested_target_set
                ):
                    raise CurrentIncidentHwpxPlanError(
                        "shared_picture_cross_target_change",
                        (
                            "a shared picture binary member is not covered by "
                            "the complete atomic target set"
                        ),
                    )
                info = normalized.get(member_key)
                if info is None or info.is_dir():
                    raise CurrentIncidentHwpxPlanError(
                        "picture_package_member_missing",
                        "a picture package member is missing",
                    )
                payload = structure._read_member_bounded(
                    archive,
                    info,
                    maximum=limits.max_evidence_bytes,
                )
                try:
                    media_type, _width, _height = (
                        _inspect_image_header(
                            payload, limits=_image_limits(limits)
                        )
                    )
                except Exception as error:
                    raise CurrentIncidentHwpxPlanError(
                        "source_picture_media_invalid",
                        "a source picture has invalid image magic or dimensions",
                    ) from error
                declared = _MEDIA_ALIASES.get(
                    item.declared_media_type
                )
                if (
                    declared != media_type
                    or media_type not in _SUPPORTED_TARGET_MEDIA
                ):
                    raise CurrentIncidentHwpxPlanError(
                        "source_picture_media_unsupported",
                        "a target picture must be BMP, PNG, or JPEG with matching manifest media",
                    )
                resolved[target_id] = _PictureSource(
                    target_id=target_id,
                    binary_item_ref=binary_ref,
                    package_member=info.filename,
                    current_sha256=hashlib.sha256(payload).hexdigest(),
                    media_type=media_type,
                )
    except CurrentIncidentHwpxPlanError:
        raise
    except Exception as error:
        raise CurrentIncidentHwpxPlanError(
            "source_picture_resolution_failed",
            "exact source picture metadata could not be resolved safely",
        ) from error
    identity_after = structure._file_identity(os.lstat(str(source)))
    if identity_after != identity_before:
        raise CurrentIncidentHwpxPlanError(
            "source_changed",
            "the exact HWPX source changed while compiling the plan",
        )
    digest = _sha256_file(source)
    if digest != snapshot.document_sha256:
        raise CurrentIncidentHwpxPlanError(
            "source_changed",
            "the exact HWPX source hash changed while compiling the plan",
        )
    return resolved


def _transcode_payload(
    payload: bytes,
    *,
    target_media: str,
    limits: ExactHwpxWriterLimits,
) -> bytes:
    format_name, _extension = _target_media(target_media)
    try:
        from PIL import Image, ImageOps
    except ImportError as error:
        raise CurrentIncidentHwpxPlanError(
            "image_runtime_unavailable",
            "Pillow is required for deterministic report evidence conversion",
        ) from error
    try:
        with Image.open(io.BytesIO(payload)) as opened:
            opened.load()
            image = ImageOps.exif_transpose(opened)
            image.load()
            if image.width <= 0 or image.height <= 0:
                raise ValueError("empty image")
            if (
                image.width > _MAX_TRANSCODE_DIMENSION
                or image.height > _MAX_TRANSCODE_DIMENSION
            ):
                image.thumbnail(
                    (
                        _MAX_TRANSCODE_DIMENSION,
                        _MAX_TRANSCODE_DIMENSION,
                    ),
                    Image.Resampling.LANCZOS,
                    reducing_gap=3.0,
                )
            converted = _flatten_rgb(image)
            output = io.BytesIO()
            _save_deterministic(
                converted, output, format_name=format_name
            )
            result = output.getvalue()
    except CurrentIncidentHwpxPlanError:
        raise
    except Exception as error:
        raise CurrentIncidentHwpxPlanError(
            "evidence_transcode_failed",
            "selected evidence could not be deterministically transcoded",
        ) from error
    if not result or len(result) > limits.max_evidence_bytes:
        raise CurrentIncidentHwpxPlanError(
            "evidence_capacity_exceeded",
            "transcoded evidence exceeds the exact writer byte limit",
        )
    try:
        media, _width, _height = _inspect_image_header(
            result, limits=_image_limits(limits)
        )
    except Exception as error:
        raise CurrentIncidentHwpxPlanError(
            "evidence_transcode_failed",
            "transcoded evidence did not pass image validation",
        ) from error
    if media != target_media:
        raise CurrentIncidentHwpxPlanError(
            "evidence_media_mismatch",
            "transcoded evidence does not match the target picture media",
        )
    return result


def _neutral_payload(media_type: str) -> bytes:
    format_name, _extension = _target_media(media_type)
    try:
        from PIL import Image
    except ImportError as error:
        raise CurrentIncidentHwpxPlanError(
            "image_runtime_unavailable",
            "Pillow is required for deterministic picture neutralization",
        ) from error
    output = io.BytesIO()
    image = Image.new("RGB", (1, 1), (255, 255, 255))
    _save_deterministic(image, output, format_name=format_name)
    return output.getvalue()


def _flatten_rgb(image):
    from PIL import Image

    if image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _save_deterministic(image, output: io.BytesIO, *, format_name: str) -> None:
    if format_name == "BMP":
        image.save(output, format="BMP")
    elif format_name == "PNG":
        image.save(
            output,
            format="PNG",
            compress_level=9,
            optimize=False,
        )
    elif format_name == "JPEG":
        image.save(
            output,
            format="JPEG",
            quality=95,
            subsampling=0,
            optimize=False,
            progressive=False,
        )
    else:  # pragma: no cover - guarded by _target_media
        raise CurrentIncidentHwpxPlanError(
            "source_picture_media_unsupported",
            "the target picture media is unsupported",
        )


def _stage_payload(
    root: Path,
    payload: bytes,
    *,
    media_type: str,
) -> Tuple[Path, str]:
    _format_name, extension = _target_media(media_type)
    digest = hashlib.sha256(payload).hexdigest()
    path = root / f"trafficvisson-hwpx-{digest}{extension}"
    if path.exists():
        existing = _read_stable_file(
            path,
            maximum=max(len(payload), 1),
            label="staged evidence",
        )
        if existing != payload:
            raise CurrentIncidentHwpxPlanError(
                "staged_evidence_conflict",
                "a content-addressed staged evidence file has conflicting bytes",
            )
        return path, digest
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        existing = _read_stable_file(
            path,
            maximum=max(len(payload), 1),
            label="staged evidence",
        )
        if existing != payload:
            raise CurrentIncidentHwpxPlanError(
                "staged_evidence_conflict",
                "a content-addressed staged evidence file has conflicting bytes",
            )
    except OSError as error:
        raise CurrentIncidentHwpxPlanError(
            "staged_evidence_write_failed",
            "deterministic evidence staging failed",
        ) from error
    return path, digest


def _expected_consumption_tokens(
    manifest: CurrentIncidentReportBindingManifest,
) -> set[str]:
    tokens = {
        f"replacement:{index}"
        for index in range(len(manifest.replacements))
    }
    tokens.update(
        f"neutralization:{index}"
        for index in range(len(manifest.neutralizations))
    )
    tokens.update(
        f"evaluation:{row_index}:{grade_index}"
        for row_index, row in enumerate(manifest.evaluation_rows)
        for grade_index in range(len(row.grade_targets))
    )
    return tokens


def _operation_source_ref(
    raw_refs: Sequence[str],
    incident_refs: Tuple[str, ...],
) -> str:
    if (
        isinstance(raw_refs, (str, bytes, bytearray))
        or not isinstance(raw_refs, Sequence)
    ):
        raise CurrentIncidentHwpxPlanError(
            "current_source_refs_invalid",
            "a binding mutation has invalid current source references",
        )
    referenced = set()
    for raw in raw_refs:
        if not isinstance(raw, str) or not raw or _CONTROL.search(raw):
            raise CurrentIncidentHwpxPlanError(
                "current_source_refs_invalid",
                "a binding mutation has invalid current source references",
            )
        if raw.startswith("incident:"):
            if raw not in incident_refs:
                raise CurrentIncidentHwpxPlanError(
                    "cross_incident_source_ref",
                    "a binding mutation crossed the approved Incident set",
                )
            referenced.add(raw)
    for incident_ref in incident_refs:
        if incident_ref in referenced:
            return incident_ref
    # Template-wide neutralization is still part of the complete current
    # report projection.  The exact writer currently accepts only approved
    # Incident refs, so anchor global work to the first current Incident.
    return incident_refs[0]


def _expected_text(value) -> Tuple[str, str]:
    if not isinstance(value, Mapping):
        raise CurrentIncidentHwpxPlanError(
            "text_guard_invalid",
            "a text mutation has no exact expected-value guard",
        )
    text = value.get("text")
    digest = value.get("text_sha256")
    if (
        not isinstance(text, str)
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
    ):
        raise CurrentIncidentHwpxPlanError(
            "text_guard_invalid",
            "a text mutation expected-value guard is invalid",
        )
    return text, digest


def _expected_excerpt_matches(
    expected_excerpt: str, exact_text: str
) -> bool:
    return expected_excerpt == exact_text or (
        len(expected_excerpt) == 320
        and exact_text.startswith(expected_excerpt)
    )


def _current_text(value) -> str:
    if not isinstance(value, Mapping):
        raise CurrentIncidentHwpxPlanError(
            "current_text_invalid",
            "a replacement text value is invalid",
        )
    text = value.get("text")
    digest = value.get("text_sha256")
    if (
        not isinstance(text, str)
        or not isinstance(digest, str)
        or hashlib.sha256(text.encode("utf-8")).hexdigest() != digest
        or _CONTROL.search(text)
    ):
        raise CurrentIncidentHwpxPlanError(
            "current_text_invalid",
            "a replacement text value or SHA-256 is invalid",
        )
    return text


def _can_consolidate_identical_text(
    first: _PendingText, second: _PendingText
) -> bool:
    return (
        first.expected_text == second.expected_text
        and first.expected_sha256 == second.expected_sha256
        and first.value == second.value
        and first.source_ref == second.source_ref
        and first.paragraph_address == second.paragraph_address
        and not first.evaluation_keys
        and not second.evaluation_keys
    )


def _is_stale_marker_clear(value: _PendingText) -> bool:
    return (
        value.value == ""
        and bool(value.roles)
        and all(
            role
            in {
                "stale_evaluation_marker",
                "unassigned_stale_evaluation_marker",
            }
            for role in value.roles
        )
    )


def _target_media(media_type: str) -> Tuple[str, str]:
    try:
        return _SUPPORTED_TARGET_MEDIA[media_type]
    except KeyError as error:
        raise CurrentIncidentHwpxPlanError(
            "source_picture_media_unsupported",
            "the target picture media must be BMP, PNG, or JPEG",
        ) from error


def _absolute_file(
    value: Union[str, os.PathLike],
    *,
    suffix: Optional[str],
    label: str,
) -> Path:
    try:
        path = Path(value)
    except (TypeError, ValueError) as error:
        raise CurrentIncidentHwpxPlanError(
            f"{label.replace(' ', '_')}_path_invalid",
            f"{label} path is invalid",
        ) from error
    if not path.is_absolute() or any(part == ".." for part in path.parts):
        raise CurrentIncidentHwpxPlanError(
            f"{label.replace(' ', '_')}_path_invalid",
            f"{label} path must be absolute and traversal-free",
        )
    if suffix is not None and path.suffix.casefold() != suffix:
        raise CurrentIncidentHwpxPlanError(
            f"{label.replace(' ', '_')}_path_invalid",
            f"{label} path must use the {suffix} extension",
        )
    structure._assert_no_link_or_reparse_components(path)
    try:
        identity = structure._file_identity(os.lstat(str(path)))
    except OSError as error:
        raise CurrentIncidentHwpxPlanError(
            f"{label.replace(' ', '_')}_missing",
            f"{label} does not exist",
        ) from error
    if not stat.S_ISREG(identity.mode):
        raise CurrentIncidentHwpxPlanError(
            f"{label.replace(' ', '_')}_path_invalid",
            f"{label} must be a regular file",
        )
    return path


def _trusted_directory(
    value: Union[str, os.PathLike], *, label: str
) -> Path:
    try:
        path = Path(value)
    except (TypeError, ValueError) as error:
        raise CurrentIncidentHwpxPlanError(
            "server_directory_invalid",
            f"{label} is invalid",
        ) from error
    if not path.is_absolute() or any(part == ".." for part in path.parts):
        raise CurrentIncidentHwpxPlanError(
            "server_directory_invalid",
            f"{label} must be absolute and traversal-free",
        )
    structure._assert_no_link_or_reparse_components(path)
    try:
        identity = structure._file_identity(os.lstat(str(path)))
    except OSError as error:
        raise CurrentIncidentHwpxPlanError(
            "server_directory_missing",
            f"{label} does not exist",
        ) from error
    if not stat.S_ISDIR(identity.mode):
        raise CurrentIncidentHwpxPlanError(
            "server_directory_invalid",
            f"{label} must be a real directory",
        )
    return path.resolve(strict=True)


def _is_within(path: Path, root: Path) -> bool:
    try:
        common = os.path.commonpath(
            (os.path.normcase(str(path)), os.path.normcase(str(root)))
        )
    except ValueError:
        return False
    return common == os.path.normcase(str(root))


def _read_stable_file(
    path: Path, *, maximum: int, label: str
) -> bytes:
    structure._assert_no_link_or_reparse_components(path)
    try:
        identity = structure._file_identity(os.lstat(str(path)))
    except OSError as error:
        raise CurrentIncidentHwpxPlanError(
            f"{label.replace(' ', '_')}_missing",
            f"{label} does not exist",
        ) from error
    if (
        not stat.S_ISREG(identity.mode)
        or identity.size_bytes <= 0
        or identity.size_bytes > maximum
    ):
        raise CurrentIncidentHwpxPlanError(
            f"{label.replace(' ', '_')}_capacity_invalid",
            f"{label} is empty, non-regular, or exceeds the byte limit",
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError as error:
        raise CurrentIncidentHwpxPlanError(
            f"{label.replace(' ', '_')}_open_failed",
            f"{label} could not be opened safely",
        ) from error
    with os.fdopen(descriptor, "rb", buffering=0, closefd=True) as stream:
        if structure._file_identity(os.fstat(stream.fileno())) != identity:
            raise CurrentIncidentHwpxPlanError(
                f"{label.replace(' ', '_')}_changed",
                f"{label} changed while opening",
            )
        payload = stream.read(maximum + 1)
        if (
            len(payload) != identity.size_bytes
            or structure._file_identity(os.fstat(stream.fileno()))
            != identity
        ):
            raise CurrentIncidentHwpxPlanError(
                f"{label.replace(' ', '_')}_changed",
                f"{label} changed while reading",
            )
    return payload


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "CURRENT_INCIDENT_HWPX_PLAN_SCHEMA",
    "CurrentIncidentHwpxPlanError",
    "ServerOwnedEvidenceFile",
    "build_exact_hwpx_write_plan",
]
