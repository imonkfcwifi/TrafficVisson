import hashlib
import json
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.contracts import PipelineJobResponse
from app.derived_outputs import DerivedOutputsResult
from app.media_intake import MediaIntakeService
from app.pipeline_jobs import (
    BridgePipelineRuntime,
    PIPELINE_STAGES,
    PipelineJobConflict,
    PipelineJobService,
    PipelineProfileMissing,
    PipelineStageFailure,
    PipelineStageResult,
)
from test_analysis_jobs import _make_video


def _wait(service, project_id, job_id, statuses, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = service.get(project_id, job_id)
        if row["status"] in statuses:
            return row
        time.sleep(0.01)
    raise AssertionError(service.get(project_id, job_id))


class FakeRuntime:
    def __init__(self):
        self.calls = []
        self.contexts = []
        self.analysis_job_id = None
        self.model_ready = True
        self.zero_detections = False
        self.block_stage = None
        self.block_started = threading.Event()
        self.block_release = threading.Event()
        self.fail_once = None
        self.cancel_calls = 0
        self.review_valid = True

    def capability_snapshot(self, profile):
        return {
            "local_models": "ready" if self.model_ready else "missing",
            "selected_report_provider": profile["report_provider"],
            "bulk_frame_provider": "local_models_only",
        }

    def execute(self, stage, context, cancel):
        self.calls.append(stage)
        self.contexts.append((stage, context))
        if stage == self.block_stage:
            self.block_started.set()
            while not self.block_release.wait(0.01):
                if cancel():
                    return PipelineStageResult(
                        "cancelled", error_code="test_cancelled"
                    )
        if stage == self.fail_once:
            self.fail_once = None
            raise PipelineStageFailure("bounded_test_failure")
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
        if stage == "candidate_rescan_5_10fps" and not self.model_ready:
            return PipelineStageResult(
                "model_unavailable",
                error_code="candidate_rescan_model_unavailable",
            )
        if (
            stage == "candidate_rescan_5_10fps"
            and self.analysis_job_id is not None
        ):
            return PipelineStageResult(
                "success",
                checkpoint={
                    "analysis_job_id": str(self.analysis_job_id),
                    "source_sha256": hashlib.sha256(
                        b"projection-gate-source"
                    ).hexdigest(),
                },
                metrics={"candidate_count": 2},
            )
        if stage == "detection_ocr" and self.zero_detections:
            return PipelineStageResult(
                "zero_detections",
                checkpoint={"detection_count": 0},
                metrics={"detection_count": 0},
            )
        if stage == "question_queue":
            checkpoint = {"question_queue_revision": "questions-v1"}
            if self.analysis_job_id is not None:
                checkpoint["analysis_job_id"] = str(
                    self.analysis_job_id
                )
            return PipelineStageResult(
                "success",
                checkpoint=checkpoint,
                metrics={"question_count": 2},
            )
        if stage == "derived_outputs":
            assert context["review_completion_id"] or self.zero_detections
            return PipelineStageResult(
                "success",
                checkpoint={"derived_output_revision": "outputs-v1"},
                metrics={"output_count": 6},
            )
        return PipelineStageResult(
            "success",
            checkpoint={f"{stage}_id": f"{stage}-v1"},
            metrics={"completed": 1},
        )

    def cancel(self, _context):
        self.cancel_calls += 1

    def validate_review_completion(
        self, _context, review_completion_id
    ):
        return {
            "review_completion_id": review_completion_id,
            "all_required_questions_answered": self.review_valid,
            "answered_question_count": 2 if self.review_valid else 0,
            "server_answer_ledger_revision": "answers-v1",
        }


def _configure(service, project_id):
    return service.configure_profile(
        project_id,
        question_policy="learning_assist",
        review_mode="precision",
        default_template_id="sample-4q-v1",
        default_template_sha256=hashlib.sha256(b"template").hexdigest(),
        detector_pack_id="road-detector-v1",
        ocr_pack_id="korean-ocr-v1",
        sam2_pack_id="sam2-v1",
        report_provider="codex",
        output_location_id="project-default",
        candidate_rescan_fps=8,
    )


def _start(service, project_id, *, suffix="same"):
    return service.start(
        project_id,
        snapshot_id=uuid4(),
        file_name="drive.mp4",
        rights_confirmed=True,
        idempotency_key=f"pipeline-test-{suffix}-0001",
    )


def test_profile_is_required_and_snapshot_has_all_north_star_fields(tmp_path):
    runtime = FakeRuntime()
    service = PipelineJobService(runtime, data_root=tmp_path.resolve())
    project_id = uuid4()
    with pytest.raises(PipelineProfileMissing):
        _start(service, project_id)
    profile = _configure(service, project_id)
    assert profile["revision"] == 1
    assert profile["review_mode"] == "precision"
    assert profile["question_policy"] == "learning_assist"
    assert profile["default_template_id"] == "sample-4q-v1"
    assert len(profile["default_template_sha256"]) == 64
    assert profile["detector_pack_id"] == "road-detector-v1"
    assert profile["ocr_pack_id"] == "korean-ocr-v1"
    assert profile["sam2_pack_id"] == "sam2-v1"
    assert profile["report_provider"] == "codex"
    assert profile["output_location_id"] == "project-default"
    assert profile["bulk_frame_provider_policy"] == "local_models_only"
    service.shutdown()


def test_pipeline_reaches_review_then_requires_explicit_completion(tmp_path):
    runtime = FakeRuntime()
    service = PipelineJobService(runtime, data_root=tmp_path.resolve())
    project_id = uuid4()
    profile = _configure(service, project_id)
    snapshot_id = uuid4()
    started, replayed = service.start(
        project_id,
        snapshot_id=snapshot_id,
        file_name="drive.mp4",
        rights_confirmed=True,
        idempotency_key="pipeline-test-review-0001",
    )
    assert replayed is False
    waiting = _wait(
        service, project_id, started["job_id"], {"awaiting_review"}
    )
    assert waiting["current_stage"] == "question_queue"
    assert "derived_outputs" not in runtime.calls
    assert waiting["profile_snapshot"] == profile
    assert waiting["provenance"]["route_status"] == "blocked_gpx_required"
    gps = next(
        stage
        for stage in waiting["stages"]
        if stage["name"] == "metadata_gps_preflight"
    )
    assert gps["status"] == "blocked"
    assert gps["checkpoint"]["capture_start_coordinate_used_as_route"] is False
    resumed = service.complete_review(
        project_id,
        started["job_id"],
        review_completion_id="review-complete-v1",
    )
    assert resumed["status"] == "queued"
    completed = _wait(
        service, project_id, started["job_id"], {"completed"}
    )
    assert completed["outcome_code"] == "success"
    assert completed["progress_percent"] == 100
    assert completed["review_completion_id"] == "review-complete-v1"
    assert runtime.calls == list(PIPELINE_STAGES)
    derived_context = next(
        context
        for stage, context in runtime.contexts
        if stage == "derived_outputs"
    )
    assert (
        derived_context["input_fingerprint"]
        == completed["input_fingerprint"]
    )
    assert (
        derived_context["profile_sha256"]
        == completed["provenance"]["profile_sha256"]
    )
    assert derived_context["review_completion"] == (
        completed["provenance"]["review_completion"]
    )
    service.shutdown()


def test_pipeline_analysis_projection_stays_gated_until_review_completion(
    tmp_path,
):
    runtime = FakeRuntime()
    runtime.analysis_job_id = uuid4()
    runtime.block_stage = "detection_ocr"
    service = PipelineJobService(runtime, data_root=tmp_path.resolve())
    project_id = uuid4()
    _configure(service, project_id)
    started, _ = _start(service, project_id, suffix="projection-gate")

    assert runtime.block_started.wait(1)
    assert (
        service.is_analysis_projection_gated(
            project_id, runtime.analysis_job_id
        )
        is True
    )
    runtime.block_release.set()
    _wait(service, project_id, started["job_id"], {"awaiting_review"})
    assert (
        service.is_analysis_projection_gated(
            project_id, runtime.analysis_job_id
        )
        is True
    )

    service.complete_review(
        project_id,
        started["job_id"],
        review_completion_id="review-projection-gate-v1",
    )
    assert (
        service.is_analysis_projection_gated(
            project_id, runtime.analysis_job_id
        )
        is False
    )
    _wait(service, project_id, started["job_id"], {"completed"})
    service.shutdown()


def test_start_is_idempotent_by_key_and_input_profile_fingerprint(tmp_path):
    runtime = FakeRuntime()
    runtime.block_stage = "intake_validation"
    service = PipelineJobService(runtime, data_root=tmp_path.resolve())
    project_id = uuid4()
    _configure(service, project_id)
    snapshot_id = uuid4()
    first, replayed = service.start(
        project_id,
        snapshot_id=snapshot_id,
        file_name="drive.mp4",
        rights_confirmed=True,
        idempotency_key="pipeline-test-idempotent-01",
    )
    assert replayed is False
    runtime.block_started.wait(1)
    same, replayed = service.start(
        project_id,
        snapshot_id=snapshot_id,
        file_name="drive.mp4",
        rights_confirmed=True,
        idempotency_key="pipeline-test-idempotent-01",
    )
    assert replayed is True
    assert same["job_id"] == first["job_id"]
    same_input, replayed = service.start(
        project_id,
        snapshot_id=snapshot_id,
        file_name="drive.mp4",
        rights_confirmed=True,
        idempotency_key="pipeline-test-another-key-01",
    )
    assert replayed is True
    assert same_input["job_id"] == first["job_id"]
    with pytest.raises(PipelineJobConflict):
        service.start(
            project_id,
            snapshot_id=uuid4(),
            file_name="other.mp4",
            rights_confirmed=True,
            idempotency_key="pipeline-test-idempotent-01",
        )
    runtime.block_release.set()
    service.shutdown()


def test_source_hash_and_profile_reuse_canonical_job_across_snapshots(
    tmp_path,
):
    source_sha256 = hashlib.sha256(
        b"same-road-video"
    ).hexdigest()

    class SourceHashRuntime(FakeRuntime):
        def execute(self, stage, context, cancel):
            if stage == "sha256":
                self.calls.append(stage)
                self.contexts.append((stage, context))
                return PipelineStageResult(
                    "success",
                    checkpoint={"source_sha256": source_sha256},
                    metrics={"completed": 1},
                )
            return super().execute(stage, context, cancel)

    runtime = SourceHashRuntime()
    root = tmp_path.resolve()
    service = PipelineJobService(runtime, data_root=root)
    project_id = uuid4()
    _configure(service, project_id)
    first, _ = service.start(
        project_id,
        snapshot_id=uuid4(),
        file_name="drive.mp4",
        rights_confirmed=True,
        idempotency_key="pipeline-source-first-0001",
    )
    canonical = _wait(
        service, project_id, first["job_id"], {"awaiting_review"}
    )
    calls_before_reselection = list(runtime.calls)

    second, replayed_at_start = service.start(
        project_id,
        snapshot_id=uuid4(),
        file_name="drive.mp4",
        rights_confirmed=True,
        idempotency_key="pipeline-source-second-0001",
    )
    assert replayed_at_start is False
    replayed = _wait(
        service, project_id, second["job_id"], {"awaiting_review"}
    )
    assert replayed["job_id"] == canonical["job_id"]
    assert service.list(project_id) == [canonical]
    assert runtime.calls[len(calls_before_reselection) :] == [
        "intake_validation",
        "sha256",
    ]
    assert (
        service.recovery_summary()["aliases_loaded"] == 0
    )
    service.shutdown()

    restarted = PipelineJobService(
        SourceHashRuntime(), data_root=root
    )
    restored = restarted.get(project_id, second["job_id"])
    assert restored["job_id"] == canonical["job_id"]
    assert restarted.recovery_summary()["aliases_loaded"] == 1
    restarted.shutdown()


def test_explicit_new_source_prefix_bypasses_hash_reuse(tmp_path):
    source_sha256 = hashlib.sha256(
        b"same-road-video"
    ).hexdigest()

    class SourceHashRuntime(FakeRuntime):
        def execute(self, stage, context, cancel):
            if stage == "sha256":
                self.calls.append(stage)
                self.contexts.append((stage, context))
                return PipelineStageResult(
                    "success",
                    checkpoint={"source_sha256": source_sha256},
                    metrics={"completed": 1},
                )
            return super().execute(stage, context, cancel)

    service = PipelineJobService(
        SourceHashRuntime(), data_root=tmp_path.resolve()
    )
    project_id = uuid4()
    _configure(service, project_id)
    first, _ = _start(service, project_id, suffix="source-normal")
    _wait(service, project_id, first["job_id"], {"awaiting_review"})
    explicit, replayed = service.start(
        project_id,
        snapshot_id=uuid4(),
        file_name="drive.mp4",
        rights_confirmed=True,
        idempotency_key="explicit-new:source-run-0001",
    )
    assert replayed is False
    explicit_waiting = _wait(
        service,
        project_id,
        explicit["job_id"],
        {"awaiting_review"},
    )
    assert explicit_waiting["job_id"] == explicit["job_id"]
    assert len(service.list(project_id)) == 2
    service.shutdown()


def test_one_stable_folder_can_queue_all_51_videos(tmp_path):
    runtime = FakeRuntime()
    runtime.block_stage = "intake_validation"
    service = PipelineJobService(runtime, data_root=tmp_path.resolve())
    project_id = uuid4()
    _configure(service, project_id)
    snapshot_id = uuid4()

    started_ids = []
    for index in range(51):
        started, replayed = service.start(
            project_id,
            snapshot_id=snapshot_id,
            file_name=f"drive-{index + 1:03d}.mp4",
            rights_confirmed=True,
            idempotency_key=f"pipeline-folder-{index + 1:03d}-0001",
        )
        assert replayed is False
        started_ids.append(started["job_id"])

    assert runtime.block_started.wait(1)
    rows = service.list(project_id, limit=128)
    assert len(rows) == 51
    assert {row["file_name"] for row in rows} == {
        f"drive-{index + 1:03d}.mp4" for index in range(51)
    }
    assert set(started_ids) == {row["job_id"] for row in rows}
    assert sum(row["status"] == "queued" for row in rows) >= 50

    runtime.block_release.set()
    service.shutdown()


def test_model_unavailable_is_blocked_and_resume_retries_exact_stage(tmp_path):
    runtime = FakeRuntime()
    runtime.model_ready = False
    service = PipelineJobService(runtime, data_root=tmp_path.resolve())
    project_id = uuid4()
    _configure(service, project_id)
    started, _ = _start(service, project_id, suffix="model")
    blocked = _wait(service, project_id, started["job_id"], {"blocked"})
    assert blocked["current_stage"] == "candidate_rescan_5_10fps"
    assert blocked["outcome_code"] == "model_unavailable"
    before = list(runtime.calls)
    runtime.model_ready = True
    resumed = service.resume(project_id, started["job_id"])
    assert resumed["retry_count"] == 1
    waiting = _wait(
        service, project_id, started["job_id"], {"awaiting_review"}
    )
    assert runtime.calls[: len(before)] == before
    assert runtime.calls[len(before)] == "candidate_rescan_5_10fps"
    assert "intake_validation" not in runtime.calls[len(before) :]
    assert waiting["resume_count"] == 1
    assert waiting["capability_snapshot"]["local_models"] == "ready"
    assert len(waiting["provenance"]["capability_attempts"]) == 1
    service.shutdown()


def test_partial_derived_outputs_stays_resumable_and_replays_bundle(tmp_path):
    class PartialRuntime(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.derived_calls = 0

        def execute(self, stage, context, cancel):
            if stage != "derived_outputs":
                return super().execute(stage, context, cancel)
            self.calls.append(stage)
            self.contexts.append((stage, context))
            self.derived_calls += 1
            return PipelineStageResult(
                "partial",
                checkpoint={
                    "state": "partial",
                    "export_bundle_id": "bundle-stable-v1",
                    "report": {
                        "state": "blocked",
                        "blocking_reasons": [
                            "report_transmission_consent_required"
                        ],
                        "com_invoked": False,
                    },
                    "absolute_paths_exposed": False,
                    "com_invoked": False,
                },
                metrics={
                    "artifact_count": 10,
                    "idempotent_replay": self.derived_calls > 1,
                },
                error_code="derived_outputs_partial",
            )

    runtime = PartialRuntime()
    service = PipelineJobService(runtime, data_root=tmp_path.resolve())
    project_id = uuid4()
    _configure(service, project_id)
    started, _ = _start(service, project_id, suffix="partial")
    _wait(service, project_id, started["job_id"], {"awaiting_review"})
    service.complete_review(
        project_id,
        started["job_id"],
        review_completion_id="review-complete-partial-v1",
    )
    blocked = _wait(
        service, project_id, started["job_id"], {"blocked"}
    )
    assert blocked["current_stage"] == "derived_outputs"
    assert blocked["outcome_code"] == "partial"
    assert blocked["error_code"] == "derived_outputs_partial"
    derived_stage = blocked["stages"][-1]
    assert derived_stage["status"] == "blocked"
    assert derived_stage["outcome"] == "partial"
    assert (
        derived_stage["checkpoint"]["export_bundle_id"]
        == "bundle-stable-v1"
    )
    PipelineJobResponse(**blocked)

    calls_before_resume = list(runtime.calls)
    resumed = service.resume(project_id, started["job_id"])
    assert (
        resumed["stages"][-1]["checkpoint"]["export_bundle_id"]
        == "bundle-stable-v1"
    )
    replayed = _wait(
        service, project_id, started["job_id"], {"blocked"}
    )
    assert runtime.calls[: len(calls_before_resume)] == calls_before_resume
    assert runtime.calls[len(calls_before_resume) :] == ["derived_outputs"]
    assert (
        replayed["stages"][-1]["checkpoint"]["export_bundle_id"]
        == "bundle-stable-v1"
    )
    assert replayed["stages"][-1]["metrics"]["idempotent_replay"] is True
    assert runtime.derived_calls == 2
    service.shutdown()


def test_bridge_runtime_builds_exact_safe_derived_output_request(tmp_path):
    project_id = uuid4()
    pipeline_job_id = uuid4()
    analysis_job_id = uuid4()
    source_sha256 = hashlib.sha256(b"source").hexdigest()
    input_fingerprint = hashlib.sha256(b"input").hexdigest()
    ledger_sha256 = hashlib.sha256(b"answer-ledger").hexdigest()
    completion_id = f"review:{ledger_sha256}"
    question_revision = hashlib.sha256(b"questions").hexdigest()
    candidate_id = "a" * 20
    profile = {
        "default_template_id": "sample-4q-v1",
        "default_template_sha256": hashlib.sha256(
            b"template"
        ).hexdigest(),
        "report_provider": "codex",
        "output_location_id": "project-default",
    }
    profile_sha256 = hashlib.sha256(
        json.dumps(
            profile,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    class CapturingDerivedOutputs:
        def __init__(self):
            self.requests = []

        def generate(self, request, *, cancelled):
            assert cancelled() is False
            self.requests.append(request)
            return DerivedOutputsResult(
                checkpoint={
                    "state": "partial",
                    "export_bundle_id": "bundle-safe-v1",
                    "report": {
                        "state": "blocked",
                        "blocking_reasons": [
                            "report_transmission_consent_required",
                            "template_semantic_map_unavailable",
                        ],
                        "provider_called": False,
                        "document_edit_plan_prepared": False,
                        "com_invoked": False,
                    },
                    "absolute_paths_exposed": False,
                    "com_invoked": False,
                },
                metrics={
                    "artifact_count": 10,
                    "incident_count": 1,
                },
                replayed=False,
            )

    derived = CapturingDerivedOutputs()
    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        derived_outputs=derived,
    )
    context = {
        "job_id": str(pipeline_job_id),
        "project_id": str(project_id),
        "snapshot_id": str(uuid4()),
        "file_name": "drive.mp4",
        "input_fingerprint": input_fingerprint,
        "profile_sha256": profile_sha256,
        "profile": profile,
        "capabilities": {},
        "checkpoints": {
            "sha256": {"source_sha256": source_sha256},
            "question_queue": {
                "analysis_job_id": str(analysis_job_id),
                "source_sha256": source_sha256,
                "question_count": 1,
                "question_candidate_ids": [candidate_id],
                "question_queue_revision": question_revision,
            },
        },
        "review_completion_id": completion_id,
        "review_completion": {
            "review_completion_id": completion_id,
            "all_required_questions_answered": True,
            "answered_question_count": 1,
            "server_answer_ledger_revision": ledger_sha256,
            "analysis_job_id": str(analysis_job_id),
            "question_queue_revision": question_revision,
        },
    }
    result = runtime.execute("derived_outputs", context, lambda: False)
    assert result.outcome == "partial"
    assert result.error_code == "derived_outputs_partial"
    assert result.checkpoint["absolute_paths_exposed"] is False
    assert result.checkpoint["com_invoked"] is False
    assert result.metrics["idempotent_replay"] is False
    request = derived.requests[0]
    assert request.project_id == project_id
    assert request.pipeline_job_id == pipeline_job_id
    assert request.analysis_job_id == analysis_job_id
    assert request.question_candidate_ids == (candidate_id,)
    assert request.pipeline_input_fingerprint == input_fingerprint
    assert request.pipeline_profile_sha256 == profile_sha256
    assert request.review_completion_id == completion_id
    assert request.answer_ledger_sha256 == ledger_sha256
    assert request.report_transmission_consent is False
    assert request.template_semantic_map_ready is False
    assert request.report_provider_ready is False
    assert request.report_attempt == 0


def test_bridge_runtime_exports_verified_zero_detection_completion(
    tmp_path,
):
    project_id = uuid4()
    pipeline_job_id = uuid4()
    analysis_job_id = uuid4()
    source_sha256 = hashlib.sha256(b"zero-source").hexdigest()
    input_fingerprint = hashlib.sha256(b"zero-input").hexdigest()
    profile = {
        "default_template_id": "sample-4q-v1",
        "default_template_sha256": hashlib.sha256(
            b"template"
        ).hexdigest(),
        "report_provider": "codex",
        "output_location_id": "project-default",
    }
    profile_sha256 = hashlib.sha256(
        json.dumps(
            profile,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    class CapturingZeroOutputs:
        def __init__(self):
            self.requests = []

        def generate(self, request, *, cancelled):
            assert cancelled() is False
            self.requests.append(request)
            return DerivedOutputsResult(
                checkpoint={
                    "state": "completed",
                    "completion_mode": "zero_detections",
                    "export_bundle_id": "bundle-zero-v1",
                    "report": {
                        "state": (
                            "not_applicable_no_approved_incidents"
                        ),
                        "blocking_reasons": [],
                        "provider_called": False,
                        "document_edit_plan_prepared": False,
                        "com_invoked": False,
                    },
                    "absolute_paths_exposed": False,
                    "com_invoked": False,
                },
                metrics={
                    "raw_detection_count": 0,
                    "reviewed_answer_count": 0,
                    "incident_count": 0,
                    "annotation_count": 0,
                    "route_point_count": 0,
                    "evidence_file_count": 0,
                    "artifact_count": 10,
                },
                replayed=False,
            )

    derived = CapturingZeroOutputs()
    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        derived_outputs=derived,
    )
    context = {
        "job_id": str(pipeline_job_id),
        "project_id": str(project_id),
        "snapshot_id": str(uuid4()),
        "file_name": "empty-drive.mp4",
        "input_fingerprint": input_fingerprint,
        "profile_sha256": profile_sha256,
        "profile": profile,
        "capabilities": {},
        "checkpoints": {
            "sha256": {"source_sha256": source_sha256},
            "candidate_rescan_5_10fps": {
                "analysis_job_id": str(analysis_job_id),
                "source_sha256": source_sha256,
                "candidate_count": 0,
            },
        },
        "review_completion_id": None,
        "review_completion": {},
    }

    result = runtime.execute("derived_outputs", context, lambda: False)

    assert result.outcome == "zero_detections"
    assert result.error_code == "zero_detections"
    assert result.checkpoint["completion_mode"] == "zero_detections"
    assert result.metrics["raw_detection_count"] == 0
    assert result.metrics["incident_count"] == 0
    request = derived.requests[0]
    empty_ledger_sha256 = hashlib.sha256(b"").hexdigest()
    assert request.project_id == project_id
    assert request.pipeline_job_id == pipeline_job_id
    assert request.analysis_job_id == analysis_job_id
    assert request.completion_mode == "zero_detections"
    assert request.question_candidate_ids == ()
    assert request.answer_ledger_sha256 == empty_ledger_sha256
    assert request.review_completion_id == (
        f"zero-detections:{empty_ledger_sha256}"
    )


def test_zero_detections_is_not_saved_as_success(tmp_path):
    runtime = FakeRuntime()
    runtime.zero_detections = True
    service = PipelineJobService(runtime, data_root=tmp_path.resolve())
    project_id = uuid4()
    _configure(service, project_id)
    started, _ = _start(service, project_id, suffix="zero")
    completed = _wait(
        service, project_id, started["job_id"], {"completed"}
    )
    assert completed["outcome_code"] == "zero_detections"
    assert "question_queue" not in runtime.calls
    assert runtime.calls[-1] == "derived_outputs"
    skipped = {
        stage["name"]: stage["status"] for stage in completed["stages"]
    }
    assert skipped["sam2_segmentation_tracking"] == "skipped"
    assert skipped["question_queue"] == "skipped"
    service.shutdown()


def test_renderer_cannot_forge_review_completion(tmp_path):
    runtime = FakeRuntime()
    runtime.review_valid = False
    service = PipelineJobService(runtime, data_root=tmp_path.resolve())
    project_id = uuid4()
    _configure(service, project_id)
    started, _ = _start(service, project_id, suffix="forged-review")
    _wait(service, project_id, started["job_id"], {"awaiting_review"})
    with pytest.raises(PipelineJobConflict, match="not fully reviewed"):
        service.complete_review(
            project_id,
            started["job_id"],
            review_completion_id="forged-review-v1",
        )
    row = service.get(project_id, started["job_id"])
    assert row["status"] == "awaiting_review"
    assert row["review_completion_id"] is None
    assert "review_completion" not in row["provenance"]
    service.shutdown()


def test_restart_requeues_the_exact_running_stage(tmp_path):
    root = tmp_path.resolve()
    first_runtime = FakeRuntime()
    first_runtime.block_stage = "dense_index_2fps"
    first = PipelineJobService(first_runtime, data_root=root)
    project_id = uuid4()
    _configure(first, project_id)
    started, _ = _start(first, project_id, suffix="restart")
    assert first_runtime.block_started.wait(1)
    first.shutdown()

    second_runtime = FakeRuntime()
    second = PipelineJobService(second_runtime, data_root=root)
    waiting = _wait(
        second, project_id, started["job_id"], {"awaiting_review"}
    )
    assert waiting["recovered_after_restart"] is True
    assert second.recovery_summary()["jobs_requeued"] == 1
    assert second_runtime.calls[0] == "dense_index_2fps"
    assert "intake_validation" not in second_runtime.calls
    second.shutdown()


def test_restart_finalizes_durable_cancelling_without_requeue(
    tmp_path,
):
    class UninterruptibleRuntime(FakeRuntime):
        def execute(self, stage, context, cancel):
            self.calls.append(stage)
            self.contexts.append((stage, context))
            if stage == "intake_validation":
                self.block_started.set()
                self.block_release.wait(5)
            return PipelineStageResult(
                "success",
                checkpoint={f"{stage}_id": f"{stage}-v1"},
                metrics={"completed": 1},
            )

    live_root = (tmp_path / "live").resolve()
    recovery_root = (tmp_path / "recovery").resolve()
    runtime = UninterruptibleRuntime()
    first = PipelineJobService(runtime, data_root=live_root)
    project_id = uuid4()
    _configure(first, project_id)
    started, _ = _start(
        first, project_id, suffix="durable-cancelling"
    )
    assert runtime.block_started.wait(1)
    cancelling = first.cancel(project_id, started["job_id"])
    assert cancelling["status"] == "cancelling"
    shutil.copytree(live_root, recovery_root)

    recovered_runtime = FakeRuntime()
    recovered = PipelineJobService(
        recovered_runtime, data_root=recovery_root
    )
    cancelled = recovered.get(project_id, started["job_id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["outcome_code"] == "cancelled"
    assert cancelled["recovered_after_restart"] is True
    assert cancelled["stages"][0]["status"] == "cancelled"
    assert recovered.recovery_summary()["jobs_requeued"] == 0
    assert (
        recovered.recovery_summary()["cancellations_finalized"]
        == 1
    )
    assert recovered_runtime.calls == []

    recovered.shutdown()
    runtime.block_release.set()
    first.shutdown()


def test_bridge_restart_restores_private_media_grant_and_reuses_dense_artifact(
    tmp_path,
):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    data_root = (tmp_path / "bridge data").resolve()
    data_root.mkdir()
    project_id = uuid4()

    class ReadyDinoStatus:
        def status(self):
            return {
                "state": "ready",
                "error_code": None,
                "review_only": True,
                "bulk_frame_detection_provider": "local_model",
                "pack_id": "dino-review",
                "pack_version": "1.0",
                "manifest_sha256": hashlib.sha256(b"dino").hexdigest(),
            }

    class RestartRuntime:
        def __init__(self, bridge, *, block_candidate):
            self.bridge = bridge
            self.block_candidate = block_candidate
            self.candidate_started = threading.Event()
            self.calls = []

        def capability_snapshot(self, profile):
            return self.bridge.capability_snapshot(profile)

        def execute(self, stage, context, cancel):
            self.calls.append(stage)
            if stage in {
                "intake_validation",
                "sha256",
                "metadata_gps_preflight",
            }:
                return self.bridge.execute(stage, context, cancel)
            if stage == "dense_index_2fps":
                run_id = "dense-frame-index-restart-fixture"
                run_root = (
                    data_root
                    / "projects"
                    / str(project_id)
                    / "pipeline"
                    / "dense"
                    / str(context["job_id"])
                    / run_id
                )
                frames_root = run_root / "frames"
                frames_root.mkdir(parents=True)
                frame = frames_root / "frame-00000000.jpg"
                frame.write_bytes(b"\xff\xd8\xff\xd9")
                frame_sha256 = hashlib.sha256(frame.read_bytes()).hexdigest()
                frame_index = {
                    "schema_version": "trafficvisson.dense-frame-index.v1",
                    "frame_count": 1,
                    "frames": [
                        {
                            "frame_id": "restart-frame-00000001",
                            "frame_index": 0,
                            "timestamp_seconds": 0.0,
                            "relative_path": (
                                "frames/frame-00000000.jpg"
                            ),
                            "size_bytes": frame.stat().st_size,
                            "sha256": frame_sha256,
                            "media_type": "image/jpeg",
                            "evidence_status": "unreviewed_candidate",
                        }
                    ],
                    "provenance": {
                        "source_name": source.name,
                        "source_sha256": context["checkpoints"]["sha256"][
                            "source_sha256"
                        ],
                        "sampling_fps": 2,
                        "review_only": True,
                    },
                }
                (run_root / "frame-index.json").write_text(
                    json.dumps(
                        frame_index,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                return PipelineStageResult(
                    "success",
                    checkpoint={
                        "dense_run_id": run_id,
                        "source_sha256": context["checkpoints"]["sha256"][
                            "source_sha256"
                        ],
                        "sampling_fps": 2,
                        "frame_index_artifact": (
                            f"pipeline-dense:{context['job_id']}:frame-index"
                        ),
                    },
                    metrics={
                        "frame_count": 1,
                        "indexed_duration_seconds": 0.0,
                        "truncated": False,
                    },
                )
            if stage == "candidate_rescan_5_10fps":
                if self.block_candidate:
                    self.candidate_started.set()
                    while not cancel():
                        time.sleep(0.01)
                    return PipelineStageResult(
                        "cancelled",
                        error_code="test_restart_boundary",
                    )
                return self.bridge.execute(stage, context, cancel)
            if stage == "question_queue":
                return PipelineStageResult(
                    "success",
                    checkpoint={
                        "question_queue_revision": "restart-questions-v1",
                        "question_candidate_ids": ["restart-candidate-0001"],
                        "annotation_count": 0,
                        "incident_count": 0,
                    },
                    metrics={"question_count": 1},
                )
            return PipelineStageResult(
                "success",
                checkpoint={
                    "state": "degraded_review_only",
                    "annotation_count": 0,
                    "incident_count": 0,
                },
                metrics={"completed": 1},
            )

        def cancel(self, context):
            self.bridge.cancel(context)

        def validate_review_completion(
            self, _context, review_completion_id
        ):
            return {
                "review_completion_id": review_completion_id,
                "all_required_questions_answered": True,
                "answered_question_count": 1,
                "server_answer_ledger_revision": "restart-ledger-v1",
            }

    first_media = MediaIntakeService(
        ffprobe_path=ffprobe,
        minimum_interval_seconds=0,
    )
    snapshot = first_media.register_local_file(project_id, str(source))
    first_bridge = BridgePipelineRuntime(
        first_media,
        data_root=data_root,
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        vision_status=lambda: {
            "enabled": False,
            "warning_code": "model_pack_not_configured",
        },
        analysis_jobs=object(),
        command_proposal_generator=ReadyDinoStatus(),
        experimental_proposals=object(),
    )
    first_runtime = RestartRuntime(first_bridge, block_candidate=True)
    first = PipelineJobService(first_runtime, data_root=data_root)
    _configure(first, project_id)
    started, replayed = first.start(
        project_id,
        snapshot_id=snapshot["snapshot_id"],
        file_name=source.name,
        rights_confirmed=True,
        idempotency_key="pipeline-test-real-media-restart-0001",
    )
    assert replayed is False
    assert first_runtime.candidate_started.wait(10), first.get(
        project_id, started["job_id"]
    )
    before_restart = first.get(project_id, started["job_id"])
    assert before_restart["current_stage"] == "candidate_rescan_5_10fps"
    dense_stage = next(
        item
        for item in before_restart["stages"]
        if item["name"] == "dense_index_2fps"
    )
    assert dense_stage["status"] == "success"
    dense_run_id = dense_stage["checkpoint"]["dense_run_id"]
    dense_index = (
        data_root
        / "projects"
        / str(project_id)
        / "pipeline"
        / "dense"
        / str(started["job_id"])
        / dense_run_id
        / "frame-index.json"
    )
    assert dense_index.is_file()
    hash_stage = next(
        item
        for item in before_restart["stages"]
        if item["name"] == "sha256"
    )
    hash_job_id = hash_stage["checkpoint"]["hash_job_id"]
    private_grant = (
        data_root
        / "projects"
        / str(project_id)
        / "pipeline"
        / "private-media-grants"
        / f"{hash_job_id}.json"
    )
    assert private_grant.is_file()
    assert str(source) not in json.dumps(
        before_restart, ensure_ascii=False, default=str
    )
    first.shutdown()
    first_media.shutdown()

    second_media = MediaIntakeService(
        ffprobe_path=ffprobe,
        minimum_interval_seconds=0,
    )
    with pytest.raises(HTTPException) as unresolved:
        second_media.resolve_completed_media(
            project_id,
            snapshot["snapshot_id"],
            source.name,
            UUID(hash_stage["checkpoint"]["hash_job_id"]),
            hash_stage["checkpoint"]["source_sha256"],
        )
    assert unresolved.value.status_code == 404

    class RestartAnalysisJobs:
        def __init__(self, media):
            self.media = media
            self.row = None
            self.start_attempts = 0

        def list(self, requested_project_id, *, limit):
            assert requested_project_id == project_id
            assert limit == 128
            return [] if self.row is None else [self.row]

        def start(
            self,
            requested_project_id,
            snapshot_id,
            file_name,
            requested_hash_job_id,
            source_sha256,
        ):
            self.start_attempts += 1
            self.media.resolve_completed_media(
                requested_project_id,
                snapshot_id,
                file_name,
                requested_hash_job_id,
                source_sha256,
            )
            self.row = {
                "job_id": uuid4(),
                "project_id": requested_project_id,
                "snapshot_id": snapshot_id,
                "hash_job_id": requested_hash_job_id,
                "file_name": file_name,
                "source_sha256": source_sha256,
                "status": "completed",
                "result": {
                    "candidates": [
                        {
                            "candidate_id": "restart-candidate-0001",
                            "representative_timestamp_seconds": 0.5,
                        }
                    ],
                    "provenance": {
                        "effective_cadence_seconds": 15.0,
                    },
                },
            }
            return self.row

        def get(self, requested_project_id, requested_job_id):
            assert self.row is not None
            assert requested_project_id == project_id
            assert requested_job_id == self.row["job_id"]
            return self.row

        def cancel(self, _project_id, _job_id):
            return None

    restarted_analysis = RestartAnalysisJobs(second_media)
    second_bridge = BridgePipelineRuntime(
        second_media,
        data_root=data_root,
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        vision_status=lambda: {
            "enabled": False,
            "warning_code": "model_pack_not_configured",
        },
        analysis_jobs=restarted_analysis,
        command_proposal_generator=ReadyDinoStatus(),
        experimental_proposals=object(),
    )
    second_runtime = RestartRuntime(second_bridge, block_candidate=False)
    second = PipelineJobService(second_runtime, data_root=data_root)
    waiting = _wait(
        second,
        project_id,
        started["job_id"],
        {"awaiting_review", "failed"},
        timeout=10,
    )
    assert waiting["status"] == "awaiting_review", waiting
    assert waiting["job_id"] == started["job_id"]
    assert waiting["snapshot_id"] == snapshot["snapshot_id"]
    assert waiting["recovered_after_restart"] is True
    assert waiting["provenance"]["source"]["snapshot_id"] == str(
        snapshot["snapshot_id"]
    )
    assert restarted_analysis.start_attempts == 2
    assert second_runtime.calls[0] == "candidate_rescan_5_10fps"
    assert "dense_index_2fps" not in second_runtime.calls
    assert dense_index.is_file()
    final_dense_stage = next(
        item
        for item in waiting["stages"]
        if item["name"] == "dense_index_2fps"
    )
    assert final_dense_stage["attempts"] == 1
    question_stage = next(
        item
        for item in waiting["stages"]
        if item["name"] == "question_queue"
    )
    assert question_stage["checkpoint"]["annotation_count"] == 0
    assert question_stage["checkpoint"]["incident_count"] == 0
    assert str(source) not in json.dumps(
        waiting, ensure_ascii=False, default=str
    )
    second.shutdown()
    second_media.shutdown()


def test_legacy_private_grant_migration_is_full_hash_verified_and_path_private(
    tmp_path,
):
    source = (tmp_path / "legacy-drive.mp4").resolve()
    source.write_bytes(b"previously authorized legacy video")
    source_stat = source.stat()
    created_time_ns = int(
        getattr(
            source_stat,
            "st_birthtime_ns",
            source_stat.st_ctime_ns,
        )
    )
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    data_root = (tmp_path / "bridge data").resolve()
    data_root.mkdir()
    project_id = uuid4()
    snapshot_id = uuid4()
    hash_job_id = uuid4()

    media = MediaIntakeService(minimum_interval_seconds=0)
    runtime = BridgePipelineRuntime(media, data_root=data_root)
    migrated = runtime.migrate_legacy_completed_media_grant(
        project_id=project_id,
        snapshot_id=snapshot_id,
        hash_job_id=hash_job_id,
        file_name=source.name,
        authorized_media_path=source,
        size_bytes=source_stat.st_size,
        created_time_ns=created_time_ns,
        modified_time_ns=source_stat.st_mtime_ns,
        source_sha256=source_sha256,
    )
    assert migrated["status"] == "migrated"
    assert migrated["path_exposed"] is False
    assert str(source) not in json.dumps(migrated, ensure_ascii=False)
    grant_record = (
        data_root
        / "projects"
        / str(project_id)
        / "pipeline"
        / "private-media-grants"
        / f"{hash_job_id}.json"
    )
    assert grant_record.is_file()
    media.shutdown()

    restarted_media = MediaIntakeService(minimum_interval_seconds=0)
    restarted_runtime = BridgePipelineRuntime(
        restarted_media,
        data_root=data_root,
    )
    restored = restarted_runtime._restore_private_media_grant(
        project_id=project_id,
        snapshot_id=snapshot_id,
        hash_job_id=hash_job_id,
        file_name=source.name,
        source_sha256=source_sha256,
        total_bytes=source_stat.st_size,
        cancel=lambda: False,
    )
    assert restored.sha256_lower == source_sha256
    assert restored.media_path == source
    restarted_media.shutdown()

    source.write_bytes(b"tampered legacy video with same bytes!")
    rejected_media = MediaIntakeService(minimum_interval_seconds=0)
    rejected_runtime = BridgePipelineRuntime(
        rejected_media,
        data_root=data_root,
    )
    with pytest.raises(PipelineStageFailure) as rejected:
        rejected_runtime._restore_private_media_grant(
            project_id=project_id,
            snapshot_id=snapshot_id,
            hash_job_id=hash_job_id,
            file_name=source.name,
            source_sha256=source_sha256,
            total_bytes=source_stat.st_size,
            cancel=lambda: False,
        )
    assert (
        rejected.value.code
        == "private_media_grant_revalidation_failed"
    )
    rejected_media.shutdown()


def test_blocked_job_restart_restores_verified_record_sha256(tmp_path):
    root = tmp_path.resolve()
    first_runtime = FakeRuntime()
    first_runtime.model_ready = False
    first = PipelineJobService(first_runtime, data_root=root)
    project_id = uuid4()
    _configure(first, project_id)
    started, _ = _start(first, project_id, suffix="blocked-restart")
    blocked = _wait(
        first, project_id, started["job_id"], {"blocked"}
    )
    assert blocked["current_stage"] == "candidate_rescan_5_10fps"
    assert blocked["outcome_code"] == "model_unavailable"
    assert len(blocked["record_sha256"]) == 64
    expected_record_sha256 = blocked["record_sha256"]
    first.shutdown()

    second = PipelineJobService(FakeRuntime(), data_root=root)
    reloaded = second.get(project_id, started["job_id"])
    assert reloaded["status"] == "blocked"
    assert reloaded["current_stage"] == "candidate_rescan_5_10fps"
    assert reloaded["record_sha256"] == expected_record_sha256
    assert second.recovery_summary()["jobs_loaded"] == 1
    assert second.recovery_summary()["jobs_requeued"] == 0
    second.shutdown()


def test_project_scope_and_cancel_are_enforced(tmp_path):
    runtime = FakeRuntime()
    runtime.block_stage = "intake_validation"
    service = PipelineJobService(runtime, data_root=tmp_path.resolve())
    project_id = uuid4()
    _configure(service, project_id)
    started, _ = _start(service, project_id, suffix="cancel")
    assert runtime.block_started.wait(1)
    with pytest.raises(Exception):
        service.get(uuid4(), started["job_id"])
    service.cancel(project_id, started["job_id"])
    runtime.block_release.set()
    cancelled = _wait(
        service, project_id, started["job_id"], {"cancelled"}
    )
    assert cancelled["outcome_code"] == "cancelled"
    assert runtime.cancel_calls == 1
    service.shutdown()


def test_bridge_runtime_matches_each_selected_local_pack_before_ready(tmp_path):
    class UnusedMediaIntake:
        pass

    status = {
        "enabled": True,
        "pack_id": "road-detector-v1",
        "capabilities": [
            "candidate_rescan_5_10fps",
            "detection",
            "ocr",
            "segmentation",
            "tracking",
        ],
        "warning_code": None,
    }
    runtime = BridgePipelineRuntime(
        UnusedMediaIntake(),
        data_root=tmp_path.resolve(),
        vision_status=lambda: status,
    )
    profile = {
        "detector_pack_id": "road-detector-v1",
        "ocr_pack_id": "different-ocr-v1",
        "sam2_pack_id": "different-sam-v1",
        "report_provider": "codex",
    }
    capabilities = runtime.capability_snapshot(profile)
    assert capabilities["candidate_rescan_5_10fps"] == "ready"
    assert capabilities["detector"] == "ready"
    assert capabilities["ocr"] == "missing"
    assert capabilities["sam2_segmentation_tracking"] == "missing"
    assert capabilities["bulk_frame_provider"] == "local_models_only"


def test_bridge_runtime_prepares_honest_dino_review_queue_without_full_pack(
    tmp_path,
):
    project_id = uuid4()
    snapshot_id = uuid4()
    analysis_job_id = uuid4()
    pipeline_job_id = uuid4()
    source_sha256 = hashlib.sha256(b"source").hexdigest()
    candidate_a = {
        "candidate_id": "a" * 20,
        "representative_timestamp_seconds": 1.0,
    }
    candidate_b = {
        "candidate_id": "b" * 20,
        "representative_timestamp_seconds": 1.5,
    }
    analysis_row = {
        "job_id": analysis_job_id,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "hash_job_id": uuid4(),
        "file_name": "drive.mp4",
        "source_sha256": source_sha256,
        "status": "completed",
        "result": {
            "candidates": [candidate_a, candidate_b],
            "provenance": {"effective_cadence_seconds": 15.0},
        },
    }

    class FakeAnalysisJobs:
        def list(self, requested_project_id, *, limit):
            assert requested_project_id == project_id
            assert limit == 128
            return [analysis_row]

        def get(self, requested_project_id, requested_job_id):
            assert requested_project_id == project_id
            assert requested_job_id == analysis_job_id
            return analysis_row

        def resolve_artifact(self, *_args):
            raise AssertionError("fake generator does not resolve images")

    class FakeGenerator:
        def status(self):
            return {
                "state": "ready",
                "error_code": None,
                "review_only": True,
                "bulk_frame_detection_provider": "local_model",
                "pack_id": "dino-review",
                "pack_version": "1.0",
                "manifest_sha256": hashlib.sha256(b"dino").hexdigest(),
            }

        def generate(self, *, command_plan, candidates, **_kwargs):
            proposals = ()
            if command_plan["issue_type"] == "road_debris":
                proposals = tuple(
                    {
                        "candidate_id": candidate["candidate_id"],
                        "status": "classified",
                        "predictions": [
                            {
                                "issue_type": "road_debris",
                                "model_score": (
                                    0.8
                                    if candidate["candidate_id"] == "a" * 20
                                    else 0.9
                                ),
                                "relative_score": None,
                                "box": {
                                    "x": 0.2,
                                    "y": 0.2,
                                    "width": 0.2,
                                    "height": 0.2,
                                },
                                "source_label": "road debris",
                                "calibration": "model_score",
                            }
                        ],
                    }
                    for candidate in candidates
                )
            return SimpleNamespace(
                available=True,
                generated=True,
                model={
                    "provider": "transformers_grounding_dino",
                    "pack_id": "dino-review",
                    "pack_version": "1.0",
                    "manifest_sha256": hashlib.sha256(b"dino").hexdigest(),
                },
                evaluation={
                    "evaluation_id": "source",
                    "producer": "test",
                    "producer_version": "1.0",
                },
                proposals=proposals,
            )

    class FakeProposalStore:
        def __init__(self):
            self.evaluation_id = None
            self.rows = {}

        def record_run(
            self,
            _project_id,
            _analysis_job_id,
            *,
            evaluation,
            proposals,
            **_kwargs,
        ):
            self.evaluation_id = evaluation["evaluation_id"]
            self.rows = {
                row["candidate_id"]: {
                    "candidate_id": row["candidate_id"],
                    "predictions": row["predictions"],
                }
                for row in proposals
            }
            return list(self.rows.values())

        def latest_for_job(
            self,
            _project_id,
            _analysis_job_id,
            *,
            evaluation_id=None,
        ):
            if (
                evaluation_id is not None
                and evaluation_id != self.evaluation_id
            ):
                return {}
            return self.rows

    class FakeLabels:
        records = []

        def latest_answers(self, *_args, **_kwargs):
            return list(self.records)

    class FakeWeakCases:
        def search(self, **_kwargs):
            return {
                "capability_state": "weak_reference_only",
                "cases": [],
            }

    proposal_store = FakeProposalStore()
    labels = FakeLabels()
    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        vision_status=lambda: {
            "enabled": False,
            "warning_code": "model_pack_not_configured",
        },
        analysis_jobs=FakeAnalysisJobs(),
        command_proposal_generator=FakeGenerator(),
        experimental_proposals=proposal_store,
        candidate_labels=labels,
        weak_case_catalog=FakeWeakCases(),
    )
    profile = {
        "detector_pack_id": "full-road-pack",
        "ocr_pack_id": "korean-ocr",
        "sam2_pack_id": "sam2",
        "report_provider": "codex",
        "candidate_rescan_fps": 8,
        "review_mode": "precision",
        "question_policy": "learning_assist",
    }
    capabilities = runtime.capability_snapshot(profile)
    assert capabilities["dino_review_only_boxes"] == "ready"
    context = {
        "job_id": str(pipeline_job_id),
        "project_id": str(project_id),
        "snapshot_id": str(snapshot_id),
        "file_name": "drive.mp4",
        "profile": profile,
        "capabilities": capabilities,
        "checkpoints": {
            "sha256": {
                "source_sha256": source_sha256,
                "hash_job_id": str(analysis_row["hash_job_id"]),
            }
        },
        "review_completion_id": None,
    }
    rescan = runtime.execute(
        "candidate_rescan_5_10fps", context, lambda: False
    )
    assert rescan.outcome == "success"
    assert rescan.checkpoint["high_frequency_rescan_performed"] is False
    context["checkpoints"]["candidate_rescan_5_10fps"] = dict(
        rescan.checkpoint
    )

    detection = runtime.execute(
        "detection_ocr", context, lambda: False
    )
    assert detection.outcome == "success"
    assert detection.checkpoint["dino_review_only"] is True
    assert detection.checkpoint["ocr_status"] == "model_unavailable"
    assert detection.checkpoint["annotation_count"] == 0
    assert detection.checkpoint["incident_count"] == 0
    context["checkpoints"]["detection_ocr"] = dict(
        detection.checkpoint
    )

    sam2 = runtime.execute(
        "sam2_segmentation_tracking", context, lambda: False
    )
    assert sam2.checkpoint["mask_generated"] is False
    context["checkpoints"]["sam2_segmentation_tracking"] = dict(
        sam2.checkpoint
    )
    temporal = runtime.execute(
        "temporal_merge", context, lambda: False
    )
    assert temporal.metrics["input_detection_count"] == 2
    assert temporal.metrics["temporal_group_count"] == 1
    assert temporal.checkpoint["question_candidate_ids"] == ["b" * 20]
    context["checkpoints"]["temporal_merge"] = dict(
        temporal.checkpoint
    )
    weak_cases = runtime.execute(
        "similar_case_search", context, lambda: False
    )
    assert weak_cases.checkpoint["approved_reference_count"] == 0
    context["checkpoints"]["similar_case_search"] = dict(
        weak_cases.checkpoint
    )
    vision = runtime.execute(
        "vision_proposal", context, lambda: False
    )
    assert vision.checkpoint["llm_bulk_frame_detection_used"] is False
    context["checkpoints"]["vision_proposal"] = dict(
        vision.checkpoint
    )
    queue = runtime.execute(
        "question_queue", context, lambda: False
    )
    assert queue.metrics["question_count"] == 1
    assert queue.checkpoint["annotation_count"] == 0
    assert queue.checkpoint["incident_count"] == 0
    assert "korean_ocr" in queue.checkpoint["capability_missing"]
    context["checkpoints"]["question_queue"] = dict(queue.checkpoint)

    record_id = str(uuid4())
    record_hash = hashlib.sha256(b"answer").hexdigest()
    labels.records = [
        {
            "candidate_id": "b" * 20,
            "record_id": record_id,
            "record_hash": record_hash,
            "answer_revision": 1,
        }
    ]
    proof_line = "\0".join(
        ("b" * 20, record_id, record_hash, "1")
    )
    completion_id = (
        "review:"
        + hashlib.sha256(proof_line.encode("utf-8")).hexdigest()
    )
    proof = runtime.validate_review_completion(
        context, completion_id
    )
    assert proof["all_required_questions_answered"] is True
    assert "annotation_count_before_projection" not in proof
    assert "incident_count_before_projection" not in proof


def test_similar_case_stage_preserves_project_approved_provenance(
    tmp_path,
):
    project_id = uuid4()

    class FakeWeakCases:
        def search(self, **_kwargs):
            return {
                "capability_state": "weak_reference_only",
                "cases": [
                    {
                        "source_id": (
                            "sample-hwpx-3q-0123456789abcdef"
                        ),
                        "case_id": (
                            "hwpx-weak-case-" + "a" * 32
                        ),
                        "quarter": 3,
                        "document_sha256": "b" * 64,
                        "image_sha256": "c" * 64,
                        "index_manifest_sha256": "d" * 64,
                        "picture_target_id": (
                            "hwpx-picture-" + "e" * 24
                        ),
                        "cell_target_id": (
                            "hwpx-cell-" + "f" * 24
                        ),
                        "reference_state": "weak_unconfirmed",
                        "review_required": True,
                        "approved_reference": False,
                    }
                ],
            }

    class FakeApprovals:
        def decorate_search(self, actual_project_id, result):
            assert actual_project_id == project_id
            decorated = dict(result)
            decorated["capability_state"] = (
                "approved_reference_available"
            )
            decorated["cases"] = [
                {
                    **result["cases"][0],
                    "reference_state": "approved_reference",
                    "review_required": False,
                    "approved_reference": True,
                    "approval_record_sha256": "1" * 64,
                }
            ]
            decorated["approved_reference_count"] = 1
            return decorated

    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        weak_case_catalog=FakeWeakCases(),
        weak_case_approvals=FakeApprovals(),
    )
    result = runtime._search_weak_reference_cases(
        {
            "project_id": str(project_id),
            "checkpoints": {
                "detection_ocr": {
                    "detected_issue_types": [
                        "roadside_mowing_required"
                    ]
                }
            },
        }
    )

    assert result.checkpoint["approved_reference_count"] == 1
    assert result.checkpoint["review_only"] is False
    assert result.checkpoint["references"] == [
        {
            "source_id": "sample-hwpx-3q-0123456789abcdef",
            "case_id": "hwpx-weak-case-" + "a" * 32,
            "issue_type": "roadside_mowing_required",
            "quarter": 3,
            "document_sha256": "b" * 64,
            "image_sha256": "c" * 64,
            "index_manifest_sha256": "d" * 64,
            "picture_target_id": "hwpx-picture-" + "e" * 24,
            "cell_target_id": "hwpx-cell-" + "f" * 24,
            "reference_state": "approved_reference",
            "review_required": False,
            "approval_record_sha256": "1" * 64,
        }
    ]
