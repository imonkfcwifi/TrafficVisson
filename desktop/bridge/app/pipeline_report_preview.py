"""Durable report-preview preparation for the reviewed PipelineJob boundary.

The pipeline may call an official CLI provider only after every approved
current Incident is deterministically bound to the exact audited 3Q/4Q sample
layout and to verified deidentified evidence.  Provider output remains a
semantic draft/plan.  The full current-Incident binding manifest is the later
exact writer's authority.

This module never invokes HWP COM and never writes an HWP/HWPX document.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence
from uuid import UUID, uuid4

from .approved_incident_authority import (
    ApprovedIncidentAuthorityError,
    validate_approved_incident_output_authority,
)
from .contracts import DocumentEditPlan
from .current_incident_report_binder import (
    CurrentIncidentReportBindingManifest,
    bind_current_incidents_to_sample,
    current_incident_report_binding_from_dict,
)
from .hwpx_sample_layout import (
    HwpxSampleLayoutError,
    bind_sample_reference_case,
    inspect_sample_layout,
    preview_sample_layout_plan,
)
from .report_generation import (
    ReportGenerationError,
    ReportGenerationResult,
)
from .weak_case_catalog import default_sample_root


PIPELINE_REPORT_PREVIEW_SCHEMA = (
    "trafficvisson.pipeline-report-preview.v1"
)
PIPELINE_TEMPLATE_SEMANTIC_MAP_SCHEMA = (
    "trafficvisson.pipeline-template-semantic-map.v1"
)
MAX_PREVIEW_RECORD_BYTES = 16 * 1024 * 1024
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROVIDER_EVIDENCE_REF = re.compile(
    r"^(?:deid|evidence|analysis):"
    r"[A-Za-z0-9][A-Za-z0-9._:-]{0,117}$"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SAFE_TEMPLATE_IDS = {
    "sample-q3": (
        "sample-3q-v1",
        "sample-q3-semantic-v1",
        "3분기 점검 내용 정리.hwpx",
    ),
    "sample-3q-v1": (
        "sample-3q-v1",
        "sample-q3-semantic-v1",
        "3분기 점검 내용 정리.hwpx",
    ),
    "sample-q4": (
        "sample-4q-v1",
        "sample-q4-semantic-v1",
        "4분기 점검 내용 정리.hwpx",
    ),
    "sample-4q-v1": (
        "sample-4q-v1",
        "sample-q4-semantic-v1",
        "4분기 점검 내용 정리.hwpx",
    ),
}


class PipelineReportPreviewError(RuntimeError):
    """A bounded report-preview error safe to map to one pipeline code."""

    code = "report_preview_failed"
    provider_called = False
    com_invoked = False
    document_written = False


class PipelineReportProfileError(PipelineReportPreviewError):
    """The one-time project report profile did not pass server verification."""


class PipelineReportPreviewCorrupt(PipelineReportPreviewError):
    """A persisted preview record failed integrity verification."""


class ExactSamplePlanBindingUnavailable(PipelineReportPreviewError):
    """The exact audited layout could not be bound safely."""

    code = "exact_sample_plan_binding_unavailable"


class ProviderDeidentifiedEvidenceRequired(
    PipelineReportPreviewError
):
    """Provider transmission is blocked without verified deidentification."""

    code = "provider_deidentified_evidence_required"


class ProviderReportPreviewValidationError(
    PipelineReportPreviewError
):
    """The provider was called but its result did not pass local validation."""

    code = "provider_report_preview_validation_failed"
    provider_called = True


class PipelineReportAttemptConflict(PipelineReportPreviewError):
    """One report attempt cannot be reused with a different exact input."""

    code = "report_preview_attempt_conflict"


class ReportGenerator(Protocol):
    def generate(
        self,
        provider: str,
        payload: Mapping[str, Any],
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> ReportGenerationResult: ...


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_value(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(
        attributes
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _real_directory(path: Path, label: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise PipelineReportPreviewError(
            f"{label} is unavailable"
        ) from error
    if path.is_symlink() or _is_reparse_point(path) or not path.is_dir():
        raise PipelineReportPreviewError(
            f"{label} must be a real directory"
        )
    return resolved


def _write_atomic(path: Path, payload: bytes) -> None:
    if not 1 <= len(payload) <= MAX_PREVIEW_RECORD_BYTES:
        raise PipelineReportPreviewError(
            "report preview record exceeds its bound"
        )
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


@dataclass(frozen=True)
class SampleTemplateSemanticMap:
    requested_template_id: str
    canonical_template_id: str
    semantic_map_id: str
    semantic_map_sha256: str
    template_sha256: str
    document_snapshot_id: str
    structure_target_summary_sha256: str
    document_structure_summary: Mapping[str, Any]
    source_path: Path

    def profile_snapshot(self) -> dict[str, Any]:
        return {
            "state": "ready",
            "requested_template_id": self.requested_template_id,
            "canonical_template_id": self.canonical_template_id,
            "template_semantic_map_id": self.semantic_map_id,
            "template_semantic_map_sha256": self.semantic_map_sha256,
            "template_sha256": self.template_sha256,
            "document_snapshot_id": self.document_snapshot_id,
            "structure_target_summary_sha256": (
                self.structure_target_summary_sha256
            ),
            "path_exposed": False,
        }


@dataclass(frozen=True)
class ExactReportPreviewContext:
    """Integrity-checked server context for exact draft preparation."""

    preview_id: str
    project_id: UUID
    pipeline_job_id: UUID
    analysis_job_id: UUID
    source_path: Path
    binding: CurrentIncidentReportBindingManifest
    record_sha256: str


class SampleTemplateSemanticMapRegistry:
    """Resolve only the two audited 3Q/4Q sample HWPX templates."""

    def __init__(self, sample_root: Optional[Path] = None) -> None:
        root = Path(sample_root or default_sample_root())
        if not root.is_absolute():
            raise ValueError("sample semantic-map root must be absolute")
        self._root = root.resolve()
        self._cache: dict[
            tuple[str, str], SampleTemplateSemanticMap
        ] = {}
        self._lock = threading.RLock()

    def resolve(
        self,
        *,
        template_id: str,
        template_sha256: str,
        semantic_map_id: Optional[str] = None,
    ) -> SampleTemplateSemanticMap:
        definition = _SAFE_TEMPLATE_IDS.get(template_id)
        if (
            definition is None
            or _SHA256.fullmatch(template_sha256) is None
        ):
            raise PipelineReportProfileError(
                "the selected sample template identity is invalid"
            )
        canonical_id, expected_map_id, file_name = definition
        if (
            semantic_map_id is not None
            and semantic_map_id != expected_map_id
        ):
            raise PipelineReportProfileError(
                "the selected semantic-map identity is invalid"
            )
        key = (canonical_id, template_sha256)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                try:
                    if (
                        cached.source_path.is_file()
                        and not cached.source_path.is_symlink()
                        and not _is_reparse_point(cached.source_path)
                        and _sha256_file(cached.source_path)
                        == template_sha256
                    ):
                        return cached
                except OSError:
                    pass
                self._cache.pop(key, None)

            source_path = (self._root / file_name).resolve()
            if (
                source_path.parent != self._root
                or source_path.name != file_name
                or not source_path.is_file()
                or source_path.is_symlink()
                or _is_reparse_point(source_path)
            ):
                raise PipelineReportProfileError(
                    "the selected sample template is unavailable"
                )
            try:
                inspection = inspect_sample_layout(source_path)
            except HwpxSampleLayoutError as error:
                raise PipelineReportProfileError(
                    "the selected sample semantic map is unavailable"
                ) from error
            if (
                inspection.baseline.template_id != canonical_id
                or inspection.snapshot.document_sha256
                != template_sha256
                or inspection.baseline.rendered_page_count != 48
            ):
                raise PipelineReportProfileError(
                    "the selected sample template provenance does not match"
                )

            cell_targets: list[str] = []
            image_targets: list[str] = []
            seen_cells: set[str] = set()
            seen_images: set[str] = set()
            for picture in inspection.snapshot.pictures:
                if (
                    picture.nearest_table_index is None
                    or picture.nearest_table_index % 3 != 2
                ):
                    continue
                try:
                    binding = bind_sample_reference_case(
                        inspection, picture.target_id
                    )
                except HwpxSampleLayoutError:
                    continue
                if binding.picture_target_id not in seen_images:
                    seen_images.add(binding.picture_target_id)
                    image_targets.append(binding.picture_target_id)
                for target in (
                    binding.photo_caption.cell_target_id,
                    binding.inspection_route.cell_target_id,
                    binding.inspection_opinion_body.cell_target_id,
                    *(
                        marker.cell_target_id
                        for marker in binding.square_markers
                    ),
                ):
                    if target not in seen_cells:
                        seen_cells.add(target)
                        cell_targets.append(target)
            if not cell_targets or not image_targets:
                raise PipelineReportProfileError(
                    "the selected sample semantic map has no report targets"
                )

            structure = {
                "document_snapshot_id": (
                    inspection.document_snapshot_id
                ),
                "field_targets": [],
                "cell_targets": cell_targets,
                "table_targets": [],
                "image_targets": image_targets,
                "map_targets": [],
                "section_headings": [
                    "민자고속도로 기술부문 실사 점검",
                    "점검 결과",
                    "사진대지",
                ],
            }
            semantic_payload = {
                "schema_version": (
                    PIPELINE_TEMPLATE_SEMANTIC_MAP_SCHEMA
                ),
                "canonical_template_id": canonical_id,
                "semantic_map_id": expected_map_id,
                "template_sha256": template_sha256,
                "document_snapshot_id": (
                    inspection.document_snapshot_id
                ),
                "structure_target_summary_sha256": (
                    inspection.snapshot.target_summary_sha256
                ),
                "document_structure_summary": structure,
                "expected_rendered_page_count": 48,
                "table_growth_allowed": False,
                "style_policy": "preserve",
            }
            resolved = SampleTemplateSemanticMap(
                requested_template_id=template_id,
                canonical_template_id=canonical_id,
                semantic_map_id=expected_map_id,
                semantic_map_sha256=_sha256_value(semantic_payload),
                template_sha256=template_sha256,
                document_snapshot_id=inspection.document_snapshot_id,
                structure_target_summary_sha256=(
                    inspection.snapshot.target_summary_sha256
                ),
                document_structure_summary=structure,
                source_path=source_path,
            )
            self._cache[key] = resolved
            return resolved


class DurablePipelineReportPreviewPreparer:
    """Generate and persist one structurally validated preview, without COM."""

    def __init__(
        self,
        *,
        data_root: Path,
        semantic_maps: SampleTemplateSemanticMapRegistry,
        report_generator: ReportGenerator,
    ) -> None:
        root = Path(data_root)
        if not root.is_absolute():
            raise ValueError("report preview data_root must be absolute")
        self._root = _real_directory(
            _real_directory(root, "report preview data root")
            / "report-previews",
            "report preview store",
        )
        self._semantic_maps = semantic_maps
        self._report_generator = report_generator
        self._lock = threading.RLock()

    def prepare(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        source_sha256: str,
        provider: str,
        template_id: str,
        template_sha256: str,
        template_semantic_map_id: str,
        template_semantic_map_sha256: str,
        approved_incidents: Sequence[Mapping[str, Any]],
        evidence_by_incident_id: Mapping[
            str, Mapping[str, Any]
        ],
        route_name: str,
        report_inspector_name: str,
        inspection_date: str,
        locations_by_incident_id: Mapping[str, str],
        report_policy: Mapping[str, Any],
        explicit_transmission_consent: bool,
        report_attempt: int,
    ) -> Mapping[str, Any]:
        evidence_mode = report_policy.get("evidence_mode")
        if evidence_mode not in {
            "internal_original",
            "shared_deidentified",
        }:
            raise PipelineReportPreviewError(
                "the report evidence mode is invalid"
            )
        if (
            evidence_mode == "shared_deidentified"
            and explicit_transmission_consent is not True
        ):
            raise PipelineReportPreviewError(
                "explicit report transmission consent is required"
            )
        if (
            evidence_mode == "shared_deidentified"
            and provider not in {"codex", "claude"}
        ):
            raise PipelineReportPreviewError(
                "the selected report provider is unavailable"
            )
        if (
            _SHA256.fullmatch(source_sha256) is None
            or _SHA256.fullmatch(template_semantic_map_sha256) is None
            or isinstance(report_attempt, bool)
            or not isinstance(report_attempt, int)
            or not 0 <= report_attempt <= 64
        ):
            raise PipelineReportPreviewError(
                "report preview provenance is invalid"
            )
        semantic_map = self._semantic_maps.resolve(
            template_id=template_id,
            template_sha256=template_sha256,
            semantic_map_id=template_semantic_map_id,
        )
        if (
            semantic_map.semantic_map_sha256
            != template_semantic_map_sha256
        ):
            raise PipelineReportPreviewError(
                "the sample semantic map changed after project setup"
            )
        normalized_incidents = self._normalize_incidents(
            approved_incidents
        )
        try:
            inspection = inspect_sample_layout(
                semantic_map.source_path
            )
        except HwpxSampleLayoutError as error:
            raise ExactSamplePlanBindingUnavailable(
                "exact_sample_plan_binding_unavailable"
            ) from error
        binding = bind_current_incidents_to_sample(
            semantic_map=semantic_map,
            inspection=inspection,
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=analysis_job_id,
            source_sha256=source_sha256,
            approved_incidents=normalized_incidents,
            evidence_by_incident_id=evidence_by_incident_id,
            route_name=route_name,
            report_inspector_name=report_inspector_name,
            inspection_date=inspection_date,
            locations_by_incident_id=locations_by_incident_id,
            report_policy=report_policy,
        )
        self._validate_binding(
            binding=binding,
            semantic_map=semantic_map,
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=analysis_job_id,
            source_sha256=source_sha256,
            incident_ids=tuple(
                item["incident_id"]
                for item in normalized_incidents
            ),
        )
        if evidence_mode == "internal_original":
            return self._prepare_internal_binding_preview(
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                analysis_job_id=analysis_job_id,
                source_sha256=source_sha256,
                semantic_map=semantic_map,
                binding=binding,
                report_attempt=report_attempt,
            )

        provider_incidents = self._provider_incidents(
            incidents=normalized_incidents,
            binding=binding,
            locations_by_incident_id=locations_by_incident_id,
        )
        provider_structure = self._provider_structure(
            binding=binding,
            semantic_map=semantic_map,
        )
        provider_payload = {
            "approved_incidents": provider_incidents,
            "document_structure_summary": provider_structure,
            "map_refs": [],
        }
        binding_payload = binding.to_dict()
        exact_input = {
            "schema_version": PIPELINE_REPORT_PREVIEW_SCHEMA,
            "project_id": str(project_id),
            "pipeline_job_id": str(pipeline_job_id),
            "analysis_job_id": str(analysis_job_id),
            "source_sha256": source_sha256,
            "provider": provider,
            "template_id": semantic_map.canonical_template_id,
            "template_sha256": semantic_map.template_sha256,
            "template_semantic_map_id": semantic_map.semantic_map_id,
            "template_semantic_map_sha256": (
                semantic_map.semantic_map_sha256
            ),
            "document_snapshot_id": (
                semantic_map.document_snapshot_id
            ),
            "report_attempt": report_attempt,
            "provider_payload": provider_payload,
            "current_binding_manifest": binding_payload,
        }
        input_sha256 = _sha256_value(exact_input)
        preview_id = f"report-preview-attempt-{report_attempt}"
        record_path = self._record_path(
            project_id, pipeline_job_id, preview_id
        )

        with self._lock:
            if record_path.exists():
                record = self._load_record(record_path)
                if (
                    record.get("preview_id") != preview_id
                    or record.get("report_attempt") != report_attempt
                ):
                    raise PipelineReportPreviewCorrupt(
                        "persisted report preview identity is invalid"
                    )
                if record.get("input_sha256") != input_sha256:
                    raise PipelineReportAttemptConflict(
                        "report_preview_attempt_conflict"
                    )
                return self._metadata(record)

            try:
                generated = self._report_generator.generate(
                    provider,
                    provider_payload,
                )
                (
                    draft,
                    plan,
                    plan_payload,
                ) = self._validate_generation(
                    generated=generated,
                    requested_provider=provider,
                    approved_incident_ids=frozenset(
                        item["incident_id"]
                        for item in provider_incidents
                    ),
                    provider_structure=provider_structure,
                    document_snapshot_id=(
                        semantic_map.document_snapshot_id
                    ),
                )
                layout_preview = preview_sample_layout_plan(
                    semantic_map.source_path,
                    plan,
                )
            except (
                ReportGenerationError,
                HwpxSampleLayoutError,
                TypeError,
                ValueError,
            ) as error:
                raise ProviderReportPreviewValidationError(
                    "provider_report_preview_validation_failed"
                ) from error

            draft_sha256 = _sha256_value(draft)
            semantic_plan_sha256 = _sha256_value(plan_payload)
            provenance = {
                "project_id": str(project_id),
                "pipeline_job_id": str(pipeline_job_id),
                "analysis_job_id": str(analysis_job_id),
                "source_sha256": source_sha256,
                "provider": provider,
                "template_id": semantic_map.canonical_template_id,
                "template_sha256": semantic_map.template_sha256,
                "template_semantic_map_id": (
                    semantic_map.semantic_map_id
                ),
                "template_semantic_map_sha256": (
                    semantic_map.semantic_map_sha256
                ),
                "document_snapshot_id": (
                    semantic_map.document_snapshot_id
                ),
                "report_attempt": report_attempt,
                "input_sha256": input_sha256,
                "current_binding_manifest_sha256": (
                    binding.binding_manifest_sha256
                ),
            }
            record_without_hash = {
                "schema_version": PIPELINE_REPORT_PREVIEW_SCHEMA,
                "state": (
                    "semantic_preview_ready_exact_writer_required"
                ),
                "preview_id": preview_id,
                "report_attempt": report_attempt,
                "input_sha256": input_sha256,
                "provenance": provenance,
                "current_binding_manifest": binding_payload,
                "current_binding_manifest_sha256": (
                    binding.binding_manifest_sha256
                ),
                "draft": draft,
                "draft_sha256": draft_sha256,
                "semantic_provider_plan": plan_payload,
                "semantic_provider_plan_sha256": (
                    semantic_plan_sha256
                ),
                # Compatibility alias for current derived-output metadata.
                "plan_sha256": semantic_plan_sha256,
                "operation_count": len(plan.operations),
                "target_layout_contract_sha256": (
                    layout_preview.layout_contract_sha256
                ),
                "expected_rendered_page_count": (
                    layout_preview.expected_rendered_page_count
                ),
                "provider_called": True,
                "exact_writer_required": True,
                "exact_writer_completed": False,
                "render_validation_completed": False,
                "com_invoked": False,
                "document_written": False,
                "apply_eligible": False,
            }
            record = {
                **record_without_hash,
                "record_sha256": _sha256_value(record_without_hash),
            }
            _write_atomic(record_path, _canonical_json(record))
            return self._metadata(record)

    def _prepare_internal_binding_preview(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        source_sha256: str,
        semantic_map: SampleTemplateSemanticMap,
        binding: CurrentIncidentReportBindingManifest,
        report_attempt: int,
    ) -> Mapping[str, Any]:
        """Persist deterministic binding authority without Provider prose."""

        exact_input = {
            "schema_version": PIPELINE_REPORT_PREVIEW_SCHEMA,
            "mode": "internal_original_binding_only",
            "project_id": str(project_id),
            "pipeline_job_id": str(pipeline_job_id),
            "analysis_job_id": str(analysis_job_id),
            "source_sha256": source_sha256,
            "template_id": semantic_map.canonical_template_id,
            "template_sha256": semantic_map.template_sha256,
            "template_semantic_map_id": semantic_map.semantic_map_id,
            "template_semantic_map_sha256": (
                semantic_map.semantic_map_sha256
            ),
            "document_snapshot_id": (
                semantic_map.document_snapshot_id
            ),
            "report_attempt": report_attempt,
            "current_binding_manifest": binding.to_dict(),
        }
        input_sha256 = _sha256_value(exact_input)
        preview_id = f"report-preview-attempt-{report_attempt}"
        record_path = self._record_path(
            project_id,
            pipeline_job_id,
            preview_id,
        )
        with self._lock:
            if record_path.exists():
                record = self._load_record(record_path)
                if (
                    record.get("preview_id") != preview_id
                    or record.get("report_attempt") != report_attempt
                ):
                    raise PipelineReportPreviewCorrupt(
                        "persisted report preview identity is invalid"
                    )
                if record.get("input_sha256") != input_sha256:
                    raise PipelineReportAttemptConflict(
                        "report_preview_attempt_conflict"
                    )
                return self._metadata(record)

            binding_payload = binding.to_dict()
            deterministic_draft = {
                "schema_version": (
                    "trafficvisson.current-binding-authority.v1"
                ),
                "provider_prose_used": False,
                "approved_incident_ids": list(
                    binding.approved_incident_ids
                ),
                "content_authority": (
                    "approved_action_location_ocr_binding"
                ),
            }
            deterministic_plan = {
                "schema_version": (
                    "trafficvisson.current-binding-authority.v1"
                ),
                "binding_manifest_sha256": (
                    binding.binding_manifest_sha256
                ),
                "required_writer_operation_count": (
                    binding.required_writer_operation_count
                ),
            }
            provenance = {
                "project_id": str(project_id),
                "pipeline_job_id": str(pipeline_job_id),
                "analysis_job_id": str(analysis_job_id),
                "source_sha256": source_sha256,
                "provider": "not_called",
                "template_id": semantic_map.canonical_template_id,
                "template_sha256": semantic_map.template_sha256,
                "template_semantic_map_id": (
                    semantic_map.semantic_map_id
                ),
                "template_semantic_map_sha256": (
                    semantic_map.semantic_map_sha256
                ),
                "document_snapshot_id": (
                    semantic_map.document_snapshot_id
                ),
                "report_attempt": report_attempt,
                "input_sha256": input_sha256,
                "current_binding_manifest_sha256": (
                    binding.binding_manifest_sha256
                ),
            }
            plan_sha256 = _sha256_value(deterministic_plan)
            record_without_hash = {
                "schema_version": PIPELINE_REPORT_PREVIEW_SCHEMA,
                "state": (
                    "semantic_preview_ready_exact_writer_required"
                ),
                "preview_id": preview_id,
                "report_attempt": report_attempt,
                "input_sha256": input_sha256,
                "provenance": provenance,
                "current_binding_manifest": binding_payload,
                "current_binding_manifest_sha256": (
                    binding.binding_manifest_sha256
                ),
                "draft": deterministic_draft,
                "draft_sha256": _sha256_value(
                    deterministic_draft
                ),
                "semantic_provider_plan": deterministic_plan,
                "semantic_provider_plan_sha256": plan_sha256,
                "plan_sha256": plan_sha256,
                "operation_count": (
                    binding.required_writer_operation_count
                ),
                "target_layout_contract_sha256": (
                    binding.structure_target_summary_sha256
                ),
                "expected_rendered_page_count": 48,
                "provider_called": False,
                "exact_writer_required": True,
                "exact_writer_completed": False,
                "render_validation_completed": False,
                "com_invoked": False,
                "document_written": False,
                "apply_eligible": False,
            }
            record = {
                **record_without_hash,
                "record_sha256": _sha256_value(
                    record_without_hash
                ),
            }
            _write_atomic(record_path, _canonical_json(record))
            return self._metadata(record)

    def load_exact_context(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        preview_id: str,
    ) -> ExactReportPreviewContext:
        """Load only a complete, hash-bound exact-writer preview context."""

        record = self._load_record(
            self._record_path(
                project_id,
                pipeline_job_id,
                preview_id,
            )
        )
        provenance = record.get("provenance")
        if not isinstance(provenance, Mapping):
            raise PipelineReportPreviewCorrupt(
                "persisted report preview provenance is invalid"
            )
        try:
            analysis_job_id = UUID(str(provenance["analysis_job_id"]))
            if (
                record.get("preview_id") != preview_id
                or record.get("state")
                != "semantic_preview_ready_exact_writer_required"
                or record.get("exact_writer_required") is not True
                or record.get("exact_writer_completed") is not False
                or record.get("render_validation_completed") is not False
                or record.get("com_invoked") is not False
                or record.get("document_written") is not False
                or record.get("apply_eligible") is not False
                or provenance.get("project_id") != str(project_id)
                or provenance.get("pipeline_job_id")
                != str(pipeline_job_id)
                or record.get("current_binding_manifest_sha256")
                != provenance.get(
                    "current_binding_manifest_sha256"
                )
            ):
                raise ValueError("preview lifecycle state is invalid")
            binding = current_incident_report_binding_from_dict(
                record["current_binding_manifest"]
            )
            semantic_map = self._semantic_maps.resolve(
                template_id=str(provenance["template_id"]),
                template_sha256=str(provenance["template_sha256"]),
                semantic_map_id=str(
                    provenance["template_semantic_map_id"]
                ),
            )
            self._validate_binding(
                binding=binding,
                semantic_map=semantic_map,
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                analysis_job_id=analysis_job_id,
                source_sha256=str(provenance["source_sha256"]),
                incident_ids=binding.approved_incident_ids,
            )
            if (
                binding.binding_manifest_sha256
                != record["current_binding_manifest_sha256"]
            ):
                raise ValueError("binding digest is invalid")
        except (
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            raise PipelineReportPreviewCorrupt(
                "persisted exact report preview failed integrity verification"
            ) from error
        return ExactReportPreviewContext(
            preview_id=preview_id,
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=analysis_job_id,
            source_path=semantic_map.source_path,
            binding=binding,
            record_sha256=str(record["record_sha256"]),
        )

    @staticmethod
    def _normalize_incidents(
        incidents: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        if (
            isinstance(incidents, (str, bytes, bytearray, Mapping))
            or not isinstance(incidents, Sequence)
            or not 1 <= len(incidents) <= 200
        ):
            raise PipelineReportPreviewError(
                "approved report incidents are invalid"
            )
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        allowed_fields = {
            "incident_id",
            "project_id",
            "pipeline_job_id",
            "analysis_job_id",
            "source_sha256",
            "review_status",
            "approval_actor",
            "approval_record_id",
            "approval_record_sha256",
            "incident_type",
            "severity",
            "maintenance_action",
            "ocr_text",
            "time_ms",
            "evidence_refs",
            "approved_output_authority",
        }
        for incident in incidents:
            if (
                not isinstance(incident, Mapping)
                or set(incident) != allowed_fields
            ):
                raise PipelineReportPreviewError(
                    "approved report incidents are invalid"
                )
            incident_id = str(incident.get("incident_id", ""))
            if (
                incident_id in seen
                or not incident_id
                or len(incident_id) > 128
                or _IDENTIFIER.fullmatch(incident_id) is None
            ):
                raise PipelineReportPreviewError(
                    "approved report incident identity is invalid"
                )
            seen.add(incident_id)
            incident_type = incident.get("incident_type")
            severity = incident.get("severity")
            maintenance_action = incident.get(
                "maintenance_action"
            )
            ocr_text = incident.get("ocr_text")
            time_ms = incident.get("time_ms")
            if (
                incident.get("review_status") != "approved"
                or incident.get("approval_actor") != "local_user"
                or not isinstance(incident.get("project_id"), str)
                or not isinstance(incident.get("pipeline_job_id"), str)
                or not isinstance(incident.get("analysis_job_id"), str)
                or not isinstance(incident.get("source_sha256"), str)
                or _SHA256.fullmatch(incident["source_sha256"]) is None
                or not isinstance(
                    incident.get("approval_record_id"), str
                )
                or _IDENTIFIER.fullmatch(
                    incident["approval_record_id"]
                )
                is None
                or not isinstance(
                    incident.get("approval_record_sha256"), str
                )
                or _SHA256.fullmatch(
                    incident["approval_record_sha256"]
                )
                is None
                or not isinstance(incident_type, str)
                or _IDENTIFIER.fullmatch(incident_type) is None
                or severity
                not in {
                    "minor",
                    "moderate",
                    "major",
                    "urgent",
                    "unassessed",
                }
                or (
                    maintenance_action is not None
                    and (
                        not isinstance(maintenance_action, str)
                        or not 1 <= len(maintenance_action) <= 1_000
                    )
                )
                or (
                    ocr_text is not None
                    and (
                        not isinstance(ocr_text, str)
                        or not 1 <= len(ocr_text) <= 1_000
                    )
                )
                or isinstance(time_ms, bool)
                or not isinstance(time_ms, int)
                or not 0 <= time_ms <= 2_678_400_000
                or not isinstance(
                    incident.get("evidence_refs"), list
                )
                or len(incident["evidence_refs"]) != 1
            ):
                raise PipelineReportPreviewError(
                    "approved report incident content is invalid"
                )
            evidence_refs = incident["evidence_refs"]
            if (
                not isinstance(evidence_refs[0], str)
                or _IDENTIFIER.fullmatch(evidence_refs[0]) is None
            ):
                raise PipelineReportPreviewError(
                    "approved report incident evidence is invalid"
                )
            try:
                validate_approved_incident_output_authority(
                    incident["approved_output_authority"],
                    project_id=UUID(incident["project_id"]),
                    pipeline_job_id=UUID(
                        incident["pipeline_job_id"]
                    ),
                    analysis_job_id=UUID(
                        incident["analysis_job_id"]
                    ),
                    source_sha256=incident["source_sha256"],
                    incident_id=incident_id,
                    approval_record_id=incident[
                        "approval_record_id"
                    ],
                    approval_record_sha256=incident[
                        "approval_record_sha256"
                    ],
                    representative_image_ref=evidence_refs[0],
                )
            except (
                ApprovedIncidentAuthorityError,
                TypeError,
                ValueError,
                AttributeError,
            ) as error:
                raise PipelineReportPreviewError(
                    "approved report Incident authority is invalid"
                ) from error
            # The opaque approved evidence reference is retained only so the
            # deterministic binder can prove that the server-resolved image
            # descriptor belongs to this exact answer record.  Local paths
            # and bytes are never accepted here or sent to a Provider.
            normalized.append(
                {
                    "incident_id": incident_id,
                    "project_id": incident["project_id"],
                    "pipeline_job_id": incident["pipeline_job_id"],
                    "analysis_job_id": incident["analysis_job_id"],
                    "source_sha256": incident["source_sha256"],
                    "review_status": "approved",
                    "approval_actor": "local_user",
                    "approval_record_id": incident[
                        "approval_record_id"
                    ],
                    "approval_record_sha256": incident[
                        "approval_record_sha256"
                    ],
                    "incident_type": incident_type,
                    "severity": severity,
                    "maintenance_action": maintenance_action,
                    "ocr_text": ocr_text,
                    "time_ms": time_ms,
                    "evidence_refs": list(evidence_refs),
                    "approved_output_authority": dict(
                        incident["approved_output_authority"]
                    ),
                }
            )
        return normalized

    @staticmethod
    def _validate_binding(
        *,
        binding: CurrentIncidentReportBindingManifest,
        semantic_map: SampleTemplateSemanticMap,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        source_sha256: str,
        incident_ids: tuple[str, ...],
    ) -> None:
        if (
            not isinstance(
                binding, CurrentIncidentReportBindingManifest
            )
            or binding.project_id != str(project_id)
            or binding.pipeline_job_id != str(pipeline_job_id)
            or binding.analysis_job_id != str(analysis_job_id)
            or binding.source_sha256 != source_sha256
            or binding.template_id
            != semantic_map.canonical_template_id
            or binding.template_sha256
            != semantic_map.template_sha256
            or binding.template_semantic_map_sha256
            != semantic_map.semantic_map_sha256
            or binding.document_snapshot_id
            != semantic_map.document_snapshot_id
            or binding.structure_target_summary_sha256
            != semantic_map.structure_target_summary_sha256
            or binding.approved_incident_ids != incident_ids
            or binding.approved_incident_count != len(incident_ids)
            or binding.state
            != "current_incidents_bound_writer_required"
            or binding.approved_incident_bound is not True
            or binding.content_provenance_bound is not True
            or binding.writer_required is not True
            or binding.apply_eligible is not False
            or binding.provider_called is not False
            or binding.com_invoked is not False
            or binding.document_written is not False
            or binding.writer_batching_required is not False
            or binding.required_writer_operation_count
            > binding.exact_hwpx_writer_operation_limit
            or _SHA256.fullmatch(binding.input_sha256) is None
            or _SHA256.fullmatch(binding.content_sha256) is None
            or _SHA256.fullmatch(binding.slot_manifest_sha256) is None
            or _SHA256.fullmatch(
                binding.survey_incident_authority_sha256
            )
            is None
            or _SHA256.fullmatch(
                binding.approved_incident_authority_set_sha256
            )
            is None
            or _SHA256.fullmatch(
                binding.binding_manifest_sha256
            )
            is None
        ):
            raise ExactSamplePlanBindingUnavailable(
                "exact_sample_plan_binding_unavailable"
            )
        bound_ids = tuple(
            item.incident_id for item in binding.incident_bindings
        )
        if (
            bound_ids != incident_ids
            or len(bound_ids) != len(set(bound_ids))
        ):
            raise ExactSamplePlanBindingUnavailable(
                "exact_sample_plan_binding_unavailable"
            )
        try:
            encoded = _canonical_json(binding.to_dict())
        except (TypeError, ValueError) as error:
            raise ExactSamplePlanBindingUnavailable(
                "exact_sample_plan_binding_unavailable"
            ) from error
        if len(encoded) > MAX_PREVIEW_RECORD_BYTES:
            raise ExactSamplePlanBindingUnavailable(
                "exact_sample_plan_binding_unavailable"
            )

    @staticmethod
    def _provider_incidents(
        *,
        incidents: Sequence[Mapping[str, Any]],
        binding: CurrentIncidentReportBindingManifest,
        locations_by_incident_id: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        bindings = {
            item.incident_id: item
            for item in binding.incident_bindings
        }
        result: list[dict[str, Any]] = []
        for incident in incidents:
            incident_id = incident["incident_id"]
            selected = bindings.get(incident_id)
            location = locations_by_incident_id.get(incident_id)
            if (
                selected is None
                or selected.evidence_policy
                != "shared_deidentified"
                or _PROVIDER_EVIDENCE_REF.fullmatch(
                    selected.selected_evidence_ref
                )
                is None
                or not isinstance(location, str)
                or not 1 <= len(location) <= 500
                or location != location.strip()
                or _CONTROL.search(location) is not None
            ):
                raise ProviderDeidentifiedEvidenceRequired(
                    "provider_deidentified_evidence_required"
                )
            item = {
                "incident_id": incident_id,
                "incident_type": incident["incident_type"],
                "severity": incident["severity"],
                "maintenance_action": incident[
                    "maintenance_action"
                ],
                "ocr_text": incident["ocr_text"],
                "location_summary": location,
                "time_ms": incident["time_ms"],
                "evidence_refs": [
                    selected.selected_evidence_ref
                ],
            }
            result.append(item)
        return result

    @staticmethod
    def _provider_structure(
        *,
        binding: CurrentIncidentReportBindingManifest,
        semantic_map: SampleTemplateSemanticMap,
    ) -> dict[str, Any]:
        # Route/date/caption/photo placement and stale-content removal are
        # deterministic writer work.  The LLM may only propose prose for the
        # exact occupied inspection-opinion cells selected by the binder.
        cell_targets = tuple(
            item.target_id
            for item in binding.replacements
            if item.role == "inspection_opinion"
            and item.operation == "replace_text"
        )
        allowed_cells = set(
            semantic_map.document_structure_summary["cell_targets"]
        )
        if (
            not 1 <= len(cell_targets) <= 100
            or len(cell_targets) != len(set(cell_targets))
            or not set(cell_targets).issubset(allowed_cells)
        ):
            raise ExactSamplePlanBindingUnavailable(
                "exact_sample_plan_binding_unavailable"
            )
        return {
            "document_snapshot_id": (
                semantic_map.document_snapshot_id
            ),
            "field_targets": [],
            "cell_targets": list(cell_targets),
            "table_targets": [],
            "image_targets": [],
            "map_targets": [],
            "section_headings": list(
                semantic_map.document_structure_summary.get(
                    "section_headings", ()
                )
            ),
        }

    @staticmethod
    def _validate_generation(
        *,
        generated: ReportGenerationResult,
        requested_provider: str,
        approved_incident_ids: frozenset[str],
        provider_structure: Mapping[str, Any],
        document_snapshot_id: str,
    ) -> tuple[dict[str, Any], DocumentEditPlan, dict[str, Any]]:
        if (
            not isinstance(generated, ReportGenerationResult)
            or generated.provider != requested_provider
            or not isinstance(generated.draft, Mapping)
            or not isinstance(generated.plan, Mapping)
        ):
            raise ValueError("provider result contract is invalid")
        draft = (
            DurablePipelineReportPreviewPreparer._validate_draft(
                generated.draft,
                approved_incident_ids=approved_incident_ids,
            )
        )
        plan = DocumentEditPlan.model_validate(generated.plan)
        if plan.document_snapshot_id != document_snapshot_id:
            raise ValueError(
                "provider plan crossed the template snapshot"
            )
        cell_targets = frozenset(provider_structure["cell_targets"])
        seen_targets: set[str] = set()
        allowed_sources = {
            *(f"incident:{item}" for item in approved_incident_ids),
            *(
                f"draft:section:{index}"
                for index in range(len(draft["sections"]))
            ),
            "summary:approved-incidents",
        }
        for operation in plan.operations:
            if (
                operation.type != "fill_cell"
                or operation.target not in cell_targets
                or operation.target in seen_targets
                or operation.source_ref not in allowed_sources
            ):
                raise ValueError(
                    "provider plan is outside the bound semantic targets"
                )
            seen_targets.add(operation.target)
        if seen_targets != cell_targets:
            raise ValueError(
                "provider plan omitted a bound semantic target"
            )
        return (
            draft,
            plan,
            plan.model_dump(mode="json"),
        )

    @staticmethod
    def _validate_draft(
        value: Mapping[str, Any],
        *,
        approved_incident_ids: frozenset[str],
    ) -> dict[str, Any]:
        if set(value) != {"title", "sections"}:
            raise ValueError("provider draft contract is invalid")
        title = value["title"]
        sections = value["sections"]
        if (
            not isinstance(title, str)
            or not 1 <= len(title.strip()) <= 300
            or _CONTROL.search(title) is not None
            or not isinstance(sections, list)
            or not 1 <= len(sections) <= 30
        ):
            raise ValueError("provider draft contract is invalid")
        normalized_sections = []
        referenced: set[str] = set()
        for section in sections:
            if (
                not isinstance(section, Mapping)
                or set(section)
                != {"heading", "body", "incident_ids"}
            ):
                raise ValueError(
                    "provider draft section is invalid"
                )
            heading = section["heading"]
            body = section["body"]
            incident_ids = section["incident_ids"]
            if (
                not isinstance(heading, str)
                or not 1 <= len(heading.strip()) <= 300
                or _CONTROL.search(heading) is not None
                or not isinstance(body, str)
                or not 1 <= len(body.strip()) <= 12_000
                or _CONTROL.search(body) is not None
                or not isinstance(incident_ids, list)
                or len(incident_ids) > 200
                or len(incident_ids) != len(set(incident_ids))
                or not set(incident_ids).issubset(
                    approved_incident_ids
                )
            ):
                raise ValueError(
                    "provider draft section is invalid"
                )
            referenced.update(incident_ids)
            normalized_sections.append(
                {
                    "heading": heading.strip(),
                    "body": body.strip(),
                    "incident_ids": list(incident_ids),
                }
            )
        if referenced != set(approved_incident_ids):
            raise ValueError(
                "provider draft omitted an approved Incident"
            )
        return {
            "title": title.strip(),
            "sections": normalized_sections,
        }

    def _record_path(
        self,
        project_id: UUID,
        pipeline_job_id: UUID,
        preview_id: str,
    ) -> Path:
        if _IDENTIFIER.fullmatch(preview_id) is None:
            raise PipelineReportPreviewError(
                "report preview identity is invalid"
            )
        project_root = _real_directory(
            self._root / str(project_id),
            "report preview project root",
        )
        job_root = _real_directory(
            project_root / str(pipeline_job_id),
            "report preview job root",
        )
        return job_root / f"{preview_id}.json"

    @staticmethod
    def _load_record(path: Path) -> dict[str, Any]:
        try:
            if path.is_symlink() or _is_reparse_point(path):
                raise ValueError("preview record is a link")
            raw = path.read_bytes()
            if not 1 <= len(raw) <= MAX_PREVIEW_RECORD_BYTES:
                raise ValueError("preview record size is invalid")
            record = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise PipelineReportPreviewCorrupt(
                "persisted report preview is unavailable"
            ) from error
        if not isinstance(record, dict):
            raise PipelineReportPreviewCorrupt(
                "persisted report preview is invalid"
            )
        supplied = record.pop("record_sha256", None)
        calculated = _sha256_value(record)
        record["record_sha256"] = supplied
        if (
            not isinstance(supplied, str)
            or supplied != calculated
            or record.get("schema_version")
            != PIPELINE_REPORT_PREVIEW_SCHEMA
        ):
            raise PipelineReportPreviewCorrupt(
                "persisted report preview failed integrity verification"
            )
        return record

    @staticmethod
    def _metadata(record: Mapping[str, Any]) -> dict[str, Any]:
        binding = record["current_binding_manifest"]
        return {
            "preview_id": record["preview_id"],
            "state": record["state"],
            "input_sha256": record["input_sha256"],
            "report_attempt": record["report_attempt"],
            "plan_sha256": record["plan_sha256"],
            "semantic_provider_plan_sha256": record[
                "semantic_provider_plan_sha256"
            ],
            "draft_sha256": record["draft_sha256"],
            "operation_count": record["operation_count"],
            "current_binding_manifest_sha256": record[
                "current_binding_manifest_sha256"
            ],
            "binding": {
                "state": binding["state"],
                "template_id": binding["template_id"],
                "document_snapshot_id": binding[
                    "document_snapshot_id"
                ],
                "approved_incident_count": binding[
                    "approved_incident_count"
                ],
                "required_writer_operation_count": binding[
                    "required_writer_operation_count"
                ],
                "exact_hwpx_writer_operation_limit": binding[
                    "exact_hwpx_writer_operation_limit"
                ],
                "approved_incident_bound": binding[
                    "approved_incident_bound"
                ],
                "content_provenance_bound": binding[
                    "content_provenance_bound"
                ],
                "writer_required": binding["writer_required"],
            },
            "provider_called": record["provider_called"],
            "exact_writer_required": record[
                "exact_writer_required"
            ],
            "exact_writer_completed": record[
                "exact_writer_completed"
            ],
            "render_validation_completed": record[
                "render_validation_completed"
            ],
            "com_invoked": record["com_invoked"],
            "document_written": record["document_written"],
            "apply_eligible": record["apply_eligible"],
        }


__all__ = [
    "DurablePipelineReportPreviewPreparer",
    "ExactReportPreviewContext",
    "ExactSamplePlanBindingUnavailable",
    "PIPELINE_REPORT_PREVIEW_SCHEMA",
    "PIPELINE_TEMPLATE_SEMANTIC_MAP_SCHEMA",
    "PipelineReportAttemptConflict",
    "PipelineReportPreviewCorrupt",
    "PipelineReportPreviewError",
    "PipelineReportProfileError",
    "ProviderDeidentifiedEvidenceRequired",
    "ProviderReportPreviewValidationError",
    "SampleTemplateSemanticMap",
    "SampleTemplateSemanticMapRegistry",
]
