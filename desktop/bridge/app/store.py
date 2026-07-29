from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Dict, List, Optional
from uuid import UUID, uuid4

from fastapi import HTTPException

from .contracts import (
    DocumentEditPlan,
    ExportCreateRequest,
    ImportCreateRequest,
    ImportInspectRequest,
    PolicyMode,
    ProjectCreateRequest,
    QuestionAnswerRequest,
    ReportApplyRequest,
    ReportDecisionRequest,
    ReportDraftRequest,
    ReportPlanRequest,
)
from .dataset_versions import LabelProjectionError
from .project_catalog import ProjectCatalog
from .security import validate_public_source_url


DEMO_PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
DEMO_TRIP_ID = UUID("22222222-2222-4222-8222-222222222222")
DEMO_INCIDENT_A = UUID("33333333-3333-4333-8333-333333333331")
DEMO_INCIDENT_B = UUID("33333333-3333-4333-8333-333333333332")
DEMO_QUESTION_A = UUID("44444444-4444-4444-8444-444444444441")
DEMO_QUESTION_B = UUID("44444444-4444-4444-8444-444444444442")


def now() -> datetime:
    return datetime.now(timezone.utc)


def evidence(prefix: str) -> Dict[str, str]:
    return {
        "representative_image": f"media:{prefix}:representative",
        "before_frame": f"media:{prefix}:before",
        "after_frame": f"media:{prefix}:after",
        "preview_clip": f"media:{prefix}:clip",
    }


class TrafficVissonStore:
    """Project store with a durable production catalog and isolated fixtures."""

    def __init__(
        self,
        *,
        fixture_mode: bool = True,
        data_root: Optional[Path] = None,
    ) -> None:
        self._lock = RLock()
        self._fixture_mode = fixture_mode
        default_project = {
            "id": DEMO_PROJECT_ID,
            "name": (
                "서울 북부 도로 점검"
                if fixture_mode
                else "도로 영상 점검 프로젝트"
            ),
            "description": (
                "질문형 라벨링과 지도 히스토리 데모 프로젝트"
                if fixture_mode
                else "로컬 영상·라벨·분석·보고서를 격리하는 기본 프로젝트"
            ),
            "question_policy": "learning_assist",
            "active_model_version": "common-initial@1.0.0",
            "created_at": datetime(2026, 7, 28, tzinfo=timezone.utc),
            "fixture": fixture_mode,
        }
        self._project_catalog = (
            ProjectCatalog(
                Path(data_root) / "catalog.sqlite3",
                default_project,
            )
            if data_root is not None
            else None
        )
        self._projects: Dict[UUID, Dict] = {
            DEMO_PROJECT_ID: default_project
        }
        demo_questions: Dict[UUID, List[Dict]] = {
            DEMO_PROJECT_ID: [
                {
                    "id": DEMO_QUESTION_A,
                    "incident_id": DEMO_INCIDENT_A,
                    "trip_id": DEMO_TRIP_ID,
                    "prompt": "표지판이 식생에 의해 가려졌나요?",
                    "proposed_class": "vegetation_occlusion",
                    "proposed_severity": "major",
                    "confidence": 0.72,
                    "status": "QUEUED",
                    "emergency_review_required": False,
                    "proposed_geometry": {
                        "kind": "polygon",
                        "points": [(312.0, 188.0), (515.0, 184.0), (522.0, 361.0)],
                    },
                    "evidence": evidence("question-a"),
                    "latitude": 37.6481,
                    "longitude": 127.0612,
                    "time_ms": 18_500,
                    "learning_effect": "승인 시 식생 가림과 삭초 조치 학습 표본에 반영됩니다.",
                },
                {
                    "id": DEMO_QUESTION_B,
                    "incident_id": DEMO_INCIDENT_B,
                    "trip_id": DEMO_TRIP_ID,
                    "prompt": "도로 위 물체가 낙하물인가요?",
                    "proposed_class": "fallen_object",
                    "proposed_severity": "urgent",
                    "confidence": 0.91,
                    "status": "QUEUED",
                    "emergency_review_required": True,
                    "proposed_geometry": {
                        "kind": "box",
                        "points": [(620.0, 405.0), (776.0, 548.0)],
                    },
                    "evidence": evidence("question-b"),
                    "latitude": 37.6494,
                    "longitude": 127.0641,
                    "time_ms": 42_000,
                    "learning_effect": "낙하물은 안전상 모든 정책에서 사용자 확인이 필요합니다.",
                },
            ]
        }
        self._questions: Dict[UUID, List[Dict]] = (
            demo_questions if fixture_mode else {DEMO_PROJECT_ID: []}
        )
        self._inspections: Dict[UUID, Dict] = {}
        self._drafts: Dict[UUID, Dict] = {}
        self._reports: Dict[UUID, Dict] = {}

    def _project(self, project_id: UUID) -> Dict:
        project = (
            self._project_catalog.get_project(project_id)
            if self._project_catalog is not None
            else self._projects.get(project_id)
        )
        if project is None:
            raise HTTPException(status_code=404, detail="Project was not found")
        return project

    def list_projects(self) -> List[Dict]:
        with self._lock:
            if self._project_catalog is not None:
                return deepcopy(self._project_catalog.list_projects())
            return deepcopy(list(self._projects.values()))

    def get_project(self, project_id: UUID) -> Dict:
        with self._lock:
            return deepcopy(self._project(project_id))

    def create_project(self, request: ProjectCreateRequest) -> Dict:
        with self._lock:
            project_id = uuid4()
            project = {
                "id": project_id,
                "name": request.name,
                "description": request.description,
                "question_policy": "learning_assist",
                "active_model_version": "common-initial@1.0.0",
                "created_at": now(),
                "fixture": self._fixture_mode,
            }
            if self._project_catalog is not None:
                self._project_catalog.create_project(project)
            else:
                self._projects[project_id] = project
            self._questions[project_id] = []
            return deepcopy(project)

    def project_label_record(
        self, project_id: UUID, record: Dict
    ) -> Dict:
        """Project one append-only label record into the normalized catalog."""

        with self._lock:
            self._project(project_id)
            if self._project_catalog is None:
                return {"created": False, "fixture_only": True}
            try:
                projected = self._project_catalog.project_label_record(
                    project_id, record
                )
            except LabelProjectionError:
                # A just-completed analysis writes its immutable record after
                # catalog startup. Refresh the read-only analysis index once,
                # then replay the label projection.
                self._project_catalog.backfill_completed_analysis_records()
                projected = self._project_catalog.project_label_record(
                    project_id, record
                )
            return deepcopy(projected)

    def project_label_records(
        self, project_id: UUID, records: List[Dict]
    ) -> List[Dict]:
        """Idempotently reconcile an audit-log prefix into SQLite."""

        with self._lock:
            self._project(project_id)
            if self._project_catalog is None:
                return []
            try:
                projected = self._project_catalog.project_label_records(
                    project_id, records
                )
            except LabelProjectionError:
                self._project_catalog.backfill_completed_analysis_records()
                projected = self._project_catalog.project_label_records(
                    project_id, records
                )
            return deepcopy(projected)

    def current_label_stats(self, project_id: UUID) -> Dict:
        with self._lock:
            self._project(project_id)
            if self._project_catalog is None:
                return {
                    "project_id": str(project_id),
                    "question_count": 0,
                    "answer_revision_count": 0,
                    "current_answer_count": 0,
                    "training_included_count": 0,
                    "source_analysis_job_count": 0,
                    "partitions": {
                        "approved": 0,
                        "negative": 0,
                        "held": 0,
                        "excluded": 0,
                    },
                }
            return deepcopy(
                self._project_catalog.current_label_stats(project_id)
            )

    def list_incidents(
        self,
        project_id: UUID,
        *,
        include_inactive: bool = False,
        analysis_job_id: Optional[UUID] = None,
    ) -> List[Dict]:
        with self._lock:
            self._project(project_id)
            if self._project_catalog is None:
                return []
            return deepcopy(
                self._project_catalog.list_incidents(
                    project_id,
                    include_inactive=include_inactive,
                    analysis_job_id=analysis_job_id,
                )
            )

    def create_dataset_version(self, project_id: UUID) -> Dict:
        with self._lock:
            self._project(project_id)
            if self._project_catalog is None:
                raise HTTPException(
                    status_code=409,
                    detail="Dataset versions require the durable catalog",
                )
            return deepcopy(
                self._project_catalog.create_dataset_version(project_id)
            )

    def list_dataset_versions(self, project_id: UUID) -> List[Dict]:
        with self._lock:
            self._project(project_id)
            if self._project_catalog is None:
                return []
            return deepcopy(
                self._project_catalog.list_dataset_versions(project_id)
            )

    def inspect_import(
        self, project_id: UUID, request: ImportInspectRequest
    ) -> Dict:
        with self._lock:
            self._project(project_id)
            if request.project_id != project_id:
                raise HTTPException(
                    status_code=409, detail="Project identity does not match the route"
                )
            if request.source_type == "local_selection":
                source_display = "선택한 로컬 영상"
                support_tier = "official"
                status = "fixture_ready"
            else:
                try:
                    hostname = validate_public_source_url(
                        request.source_type, str(request.source_url)
                    )
                except ValueError as error:
                    raise HTTPException(status_code=422, detail=str(error)) from error
                source_display = hostname
                support_tier = {
                    "youtube": "conditional",
                    "webpage": "best_effort",
                }.get(request.source_type, "official")
                status = "metadata_only"

            inspection_id = uuid4()
            record = {
                "project_id": project_id,
                "source_type": request.source_type,
                "local_selection_id": request.local_selection_id,
                "inspected_source_id": inspection_id,
                "source_type_response": request.source_type,
                "support_tier": support_tier,
                "source_display": source_display,
                "expected_bytes": 734_003_200,
                "duration_seconds": 612.4,
                "rights_confirmation_required": True,
                "inspection_status": status,
                "warnings": (
                    ["권리 보유 영상만 가져올 수 있습니다."]
                    if request.source_type == "youtube"
                    else []
                ),
                "created_at": now(),
            }
            self._inspections[inspection_id] = record
            if self._project_catalog is not None:
                self._project_catalog.save_import_inspection(record)
            return {
                "inspected_source_id": inspection_id,
                "source_type": request.source_type,
                "support_tier": support_tier,
                "source_display": source_display,
                "expected_bytes": record["expected_bytes"],
                "duration_seconds": record["duration_seconds"],
                "rights_confirmation_required": True,
                "inspection_status": status,
                "warnings": record["warnings"],
            }

    def create_import(
        self, project_id: UUID, request: ImportCreateRequest
    ) -> Dict:
        with self._lock:
            self._project(project_id)
            if request.project_id != project_id:
                raise HTTPException(
                    status_code=409, detail="Project identity does not match the route"
                )
            inspection = self._inspections.get(request.inspected_source_id)
            if inspection is None and self._project_catalog is not None:
                inspection = self._project_catalog.get_import_inspection(
                    request.inspected_source_id
                )
            if not inspection or inspection["project_id"] != project_id:
                raise HTTPException(
                    status_code=404,
                    detail="Inspected source was not found in this project",
                )
            if not request.rights_confirmed:
                raise HTTPException(
                    status_code=412, detail="Rights confirmation is required"
                )
            record = {
                "import_id": uuid4(),
                "project_id": project_id,
                "inspected_source_id": request.inspected_source_id,
                "status": "queued",
                "source_type": inspection["source_type"],
                "provenance_status": "pending_download_hash",
                "created_at": now(),
            }
            if self._project_catalog is not None:
                self._project_catalog.save_import(record)
            return {
                key: value
                for key, value in record.items()
                if key
                in {
                    "import_id",
                    "status",
                    "source_type",
                    "provenance_status",
                }
            }

    def list_questions(
        self,
        project_id: UUID,
        status: Optional[str],
        policy_mode: PolicyMode,
    ) -> List[Dict]:
        with self._lock:
            self._project(project_id)
            questions = deepcopy(self._questions.get(project_id, []))
            if status:
                questions = [item for item in questions if item["status"] == status]
            if policy_mode == "quick_review":
                questions = [
                    item
                    for item in questions
                    if item["confidence"] < 0.75
                    or item["emergency_review_required"]
                ]
            return questions

    def answer_question(
        self, project_id: UUID, question_id: UUID, request: QuestionAnswerRequest
    ) -> Dict:
        with self._lock:
            self._project(project_id)
            if request.project_id != project_id or request.question_id != question_id:
                raise HTTPException(
                    status_code=409,
                    detail="Question identity does not match the route",
                )
            question = next(
                (
                    item
                    for item in self._questions.get(project_id, [])
                    if item["id"] == question_id
                ),
                None,
            )
            if question is None:
                raise HTTPException(
                    status_code=404, detail="Question was not found in this project"
                )

            state_map = {
                "yes": ("ANSWERED_ACCEPT", "approved"),
                "edit": ("ANSWERED_ACCEPT", "approved"),
                "no": ("ANSWERED_REJECT", "negative"),
                "unsure": ("ANSWERED_UNSURE", "held"),
            }
            question_status, annotation_state = state_map[request.answer]
            question["status"] = question_status
            include = (
                request.include_in_training
                if request.include_in_training is not None
                else request.answer in {"yes", "edit", "no"}
            )
            if request.answer == "unsure":
                include = False
            final_geometry = (
                request.final_geometry.model_dump()
                if request.final_geometry
                else question["proposed_geometry"]
                if request.answer == "yes"
                else None
            )
            return {
                "question_id": question_id,
                "status": question_status,
                "annotation_state": annotation_state,
                "include_in_training": include,
                "final_geometry": final_geometry,
                "answered_at": now(),
            }

    def history(self, project_id: UUID, trip_id: UUID) -> Dict:
        with self._lock:
            self._project(project_id)
            if project_id != DEMO_PROJECT_ID or trip_id != DEMO_TRIP_ID:
                raise HTTPException(
                    status_code=404, detail="Trip was not found in this project"
                )
            route = [
                {
                    "sequence": index,
                    "time_ms": index * 10_000,
                    "latitude": 37.646 + index * 0.0007,
                    "longitude": 127.059 + index * 0.0011,
                    "speed_kph": 28.0 + index,
                }
                for index in range(7)
            ]
            incidents = []
            for question in self._questions[DEMO_PROJECT_ID]:
                incidents.append(
                    {
                        "id": question["incident_id"],
                        "question_id": question["id"],
                        "time_ms": question["time_ms"],
                        "latitude": question["latitude"],
                        "longitude": question["longitude"],
                        "incident_type": question["proposed_class"],
                        "severity": question["proposed_severity"],
                        "maintenance_action": (
                            "삭초 및 표지판 시야 확보"
                            if question["proposed_class"] == "vegetation_occlusion"
                            else "낙하물 긴급 수거"
                        ),
                        "ocr_text": (
                            "동부간선도로"
                            if question["proposed_class"] == "vegetation_occlusion"
                            else None
                        ),
                        "evidence": question["evidence"],
                    }
                )
            return {
                "project_id": project_id,
                "trip_id": trip_id,
                "route_points": route,
                "incidents": incidents,
                "playback_duration_ms": 60_000,
            }

    def providers(self) -> List[Dict]:
        return [
            {
                "provider": "codex",
                "display_name": "Codex CLI",
                "state": "not_connected",
                "authentication_owner": "official_cli",
                "capabilities": {
                    "report_draft": True,
                    "document_edit_plan": True,
                    "usage_information": True,
                },
                "message": "로그인과 토큰은 공식 Codex CLI가 관리합니다.",
            },
            {
                "provider": "claude",
                "display_name": "Claude Code",
                "state": "not_connected",
                "authentication_owner": "official_cli",
                "capabilities": {
                    "report_draft": True,
                    "document_edit_plan": True,
                    "usage_information": True,
                },
                "message": "로그인과 토큰은 공식 Claude Code가 관리합니다.",
            },
            {
                "provider": "local",
                "display_name": "Local Model - 준비 중",
                "state": "coming_soon",
                "authentication_owner": "not_applicable",
                "capabilities": {
                    "report_draft": False,
                    "document_edit_plan": False,
                    "usage_information": False,
                },
                "message": "Provider 계약만 등록된 비활성 슬롯입니다.",
            },
        ]

    def create_draft(
        self, project_id: UUID, request: ReportDraftRequest
    ) -> Dict:
        with self._lock:
            self._project(project_id)
            if request.project_id != project_id:
                raise HTTPException(
                    status_code=409, detail="Project identity does not match the route"
                )
            if not request.consent:
                raise HTTPException(
                    status_code=412, detail="Explicit AI transmission consent is required"
                )
            if request.provider == "local":
                raise HTTPException(
                    status_code=409, detail="Local model provider is not active"
                )
            known_incidents = {
                question["incident_id"]
                for question in self._questions.get(project_id, [])
            }
            incident_ids = request.incident_ids or sorted(
                known_incidents, key=lambda item: item.hex
            )
            if any(item not in known_incidents for item in incident_ids):
                raise HTTPException(
                    status_code=404, detail="An incident was not found in this project"
                )
            draft_id = uuid4()
            record = {
                "draft_id": draft_id,
                "project_id": project_id,
                "provider": request.provider,
                "state": "fixture_draft",
                "title": "도로시설물 현장 점검 결과",
                "sections": [
                    {
                        "heading": "점검 개요",
                        "body": "GPS 연계 주행 영상에서 유지보수 후보를 확인하였습니다.",
                        "incident_ids": incident_ids,
                    },
                    {
                        "heading": "조치 권고",
                        "body": "긴급 후보를 우선 확인하고 승인된 사건부터 정비합니다.",
                        "incident_ids": incident_ids,
                    },
                ],
                "transmitted_fields": [
                    "approved_incident_json",
                    "ocr_results",
                    "deidentified_evidence_refs",
                    "document_structure_summary",
                ],
            }
            self._drafts[draft_id] = record
            return deepcopy(
                {
                    key: value
                    for key, value in record.items()
                    if key != "project_id"
                }
            )

    def create_plan(
        self, project_id: UUID, request: ReportPlanRequest
    ) -> Dict:
        with self._lock:
            self._project(project_id)
            if request.project_id != project_id:
                raise HTTPException(
                    status_code=409, detail="Project identity does not match the route"
                )
            draft = self._drafts.get(request.draft_id)
            if not draft or draft["project_id"] != project_id:
                raise HTTPException(
                    status_code=404, detail="Report draft was not found in this project"
                )
            plan = {
                "schema_version": "1.0",
                "plan_id": uuid4(),
                "document_snapshot_id": request.document_snapshot_id,
                "operations": [
                    {
                        "operation_id": uuid4(),
                        "type": "fill_text",
                        "target": "bookmark:inspection_summary",
                        "expected_value": None,
                        "value": draft["sections"][0]["body"],
                        "source_ref": f"draft:{request.draft_id}",
                        "style_policy": "preserve",
                        "preview": "점검 개요 문단을 기존 서식으로 채웁니다.",
                    }
                ],
            }
            return {"plan": plan, "state": "preview_required"}

    def apply_report(
        self, project_id: UUID, request: ReportApplyRequest
    ) -> Dict:
        with self._lock:
            self._project(project_id)
            if request.project_id != project_id:
                raise HTTPException(
                    status_code=409, detail="Project identity does not match the route"
                )
            report_id = uuid4()
            operation_ids = [
                operation.operation_id for operation in request.plan.operations
            ]
            self._reports[report_id] = {
                "project_id": project_id,
                "operation_ids": operation_ids,
                "state": "preview_validation_only",
            }
            return {
                "report_id": report_id,
                "state": "preview_validation_only",
                "validated_operation_ids": operation_ids,
                "message": (
                    "편집 계획 스키마만 검증했습니다. 실제 한/글 COM 쓰기는 "
                    "결정적 편집기 구현 전까지 수행하지 않습니다."
                ),
            }

    def decide_report(
        self,
        project_id: UUID,
        request: ReportDecisionRequest,
        decision: str,
    ) -> Dict:
        with self._lock:
            self._project(project_id)
            if request.project_id != project_id:
                raise HTTPException(
                    status_code=409, detail="Project identity does not match the route"
                )
            report = self._reports.get(request.report_id)
            if not report or report["project_id"] != project_id:
                raise HTTPException(
                    status_code=404, detail="Report preview was not found in this project"
                )
            known = set(report["operation_ids"])
            selected = request.operation_ids or report["operation_ids"]
            if any(item not in known for item in selected):
                raise HTTPException(
                    status_code=409, detail="Operation does not belong to this report"
                )
            report["state"] = decision
            return {
                "report_id": request.report_id,
                "state": decision,
                "operation_ids": selected,
            }

    def create_export(
        self, project_id: UUID, request: ExportCreateRequest
    ) -> Dict:
        with self._lock:
            self._project(project_id)
            if request.project_id != project_id:
                raise HTTPException(
                    status_code=409, detail="Project identity does not match the route"
                )
            suffix = {
                "report_payload": ".json",
                "manifest": ".json",
            }
            artifacts = [
                {
                    "artifact": artifact,
                    "file_name": f"{artifact}{suffix.get(artifact, '.csv')}",
                }
                for artifact in request.artifacts
            ]
            return {
                "export_id": uuid4(),
                "status": "fixture_ready",
                "artifacts": artifacts,
                "manifest_included": "manifest" in request.artifacts,
            }
