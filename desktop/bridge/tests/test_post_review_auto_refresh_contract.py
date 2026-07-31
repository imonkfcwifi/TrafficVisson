from __future__ import annotations

import csv
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID, uuid4

from app.analysis_jobs import AnalysisJobService
from app.candidate_labels import CandidateLabelStore
from app.derived_outputs import DerivedOutputsService
from app.pipeline_jobs import (
    BridgePipelineRuntime,
    PipelineJobService,
    PipelineStageResult,
)
from app.store import DEMO_PROJECT_ID, TrafficSuperVisionStore
from test_project_catalog_v2 import _write_completed_analysis


def _wait_for_status(
    service: PipelineJobService,
    project_id: UUID,
    job_id: UUID,
    expected: set[str],
    *,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        row = service.get(project_id, job_id)
        if row["status"] in expected:
            return row
        time.sleep(0.01)
    raise AssertionError(service.get(project_id, job_id))


def _candidate_resolver(analysis_jobs: AnalysisJobService):
    def resolve(
        project_id: UUID,
        analysis_job_id: UUID,
        candidate_id: str,
    ) -> Mapping[str, Any]:
        analysis = analysis_jobs.get(project_id, analysis_job_id)
        candidate = next(
            item
            for item in analysis["result"]["candidates"]
            if item["candidate_id"] == candidate_id
        )
        return {
            "status": analysis["status"],
            "project_id": str(project_id),
            "analysis_job_id": str(analysis_job_id),
            "candidate_id": candidate_id,
            "metadata": candidate,
        }

    return resolve


def _question(
    labels: CandidateLabelStore,
    *,
    project_id: UUID,
    analysis_job_id: UUID,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    candidate_id = str(candidate["candidate_id"])
    return {
        "question_id": str(
            labels.question_id(analysis_job_id, candidate_id)
        ),
        "candidate_id": candidate_id,
        "ai_original_candidate": dict(candidate),
    }


def _review_completion_id(record: Mapping[str, Any]) -> str:
    proof = "\0".join(
        (
            str(record["candidate_id"]),
            str(record["record_id"]).lower(),
            str(record["record_hash"]),
            str(record["answer_revision"]),
        )
    )
    ledger_sha256 = hashlib.sha256(proof.encode("utf-8")).hexdigest()
    return f"review:{ledger_sha256}"


class _ExactPreviewDouble:
    """No Provider, COM, HWPX or user data is touched in this contract test."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def prepare(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        incident_count = len(kwargs["approved_incidents"])
        operation_count = incident_count * 3
        plan_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "pipeline_job_id": str(kwargs["pipeline_job_id"]),
                    "analysis_job_id": str(kwargs["analysis_job_id"]),
                    "incident_ids": [
                        row["incident_id"]
                        for row in kwargs["approved_incidents"]
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "preview_id": "exact-preview-contract-v1",
            "state": "semantic_preview_ready_exact_writer_required",
            "input_sha256": hashlib.sha256(
                b"fixture-exact-preview-input"
            ).hexdigest(),
            "report_attempt": kwargs["report_attempt"],
            "plan_sha256": plan_sha256,
            "semantic_provider_plan_sha256": plan_sha256,
            "draft_sha256": hashlib.sha256(
                b"fixture-report-draft"
            ).hexdigest(),
            "operation_count": operation_count,
            "current_binding_manifest_sha256": hashlib.sha256(
                b"fixture-current-incident-binding"
            ).hexdigest(),
            "binding": {
                "state": "current_incidents_bound_writer_required",
                "template_id": kwargs["template_id"],
                "document_snapshot_id": "sample-q4-snapshot-contract-v1",
                "approved_incident_count": incident_count,
                "required_writer_operation_count": operation_count,
                "exact_hwpx_writer_operation_limit": 5_000,
                "approved_incident_bound": True,
                "content_provenance_bound": True,
                "writer_required": True,
            },
            "provider_called": False,
            "exact_writer_required": True,
            "exact_writer_completed": False,
            "render_validation_completed": False,
            "com_invoked": False,
            "document_written": False,
            "apply_eligible": False,
        }


class _PostReviewContractRuntime:
    """Fixture stages plus the production review/derived-output boundary."""

    def __init__(
        self,
        *,
        bridge_runtime: BridgePipelineRuntime,
        store: TrafficSuperVisionStore,
        selected_analysis: Mapping[str, Any],
        labels: CandidateLabelStore,
        semantic_map_sha256: str,
    ) -> None:
        self._bridge = bridge_runtime
        self._store = store
        self._analysis = selected_analysis
        self._labels = labels
        self._semantic_map_sha256 = semantic_map_sha256
        self.derived_calls = 0

    def capability_snapshot(
        self, profile: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "local_models": "ready",
            "selected_report_provider": profile["report_provider"],
            "bulk_frame_provider": "local_models_only",
            "template_semantic_map_ready": True,
            "template_semantic_map_sha256": (
                self._semantic_map_sha256
            ),
            "report_provider_ready": True,
        }

    def validate_report_profile(
        self,
        *,
        semantic_map_id: str,
        provider: str,
        explicit_transmission_consent: bool,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        return {
            "template_semantic_map_id": semantic_map_id,
            "template_semantic_map_sha256": (
                self._semantic_map_sha256
            ),
            "template_semantic_map_ready": True,
            "report_provider_ready": True,
            "report_provider_state": "ready",
            "report_provider_capabilities": [
                "report_draft",
                "document_edit_plan",
            ],
            "explicit_transmission_consent": (
                explicit_transmission_consent
            ),
            "provider": provider,
            "path_exposed": False,
        }

    def execute(
        self,
        stage: str,
        context: Mapping[str, Any],
        cancel,
    ) -> PipelineStageResult:
        if stage == "derived_outputs":
            self.derived_calls += 1
            return self._bridge.execute(stage, context, cancel)

        source_sha256 = str(self._analysis["source_sha256"])
        analysis_job_id = UUID(str(self._analysis["job_id"]))
        candidates = self._analysis["result"]["candidates"]
        if stage == "intake_validation":
            return PipelineStageResult(
                "success",
                checkpoint={
                    "intake_validation_id": "fixture-intake-v1",
                    "duration_seconds": 30.0,
                },
                metrics={"completed": 1},
            )
        if stage == "sha256":
            return PipelineStageResult(
                "success",
                checkpoint={
                    "hash_job_id": str(uuid4()),
                    "source_sha256": source_sha256,
                    "total_bytes": 1_024,
                },
                metrics={"bytes_hashed": 1_024},
            )
        if stage == "metadata_gps_preflight":
            return PipelineStageResult(
                "blocked_gpx_required",
                checkpoint={
                    "timed_track_status": "missing",
                    "capture_start_coordinate_used_as_route": False,
                },
                metrics={"route_point_count": 0},
                error_code="gpx_required",
                non_blocking=True,
            )
        if stage == "candidate_rescan_5_10fps":
            return PipelineStageResult(
                "success",
                checkpoint={
                    "analysis_job_id": str(analysis_job_id),
                    "source_sha256": source_sha256,
                    "candidate_count": len(candidates),
                },
                metrics={"candidate_count": len(candidates)},
            )
        if stage == "question_queue":
            questions = [
                _question(
                    self._labels,
                    project_id=DEMO_PROJECT_ID,
                    analysis_job_id=analysis_job_id,
                    candidate=candidate,
                )
                for candidate in candidates
            ]
            registered = self._store.register_label_questions(
                DEMO_PROJECT_ID,
                analysis_job_id,
                source_sha256,
                questions,
            )
            question_revision = hashlib.sha256(
                json.dumps(
                    {
                        "analysis_job_id": str(analysis_job_id),
                        "candidate_ids": [
                            row["candidate_id"] for row in questions
                        ],
                        "question_catalog_revision": registered[
                            "question_set_sha256"
                        ],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            return PipelineStageResult(
                "success",
                checkpoint={
                    "analysis_job_id": str(analysis_job_id),
                    "source_sha256": source_sha256,
                    "question_count": len(questions),
                    "question_candidate_ids": [
                        row["candidate_id"] for row in questions
                    ],
                    "question_queue_revision": question_revision,
                    "question_catalog_state": "registered",
                    "question_catalog_revision": registered[
                        "question_set_sha256"
                    ],
                    "review_event_set_state": (
                        "legacy_candidate_queue"
                    ),
                    "review_event_bindings": [],
                },
                metrics={"question_count": len(questions)},
            )
        return PipelineStageResult(
            "success",
            checkpoint={f"{stage}_id": f"fixture-{stage}-v1"},
            metrics={"completed": 1},
        )

    def validate_review_completion(
        self,
        context: Mapping[str, Any],
        review_completion_id: str,
    ) -> Mapping[str, Any]:
        return self._bridge.validate_review_completion(
            context, review_completion_id
        )

    def reconcile_question_catalog(
        self, _context: Mapping[str, Any]
    ) -> PipelineStageResult:
        return PipelineStageResult(
            "success",
            checkpoint={},
            metrics={},
        )

    def cancel(self, _context: Mapping[str, Any]) -> None:
        return None


def test_initial_review_refreshes_training_outputs_without_incident_truth(
    tmp_path: Path,
) -> None:
    """All external integrations here are fixtures/test doubles by design."""

    data_root = (tmp_path / "bridge-data").resolve()
    selected_fixture = _write_completed_analysis(
        data_root,
        project_id=DEMO_PROJECT_ID,
        source_seed="20260730_selected",
        candidate_count=1,
    )
    foreign_fixture = _write_completed_analysis(
        data_root,
        project_id=DEMO_PROJECT_ID,
        source_seed="20260730_foreign",
        candidate_count=1,
    )
    store = TrafficSuperVisionStore(
        fixture_mode=False, data_root=data_root
    )
    analysis_jobs = AnalysisJobService(
        object(), data_root=data_root
    )
    labels = CandidateLabelStore(
        data_root, _candidate_resolver(analysis_jobs)
    )
    exact_preview = _ExactPreviewDouble()
    derived = DerivedOutputsService(
        data_root=data_root,
        candidate_labels=labels,
        store=store,
        analysis_jobs=analysis_jobs,
        report_preparer=exact_preview,
        approved_reference_resolver=lambda _project_id: [],
    )
    bridge = BridgePipelineRuntime(
        object(),
        data_root=data_root,
        candidate_labels=labels,
        derived_outputs=derived,
    )
    semantic_map_sha256 = hashlib.sha256(
        b"fixture-sample-q4-semantic-map"
    ).hexdigest()
    runtime = _PostReviewContractRuntime(
        bridge_runtime=bridge,
        store=store,
        selected_analysis=selected_fixture["payload"],
        labels=labels,
        semantic_map_sha256=semantic_map_sha256,
    )
    pipeline = PipelineJobService(runtime, data_root=data_root)

    try:
        profile = pipeline.configure_profile(
            DEMO_PROJECT_ID,
            question_policy="learning_assist",
            review_mode="precision",
            default_template_id="sample-q4-v1",
            default_template_sha256=hashlib.sha256(
                b"fixture-sample-q4-template"
            ).hexdigest(),
            detector_pack_id="fixture-road-detector-v1",
            ocr_pack_id="fixture-korean-ocr-v1",
            sam2_pack_id="fixture-sam2-v1",
            report_provider="codex",
            report_transmission_consent=False,
            template_semantic_map_id="sample-q4-semantic-v1",
            report_route_name="계약 테스트 노선",
            report_inspector_name="계약 테스트 점검자",
            report_evidence_mode="internal_original",
            output_location_id="project-default",
            candidate_rescan_fps=8,
        )
        started, replayed = pipeline.start(
            DEMO_PROJECT_ID,
            snapshot_id=selected_fixture["snapshot_id"],
            file_name="20260730_drive.mp4",
            rights_confirmed=True,
            idempotency_key="post-review-auto-refresh-contract-v1",
        )
        assert replayed is False
        waiting = _wait_for_status(
            pipeline,
            DEMO_PROJECT_ID,
            UUID(str(started["job_id"])),
            {"awaiting_review"},
        )

        # A prepared queue and even append-only answers are not approved truth.
        assert store.list_current_annotations(DEMO_PROJECT_ID) == []
        assert store.list_incidents(DEMO_PROJECT_ID) == []
        assert store.list_dataset_versions(DEMO_PROJECT_ID) == []
        assert exact_preview.calls == []
        assert runtime.derived_calls == 0
        assert not (data_root / "exports").exists() or not list(
            (data_root / "exports").iterdir()
        )

        selected_analysis_id = UUID(
            str(selected_fixture["job_id"])
        )
        selected_candidate = selected_fixture["payload"]["result"][
            "candidates"
        ][0]
        selected_record = labels.record_answer(
            DEMO_PROJECT_ID,
            selected_analysis_id,
            selected_candidate["candidate_id"],
            answer="yes",
            selected_issue_type="road_debris",
            severity="major",
            maintenance_action="낙하물 수거",
            normalized_box={
                "x": 0.1,
                "y": 0.2,
                "width": 0.3,
                "height": 0.2,
            },
            answered_by="local_user",
        )

        # A reviewed answer from another analysis stays outside this job.
        foreign_analysis_id = UUID(str(foreign_fixture["job_id"]))
        foreign_candidate = foreign_fixture["payload"]["result"][
            "candidates"
        ][0]
        store.register_label_questions(
            DEMO_PROJECT_ID,
            foreign_analysis_id,
            foreign_fixture["source_sha256"],
            [
                _question(
                    labels,
                    project_id=DEMO_PROJECT_ID,
                    analysis_job_id=foreign_analysis_id,
                    candidate=foreign_candidate,
                )
            ],
        )
        labels.record_answer(
            DEMO_PROJECT_ID,
            foreign_analysis_id,
            foreign_candidate["candidate_id"],
            answer="yes",
            selected_issue_type="pothole",
            severity="minor",
            maintenance_action="포트홀 보수",
            normalized_box={
                "x": 0.2,
                "y": 0.3,
                "width": 0.2,
                "height": 0.2,
            },
            answered_by="local_user",
        )
        assert store.list_current_annotations(DEMO_PROJECT_ID) == []
        assert store.list_incidents(DEMO_PROJECT_ID) == []

        completion_id = _review_completion_id(selected_record)
        resumed = pipeline.complete_review(
            DEMO_PROJECT_ID,
            UUID(str(started["job_id"])),
            review_completion_id=completion_id,
        )
        assert resumed["status"] == "queued"
        completed = _wait_for_status(
            pipeline,
            DEMO_PROJECT_ID,
            UUID(str(started["job_id"])),
            {"completed", "blocked"},
        )

        assert completed["status"] == "completed"
        assert completed["outcome_code"] == "success"
        assert completed["review_completion_id"] == completion_id
        assert completed["profile_snapshot"] == profile
        assert runtime.derived_calls == 1

        annotations = store.list_current_annotations(DEMO_PROJECT_ID)
        assert len(annotations) == 1
        assert annotations[0]["analysis_job_id"] == str(
            selected_analysis_id
        )
        assert annotations[0]["candidate_id"] == (
            selected_candidate["candidate_id"]
        )
        assert store.list_incidents(
            DEMO_PROJECT_ID,
            analysis_job_id=foreign_analysis_id,
        ) == []
        incidents = store.list_incidents(
            DEMO_PROJECT_ID,
            analysis_job_id=selected_analysis_id,
        )
        assert incidents == []

        datasets = store.list_dataset_versions(DEMO_PROJECT_ID)
        assert len(datasets) == 1
        dataset = datasets[0]
        assert dataset["training_gate_member_count"] == 1
        assert {
            member["analysis_job_id"]
            for member in dataset["manifest"]["members"]
        } == {str(selected_analysis_id)}
        assert (
            dataset["manifest"][
                "approved_references_count_toward_validation"
            ]
            is False
        )

        derived_stage = next(
            stage
            for stage in completed["stages"]
            if stage["name"] == "derived_outputs"
        )
        checkpoint = derived_stage["checkpoint"]
        assert checkpoint["dataset_version_id"] == (
            dataset["dataset_version_id"]
        )
        assert checkpoint["dataset_hash"] == dataset["dataset_hash"]
        assert checkpoint["pipeline_job_id"] == str(started["job_id"])
        assert checkpoint["analysis_job_id"] == str(
            selected_analysis_id
        )
        assert checkpoint["review_completion_id"] == completion_id
        assert checkpoint["report"]["state"] == (
            "not_applicable_no_approved_incidents"
        )
        assert checkpoint["report"]["document_edit_plan_prepared"] is False
        assert checkpoint["report"]["com_invoked"] is False

        assert exact_preview.calls == []

        bundles = list(
            (data_root / "exports").glob(
                "trafficsupervision-export-*"
            )
        )
        assert len(bundles) == 1
        bundle = bundles[0]
        with (bundle / "raw_detections.csv").open(
            "r", encoding="utf-8-sig", newline=""
        ) as source:
            raw_rows = list(csv.DictReader(source))
        with (bundle / "reviewed_incidents.csv").open(
            "r", encoding="utf-8-sig", newline=""
        ) as source:
            reviewed_rows = list(csv.DictReader(source))
        with (bundle / "annotations.csv").open(
            "r", encoding="utf-8-sig", newline=""
        ) as source:
            annotation_rows = list(csv.DictReader(source))
        manifest = json.loads(
            (bundle / "manifest.json").read_text(encoding="utf-8")
        )
        report_payload = json.loads(
            (bundle / "report_payload.json").read_text(
                encoding="utf-8"
            )
        )

        assert len(raw_rows) == 1
        assert reviewed_rows == []
        assert len(annotation_rows) == 1
        assert raw_rows[0]["detection_id"] == (
            selected_candidate["candidate_id"]
        )
        assert annotation_rows[0]["question_id"] == (
            selected_record["question_id"]
        )
        assert annotation_rows[0]["trip_id"] == str(
            selected_analysis_id
        )
        assert manifest["provenance"]["pipeline_job_id"] == str(
            started["job_id"]
        )
        assert manifest["provenance"]["analysis_job_id"] == str(
            selected_analysis_id
        )
        assert manifest["provenance"]["review_completion_id"] == (
            completion_id
        )
        assert manifest["provenance"]["dataset_version"] == (
            checkpoint["dataset_version"]
        )
        assert report_payload["report"]["dataset_version"] == (
            checkpoint["dataset_version"]
        )
        assert report_payload["report"][
            "document_edit_plan_sha256"
        ] is None
        assert report_payload["report"]["approved_incident_count"] == 0
        assert report_payload["report"]["report_incident_count"] == 0
        assert manifest["evidence"]["incident_count"] == 0
        assert manifest["evidence"]["file_count"] == 0
    finally:
        pipeline.shutdown()
