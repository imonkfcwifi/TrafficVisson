from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.candidate_labels import CandidateLabelStore
from app.derived_outputs import (
    DerivedOutputsConflict,
    DerivedOutputsRequest,
    DerivedOutputsService,
)


PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
PIPELINE_JOB_ID = UUID("22222222-2222-4222-8222-222222222222")
ANALYSIS_JOB_ID = UUID("33333333-3333-4333-8333-333333333333")
SOURCE_SHA256 = hashlib.sha256(b"derived-output-source").hexdigest()
CANDIDATE_A = "a" * 20
CANDIDATE_B = "b" * 20


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate(
    evidence_root: Path,
    candidate_id: str,
    *,
    timestamp: float,
):
    image = evidence_root / f"{candidate_id}.jpg"
    clip = evidence_root / f"{candidate_id}.mp4"
    image.write_bytes(b"\xff\xd8\xff\xe0" + candidate_id.encode("ascii"))
    clip.write_bytes(
        b"\x00\x00\x00\x18ftypisom" + candidate_id.encode("ascii")
    )
    return {
        "candidate_id": candidate_id,
        "start_seconds": timestamp - 0.25,
        "representative_timestamp_seconds": timestamp,
        "end_seconds": timestamp + 0.25,
        "representative_image": {
            "artifact_ref": (
                f"analysis:{ANALYSIS_JOB_ID}:{candidate_id}.jpg"
            ),
            "relative_path": f"{candidate_id}.jpg",
            "media_type": "image/jpeg",
            "size_bytes": image.stat().st_size,
            "sha256": _sha256(image),
        },
        "preview_clip": {
            "artifact_ref": (
                f"analysis:{ANALYSIS_JOB_ID}:{candidate_id}.mp4"
            ),
            "relative_path": f"{candidate_id}.mp4",
            "media_type": "video/mp4",
            "size_bytes": clip.stat().st_size,
            "sha256": _sha256(clip),
        },
        "ai_inference": {
            "mode": "local_model",
            "provider": "test_detector",
            "predictions": [
                {
                    "issue_type": (
                        "road_debris"
                        if candidate_id == CANDIDATE_A
                        else "sign_damage"
                    ),
                    "confidence": 0.8,
                    "box": {
                        "x": 0.1,
                        "y": 0.2,
                        "width": 0.3,
                        "height": 0.2,
                    },
                }
            ],
        },
        "_image_path": image,
        "_clip_path": clip,
    }


def _record(
    candidate: dict,
    *,
    answer: str,
    previous_hash: str | None,
):
    question_id = str(
        CandidateLabelStore.question_id(
            ANALYSIS_JOB_ID, candidate["candidate_id"]
        )
    )
    record_id = str(uuid4())
    final_answer = {
        "answer": answer,
        "selected_issue_type": (
            "road_debris" if answer == "yes" else None
        ),
        "severity": "major" if answer == "yes" else None,
        "maintenance_action": (
            "낙하물 수거" if answer == "yes" else None
        ),
        "ocr_text": None,
        "normalized_box": None,
        "include_in_training": answer in {"yes", "no"},
        "answered_by": "local_user",
    }
    seed = {
        "record_id": record_id,
        "project_id": str(PROJECT_ID),
        "analysis_job_id": str(ANALYSIS_JOB_ID),
        "candidate_id": candidate["candidate_id"],
        "question_id": question_id,
        "answer_revision": 1,
        "partition": "approved" if answer == "yes" else "negative",
        "final_answer": final_answer,
        "recorded_at": "2026-07-29T05:00:00.000Z",
        "previous_record_hash": previous_hash,
    }
    record_hash = hashlib.sha256(
        json.dumps(seed, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        **seed,
        "record_hash": record_hash,
        "ai_original_candidate": {
            key: value
            for key, value in candidate.items()
            if not key.startswith("_")
        },
    }


def _ledger(records):
    proofs = [
        "\0".join(
            (
                record["candidate_id"],
                record["record_id"].lower(),
                record["record_hash"],
                str(record["answer_revision"]),
            )
        )
        for record in records
    ]
    return hashlib.sha256(
        "\n".join(sorted(proofs)).encode("utf-8")
    ).hexdigest()


class FakeCandidateLabels:
    def __init__(self, records):
        self.records = records

    def audit_records(self, project_id, *, analysis_job_id=None):
        assert project_id == PROJECT_ID
        if analysis_job_id is None:
            return list(self.records)
        assert analysis_job_id == ANALYSIS_JOB_ID
        return [
            row
            for row in self.records
            if row["analysis_job_id"] == str(analysis_job_id)
        ]

    def latest_answers(
        self,
        project_id,
        *,
        analysis_job_id=None,
        partition=None,
    ):
        assert project_id == PROJECT_ID
        assert analysis_job_id == ANALYSIS_JOB_ID
        rows = [
            row
            for row in self.records
            if row["analysis_job_id"] == str(analysis_job_id)
        ]
        if partition is not None:
            rows = [
                row for row in rows if row["partition"] == partition
            ]
        return rows


class FakeStore:
    def __init__(self, approved_record, candidate):
        self.projected = []
        self.materialized_job_ids = []
        self.incident = {
            "incident_id": approved_record["question_id"],
            "project_id": str(PROJECT_ID),
            "question_id": approved_record["question_id"],
            "analysis_job_id": str(ANALYSIS_JOB_ID),
            "candidate_id": approved_record["candidate_id"],
            "status": "active",
            "issue_type": "road_debris",
            "severity": "major",
            "maintenance_action": "낙하물 수거",
            "ocr_text": None,
            "answer_revision": 1,
            "source_answer_record_id": approved_record["record_id"],
            "source_record_hash": approved_record["record_hash"],
            "evidence_image_ref": candidate["representative_image"][
                "artifact_ref"
            ],
            "preview_clip_ref": candidate["preview_clip"]["artifact_ref"],
            "start_time_ms": round(candidate["start_seconds"] * 1000),
            "representative_time_ms": round(
                candidate["representative_timestamp_seconds"] * 1000
            ),
            "end_time_ms": round(candidate["end_seconds"] * 1000),
        }

    def project_label_records(
        self,
        project_id,
        records,
        *,
        materialize_analysis_job_ids=None,
    ):
        assert project_id == PROJECT_ID
        self.projected.append(list(records))
        self.materialized_job_ids.append(
            list(materialize_analysis_job_ids or [])
        )
        return []

    def list_incidents(
        self,
        project_id,
        *,
        include_inactive=False,
        analysis_job_id=None,
    ):
        assert project_id == PROJECT_ID
        assert include_inactive is False
        assert analysis_job_id == ANALYSIS_JOB_ID
        return [dict(self.incident)]


class EmptyStore:
    def __init__(self):
        self.projected = []

    def project_label_records(
        self,
        project_id,
        records,
        *,
        materialize_analysis_job_ids=None,
    ):
        self.projected.append(
            (
                project_id,
                list(records),
                list(materialize_analysis_job_ids or []),
            )
        )
        return []

    def list_incidents(
        self,
        project_id,
        *,
        include_inactive=False,
        analysis_job_id=None,
    ):
        assert project_id == PROJECT_ID
        assert include_inactive is False
        assert analysis_job_id == ANALYSIS_JOB_ID
        return []


class FakeAnalysisJobs:
    def __init__(self, candidates):
        self.candidates = candidates

    def get(self, project_id, job_id):
        assert project_id == PROJECT_ID
        assert job_id == ANALYSIS_JOB_ID
        return {
            "job_id": ANALYSIS_JOB_ID,
            "project_id": PROJECT_ID,
            "status": "completed",
            "source_sha256": SOURCE_SHA256,
            "result": {
                "candidate_count": len(self.candidates),
                "candidates": [
                    {
                        key: value
                        for key, value in candidate.items()
                        if not key.startswith("_")
                    }
                    for candidate in self.candidates
                ],
                "provenance": {"source_sha256": SOURCE_SHA256},
            },
        }

    def resolve_artifact(
        self,
        project_id,
        job_id,
        candidate_id,
        kind,
    ):
        assert project_id == PROJECT_ID
        assert job_id == ANALYSIS_JOB_ID
        candidate = next(
            item
            for item in self.candidates
            if item["candidate_id"] == candidate_id
        )
        metadata = (
            candidate["representative_image"]
            if kind == "image"
            else candidate["preview_clip"]
        )
        return SimpleNamespace(
            path=(
                candidate["_image_path"]
                if kind == "image"
                else candidate["_clip_path"]
            ),
            media_type=metadata["media_type"],
            size_bytes=metadata["size_bytes"],
            sha256=metadata["sha256"],
        )


class FakeReportPreparer:
    def __init__(self):
        self.calls = []

    def prepare(self, **kwargs):
        assert kwargs["explicit_transmission_consent"] is True
        self.calls.append(kwargs)
        return {
            "preview_id": "report-preview-1",
            "plan_sha256": "c" * 64,
            "draft_sha256": "d" * 64,
            "operation_count": 3,
        }


def _request(records, **overrides):
    ledger = _ledger(records)
    values = {
        "project_id": PROJECT_ID,
        "pipeline_job_id": PIPELINE_JOB_ID,
        "analysis_job_id": ANALYSIS_JOB_ID,
        "source_sha256": SOURCE_SHA256,
        "pipeline_input_fingerprint": "1" * 64,
        "pipeline_profile_sha256": "2" * 64,
        "review_completion_id": f"review:{ledger}",
        "answer_ledger_sha256": ledger,
        "question_candidate_ids": (CANDIDATE_A, CANDIDATE_B),
        "template_id": "sample-4q-v1",
        "template_sha256": "3" * 64,
        "report_provider": "codex",
        "report_transmission_consent": False,
        "template_semantic_map_ready": False,
        "report_provider_ready": False,
    }
    values.update(overrides)
    return DerivedOutputsRequest(**values)


def _fixture(tmp_path, *, report_preparer=None):
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    candidate_a = _candidate(
        evidence_root, CANDIDATE_A, timestamp=1.0
    )
    candidate_b = _candidate(
        evidence_root, CANDIDATE_B, timestamp=2.0
    )
    approved = _record(
        candidate_a, answer="yes", previous_hash=None
    )
    negative = _record(
        candidate_b,
        answer="no",
        previous_hash=approved["record_hash"],
    )
    records = [approved, negative]
    store = FakeStore(approved, candidate_a)
    service = DerivedOutputsService(
        data_root=(tmp_path / "bridge-data").resolve(),
        candidate_labels=FakeCandidateLabels(records),
        store=store,
        analysis_jobs=FakeAnalysisJobs([candidate_a, candidate_b]),
        report_preparer=report_preparer,
    )
    return service, store, records


def test_reviewed_outputs_are_exact_job_idempotent_and_report_blocked(
    tmp_path,
):
    report_preparer = FakeReportPreparer()
    service, store, records = _fixture(
        tmp_path, report_preparer=report_preparer
    )
    request = _request(records)

    first = service.generate(request)
    second = service.generate(request)

    assert first.replayed is False
    assert second.replayed is True
    assert first.checkpoint == second.checkpoint
    assert first.checkpoint["state"] == "partial"
    assert first.checkpoint["report"] == {
        "state": "blocked",
        "blocking_reasons": [
            "report_transmission_consent_required",
            "template_semantic_map_unavailable",
            "report_provider_unavailable",
        ],
        "provider_called": False,
        "document_edit_plan_prepared": False,
        "com_invoked": False,
    }
    assert report_preparer.calls == []
    assert first.metrics["raw_detection_count"] == 2
    assert first.metrics["reviewed_answer_count"] == 2
    assert first.metrics["annotation_count"] == 2
    assert first.metrics["incident_count"] == 1
    assert first.metrics["route_point_count"] == 0
    assert first.metrics["evidence_file_count"] == 2
    assert len(store.projected) == 2
    assert all(len(prefix) == 2 for prefix in store.projected)
    assert store.materialized_job_ids == [
        [ANALYSIS_JOB_ID],
        [ANALYSIS_JOB_ID],
    ]

    serialized_checkpoint = json.dumps(
        first.checkpoint, ensure_ascii=False
    )
    assert str(tmp_path) not in serialized_checkpoint
    assert "\\" not in serialized_checkpoint
    assert "낙하물 수거" not in serialized_checkpoint
    assert "DocumentEditPlan" not in serialized_checkpoint
    assert first.checkpoint["absolute_paths_exposed"] is False
    assert first.checkpoint["com_invoked"] is False

    export_root = tmp_path / "bridge-data" / "exports"
    bundles = list(export_root.glob("trafficsupervision-export-*"))
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
    with (bundle / "route_points.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as source:
        route_rows = list(csv.DictReader(source))
    assert len(raw_rows) == 2
    assert len(reviewed_rows) == 1
    assert reviewed_rows[0]["event_type"] == "road_debris"
    assert route_rows == []

    manifest = json.loads(
        (bundle / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["provenance"]["pipeline_job_id"] == str(
        PIPELINE_JOB_ID
    )
    assert manifest["provenance"]["analysis_job_id"] == str(
        ANALYSIS_JOB_ID
    )
    assert manifest["provenance"]["answer_ledger_sha256"] == (
        request.answer_ledger_sha256
    )
    assert manifest["provenance"]["gpx_track"]["status"] == (
        "not_attached"
    )
    assert manifest["record_counts"]["incidents"] == 1
    assert manifest["record_counts"]["route_points"] == 0
    assert manifest["evidence"]["incident_count"] == 1
    assert manifest["evidence"]["file_count"] == 2


def test_zero_detections_exports_empty_bundle_without_review_projection(
    tmp_path,
):
    empty_ledger_sha256 = hashlib.sha256(b"").hexdigest()
    store = EmptyStore()
    service = DerivedOutputsService(
        data_root=(tmp_path / "bridge-data").resolve(),
        candidate_labels=FakeCandidateLabels([]),
        store=store,
        analysis_jobs=FakeAnalysisJobs([]),
    )
    request = DerivedOutputsRequest(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        source_sha256=SOURCE_SHA256,
        pipeline_input_fingerprint="1" * 64,
        pipeline_profile_sha256="2" * 64,
        review_completion_id=(
            f"zero-detections:{empty_ledger_sha256}"
        ),
        answer_ledger_sha256=empty_ledger_sha256,
        question_candidate_ids=(),
        template_id="sample-4q-v1",
        template_sha256="3" * 64,
        report_provider="codex",
        report_transmission_consent=False,
        template_semantic_map_ready=False,
        report_provider_ready=False,
        completion_mode="zero_detections",
    )

    first = service.generate(request)
    second = service.generate(request)

    assert first.replayed is False
    assert second.replayed is True
    assert first.checkpoint == second.checkpoint
    assert first.checkpoint["state"] == "completed"
    assert first.checkpoint["completion_mode"] == "zero_detections"
    assert first.checkpoint["report"] == {
        "state": "not_applicable_no_approved_incidents",
        "blocking_reasons": [],
        "provider_called": False,
        "document_edit_plan_prepared": False,
        "com_invoked": False,
    }
    assert first.metrics["raw_detection_count"] == 0
    assert first.metrics["reviewed_answer_count"] == 0
    assert first.metrics["annotation_count"] == 0
    assert first.metrics["incident_count"] == 0
    assert first.metrics["evidence_file_count"] == 0
    assert store.projected == []

    bundles = list(
        (tmp_path / "bridge-data" / "exports").glob(
            "trafficsupervision-export-*"
        )
    )
    assert len(bundles) == 1
    bundle = bundles[0]
    for file_name in (
        "raw_detections.csv",
        "reviewed_incidents.csv",
        "label_questions.csv",
        "annotations.csv",
        "ocr_observations.csv",
        "maintenance_actions.csv",
    ):
        with (bundle / file_name).open(
            "r", encoding="utf-8-sig", newline=""
        ) as source:
            assert list(csv.DictReader(source)) == []
    manifest = json.loads(
        (bundle / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["provenance"]["completion_mode"] == (
        "zero_detections"
    )
    assert manifest["provenance"]["answer_ledger_sha256"] == (
        empty_ledger_sha256
    )
    assert manifest["record_counts"]["detections"] == 0
    assert manifest["record_counts"]["incidents"] == 0


def test_zero_detections_rejects_a_nonempty_analysis(tmp_path):
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    candidate = _candidate(
        evidence_root, CANDIDATE_A, timestamp=1.0
    )
    empty_ledger_sha256 = hashlib.sha256(b"").hexdigest()
    service = DerivedOutputsService(
        data_root=(tmp_path / "bridge-data").resolve(),
        candidate_labels=FakeCandidateLabels([]),
        store=EmptyStore(),
        analysis_jobs=FakeAnalysisJobs([candidate]),
    )
    request = DerivedOutputsRequest(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        source_sha256=SOURCE_SHA256,
        pipeline_input_fingerprint="1" * 64,
        pipeline_profile_sha256="2" * 64,
        review_completion_id=(
            f"zero-detections:{empty_ledger_sha256}"
        ),
        answer_ledger_sha256=empty_ledger_sha256,
        question_candidate_ids=(),
        template_id="sample-4q-v1",
        template_sha256="3" * 64,
        report_provider="none",
        report_transmission_consent=False,
        template_semantic_map_ready=False,
        report_provider_ready=False,
        completion_mode="zero_detections",
    )

    with pytest.raises(
        DerivedOutputsConflict,
        match="contains candidates",
    ):
        service.generate(request)


def test_forged_review_ledger_creates_no_export(tmp_path):
    service, store, records = _fixture(tmp_path)
    request = _request(
        records,
        answer_ledger_sha256="f" * 64,
        review_completion_id=f"review:{'f' * 64}",
    )

    with pytest.raises(
        DerivedOutputsConflict,
        match="server answer ledger",
    ):
        service.generate(request)

    export_root = tmp_path / "bridge-data" / "exports"
    assert list(export_root.iterdir()) == []
    assert store.projected == []


def test_source_mismatch_does_not_project_or_export(tmp_path):
    service, store, records = _fixture(tmp_path)
    request = _request(
        records,
        source_sha256=hashlib.sha256(b"other-source").hexdigest(),
    )

    with pytest.raises(
        DerivedOutputsConflict,
        match="analysis provenance",
    ):
        service.generate(request)

    assert store.projected == []
    export_root = tmp_path / "bridge-data" / "exports"
    assert list(export_root.iterdir()) == []


def test_report_preparer_runs_only_with_all_explicit_gates(tmp_path):
    report_preparer = FakeReportPreparer()
    service, _store, records = _fixture(
        tmp_path, report_preparer=report_preparer
    )
    request = _request(
        records,
        report_transmission_consent=True,
        template_semantic_map_ready=True,
        report_provider_ready=True,
    )

    result = service.generate(request)

    assert result.checkpoint["state"] == "completed"
    assert result.checkpoint["report"] == {
        "state": "validated_preview",
        "preview_id": "report-preview-1",
        "plan_sha256": "c" * 64,
        "draft_sha256": "d" * 64,
        "operation_count": 3,
        "blocking_reasons": [],
        "provider_called": True,
        "document_edit_plan_prepared": True,
        "com_invoked": False,
    }
    assert len(report_preparer.calls) == 1
    approved = report_preparer.calls[0]["approved_incidents"]
    assert len(approved) == 1
    assert approved[0]["incident_id"] == str(
        CandidateLabelStore.question_id(
            ANALYSIS_JOB_ID, CANDIDATE_A
        )
    )
    assert len(approved[0]["evidence_refs"]) == 1
    assert approved[0]["evidence_refs"][0].startswith(
        f"analysis:{ANALYSIS_JOB_ID}:"
    )
    assert "preview_clip_ref" not in approved[0]


def test_custom_output_location_is_not_treated_as_a_path(tmp_path):
    service, _store, records = _fixture(tmp_path)
    request = _request(records, output_location_id="renderer-selected")

    with pytest.raises(
        DerivedOutputsConflict,
        match="server-owned resolver",
    ):
        service.generate(request)

    assert list((tmp_path / "bridge-data" / "exports").iterdir()) == []
