"""Path-free user-review plan for one exact current-Incident HWPX report.

The language model is never an address authority here.  This module reopens
the byte-exact audited sample, validates the server-owned
``CurrentIncidentReportBindingManifest`` and ``ExactHwpxWritePlan``, and
projects their already-determined mutations into a path-free review contract.

The contract intentionally performs no Provider, exact-writer, or HWP COM
call.  It is the immutable input that a later accept/reject/rollback workflow
can bind to the exact project, PipelineJob, AnalysisJob, approved Incidents,
template, target addresses, and plan hashes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from uuid import UUID

from PIL import Image

from .current_incident_hwpx_plan import (
    CurrentIncidentHwpxPlanError,
    _absolute_file,
    _is_within,
    _load_stale_factual_audit,
    _read_stable_file,
    _trusted_directory,
    _validate_incident_picture_bindings,
    _validate_manifest_header,
    _validate_source_binding,
    _validate_stale_factual_manifest,
)
from .current_incident_report_binder import (
    CurrentIncidentReportBindingManifest,
    EvaluationRowBinding,
    ReportTargetMutation,
)
from .hwpx_exact_writer import (
    ExactHwpxParagraphTextOperation,
    ExactHwpxPictureOperation,
    ExactHwpxTextOperation,
    ExactHwpxWritePlan,
    ExactHwpxWriterLimits,
    HwpxExactWriterError,
    _validate_plan,
)
from .hwpx_sample_layout import (
    HwpxSampleLayoutError,
    SampleLayoutInspection,
    bind_sample_reference_case,
    inspect_sample_layout,
)
from .hwpx_stale_factual_targets import HwpxStaleFactualTargetAudit


CURRENT_REPORT_DOCUMENT_EDIT_PLAN_SCHEMA = (
    "trafficvisson.current-report-document-edit-plan.v2"
)
CURRENT_REPORT_DOCUMENT_EDIT_PLAN_STATE = (
    "exact_targets_bound_user_review_required"
)
_SHA256_LENGTH = 64
_MAX_STAGED_EVIDENCE_BYTES = 64 * 1024**2
_UNUSED_SAMPLE_ROLE_PREFIXES = (
    "unassigned_stale_",
    "overlaid_stale_",
    "neutralized_stale_",
)


class CurrentReportDocumentEditPlanError(RuntimeError):
    """Fail-closed error before any Provider, writer, or COM invocation."""

    provider_called = False
    writer_invoked = False
    com_invoked = False
    apply_blocked = True

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExactDocumentTargetAddress:
    """One address resolved only from the audited HWPX structure."""

    target_id: str
    target_kind: str
    page_pair_index: int
    page_number: int
    section_path: str
    section_index: int
    table_id: Optional[str]
    table_index: Optional[int]
    cell_target_id: Optional[str]
    cell_index: Optional[int]
    row: Optional[int]
    column: Optional[int]
    row_span: Optional[int]
    column_span: Optional[int]
    picture_index: Optional[int]
    paragraph_address_sha256: Optional[str]
    photo_slot_ids: tuple[str, ...]


@dataclass(frozen=True)
class ExactCheckboxTarget:
    """Current evaluation selection bound to one exact physical cell."""

    page_pair_index: int
    page_number: int
    row_order: int
    row: int
    grade: str
    anchor_column: int
    selected: bool
    selected_grade: str


@dataclass(frozen=True)
class ApprovedIncidentDocumentProvenance:
    """Local-user approval and evidence lineage used by this report."""

    incident_id: str
    incident_order: int
    project_id: str
    pipeline_job_id: str
    analysis_job_id: str
    source_sha256: str
    approval_actor: str
    approval_record_id: str
    approval_record_sha256: str
    selected_evidence_ref: str
    selected_evidence_sha256: str
    evidence_policy: str
    photo_slot_id: str
    photo_binding_sha256: str
    provenance_sha256: str


@dataclass(frozen=True)
class ExactDocumentEditReviewOperation:
    """Path-free rendering of one deterministic exact-writer operation."""

    operation_id: str
    exact_operation_kind: str
    semantic_roles: tuple[str, ...]
    binding_groups: tuple[str, ...]
    target: ExactDocumentTargetAddress
    expected_sample_value_sha256: str
    proposed_value_sha256: str
    proposed_text: Optional[str]
    proposed_evidence_sha256: Optional[str]
    current_source_refs: tuple[str, ...]
    approved_incident_ids: tuple[str, ...]
    checkbox_targets: tuple[ExactCheckboxTarget, ...]
    opinion_target: bool
    neutralizes_historical_sample_fact: bool
    unused_sample_factual_slot_neutralized: bool


@dataclass(frozen=True)
class CurrentReportDocumentEditPlan:
    """Immutable user-review contract for the exact current report."""

    schema_version: str
    state: str
    plan_id: str
    project_id: str
    pipeline_job_id: str
    analysis_job_id: str
    source_sha256: str
    template_id: str
    template_quarter: int
    document_snapshot_id: str
    template_sha256: str
    structure_target_summary_sha256: str
    binding_manifest_sha256: str
    exact_plan_sha256: str
    approved_incident_set_sha256: str
    approved_incidents: tuple[
        ApprovedIncidentDocumentProvenance, ...
    ]
    operations: tuple[ExactDocumentEditReviewOperation, ...]
    stale_sample_factual_target_count: int
    stale_sample_factual_target_coverage_complete: bool
    unused_sample_neutralization_operation_ids: tuple[str, ...]
    document_edit_plan_sha256: str
    accept_reject_rollback_binding_sha256: str
    expected_rendered_page_count: int = 48
    address_authority: str = "server_audited_sample_layout"
    factual_authority: str = "current_local_user_approved_incidents"
    provider_may_select_target_addresses: bool = False
    apply_approval_required: bool = True
    accept_reject_required_after_apply: bool = True
    rollback_supported_after_apply: bool = True
    provider_invocation_count: int = 0
    exact_writer_invocation_count: int = 0
    com_invocation_count: int = 0
    document_written: bool = False
    path_exposed: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe, path-free review payload."""

        return asdict(self)


@dataclass(frozen=True)
class _PhotoSlotAddress:
    photo_slot_id: str
    page_pair_index: int
    photo_page_number: int
    photo_table_index: int
    photo_cell_target_id: str
    photo_cell_row: int
    photo_cell_column: int
    caption_target_id: str
    picture_target_ids: tuple[str, ...]


@dataclass(frozen=True)
class _BoundAction:
    group: str
    role: str
    page_pair_index: int
    page_number: int
    current_source_refs: tuple[str, ...]
    mutation: Optional[ReportTargetMutation] = None
    evaluation_row: Optional[EvaluationRowBinding] = None
    evaluation_target: Optional[Mapping[str, Any]] = None


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_LENGTH
        and all(
            character in "0123456789abcdef"
            for character in value
        )
    )


def _contains_absolute_filesystem_path(value: Any) -> bool:
    if isinstance(value, str):
        return (
            value.startswith(("/", "\\\\"))
            or (
                len(value) >= 3
                and value[0].isalpha()
                and value[1] == ":"
                and value[2] in ("\\", "/")
            )
        )
    if isinstance(value, Mapping):
        return any(
            _contains_absolute_filesystem_path(item)
            for item in value.values()
        )
    if isinstance(value, (tuple, list)):
        return any(
            _contains_absolute_filesystem_path(item)
            for item in value
        )
    return False


def _ordered_unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _expected_exact_operation_source_ref(
    *,
    current_source_refs: Sequence[str],
    approved_incident_ids: Sequence[str],
) -> str:
    """Resolve the exact writer's incident-scoped technical anchor.

    ``current_source_refs`` is the factual provenance shown in the review
    plan.  Template-wide operations (for example route headings and stale
    historical-slot neutralization) correctly cite the current source video,
    PipelineJob, or report profile and therefore do not pretend to be facts
    from one particular Incident.  The exact writer nevertheless requires
    every operation to carry an approved-Incident source ref, so its compiler
    deterministically anchors such global operations to the first approved
    Incident.  Reproduce that rule here instead of requiring the technical
    anchor to appear in the factual provenance set.
    """

    incident_refs = tuple(
        f"incident:{incident_id}"
        for incident_id in approved_incident_ids
    )
    if not incident_refs:
        raise CurrentReportDocumentEditPlanError(
            "approved_incident_set_invalid",
            "the current report has no approved Incident source anchor",
        )
    referenced = {
        source_ref
        for source_ref in current_source_refs
        if source_ref.startswith("incident:")
    }
    for incident_ref in incident_refs:
        if incident_ref in referenced:
            return incident_ref
    return incident_refs[0]


def _exact_plan_sha256(plan: ExactHwpxWritePlan) -> str:
    return _canonical_sha256(asdict(plan))


def _plan_identity_payload(
    *,
    project_id: str,
    pipeline_job_id: str,
    analysis_job_id: str,
    source_sha256: str,
    template_id: str,
    template_quarter: int,
    document_snapshot_id: str,
    template_sha256: str,
    structure_target_summary_sha256: str,
    binding_manifest_sha256: str,
    exact_plan_sha256: str,
    approved_incident_set_sha256: str,
    approved_incidents: Sequence[
        ApprovedIncidentDocumentProvenance
    ],
    operations: Sequence[ExactDocumentEditReviewOperation],
    stale_sample_factual_target_count: int,
    unused_sample_neutralization_operation_ids: Sequence[str],
    address_authority: str,
    factual_authority: str,
) -> dict[str, Any]:
    return {
        "schema_version": CURRENT_REPORT_DOCUMENT_EDIT_PLAN_SCHEMA,
        "project_id": project_id,
        "pipeline_job_id": pipeline_job_id,
        "analysis_job_id": analysis_job_id,
        "source_sha256": source_sha256,
        "template_id": template_id,
        "template_quarter": template_quarter,
        "document_snapshot_id": document_snapshot_id,
        "template_sha256": template_sha256,
        "structure_target_summary_sha256": (
            structure_target_summary_sha256
        ),
        "binding_manifest_sha256": binding_manifest_sha256,
        "exact_plan_sha256": exact_plan_sha256,
        "approved_incident_set_sha256": (
            approved_incident_set_sha256
        ),
        "approved_incidents": [
            asdict(item) for item in approved_incidents
        ],
        "operations": [
            asdict(operation) for operation in operations
        ],
        "stale_sample_factual_target_count": (
            stale_sample_factual_target_count
        ),
        "unused_sample_neutralization_operation_ids": tuple(
            unused_sample_neutralization_operation_ids
        ),
        "address_authority": address_authority,
        "factual_authority": factual_authority,
    }


def _decision_binding_sha256(
    *,
    plan_id: str,
    project_id: str,
    pipeline_job_id: str,
    analysis_job_id: str,
    binding_manifest_sha256: str,
    exact_plan_sha256: str,
    document_edit_plan_sha256: str,
) -> str:
    return _canonical_sha256(
        {
            "schema_version": (
                CURRENT_REPORT_DOCUMENT_EDIT_PLAN_SCHEMA
            ),
            "plan_id": plan_id,
            "project_id": project_id,
            "pipeline_job_id": pipeline_job_id,
            "analysis_job_id": analysis_job_id,
            "binding_manifest_sha256": binding_manifest_sha256,
            "exact_plan_sha256": exact_plan_sha256,
            "document_edit_plan_sha256": (
                document_edit_plan_sha256
            ),
            "allowed_decisions": (
                "accept",
                "reject",
                "rollback",
            ),
        }
    )


def _page_for_table(table_index: int) -> tuple[int, int]:
    if (
        isinstance(table_index, bool)
        or not isinstance(table_index, int)
        or table_index < 0
    ):
        raise CurrentReportDocumentEditPlanError(
            "exact_target_address_invalid",
            "an audited table index is invalid",
        )
    pair = table_index // 3
    page = pair * 2 + (2 if table_index % 3 == 2 else 1)
    return pair, page


def _photo_slots(
    inspection: SampleLayoutInspection,
) -> tuple[
    dict[str, tuple[_PhotoSlotAddress, ...]],
    dict[str, _PhotoSlotAddress],
]:
    grouped: dict[
        tuple[int, str], list[tuple[Any, Any]]
    ] = {}
    for picture in inspection.snapshot.pictures:
        if (
            picture.nearest_table_index is None
            or picture.nearest_table_index % 3 != 2
        ):
            continue
        try:
            reference = bind_sample_reference_case(
                inspection, picture.target_id
            )
        except HwpxSampleLayoutError as error:
            raise CurrentReportDocumentEditPlanError(
                "exact_photo_slot_unavailable",
                "an audited sample photo slot could not be resolved",
            ) from error
        key = (
            reference.page_pair_index,
            reference.photo_cell_target_id,
        )
        grouped.setdefault(key, []).append((picture, reference))

    target_memberships: dict[str, list[_PhotoSlotAddress]] = {}
    by_id: dict[str, _PhotoSlotAddress] = {}
    for (_pair, _cell), members in grouped.items():
        members.sort(
            key=lambda item: (
                item[0].picture_index,
                item[0].target_id,
            )
        )
        first = members[0][1]
        picture_target_ids = tuple(
            item[0].target_id for item in members
        )
        caption_target_id = first.photo_caption.cell_target_id
        if any(
            (
                item[1].page_pair_index,
                item[1].photo_cell_target_id,
                item[1].photo_caption.cell_target_id,
            )
            != (
                first.page_pair_index,
                first.photo_cell_target_id,
                caption_target_id,
            )
            for item in members
        ):
            raise CurrentReportDocumentEditPlanError(
                "exact_photo_slot_conflict",
                "an audited photo slot has conflicting exact targets",
            )
        slot_payload = {
            "document_sha256": (
                inspection.snapshot.document_sha256
            ),
            "page_pair_index": first.page_pair_index,
            "photo_cell_target_id": first.photo_cell_target_id,
            "picture_target_ids": list(picture_target_ids),
            "caption_target_id": caption_target_id,
        }
        slot = _PhotoSlotAddress(
            photo_slot_id=(
                "current-photo-slot-"
                + _canonical_sha256(slot_payload)[:24]
            ),
            page_pair_index=first.page_pair_index,
            photo_page_number=first.photo_page_number,
            photo_table_index=first.photo_table_index,
            photo_cell_target_id=first.photo_cell_target_id,
            photo_cell_row=first.photo_cell_row,
            photo_cell_column=first.photo_cell_column,
            caption_target_id=caption_target_id,
            picture_target_ids=picture_target_ids,
        )
        if slot.photo_slot_id in by_id:
            raise CurrentReportDocumentEditPlanError(
                "exact_photo_slot_duplicate",
                "an audited photo slot identity is duplicated",
            )
        by_id[slot.photo_slot_id] = slot
        for target_id in (
            *picture_target_ids,
            caption_target_id,
        ):
            target_memberships.setdefault(target_id, []).append(slot)
    if not by_id:
        raise CurrentReportDocumentEditPlanError(
            "exact_photo_slot_unavailable",
            "the audited sample has no report photo slots",
        )
    by_target = {
        target_id: tuple(
            sorted(
                dict.fromkeys(slots),
                key=lambda item: (
                    item.page_pair_index,
                    item.photo_cell_row,
                    item.photo_cell_column,
                    item.photo_slot_id,
                ),
            )
        )
        for target_id, slots in target_memberships.items()
    }
    return by_target, by_id


def _actions_by_target(
    binding: CurrentIncidentReportBindingManifest,
) -> dict[str, tuple[_BoundAction, ...]]:
    actions: dict[str, list[_BoundAction]] = {}
    for group, mutations in (
        ("replacement", binding.replacements),
        ("neutralization", binding.neutralizations),
    ):
        for mutation in mutations:
            actions.setdefault(mutation.target_id, []).append(
                _BoundAction(
                    group=group,
                    role=mutation.role,
                    page_pair_index=mutation.page_pair_index,
                    page_number=mutation.page_number,
                    current_source_refs=(
                        mutation.current_source_refs
                    ),
                    mutation=mutation,
                )
            )
    for row in binding.evaluation_rows:
        for target in row.grade_targets:
            target_id = target.get("target_id")
            if not isinstance(target_id, str):
                raise CurrentReportDocumentEditPlanError(
                    "evaluation_target_invalid",
                    "an evaluation target identity is invalid",
                )
            actions.setdefault(target_id, []).append(
                _BoundAction(
                    group="evaluation",
                    role="current_evaluation_grade",
                    page_pair_index=row.page_pair_index,
                    page_number=row.page_number,
                    current_source_refs=row.current_source_refs,
                    evaluation_row=row,
                    evaluation_target=target,
                )
            )
    return {
        target_id: tuple(items)
        for target_id, items in actions.items()
    }


def _approved_incident_provenance(
    binding: CurrentIncidentReportBindingManifest,
) -> tuple[ApprovedIncidentDocumentProvenance, ...]:
    photo_mutations = {
        mutation.target_id: mutation
        for mutation in binding.replacements
        if (
            mutation.role == "incident_photo"
            and mutation.operation == "replace_picture"
        )
    }
    result = []
    for item in binding.incident_bindings:
        mutation = photo_mutations.get(
            item.primary_picture_target_id
        )
        if mutation is None:
            raise CurrentReportDocumentEditPlanError(
                "approved_incident_provenance_incomplete",
                "an approved Incident has no exact photo mutation",
            )
        expected_incident_ref = f"incident:{item.incident_id}"
        approval_ids = tuple(
            value.removeprefix("approval-record:")
            for value in mutation.current_source_refs
            if value.startswith("approval-record:")
            and not value.startswith("approval-record-sha256:")
        )
        approval_hashes = tuple(
            value.removeprefix("approval-record-sha256:")
            for value in mutation.current_source_refs
            if value.startswith("approval-record-sha256:")
        )
        if (
            expected_incident_ref not in mutation.current_source_refs
            or len(approval_ids) != 1
            or len(approval_hashes) != 1
            or len(approval_hashes[0]) != _SHA256_LENGTH
            or any(
                character not in "0123456789abcdef"
                for character in approval_hashes[0]
            )
            or f"source-video:{binding.source_sha256}"
            not in mutation.current_source_refs
            or f"evidence-sha256:{item.selected_evidence_sha256}"
            not in mutation.current_source_refs
        ):
            raise CurrentReportDocumentEditPlanError(
                "approved_incident_provenance_incomplete",
                "an approved Incident lacks exact local review provenance",
            )
        payload = {
            "incident_id": item.incident_id,
            "incident_order": item.incident_order,
            "project_id": binding.project_id,
            "pipeline_job_id": binding.pipeline_job_id,
            "analysis_job_id": binding.analysis_job_id,
            "source_sha256": binding.source_sha256,
            "approval_actor": "local_user",
            "approval_record_id": approval_ids[0],
            "approval_record_sha256": approval_hashes[0],
            "selected_evidence_ref": item.selected_evidence_ref,
            "selected_evidence_sha256": (
                item.selected_evidence_sha256
            ),
            "evidence_policy": item.evidence_policy,
            "photo_slot_id": item.photo_slot_id,
            "photo_binding_sha256": item.photo_binding_sha256,
        }
        result.append(
            ApprovedIncidentDocumentProvenance(
                **payload,
                provenance_sha256=_canonical_sha256(payload),
            )
        )
    if tuple(item.incident_id for item in result) != (
        binding.approved_incident_ids
    ):
        raise CurrentReportDocumentEditPlanError(
            "approved_incident_provenance_incomplete",
            "the complete ordered approved-Incident set is not bound",
        )
    return tuple(result)


def _validate_context(
    *,
    binding: CurrentIncidentReportBindingManifest,
    project_id: UUID,
    pipeline_job_id: UUID,
    analysis_job_id: UUID,
    source_sha256: str,
) -> None:
    if not all(
        isinstance(item, UUID)
        for item in (project_id, pipeline_job_id, analysis_job_id)
    ):
        raise CurrentReportDocumentEditPlanError(
            "current_report_context_invalid",
            "current report context identities must be UUIDs",
        )
    if (
        binding.project_id != str(project_id)
        or binding.pipeline_job_id != str(pipeline_job_id)
        or binding.analysis_job_id != str(analysis_job_id)
        or binding.source_sha256 != source_sha256
    ):
        raise CurrentReportDocumentEditPlanError(
            "cross_job_report_plan",
            "the report plan crossed its current project or analysis job",
        )


def _validate_plan_binding(
    *,
    binding: CurrentIncidentReportBindingManifest,
    exact_plan: ExactHwpxWritePlan,
    inspection: SampleLayoutInspection,
    actions_by_target: Mapping[str, Sequence[_BoundAction]],
    limits: ExactHwpxWriterLimits,
) -> None:
    expected_incident_refs = tuple(
        f"incident:{incident_id}"
        for incident_id in binding.approved_incident_ids
    )
    operation_targets = tuple(
        operation.target_id for operation in exact_plan.operations
    )
    if (
        exact_plan.plan_id
        != "current-report-"
        + binding.binding_manifest_sha256[:32]
        or exact_plan.source_document_sha256
        != binding.template_sha256
        or exact_plan.source_document_sha256
        != inspection.snapshot.document_sha256
        or exact_plan.source_target_summary_sha256
        != binding.structure_target_summary_sha256
        or exact_plan.incident_source_refs
        != expected_incident_refs
        or len(exact_plan.operations)
        != binding.required_writer_operation_count
        or len(operation_targets) != len(set(operation_targets))
    ):
        raise CurrentReportDocumentEditPlanError(
            "stale_exact_plan",
            "the exact HWPX plan is stale or belongs to another binding",
        )
    try:
        _validate_plan(exact_plan, limits)
    except HwpxExactWriterError as error:
        raise CurrentReportDocumentEditPlanError(
            "exact_plan_invalid",
            "the exact HWPX plan failed writer-contract validation",
        ) from error
    if set(operation_targets) != set(actions_by_target):
        raise CurrentReportDocumentEditPlanError(
            "exact_plan_target_set_mismatch",
            "the exact plan does not consume every bound report target",
        )
    if any(
        operation.source_ref not in expected_incident_refs
        for operation in exact_plan.operations
    ):
        raise CurrentReportDocumentEditPlanError(
            "cross_incident_source_ref",
            "an exact operation crossed the approved Incident set",
        )


def _target_address(
    *,
    target_id: str,
    inspection: SampleLayoutInspection,
    stale_audit: HwpxStaleFactualTargetAudit,
    photo_slots_by_target: Mapping[
        str, Sequence[_PhotoSlotAddress]
    ],
    actions: Sequence[_BoundAction],
) -> ExactDocumentTargetAddress:
    cells = {
        cell.target_id: cell
        for table in inspection.snapshot.tables
        for cell in table.cells
    }
    pictures = {
        picture.target_id: picture
        for picture in inspection.snapshot.pictures
    }
    paragraphs = {
        pair.photo_page_header.target_id: pair.photo_page_header
        for pair in stale_audit.page_pairs
    }
    candidates = {
        (action.page_pair_index, action.page_number)
        for action in actions
    }
    if len(candidates) != 1:
        raise CurrentReportDocumentEditPlanError(
            "exact_target_address_conflict",
            "one target has conflicting page bindings",
        )
    page_pair_index, page_number = next(iter(candidates))
    slots = tuple(photo_slots_by_target.get(target_id, ()))
    photo_slot_ids = tuple(
        slot.photo_slot_id for slot in slots
    )
    cell = cells.get(target_id)
    picture = pictures.get(target_id)
    paragraph = paragraphs.get(target_id)
    resolved_count = sum(
        item is not None for item in (cell, picture, paragraph)
    )
    if resolved_count != 1:
        raise CurrentReportDocumentEditPlanError(
            "exact_target_address_unavailable",
            "one report target does not resolve exactly once",
        )
    if cell is not None:
        derived_pair, derived_page = _page_for_table(
            cell.table_index
        )
        if (derived_pair, derived_page) != (
            page_pair_index,
            page_number,
        ):
            raise CurrentReportDocumentEditPlanError(
                "exact_target_address_conflict",
                "a bound cell page does not match the audited layout",
            )
        return ExactDocumentTargetAddress(
            target_id=target_id,
            target_kind="cell",
            page_pair_index=page_pair_index,
            page_number=page_number,
            section_path=cell.section_path,
            section_index=cell.section_index,
            table_id=cell.table_id,
            table_index=cell.table_index,
            cell_target_id=cell.target_id,
            cell_index=cell.cell_index,
            row=cell.row,
            column=cell.column,
            row_span=cell.row_span,
            column_span=cell.column_span,
            picture_index=None,
            paragraph_address_sha256=None,
            photo_slot_ids=photo_slot_ids,
        )
    if picture is not None:
        if (
            picture.nearest_table_index is None
            or picture.nearest_cell_target_id is None
        ):
            raise CurrentReportDocumentEditPlanError(
                "exact_target_address_unavailable",
                "a report picture has no audited table/cell address",
            )
        host_cell = cells.get(picture.nearest_cell_target_id)
        if (
            host_cell is None
            or host_cell.section_path != picture.section_path
            or host_cell.section_index != picture.section_index
            or host_cell.table_id != picture.nearest_table_id
            or host_cell.table_index != picture.nearest_table_index
            or host_cell.row != picture.nearest_cell_row
            or host_cell.column != picture.nearest_cell_column
        ):
            raise CurrentReportDocumentEditPlanError(
                "exact_target_address_conflict",
                "a report picture crossed its audited containing cell",
            )
        derived_pair, derived_page = _page_for_table(
            picture.nearest_table_index
        )
        if (derived_pair, derived_page) != (
            page_pair_index,
            page_number,
        ):
            raise CurrentReportDocumentEditPlanError(
                "exact_target_address_conflict",
                "a bound picture page does not match the audited layout",
            )
        return ExactDocumentTargetAddress(
            target_id=target_id,
            target_kind="picture",
            page_pair_index=page_pair_index,
            page_number=page_number,
            section_path=picture.section_path,
            section_index=picture.section_index,
            table_id=picture.nearest_table_id,
            table_index=picture.nearest_table_index,
            cell_target_id=host_cell.target_id,
            cell_index=host_cell.cell_index,
            row=host_cell.row,
            column=host_cell.column,
            row_span=host_cell.row_span,
            column_span=host_cell.column_span,
            picture_index=picture.picture_index,
            paragraph_address_sha256=None,
            photo_slot_ids=photo_slot_ids,
        )
    assert paragraph is not None
    if (
        paragraph.page_pair_index != page_pair_index
        or paragraph.page_number != page_number
    ):
        raise CurrentReportDocumentEditPlanError(
            "exact_target_address_conflict",
            "a bound paragraph page does not match the audited layout",
        )
    address = paragraph.address
    return ExactDocumentTargetAddress(
        target_id=target_id,
        target_kind="paragraph",
        page_pair_index=page_pair_index,
        page_number=page_number,
        section_path=address.section_path,
        section_index=address.section_index,
        table_id=None,
        table_index=None,
        cell_target_id=None,
        cell_index=None,
        row=None,
        column=None,
        row_span=None,
        column_span=None,
        picture_index=None,
        paragraph_address_sha256=address.address_sha256,
        photo_slot_ids=(),
    )


def _checkbox_targets(
    actions: Sequence[_BoundAction],
) -> tuple[ExactCheckboxTarget, ...]:
    result = []
    for action in actions:
        row = action.evaluation_row
        target = action.evaluation_target
        if row is None or target is None:
            continue
        grade = target.get("grade")
        anchor = target.get("anchor_column")
        if (
            not isinstance(grade, str)
            or isinstance(anchor, bool)
            or not isinstance(anchor, int)
        ):
            raise CurrentReportDocumentEditPlanError(
                "evaluation_target_invalid",
                "an exact checkbox address is invalid",
            )
        result.append(
            ExactCheckboxTarget(
                page_pair_index=row.page_pair_index,
                page_number=row.page_number,
                row_order=row.row_order,
                row=row.row,
                grade=grade,
                anchor_column=anchor,
                selected=(
                    target.get("target_id")
                    == row.selected_target_id
                ),
                selected_grade=row.selected_grade,
            )
        )
    return tuple(result)


def _validate_staged_picture(
    *,
    operation: ExactHwpxPictureOperation,
    staged_root: Path,
    require_neutral: bool,
) -> None:
    try:
        path = _absolute_file(
            operation.evidence_path,
            suffix=None,
            label="document edit plan evidence",
        )
        if not _is_within(path, staged_root):
            raise CurrentReportDocumentEditPlanError(
                "exact_plan_evidence_outside_staging",
                "an exact plan image is outside its server-owned staging root",
            )
        payload = _read_stable_file(
            path,
            maximum=_MAX_STAGED_EVIDENCE_BYTES,
            label="document edit plan evidence",
        )
    except CurrentReportDocumentEditPlanError:
        raise
    except CurrentIncidentHwpxPlanError as error:
        raise CurrentReportDocumentEditPlanError(
            "exact_plan_evidence_invalid",
            "an exact plan image failed server-owned file validation",
        ) from error
    if hashlib.sha256(payload).hexdigest() != operation.evidence_sha256:
        raise CurrentReportDocumentEditPlanError(
            "exact_plan_evidence_hash_mismatch",
            "an exact plan image changed after plan compilation",
        )
    if not require_neutral:
        return
    try:
        with Image.open(path) as image:
            image.load()
            if image.size != (1, 1):
                raise ValueError("neutral image size is not one pixel")
            pixel = image.convert("RGB").getpixel((0, 0))
    except (OSError, ValueError) as error:
        raise CurrentReportDocumentEditPlanError(
            "stale_sample_content",
            "an unused historical picture was not explicitly neutralized",
        ) from error
    if pixel != (255, 255, 255):
        raise CurrentReportDocumentEditPlanError(
            "stale_sample_content",
            "an unused historical picture was not neutralized to white",
        )


def _review_operation(
    *,
    operation: Any,
    actions: Sequence[_BoundAction],
    address: ExactDocumentTargetAddress,
    staged_root: Path,
) -> ExactDocumentEditReviewOperation:
    roles = _ordered_unique(
        tuple(action.role for action in actions)
    )
    groups = _ordered_unique(
        tuple(action.group for action in actions)
    )
    source_refs = _ordered_unique(
        tuple(
            source_ref
            for action in actions
            for source_ref in action.current_source_refs
        )
    )
    incident_ids = _ordered_unique(
        tuple(
            source_ref.removeprefix("incident:")
            for source_ref in source_refs
            if source_ref.startswith("incident:")
        )
    )
    if not source_refs or any(
        source_ref.startswith(
            ("sample-case:", "weak-case:", "historical-sample:")
        )
        for source_ref in source_refs
    ):
        raise CurrentReportDocumentEditPlanError(
            "sample_placeholder_source_forbidden",
            "historical sample content cannot be a current report fact source",
        )
    unused = any(
        role.startswith(_UNUSED_SAMPLE_ROLE_PREFIXES)
        for role in roles
    )
    neutralizes = "neutralization" in groups
    proposed_text: Optional[str] = None
    proposed_evidence_sha256: Optional[str] = None
    if isinstance(
        operation,
        (ExactHwpxTextOperation, ExactHwpxParagraphTextOperation),
    ):
        expected_sha = operation.expected_current_sha256
        proposed_text = operation.value or None
        proposed_sha = _text_sha256(operation.value)
        if unused and operation.value != "":
            raise CurrentReportDocumentEditPlanError(
                "stale_sample_content",
                "an unused historical text slot was not neutralized",
            )
    elif isinstance(operation, ExactHwpxPictureOperation):
        expected_sha = operation.expected_current_sha256
        proposed_sha = operation.evidence_sha256
        proposed_evidence_sha256 = operation.evidence_sha256
        _validate_staged_picture(
            operation=operation,
            staged_root=staged_root,
            require_neutral=unused,
        )
    else:  # pragma: no cover - guarded by exact-writer validation
        raise CurrentReportDocumentEditPlanError(
            "exact_plan_operation_unsupported",
            "the exact plan contains an unsupported operation",
        )
    checkbox_targets = _checkbox_targets(actions)
    selected = tuple(
        item for item in checkbox_targets if item.selected
    )
    if len(selected) > 1:
        raise CurrentReportDocumentEditPlanError(
            "evaluation_target_conflict",
            "one physical checkbox cell has conflicting selections",
        )
    if selected and proposed_text != "■":
        raise CurrentReportDocumentEditPlanError(
            "evaluation_selection_invalid",
            "a selected checkbox does not contain the current marker",
        )
    if checkbox_targets and not selected and proposed_text is not None:
        raise CurrentReportDocumentEditPlanError(
            "evaluation_selection_invalid",
            "an unselected checkbox cell was not cleared",
        )
    return ExactDocumentEditReviewOperation(
        operation_id=operation.operation_id,
        exact_operation_kind=operation.kind,
        semantic_roles=roles,
        binding_groups=groups,
        target=address,
        expected_sample_value_sha256=expected_sha,
        proposed_value_sha256=proposed_sha,
        proposed_text=proposed_text,
        proposed_evidence_sha256=proposed_evidence_sha256,
        current_source_refs=source_refs,
        approved_incident_ids=incident_ids,
        checkbox_targets=checkbox_targets,
        opinion_target=any(
            role
            in {"inspection_opinion", "unassigned_stale_opinion"}
            for role in roles
        ),
        neutralizes_historical_sample_fact=neutralizes,
        unused_sample_factual_slot_neutralized=unused,
    )


def validate_current_report_document_edit_plan(
    plan: CurrentReportDocumentEditPlan,
    *,
    binding_manifest: CurrentIncidentReportBindingManifest,
    exact_plan: ExactHwpxWritePlan,
    project_id: UUID,
    pipeline_job_id: UUID,
    analysis_job_id: UUID,
    source_sha256: str,
) -> None:
    """Revalidate the immutable four-hash review/apply boundary.

    This validator intentionally does not open a document, call a Provider,
    invoke the exact writer, or invoke HWP COM.  The draft loader separately
    rebuilds the plan from the byte-exact sample; later approval/application
    boundaries use this function to prove that the already rebuilt plan still
    belongs to the same binding and exact-writer plan.
    """

    if not isinstance(plan, CurrentReportDocumentEditPlan):
        raise CurrentReportDocumentEditPlanError(
            "document_edit_plan_required",
            "a server-owned current report DocumentEditPlan is required",
        )
    if _contains_absolute_filesystem_path(plan.to_dict()):
        raise CurrentReportDocumentEditPlanError(
            "document_edit_plan_path_exposed",
            "the path-free DocumentEditPlan exposed a filesystem path",
        )
    _validate_context(
        binding=binding_manifest,
        project_id=project_id,
        pipeline_job_id=pipeline_job_id,
        analysis_job_id=analysis_job_id,
        source_sha256=source_sha256,
    )
    exact_plan_sha256 = _exact_plan_sha256(exact_plan)
    expected_incident_ids = tuple(
        binding_manifest.approved_incident_ids
    )
    approved_incident_ids = tuple(
        item.incident_id for item in plan.approved_incidents
    )
    approved_set_sha256 = _canonical_sha256(
        [asdict(item) for item in plan.approved_incidents]
    )
    operation_ids = tuple(
        item.operation_id for item in plan.operations
    )
    exact_operation_ids = tuple(
        item.operation_id for item in exact_plan.operations
    )
    if (
        plan.schema_version
        != CURRENT_REPORT_DOCUMENT_EDIT_PLAN_SCHEMA
        or plan.state != CURRENT_REPORT_DOCUMENT_EDIT_PLAN_STATE
        or plan.project_id != str(project_id)
        or plan.pipeline_job_id != str(pipeline_job_id)
        or plan.analysis_job_id != str(analysis_job_id)
        or plan.source_sha256 != source_sha256
        or plan.template_id != binding_manifest.template_id
        or plan.document_snapshot_id
        != binding_manifest.document_snapshot_id
        or plan.template_sha256
        != binding_manifest.template_sha256
        or plan.structure_target_summary_sha256
        != binding_manifest.structure_target_summary_sha256
        or plan.binding_manifest_sha256
        != binding_manifest.binding_manifest_sha256
        or plan.exact_plan_sha256 != exact_plan_sha256
        or plan.approved_incident_set_sha256
        != approved_set_sha256
        or approved_incident_ids != expected_incident_ids
        or operation_ids != exact_operation_ids
        or len(plan.operations)
        != binding_manifest.required_writer_operation_count
        or not isinstance(
            plan.stale_sample_factual_target_count, int
        )
        or isinstance(
            plan.stale_sample_factual_target_count, bool
        )
        or plan.stale_sample_factual_target_count < 0
        or plan.stale_sample_factual_target_coverage_complete
        is not True
        or plan.address_authority
        != "server_audited_sample_layout"
        or plan.factual_authority
        != "current_local_user_approved_incidents"
        or plan.provider_may_select_target_addresses is not False
        or plan.apply_approval_required is not True
        or plan.accept_reject_required_after_apply is not True
        or plan.rollback_supported_after_apply is not True
        or plan.provider_invocation_count != 0
        or plan.exact_writer_invocation_count != 0
        or plan.com_invocation_count != 0
        or plan.document_written is not False
        or plan.path_exposed is not False
        or plan.expected_rendered_page_count != 48
    ):
        raise CurrentReportDocumentEditPlanError(
            "document_edit_plan_binding_invalid",
            "the DocumentEditPlan crossed or changed its exact report binding",
        )
    for item in plan.approved_incidents:
        provenance_payload = {
            "incident_id": item.incident_id,
            "incident_order": item.incident_order,
            "project_id": item.project_id,
            "pipeline_job_id": item.pipeline_job_id,
            "analysis_job_id": item.analysis_job_id,
            "source_sha256": item.source_sha256,
            "approval_actor": item.approval_actor,
            "approval_record_id": item.approval_record_id,
            "approval_record_sha256": (
                item.approval_record_sha256
            ),
            "selected_evidence_ref": item.selected_evidence_ref,
            "selected_evidence_sha256": (
                item.selected_evidence_sha256
            ),
            "evidence_policy": item.evidence_policy,
            "photo_slot_id": item.photo_slot_id,
            "photo_binding_sha256": item.photo_binding_sha256,
        }
        if (
            item.project_id != str(project_id)
            or item.pipeline_job_id != str(pipeline_job_id)
            or item.analysis_job_id != str(analysis_job_id)
            or item.source_sha256 != source_sha256
            or item.approval_actor != "local_user"
            or _canonical_sha256(provenance_payload)
            != item.provenance_sha256
            or not _is_sha256(item.approval_record_sha256)
            or not _is_sha256(item.selected_evidence_sha256)
            or not _is_sha256(item.photo_binding_sha256)
        ):
            raise CurrentReportDocumentEditPlanError(
                "approved_incident_provenance_invalid",
                "the DocumentEditPlan has invalid approval provenance",
            )
    for review, exact in zip(
        plan.operations, exact_plan.operations
    ):
        expected_exact_source_ref = (
            _expected_exact_operation_source_ref(
                current_source_refs=review.current_source_refs,
                approved_incident_ids=expected_incident_ids,
            )
        )
        if (
            review.operation_id != exact.operation_id
            or review.exact_operation_kind != exact.kind
            or review.target.target_id != exact.target_id
            or review.expected_sample_value_sha256
            != exact.expected_current_sha256
            or exact.source_ref != expected_exact_source_ref
            or any(
                source_ref.startswith(
                    (
                        "sample-case:",
                        "weak-case:",
                        "historical-sample:",
                    )
                )
                for source_ref in review.current_source_refs
            )
            or any(
                incident_id not in expected_incident_ids
                for incident_id in review.approved_incident_ids
            )
        ):
            raise CurrentReportDocumentEditPlanError(
                "document_edit_operation_binding_invalid",
                "a DocumentEditPlan operation changed its exact target",
            )
        if isinstance(
            exact,
            (ExactHwpxTextOperation, ExactHwpxParagraphTextOperation),
        ):
            if (
                # The path-free review contract represents an exact empty
                # string mutation as ``None`` so cleared historical text and
                # unselected checkbox cells are not rendered as factual copy.
                # Its value hash still binds the writer's literal empty
                # string, and the exact-plan hash binds the full operation.
                review.proposed_text != (exact.value or None)
                or review.proposed_evidence_sha256 is not None
                or review.proposed_value_sha256
                != _text_sha256(exact.value)
            ):
                raise CurrentReportDocumentEditPlanError(
                    "document_edit_operation_value_invalid",
                    "a DocumentEditPlan text value changed",
                )
        elif isinstance(exact, ExactHwpxPictureOperation):
            if (
                review.proposed_text is not None
                or review.proposed_evidence_sha256
                != exact.evidence_sha256
                or review.proposed_value_sha256
                != exact.evidence_sha256
            ):
                raise CurrentReportDocumentEditPlanError(
                    "document_edit_operation_value_invalid",
                    "a DocumentEditPlan evidence value changed",
                )
        else:  # pragma: no cover - exact plan validation owns this
            raise CurrentReportDocumentEditPlanError(
                "exact_plan_operation_unsupported",
                "the exact plan contains an unsupported operation",
            )
    neutralized_ids = tuple(
        item.operation_id
        for item in plan.operations
        if item.unused_sample_factual_slot_neutralized
    )
    if (
        neutralized_ids
        != plan.unused_sample_neutralization_operation_ids
        or len(neutralized_ids) != len(set(neutralized_ids))
    ):
        raise CurrentReportDocumentEditPlanError(
            "sample_placeholder_neutralization_incomplete",
            "the DocumentEditPlan neutralization set changed",
        )
    identity_payload = _plan_identity_payload(
        project_id=plan.project_id,
        pipeline_job_id=plan.pipeline_job_id,
        analysis_job_id=plan.analysis_job_id,
        source_sha256=plan.source_sha256,
        template_id=plan.template_id,
        template_quarter=plan.template_quarter,
        document_snapshot_id=plan.document_snapshot_id,
        template_sha256=plan.template_sha256,
        structure_target_summary_sha256=(
            plan.structure_target_summary_sha256
        ),
        binding_manifest_sha256=plan.binding_manifest_sha256,
        exact_plan_sha256=plan.exact_plan_sha256,
        approved_incident_set_sha256=(
            plan.approved_incident_set_sha256
        ),
        approved_incidents=plan.approved_incidents,
        operations=plan.operations,
        stale_sample_factual_target_count=(
            plan.stale_sample_factual_target_count
        ),
        unused_sample_neutralization_operation_ids=(
            plan.unused_sample_neutralization_operation_ids
        ),
        address_authority=plan.address_authority,
        factual_authority=plan.factual_authority,
    )
    expected_plan_sha256 = _canonical_sha256(identity_payload)
    expected_plan_id = (
        "exact-document-edit-plan-" + expected_plan_sha256[:32]
    )
    expected_decision_sha256 = _decision_binding_sha256(
        plan_id=expected_plan_id,
        project_id=plan.project_id,
        pipeline_job_id=plan.pipeline_job_id,
        analysis_job_id=plan.analysis_job_id,
        binding_manifest_sha256=plan.binding_manifest_sha256,
        exact_plan_sha256=plan.exact_plan_sha256,
        document_edit_plan_sha256=expected_plan_sha256,
    )
    if (
        plan.document_edit_plan_sha256 != expected_plan_sha256
        or plan.plan_id != expected_plan_id
        or plan.accept_reject_rollback_binding_sha256
        != expected_decision_sha256
    ):
        raise CurrentReportDocumentEditPlanError(
            "document_edit_plan_hash_invalid",
            "the DocumentEditPlan hash quartet failed verification",
        )


def build_current_report_document_edit_plan(
    *,
    source_path: Path | str,
    binding_manifest: CurrentIncidentReportBindingManifest,
    exact_plan: ExactHwpxWritePlan,
    staged_evidence_root: Path | str,
    project_id: UUID,
    pipeline_job_id: UUID,
    analysis_job_id: UUID,
    source_sha256: str,
    limits: Optional[ExactHwpxWriterLimits] = None,
) -> CurrentReportDocumentEditPlan:
    """Build a deterministic, path-free plan for the user's final review.

    The caller must pass the current project/job/video identities explicitly.
    This prevents a valid old binding from being shown in another current
    report flow.  ``staged_evidence_root`` is used only to verify plan images;
    no path is returned in the review contract.
    """

    active_limits = limits or ExactHwpxWriterLimits()
    if not isinstance(
        binding_manifest, CurrentIncidentReportBindingManifest
    ) or not isinstance(exact_plan, ExactHwpxWritePlan):
        raise CurrentReportDocumentEditPlanError(
            "server_owned_plan_required",
            "server-owned binding and exact HWPX plan objects are required",
        )
    _validate_context(
        binding=binding_manifest,
        project_id=project_id,
        pipeline_job_id=pipeline_job_id,
        analysis_job_id=analysis_job_id,
        source_sha256=source_sha256,
    )
    try:
        _validate_manifest_header(
            binding_manifest, active_limits
        )
        _validate_incident_picture_bindings(binding_manifest)
        inspection = inspect_sample_layout(source_path)
        _validate_source_binding(binding_manifest, inspection)
        stale_audit = _load_stale_factual_audit(
            Path(source_path),
            inspection=inspection,
        )
        _validate_stale_factual_manifest(
            binding_manifest,
            audit=stale_audit,
            paragraph_targets={
                pair.photo_page_header.target_id: (
                    pair.photo_page_header
                )
                for pair in stale_audit.page_pairs
            },
            signature_targets={
                pair.inspector_signature.target_id: (
                    pair.inspector_signature
                )
                for pair in stale_audit.page_pairs
            },
        )
        staged_root = _trusted_directory(
            staged_evidence_root,
            label="document edit plan staging root",
        )
    except CurrentReportDocumentEditPlanError:
        raise
    except (
        CurrentIncidentHwpxPlanError,
        HwpxSampleLayoutError,
        OSError,
        ValueError,
    ) as error:
        code = getattr(error, "code", "exact_sample_plan_invalid")
        raise CurrentReportDocumentEditPlanError(
            code,
            "the exact sample, binding, or staged plan is invalid",
        ) from error

    actions_by_target = _actions_by_target(binding_manifest)
    _validate_plan_binding(
        binding=binding_manifest,
        exact_plan=exact_plan,
        inspection=inspection,
        actions_by_target=actions_by_target,
        limits=active_limits,
    )
    photo_slots_by_target, photo_slots_by_id = _photo_slots(
        inspection
    )
    for incident in binding_manifest.incident_bindings:
        slot = photo_slots_by_id.get(incident.photo_slot_id)
        if (
            slot is None
            or incident.page_pair_index != slot.page_pair_index
            or incident.photo_page_number != slot.photo_page_number
            or incident.photo_cell_target_id
            != slot.photo_cell_target_id
            or incident.caption_cell_target_id
            != slot.caption_target_id
            or incident.primary_picture_target_id
            not in slot.picture_target_ids
            or tuple(incident.additional_picture_target_ids)
            != tuple(
                target_id
                for target_id in slot.picture_target_ids
                if target_id
                != incident.primary_picture_target_id
            )
        ):
            raise CurrentReportDocumentEditPlanError(
                "stale_photo_slot_binding",
                "an approved Incident crossed its audited photo slot",
            )

    approved = _approved_incident_provenance(
        binding_manifest
    )
    review_operations = []
    for operation in exact_plan.operations:
        actions = actions_by_target[operation.target_id]
        address = _target_address(
            target_id=operation.target_id,
            inspection=inspection,
            stale_audit=stale_audit,
            photo_slots_by_target=photo_slots_by_target,
            actions=actions,
        )
        review_operations.append(
            _review_operation(
                operation=operation,
                actions=actions,
                address=address,
                staged_root=staged_root,
            )
        )
    operations = tuple(review_operations)
    unused_operation_ids = tuple(
        operation.operation_id
        for operation in operations
        if operation.unused_sample_factual_slot_neutralized
    )
    if not unused_operation_ids:
        raise CurrentReportDocumentEditPlanError(
            "sample_placeholder_neutralization_incomplete",
            "the completed historical sample exposes no neutralization plan",
        )

    exact_plan_sha256 = _exact_plan_sha256(exact_plan)
    approved_set_sha256 = _canonical_sha256(
        [asdict(item) for item in approved]
    )
    plan_identity_payload = _plan_identity_payload(
        project_id=str(project_id),
        pipeline_job_id=str(pipeline_job_id),
        analysis_job_id=str(analysis_job_id),
        source_sha256=source_sha256,
        template_id=binding_manifest.template_id,
        template_quarter=inspection.baseline.quarter,
        document_snapshot_id=(
            binding_manifest.document_snapshot_id
        ),
        template_sha256=binding_manifest.template_sha256,
        structure_target_summary_sha256=(
            binding_manifest.structure_target_summary_sha256
        ),
        binding_manifest_sha256=(
            binding_manifest.binding_manifest_sha256
        ),
        exact_plan_sha256=exact_plan_sha256,
        approved_incident_set_sha256=approved_set_sha256,
        approved_incidents=approved,
        operations=operations,
        stale_sample_factual_target_count=(
            stale_audit.counts.required_target_count
        ),
        unused_sample_neutralization_operation_ids=(
            unused_operation_ids
        ),
        address_authority="server_audited_sample_layout",
        factual_authority=(
            "current_local_user_approved_incidents"
        ),
    )
    document_edit_plan_sha256 = _canonical_sha256(
        plan_identity_payload
    )
    plan_id = (
        "exact-document-edit-plan-"
        + document_edit_plan_sha256[:32]
    )
    decision_binding_sha256 = _decision_binding_sha256(
        plan_id=plan_id,
        project_id=str(project_id),
        pipeline_job_id=str(pipeline_job_id),
        analysis_job_id=str(analysis_job_id),
        binding_manifest_sha256=(
            binding_manifest.binding_manifest_sha256
        ),
        exact_plan_sha256=exact_plan_sha256,
        document_edit_plan_sha256=document_edit_plan_sha256,
    )
    return CurrentReportDocumentEditPlan(
        schema_version=(
            CURRENT_REPORT_DOCUMENT_EDIT_PLAN_SCHEMA
        ),
        state=CURRENT_REPORT_DOCUMENT_EDIT_PLAN_STATE,
        plan_id=plan_id,
        project_id=str(project_id),
        pipeline_job_id=str(pipeline_job_id),
        analysis_job_id=str(analysis_job_id),
        source_sha256=source_sha256,
        template_id=binding_manifest.template_id,
        template_quarter=inspection.baseline.quarter,
        document_snapshot_id=(
            binding_manifest.document_snapshot_id
        ),
        template_sha256=binding_manifest.template_sha256,
        structure_target_summary_sha256=(
            binding_manifest.structure_target_summary_sha256
        ),
        binding_manifest_sha256=(
            binding_manifest.binding_manifest_sha256
        ),
        exact_plan_sha256=exact_plan_sha256,
        approved_incident_set_sha256=approved_set_sha256,
        approved_incidents=approved,
        operations=operations,
        stale_sample_factual_target_count=(
            stale_audit.counts.required_target_count
        ),
        stale_sample_factual_target_coverage_complete=True,
        unused_sample_neutralization_operation_ids=(
            unused_operation_ids
        ),
        document_edit_plan_sha256=document_edit_plan_sha256,
        accept_reject_rollback_binding_sha256=(
            decision_binding_sha256
        ),
    )


__all__ = [
    "CURRENT_REPORT_DOCUMENT_EDIT_PLAN_SCHEMA",
    "CURRENT_REPORT_DOCUMENT_EDIT_PLAN_STATE",
    "ApprovedIncidentDocumentProvenance",
    "CurrentReportDocumentEditPlan",
    "CurrentReportDocumentEditPlanError",
    "ExactCheckboxTarget",
    "ExactDocumentEditReviewOperation",
    "ExactDocumentTargetAddress",
    "build_current_report_document_edit_plan",
    "validate_current_report_document_edit_plan",
]
