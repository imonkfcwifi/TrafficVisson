"""Durable report-preview preparation for the reviewed PipelineJob boundary.

The pipeline may call an official CLI provider only when the project profile
contains explicit transmission consent and the selected audited sample HWPX
has a server-verified semantic target map.  This module never invokes HWP COM
and never writes an HWP/HWPX document.

Successful provider output is persisted before the derived export bundle is
written.  If the bridge stops between those two operations, the next run
replays the verified preview record instead of sending the same incident
payload to the provider again.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence
from uuid import UUID, uuid4

from .contracts import DocumentEditPlan
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
MAX_PREVIEW_RECORD_BYTES = 2 * 1024 * 1024
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
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


class PipelineReportProfileError(PipelineReportPreviewError):
    """The one-time project report profile did not pass server verification."""


class PipelineReportPreviewCorrupt(PipelineReportPreviewError):
    """A persisted preview record failed integrity verification."""


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
                        and hashlib.sha256(
                            cached.source_path.read_bytes()
                        ).hexdigest()
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
        explicit_transmission_consent: bool,
        report_attempt: int,
    ) -> Mapping[str, Any]:
        if explicit_transmission_consent is not True:
            raise PipelineReportPreviewError(
                "explicit report transmission consent is required"
            )
        if provider not in {"codex", "claude"}:
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
        input_snapshot = {
            "schema_version": PIPELINE_REPORT_PREVIEW_SCHEMA,
            "project_id": str(project_id),
            "pipeline_job_id": str(pipeline_job_id),
            "analysis_job_id": str(analysis_job_id),
            "source_sha256": source_sha256,
            "provider": provider,
            "template_id": template_id,
            "template_sha256": template_sha256,
            "template_semantic_map_id": template_semantic_map_id,
            "template_semantic_map_sha256": (
                template_semantic_map_sha256
            ),
            "report_attempt": report_attempt,
            "approved_incidents": normalized_incidents,
            "document_structure_summary": (
                semantic_map.document_structure_summary
            ),
        }
        input_sha256 = _sha256_value(input_snapshot)
        preview_id = f"report-preview-{input_sha256[:24]}"
        record_path = self._record_path(
            project_id, pipeline_job_id, preview_id
        )
        with self._lock:
            if record_path.exists():
                record = self._load_record(record_path)
                if (
                    record.get("input_sha256") != input_sha256
                    or record.get("preview_id") != preview_id
                ):
                    raise PipelineReportPreviewCorrupt(
                        "persisted report preview identity is invalid"
                    )
                return self._metadata(record)

            payload = {
                "approved_incidents": normalized_incidents,
                "document_structure_summary": (
                    semantic_map.document_structure_summary
                ),
                "map_refs": [],
            }
            try:
                generated = self._report_generator.generate(
                    provider,
                    payload,
                )
                plan = DocumentEditPlan.model_validate(
                    generated.plan
                )
                if (
                    plan.document_snapshot_id
                    != semantic_map.document_snapshot_id
                ):
                    raise PipelineReportPreviewError(
                        "the provider plan crossed the template snapshot"
                    )
                layout_preview = preview_sample_layout_plan(
                    semantic_map.source_path,
                    plan,
                )
            except (
                ReportGenerationError,
                HwpxSampleLayoutError,
                ValueError,
            ) as error:
                raise PipelineReportPreviewError(
                    "the provider report preview failed validation"
                ) from error
            draft = dict(generated.draft)
            plan_payload = plan.model_dump(mode="json")
            draft_sha256 = _sha256_value(draft)
            plan_sha256 = _sha256_value(plan_payload)
            record_without_hash = {
                **input_snapshot,
                "preview_id": preview_id,
                "input_sha256": input_sha256,
                "draft": draft,
                "draft_sha256": draft_sha256,
                "plan": plan_payload,
                "plan_sha256": plan_sha256,
                "operation_count": len(plan.operations),
                "layout_contract_sha256": (
                    layout_preview.layout_contract_sha256
                ),
                "expected_rendered_page_count": (
                    layout_preview.expected_rendered_page_count
                ),
                "provider_called": True,
                "com_invoked": False,
            }
            record = {
                **record_without_hash,
                "record_sha256": _sha256_value(record_without_hash),
            }
            _write_atomic(record_path, _canonical_json(record))
            return self._metadata(record)

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
            "incident_type",
            "severity",
            "maintenance_action",
            "ocr_text",
            "time_ms",
            "evidence_refs",
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
                not isinstance(incident_type, str)
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
            # The provider receives no image bytes or local path.  Replace the
            # internal analysis artifact handle with an opaque deidentified
            # evidence identity; the deterministic local editor resolves it
            # only after a later HWP apply approval.
            normalized.append(
                {
                    "incident_id": incident_id,
                    "incident_type": incident_type,
                    "severity": severity,
                    "maintenance_action": maintenance_action,
                    "ocr_text": ocr_text,
                    "time_ms": time_ms,
                    "evidence_refs": [
                        f"deid:{incident_id}:representative"
                    ],
                }
            )
        return normalized

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
        return {
            "preview_id": record["preview_id"],
            "plan_sha256": record["plan_sha256"],
            "draft_sha256": record["draft_sha256"],
            "operation_count": record["operation_count"],
        }


__all__ = [
    "DurablePipelineReportPreviewPreparer",
    "PIPELINE_REPORT_PREVIEW_SCHEMA",
    "PIPELINE_TEMPLATE_SEMANTIC_MAP_SCHEMA",
    "PipelineReportPreviewCorrupt",
    "PipelineReportPreviewError",
    "PipelineReportProfileError",
    "SampleTemplateSemanticMap",
    "SampleTemplateSemanticMapRegistry",
]
