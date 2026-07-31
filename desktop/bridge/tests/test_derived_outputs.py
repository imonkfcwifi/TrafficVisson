from __future__ import annotations

import csv
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import pytest

from app.candidate_labels import CandidateLabelStore
from app.approved_incident_authority import canonical_sha256
from app.current_incident_report_binder import (
    deidentification_record_sha256,
)
from app.derived_outputs import (
    DatasetSnapshotFailed,
    DerivedOutputsConflict,
    DerivedOutputsCorrupt,
    DerivedOutputsRequest,
    DerivedOutputsService,
    ResolvedDerivedOutputsArtifactRoot,
    _safe_report_preview,
)
from app.pipeline_report_preview import (
    ExactSamplePlanBindingUnavailable,
)


PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
PIPELINE_JOB_ID = UUID("22222222-2222-4222-8222-222222222222")
ANALYSIS_JOB_ID = UUID("33333333-3333-4333-8333-333333333333")
SOURCE_SHA256 = hashlib.sha256(b"derived-output-source").hexdigest()
CANDIDATE_A = "a" * 20
CANDIDATE_B = "b" * 20
QUESTION_QUEUE_REVISION = "7" * 64
REVIEW_EVENT_SET_ID = "res_" + ("8" * 32)
REVIEW_EVENT_SET_REVISION = "9" * 64
REVIEW_EVENT_SET_MANIFEST_SHA256 = "a" * 64


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
        "review_event_binding": {
            "schema_version": "trafficvisson.review-event-binding.v1",
            "review_event_set_id": REVIEW_EVENT_SET_ID,
            "review_event_set_revision": REVIEW_EVENT_SET_REVISION,
            "review_event_set_manifest_sha256": (
                REVIEW_EVENT_SET_MANIFEST_SHA256
            ),
            "review_event_id": (
                "rve_"
                + hashlib.sha256(
                    candidate_id.encode("ascii")
                ).hexdigest()[:32]
            ),
            "review_event_revision": hashlib.sha256(
                f"review-event:{candidate_id}".encode("ascii")
            ).hexdigest(),
            "representative_candidate_id": candidate_id,
            "review_only": True,
        },
        "_image_path": image,
        "_clip_path": clip,
    }


def _record(
    candidate: dict,
    *,
    answer: str,
    previous_hash: str | None,
    selected_issue_type: str | None = None,
    severity: str | None = None,
    maintenance_action: str | None = None,
    ocr_text: str | None = None,
    answered_by: str = "local_user",
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
            (selected_issue_type or "road_debris")
            if answer == "yes"
            else None
        ),
        "severity": (
            (severity or "major") if answer == "yes" else None
        ),
        "maintenance_action": (
            (
                maintenance_action
                if maintenance_action is not None
                else "낙하물 수거"
            )
            if answer == "yes"
            else None
        ),
        "ocr_text": ocr_text if answer == "yes" else None,
        "normalized_box": None,
        "include_in_training": answer in {"yes", "no"},
        "answered_by": answered_by,
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


def _survey_authority(ledger: str) -> dict:
    return {
        "schema_version": (
            "trafficvisson.survey-incident-authority.v2"
        ),
        "project_id": str(PROJECT_ID),
        "pipeline_job_id": str(PIPELINE_JOB_ID),
        "analysis_job_id": str(ANALYSIS_JOB_ID),
        "source_sha256": SOURCE_SHA256,
        "question_queue_revision": QUESTION_QUEUE_REVISION,
        "review_completion_id": f"review:{ledger}",
        "answer_ledger_sha256": ledger,
        "survey_qa_revision": "b" * 64,
        "model_version_id": "model-promoted-v1",
        "model_promotion_state": "promoted",
        "dataset_version_id": str(
            uuid5(PROJECT_ID, "promoted-dataset")
        ),
        "dataset_hash": "c" * 64,
        "dataset_provenance_graph_sha256": "d" * 64,
        "fixed_validation_sha256": "e" * 64,
        "primary_artifact_sha256": "f" * 64,
        "artifact_manifest_sha256": "1" * 64,
        "source_analysis_lineage_sha256": "2" * 64,
        "promoted_model_inference_sha256": "3" * 64,
        "dense_index_sha256": "4" * 64,
        "dense_candidate_discovery_revision": "5" * 64,
        "sampling_fps": 2,
        "scanned_frame_count": 42,
        "event_binding": {
            "state": "materialized",
            "review_event_set_id": REVIEW_EVENT_SET_ID,
            "review_event_set_revision": REVIEW_EVENT_SET_REVISION,
            "review_event_set_manifest_sha256": (
                REVIEW_EVENT_SET_MANIFEST_SHA256
            ),
        },
        "question_catalog_binding": {
            "state": "registered",
            "question_catalog_revision": "6" * 64,
        },
        "final_qa_choice": "no_miss_complete",
        "user_discovered_registrations_sha256": (
            hashlib.sha256(b"[]").hexdigest()
        ),
        "user_discovered_candidate_count": 0,
        "actor": "local_user",
        "independent_survey_verified": True,
    }


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
        self.incident_job_ids = []
        self.fixed_validation_job_ids = []
        self.incidents = []
        if (
            approved_record["final_answer"].get("answered_by")
            == "local_user"
        ):
            self.add_incident(approved_record, candidate)

    def add_incident(self, approved_record, candidate):
        answer = approved_record["final_answer"]
        self.incidents.append({
            "incident_id": approved_record["question_id"],
            "project_id": str(PROJECT_ID),
            "question_id": approved_record["question_id"],
            "analysis_job_id": str(ANALYSIS_JOB_ID),
            "candidate_id": approved_record["candidate_id"],
            "status": "active",
            "issue_type": answer["selected_issue_type"],
            "severity": answer["severity"],
            "maintenance_action": answer["maintenance_action"],
            "ocr_text": answer["ocr_text"],
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
        })

    def project_label_records(
        self,
        project_id,
        records,
        *,
        materialize_analysis_job_ids=None,
        incident_analysis_job_ids=None,
    ):
        assert project_id == PROJECT_ID
        self.projected.append(list(records))
        self.materialized_job_ids.append(
            list(materialize_analysis_job_ids or [])
        )
        self.incident_job_ids.append(
            list(incident_analysis_job_ids or [])
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
        return [dict(incident) for incident in self.incidents]

    def create_dataset_version(
        self,
        project_id,
        *,
        approved_reference_records=(),
        fixed_validation_analysis_job_ids=None,
    ):
        assert project_id == PROJECT_ID
        assert list(approved_reference_records) == []
        self.fixed_validation_job_ids.append(
            list(fixed_validation_analysis_job_ids or [])
        )
        members = []
        dataset_version_id = str(
            uuid5(PROJECT_ID, "derived-output-dataset")
        )
        dataset_hash = hashlib.sha256(
            b"derived-output-dataset"
        ).hexdigest()
        members_sha256 = hashlib.sha256(
            json.dumps(
                members,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        manifest = {
            "schema_version": "trafficvisson.dataset-version.v1",
            "project_id": str(PROJECT_ID),
            "dataset_id": dataset_version_id,
            "dataset_hash": dataset_hash,
            "approved_reference_members": members,
            "approved_reference_members_sha256": members_sha256,
            "approved_references_count_toward_training_gate": False,
            "approved_references_count_toward_validation": False,
            "counts": {
                "approved_reference_training_members": 0,
                "approved_reference_training_events": 0,
                "train": 2,
                "training_members": 2,
                "training_gate_members": 2,
            },
        }
        return {
            "dataset_version_id": dataset_version_id,
            "project_id": str(PROJECT_ID),
            "schema_version": "trafficvisson.dataset-version.v1",
            "dataset_hash": dataset_hash,
            "manifest": manifest,
            "approved_reference_members": members,
            "approved_reference_member_count": 0,
            "approved_reference_training_event_count": 0,
            "approved_reference_members_sha256": manifest[
                "approved_reference_members_sha256"
            ],
            "training_member_count": 2,
            "train_count": 2,
            "training_gate_member_count": 2,
            "approved_references_count_toward_training_gate": False,
            "approved_references_count_toward_validation": False,
            "reused": bool(self.projected[:-1]),
        }


class EmptyStore:
    def __init__(self):
        self.projected = []

    def project_label_records(
        self,
        project_id,
        records,
        *,
        materialize_analysis_job_ids=None,
        incident_analysis_job_ids=None,
    ):
        self.projected.append(
            (
                project_id,
                list(records),
                list(materialize_analysis_job_ids or []),
                list(incident_analysis_job_ids or []),
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
        if (
            kwargs["report_policy"]["evidence_mode"]
            == "shared_deidentified"
        ):
            assert kwargs["explicit_transmission_consent"] is True
        self.calls.append(kwargs)
        return {
            "preview_id": "report-preview-1",
            "plan_sha256": "c" * 64,
            "draft_sha256": "d" * 64,
            "operation_count": 3,
        }


class ExactBindingUnavailablePreparer:
    def __init__(self):
        self.calls = []

    def prepare(self, **kwargs):
        self.calls.append(kwargs)
        raise ExactSamplePlanBindingUnavailable(
            "exact_sample_plan_binding_unavailable"
        )


class SafeCodeFailurePreparer:
    def __init__(self, code, *, provider_called=False):
        self.calls = []
        self.code = code
        self.provider_called = provider_called

    def prepare(self, **kwargs):
        self.calls.append(kwargs)
        error_type = type(
            "SafeReportFailure",
            (RuntimeError,),
            {
                "code": self.code,
                "provider_called": self.provider_called,
            },
        )
        raise error_type(self.code)


class FakeGpxTracks:
    def export_snapshot(self, project_id, trip_id):
        assert project_id == PROJECT_ID
        assert trip_id == ANALYSIS_JOB_ID
        track_id = UUID("44444444-4444-4444-8444-444444444444")
        return {
            "route_points": [
                {
                    "route_point_id": str(
                        uuid5(track_id, "route-point:0")
                    ),
                    "trip_id": str(ANALYSIS_JOB_ID),
                    "sequence": 0,
                    "timestamp_ms": -1_000,
                    "latitude": 35.0,
                    "longitude": 125.0,
                    "altitude_m": None,
                    "speed_kph": None,
                    "heading_deg": None,
                    "accuracy_m": None,
                    "source": "gpx",
                },
                {
                    "route_point_id": str(
                        uuid5(track_id, "route-point:1")
                    ),
                    "trip_id": str(ANALYSIS_JOB_ID),
                    "sequence": 1,
                    "timestamp_ms": 1_250,
                    "latitude": 37.51234567,
                    "longitude": 127.09876543,
                    "altitude_m": None,
                    "speed_kph": None,
                    "heading_deg": None,
                    "accuracy_m": None,
                    "source": "gpx",
                },
                {
                    "route_point_id": str(
                        uuid5(track_id, "route-point:2")
                    ),
                    "trip_id": str(ANALYSIS_JOB_ID),
                    "sequence": 2,
                    "timestamp_ms": 9_000,
                    "latitude": 36.0,
                    "longitude": 126.0,
                    "altitude_m": None,
                    "speed_kph": None,
                    "heading_deg": None,
                    "accuracy_m": None,
                    "source": "gpx",
                },
            ],
            "provenance": {
                "schema_version": (
                    "trafficvisson.gpx-export-provenance.v1"
                ),
                "status": "attached",
                "project_id": str(PROJECT_ID),
                "trip_id": str(ANALYSIS_JOB_ID),
                "track_id": str(track_id),
                "track_schema_version": "trafficvisson.gpx-track.v1",
                "track_revision": 1,
                "track_record_sha256": "5" * 64,
                "source_name": "verified.gpx",
                "source_size_bytes": 100,
                "source_sha256": "6" * 64,
                "source_integrity_verified": True,
                "point_count": 3,
                "exported_point_count": 3,
                "sampled_for_export": False,
                "video_offset_seconds": 0.0,
                "video_duration_seconds": 3.0,
                "points_before_video_count": 1,
                "points_after_video_count": 1,
                "gap_point_count": 0,
                "started_at": "2026-07-29T00:00:00+00:00",
                "ended_at": "2026-07-29T00:00:09+00:00",
                "imported_at": "2026-07-29T00:00:10+00:00",
                "updated_at": "2026-07-29T00:00:10+00:00",
                "timestamp_basis": (
                    "gpx_elapsed_seconds_plus_video_offset"
                ),
            },
        }


def _request(records, **overrides):
    ledger = _ledger(records)
    survey_authority = _survey_authority(ledger)
    values = {
        "project_id": PROJECT_ID,
        "pipeline_job_id": PIPELINE_JOB_ID,
        "analysis_job_id": ANALYSIS_JOB_ID,
        "source_sha256": SOURCE_SHA256,
        "pipeline_input_fingerprint": "1" * 64,
        "pipeline_profile_sha256": "2" * 64,
        "review_completion_id": f"review:{ledger}",
        "answer_ledger_sha256": ledger,
        "question_queue_revision": QUESTION_QUEUE_REVISION,
        "question_candidate_ids": (CANDIDATE_A, CANDIDATE_B),
        "template_id": "sample-4q-v1",
        "template_sha256": "3" * 64,
        "report_provider": "codex",
        "report_transmission_consent": False,
        "template_semantic_map_ready": False,
        "report_provider_ready": False,
        "survey_incident_authority_sha256": canonical_sha256(
            survey_authority
        ),
        "survey_incident_authority_manifest": survey_authority,
        "template_semantic_map_id": "sample-q4-semantic-v1",
        "template_semantic_map_sha256": "4" * 64,
        "report_route_name": "서울북부고속도로",
        "report_inspector_name": "홍길동",
        "report_inspection_date": "2026-07-29",
        "report_policy": {
            "evidence_mode": "internal_original",
            "evaluation_ratings": [
                "clear_unassessed"
            ] * 27,
        },
    }
    values.update(overrides)
    if (
        "survey_incident_authority_sha256" in overrides
        and overrides["survey_incident_authority_sha256"] is None
        and "survey_incident_authority_manifest" not in overrides
    ):
        values["survey_incident_authority_manifest"] = None
    return DerivedOutputsRequest(**values)


def _fixture(
    tmp_path,
    *,
    report_preparer=None,
    gpx_tracks=None,
    report_deidentified_evidence=None,
    fixed_validation_analysis_jobs_resolver=None,
):
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
        gpx_tracks=gpx_tracks,
        report_preparer=report_preparer,
        report_deidentified_evidence=(
            report_deidentified_evidence
        ),
        approved_reference_resolver=lambda project_id: [],
        fixed_validation_analysis_jobs_resolver=(
            fixed_validation_analysis_jobs_resolver
        ),
    )
    return service, store, records


def test_reviewed_survey_dataset_inherits_promoted_fixed_validation_jobs(
    tmp_path,
):
    fixed_jobs = tuple(
        sorted(
            (
                uuid5(PROJECT_ID, "fixed-validation-a"),
                uuid5(PROJECT_ID, "fixed-validation-b"),
            ),
            key=str,
        )
    )
    service, store, records = _fixture(
        tmp_path,
        fixed_validation_analysis_jobs_resolver=(
            lambda project_id: (
                fixed_jobs if project_id == PROJECT_ID else ()
            )
        ),
    )

    service.generate(_request(records))

    assert store.fixed_validation_job_ids == [
        list(fixed_jobs)
    ]


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
    assert first.checkpoint["state"] == "blocked"
    assert first.checkpoint["report"] == {
        "state": "blocked",
        "blocking_reasons": [
            "template_semantic_map_unavailable",
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
    assert store.incident_job_ids == [
        [ANALYSIS_JOB_ID],
        [ANALYSIS_JOB_ID],
    ]
    assert first.checkpoint[
        "survey_incident_authority_sha256"
    ] == request.survey_incident_authority_sha256

    serialized_checkpoint = json.dumps(
        first.checkpoint, ensure_ascii=False
    )
    assert str(tmp_path) not in serialized_checkpoint
    assert "\\" not in serialized_checkpoint
    assert "낙하물 수거" not in serialized_checkpoint
    assert "DocumentEditPlan" not in serialized_checkpoint
    assert first.checkpoint["absolute_paths_exposed"] is False
    assert first.checkpoint["com_invoked"] is False
    assert first.checkpoint["dataset_version"]["state"] == "prepared"
    assert first.checkpoint["dataset_version"][
        "approved_references_count_toward_validation"
    ] is False

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
    assert (
        manifest["provenance"]["dataset_version"]
        == first.checkpoint["dataset_version"]
    )
    assert manifest["provenance"]["gpx_track"]["status"] == (
        "not_attached"
    )
    assert manifest["record_counts"]["incidents"] == 1
    assert manifest["record_counts"]["route_points"] == 0
    assert manifest["evidence"]["incident_count"] == 1
    assert manifest["evidence"]["file_count"] == 2


def test_initial_human_review_is_annotation_only_without_survey_authority(
    tmp_path,
):
    service, store, records = _fixture(tmp_path)
    # The fake store normally starts with the survey Incident expected by the
    # report-path tests. Remove it to exercise the ordinary training boundary.
    store.incidents.clear()

    result = service.generate(
        _request(
            records,
            survey_incident_authority_sha256=None,
        )
    )

    assert result.metrics["annotation_count"] == 2
    assert result.metrics["incident_count"] == 0
    assert result.metrics["evidence_file_count"] == 0
    assert store.materialized_job_ids == [[ANALYSIS_JOB_ID]]
    assert store.incident_job_ids == [[]]
    assert result.checkpoint[
        "survey_incident_authority_sha256"
    ] is None
    assert result.checkpoint["report"] == {
        "state": "not_applicable_no_approved_incidents",
        "blocking_reasons": [],
        "provider_called": False,
        "document_edit_plan_prepared": False,
        "com_invoked": False,
    }
    bundle = next(
        (tmp_path / "bridge-data" / "exports").glob(
            "trafficsupervision-export-*"
        )
    )
    with (bundle / "reviewed_incidents.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as source:
        assert list(csv.DictReader(source)) == []
    with (bundle / "annotations.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as source:
        rows = list(csv.DictReader(source))
    assert len(rows) == 2
    report_payload = json.loads(
        (bundle / "report_payload.json").read_text(
            encoding="utf-8"
        )
    )
    assert report_payload["report"]["approved_incident_count"] == 0
    assert report_payload["report"]["report_incident_count"] == 0


def test_llm_assisted_sign_identity_is_training_only_not_survey_truth(
    tmp_path,
):
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    candidate_a = _candidate(
        evidence_root, CANDIDATE_A, timestamp=1.0
    )
    candidate_b = _candidate(
        evidence_root, CANDIDATE_B, timestamp=2.0
    )
    approved = _record(
        candidate_a,
        answer="yes",
        previous_hash=None,
        selected_issue_type="guide_sign_identity",
        severity="minor",
        maintenance_action="이정표 표기 및 위치 확인",
        ocr_text="대전",
        answered_by="structured_llm_assisted",
    )
    negative = _record(
        candidate_b,
        answer="no",
        previous_hash=approved["record_hash"],
    )
    records = [approved, negative]
    store = FakeStore(approved, candidate_a)
    report_preparer = FakeReportPreparer()
    service = DerivedOutputsService(
        data_root=(tmp_path / "bridge-data").resolve(),
        candidate_labels=FakeCandidateLabels(records),
        store=store,
        analysis_jobs=FakeAnalysisJobs([candidate_a, candidate_b]),
        report_preparer=report_preparer,
        approved_reference_resolver=lambda project_id: [],
    )

    result = service.generate(_request(records))

    assert result.checkpoint["state"] == "completed"
    assert result.checkpoint["report"] == {
        "state": "not_applicable_no_approved_incidents",
        "blocking_reasons": [],
        "provider_called": False,
        "document_edit_plan_prepared": False,
        "com_invoked": False,
    }
    assert result.metrics["incident_count"] == 0
    assert report_preparer.calls == []

    bundle = next(
        (tmp_path / "bridge-data" / "exports").glob(
            "trafficsupervision-export-*"
        )
    )
    with (bundle / "reviewed_incidents.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as source:
        incidents = list(csv.DictReader(source))
    with (bundle / "maintenance_actions.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as source:
        actions = list(csv.DictReader(source))
    with (bundle / "ocr_observations.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as source:
        ocr_rows = list(csv.DictReader(source))
    with (bundle / "annotations.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as source:
        annotations = list(csv.DictReader(source))
    report_payload = json.loads(
        (bundle / "report_payload.json").read_text(encoding="utf-8")
    )

    assert incidents == []
    assert actions == []
    assert ocr_rows == []
    assert annotations[0]["label_source"] == "structured_llm_assisted"
    assert report_payload["report"]["maintenance_incident_count"] == 0
    assert report_payload["report"]["document_edit_plan_state"] == (
        "not_applicable_no_approved_incidents"
    )


def test_user_approved_nonmaintenance_incident_is_not_omitted_from_report(
    tmp_path,
):
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    candidate_a = _candidate(
        evidence_root, CANDIDATE_A, timestamp=1.0
    )
    candidate_b = _candidate(
        evidence_root, CANDIDATE_B, timestamp=2.0
    )
    approved = _record(
        candidate_a,
        answer="yes",
        previous_hash=None,
        selected_issue_type="guide_sign_identity",
        severity="minor",
        maintenance_action="이정표 표기 및 위치 확인",
        ocr_text="대전",
        answered_by="local_user",
    )
    negative = _record(
        candidate_b,
        answer="no",
        previous_hash=approved["record_hash"],
    )
    records = [approved, negative]
    store = FakeStore(approved, candidate_a)
    report_preparer = FakeReportPreparer()
    service = DerivedOutputsService(
        data_root=(tmp_path / "bridge-data").resolve(),
        candidate_labels=FakeCandidateLabels(records),
        store=store,
        analysis_jobs=FakeAnalysisJobs([candidate_a, candidate_b]),
        report_preparer=report_preparer,
        approved_reference_resolver=lambda project_id: [],
    )

    result = service.generate(
        _request(
            records,
            report_provider="none",
            report_transmission_consent=False,
            template_semantic_map_ready=True,
            report_provider_ready=False,
        )
    )

    assert result.checkpoint["state"] == "completed"
    assert len(report_preparer.calls) == 1
    supplied = report_preparer.calls[0]["approved_incidents"]
    assert [item["incident_id"] for item in supplied] == [
        approved["question_id"]
    ]
    assert supplied[0]["approval_actor"] == "local_user"
    assert supplied[0]["approval_record_id"] == approved["record_id"]
    assert (
        supplied[0]["approval_record_sha256"]
        == approved["record_hash"]
    )
    assert supplied[0]["pipeline_job_id"] == str(PIPELINE_JOB_ID)
    assert supplied[0]["analysis_job_id"] == str(ANALYSIS_JOB_ID)

    bundle = next(
        (tmp_path / "bridge-data" / "exports").glob(
            "trafficsupervision-export-*"
        )
    )
    report_payload = json.loads(
        (bundle / "report_payload.json").read_text(encoding="utf-8")
    )
    assert report_payload["report"]["report_incident_count"] == 1
    assert report_payload["report"]["maintenance_incident_count"] == 0


def test_reviewed_export_fails_before_manifest_without_dataset_snapshot(
    tmp_path,
):
    service, store, records = _fixture(tmp_path)
    service._approved_reference_resolver = None

    with pytest.raises(DatasetSnapshotFailed):
        service.generate(_request(records))

    assert not (tmp_path / "bridge-data" / "exports").exists() or not list(
        (tmp_path / "bridge-data" / "exports").glob(
            "trafficsupervision-export-*"
        )
    )


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
        question_queue_revision=QUESTION_QUEUE_REVISION,
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


def test_zero_detections_allows_a_nonclassifying_analysis_seed(tmp_path):
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    candidate = _candidate(
        evidence_root, CANDIDATE_A, timestamp=1.0
    )
    candidate["ai_inference"] = {
        "mode": "local_model",
        "status": "no_detection",
        "predictions": [],
        "ocr_observations": [],
    }
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
        question_queue_revision=QUESTION_QUEUE_REVISION,
        question_candidate_ids=(),
        template_id="sample-4q-v1",
        template_sha256="3" * 64,
        report_provider="none",
        report_transmission_consent=False,
        template_semantic_map_ready=False,
        report_provider_ready=False,
        completion_mode="zero_detections",
    )

    result = service.generate(request)

    assert result.checkpoint["state"] == "completed"
    assert result.metrics["raw_detection_count"] == 0
    assert result.metrics["reviewed_answer_count"] == 0


def test_zero_detections_rejects_a_reviewable_analysis_proposal(tmp_path):
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
        question_queue_revision=QUESTION_QUEUE_REVISION,
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
        match="contains review proposals",
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


def test_digest_only_survey_authority_cannot_create_final_outputs(
    tmp_path,
):
    service, store, records = _fixture(tmp_path)
    request = _request(
        records,
        survey_incident_authority_manifest=None,
    )

    with pytest.raises(
        DerivedOutputsConflict,
        match="complete promoted-survey Incident authority",
    ):
        service.generate(request)

    assert store.projected == []
    assert list(
        (tmp_path / "bridge-data" / "exports").iterdir()
    ) == []


@pytest.mark.parametrize(
    ("path", "tampered_value"),
    [
        (("model_version_id",), "model-promoted-v2"),
        (("dataset_version_id",), "dataset-promoted-v2"),
        (("dataset_hash",), "0" * 64),
        (("dataset_provenance_graph_sha256",), "0" * 64),
        (("fixed_validation_sha256",), "0" * 64),
        (("source_analysis_lineage_sha256",), "0" * 64),
        (("promoted_model_inference_sha256",), "0" * 64),
        (
            ("event_binding", "review_event_set_revision"),
            "0" * 64,
        ),
        (
            (
                "question_catalog_binding",
                "question_catalog_revision",
            ),
            "0" * 64,
        ),
    ],
)
def test_tampered_survey_authority_fails_before_projection_or_export(
    tmp_path,
    path,
    tampered_value,
):
    service, store, records = _fixture(tmp_path)
    valid_request = _request(records)
    manifest = deepcopy(
        valid_request.survey_incident_authority_manifest
    )
    target = manifest
    for field in path[:-1]:
        target = target[field]
    target[path[-1]] = tampered_value
    request = _request(
        records,
        survey_incident_authority_manifest=manifest,
        survey_incident_authority_sha256=(
            valid_request.survey_incident_authority_sha256
        ),
    )

    with pytest.raises(
        DerivedOutputsConflict,
        match="promoted-survey Incident authority is invalid",
    ):
        service.generate(request)

    assert store.projected == []
    assert list(
        (tmp_path / "bridge-data" / "exports").iterdir()
    ) == []


def test_stale_review_event_binding_cannot_create_final_outputs(
    tmp_path,
):
    service, store, records = _fixture(tmp_path)
    stale = deepcopy(service._analysis_jobs.candidates[0])
    stale["review_event_binding"][
        "review_event_set_revision"
    ] = "0" * 64
    service._analysis_jobs.candidates[0] = stale

    with pytest.raises(
        DerivedOutputsConflict,
        match="survey ReviewEvent authority changed",
    ):
        service.generate(_request(records))

    assert store.projected == []
    assert list(
        (tmp_path / "bridge-data" / "exports").iterdir()
    ) == []


def test_tampered_candidate_evidence_fails_before_projection_or_export(
    tmp_path,
):
    service, store, records = _fixture(tmp_path)
    image_path = Path(
        service._analysis_jobs.candidates[0]["_image_path"]
    )
    image_path.write_bytes(b"tampered-candidate-evidence")

    with pytest.raises(
        DerivedOutputsConflict,
        match="failed integrity verification",
    ):
        service.generate(_request(records))

    assert store.projected == []
    assert list(
        (tmp_path / "bridge-data" / "exports").iterdir()
    ) == []


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
    assert approved[0]["evidence_refs"] == [
        f"analysis:{CANDIDATE_A}:representative"
    ]
    assert "preview_clip_ref" not in approved[0]
    incident_id = approved[0]["incident_id"]
    evidence = report_preparer.calls[0][
        "evidence_by_incident_id"
    ][incident_id]
    assert evidence == {
        "incident_id": incident_id,
        "project_id": str(PROJECT_ID),
        "pipeline_job_id": str(PIPELINE_JOB_ID),
        "analysis_job_id": str(ANALYSIS_JOB_ID),
        "source_sha256": SOURCE_SHA256,
        "original_ref": (
            f"analysis:{CANDIDATE_A}:representative"
        ),
        "original_sha256": service._analysis_jobs.candidates[0][
            "representative_image"
        ]["sha256"],
        "deidentified_ref": None,
        "deidentified_sha256": None,
        "deidentification_record_sha256": None,
        "deidentified_verified": False,
    }
    assert report_preparer.calls[0]["locations_by_incident_id"] == {
        incident_id: "영상 00:00:01.000 · GPS 미연결"
    }
    assert report_preparer.calls[0]["route_name"] == (
        "서울북부고속도로"
    )
    assert report_preparer.calls[0]["inspection_date"] == (
        "2026-07-29"
    )
    assert report_preparer.calls[0]["report_policy"] == {
        "evidence_mode": "internal_original",
        "evaluation_ratings": ["clear_unassessed"] * 27,
    }


def test_internal_exact_preview_needs_no_provider_transmission_consent(
    tmp_path,
):
    report_preparer = FakeReportPreparer()
    service, _store, records = _fixture(
        tmp_path, report_preparer=report_preparer
    )
    request = _request(
        records,
        report_provider="none",
        report_transmission_consent=False,
        template_semantic_map_ready=True,
        report_provider_ready=False,
    )

    result = service.generate(request)

    assert result.checkpoint["state"] == "completed"
    assert result.checkpoint["report"]["state"] == "validated_preview"
    assert len(report_preparer.calls) == 1
    assert (
        report_preparer.calls[0]["explicit_transmission_consent"]
        is False
    )
    assert (
        report_preparer.calls[0]["report_policy"]["evidence_mode"]
        == "internal_original"
    )


def test_real_durable_preview_metadata_accepts_complete_exact_writer_count():
    metadata = {
        "preview_id": "report-preview-attempt-0",
        "state": "semantic_preview_ready_exact_writer_required",
        "input_sha256": "a" * 64,
        "report_attempt": 0,
        "plan_sha256": "b" * 64,
        "semantic_provider_plan_sha256": "b" * 64,
        "draft_sha256": "c" * 64,
        "operation_count": 1_257,
        "current_binding_manifest_sha256": "d" * 64,
        "binding": {
            "state": "current_incidents_bound_writer_required",
            "template_id": "sample-3q-v1",
            "document_snapshot_id": "hwpx-sample-3q-" + ("e" * 24),
            "survey_incident_authority_sha256": "f" * 64,
            "approved_incident_authority_set_sha256": "1" * 64,
            "approved_incident_count": 4,
            "required_writer_operation_count": 1_257,
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

    safe = _safe_report_preview(metadata)

    assert safe == {
        "state": "validated_preview",
        "preview_id": "report-preview-attempt-0",
        "plan_sha256": "b" * 64,
        "draft_sha256": "c" * 64,
        "operation_count": 1_257,
        "blocking_reasons": [],
        "provider_called": False,
        "document_edit_plan_prepared": True,
        "com_invoked": False,
    }


def test_verified_deidentified_descriptor_is_forwarded_without_rewriting(
    tmp_path,
):
    report_preparer = FakeReportPreparer()
    service, _store, records = _fixture(
        tmp_path, report_preparer=report_preparer
    )
    incident_id = records[0]["question_id"]
    original_ref = f"analysis:{CANDIDATE_A}:representative"
    original_sha256 = service._analysis_jobs.candidates[0][
        "representative_image"
    ]["sha256"]
    deidentified_ref = f"deid:{CANDIDATE_A}:representative"
    deidentified_sha256 = "7" * 64
    verification_sha256 = deidentification_record_sha256(
        incident_id=incident_id,
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        source_sha256=SOURCE_SHA256,
        original_ref=original_ref,
        original_sha256=original_sha256,
        deidentified_ref=deidentified_ref,
        deidentified_sha256=deidentified_sha256,
    )
    descriptor = {
        "deidentified_ref": deidentified_ref,
        "deidentified_sha256": deidentified_sha256,
        "deidentification_record_sha256": verification_sha256,
        "deidentified_verified": True,
    }
    request = _request(
        records,
        report_transmission_consent=True,
        template_semantic_map_ready=True,
        report_provider_ready=True,
        report_policy={
            "evidence_mode": "shared_deidentified",
            "evaluation_ratings": ["clear_unassessed"] * 27,
        },
        verified_deidentified_evidence_by_incident_id={
            incident_id: descriptor
        },
    )

    result = service.generate(request)

    assert result.checkpoint["state"] == "completed"
    passed = report_preparer.calls[0][
        "evidence_by_incident_id"
    ][incident_id]
    assert passed["original_ref"] == original_ref
    assert passed["original_sha256"] == original_sha256
    for key, value in descriptor.items():
        assert passed[key] == value


def test_shared_report_resolves_human_verified_registry_descriptor(
    tmp_path,
):
    report_preparer = FakeReportPreparer()
    registry_calls = []
    descriptor_holder = {}

    class Registry:
        def resolve_verified_descriptor(self, **kwargs):
            registry_calls.append(kwargs)
            return descriptor_holder["value"]

    service, _store, records = _fixture(
        tmp_path,
        report_preparer=report_preparer,
        report_deidentified_evidence=Registry(),
    )
    incident_id = records[0]["question_id"]
    original_ref = f"analysis:{CANDIDATE_A}:representative"
    original_sha256 = service._analysis_jobs.candidates[0][
        "representative_image"
    ]["sha256"]
    deidentified_ref = (
        f"report-deid:{UUID(incident_id).hex}:{'7' * 32}"
    )
    deidentified_sha256 = "8" * 64
    descriptor_holder["value"] = {
        "deidentified_ref": deidentified_ref,
        "deidentified_sha256": deidentified_sha256,
        "deidentification_record_sha256": (
            deidentification_record_sha256(
                incident_id=incident_id,
                project_id=PROJECT_ID,
                pipeline_job_id=PIPELINE_JOB_ID,
                analysis_job_id=ANALYSIS_JOB_ID,
                source_sha256=SOURCE_SHA256,
                original_ref=original_ref,
                original_sha256=original_sha256,
                deidentified_ref=deidentified_ref,
                deidentified_sha256=deidentified_sha256,
            )
        ),
        "deidentified_verified": True,
    }
    request = _request(
        records,
        report_transmission_consent=True,
        template_semantic_map_ready=True,
        report_provider_ready=True,
        report_policy={
            "evidence_mode": "shared_deidentified",
            "evaluation_ratings": ["clear_unassessed"] * 27,
        },
    )

    result = service.generate(request)

    assert result.checkpoint["state"] == "completed"
    assert len(registry_calls) == 1
    assert registry_calls[0] == {
        "project_id": PROJECT_ID,
        "pipeline_job_id": PIPELINE_JOB_ID,
        "analysis_job_id": ANALYSIS_JOB_ID,
        "source_sha256": SOURCE_SHA256,
        "incident_id": incident_id,
        "original_ref": original_ref,
        "original_sha256": original_sha256,
    }
    passed = report_preparer.calls[0][
        "evidence_by_incident_id"
    ][incident_id]
    assert passed["deidentified_ref"] == deidentified_ref
    assert passed["deidentified_verified"] is True


def test_report_preview_receives_every_approved_incident_from_this_job(
    tmp_path,
):
    report_preparer = FakeReportPreparer()
    service, store, records = _fixture(
        tmp_path, report_preparer=report_preparer
    )
    candidate_b = service._analysis_jobs.candidates[1]
    second_approved = _record(
        candidate_b,
        answer="yes",
        previous_hash=records[0]["record_hash"],
    )
    records[1] = second_approved
    store.add_incident(second_approved, candidate_b)
    request = _request(
        records,
        report_transmission_consent=True,
        template_semantic_map_ready=True,
        report_provider_ready=True,
    )

    result = service.generate(request)

    assert result.checkpoint["state"] == "completed"
    assert result.metrics["incident_count"] == 2
    assert len(report_preparer.calls) == 1
    approved = report_preparer.calls[0]["approved_incidents"]
    assert {
        item["incident_id"] for item in approved
    } == {
        records[0]["question_id"],
        records[1]["question_id"],
    }
    assert report_preparer.calls[0]["pipeline_job_id"] == PIPELINE_JOB_ID
    assert report_preparer.calls[0]["analysis_job_id"] == ANALYSIS_JOB_ID

    bundle = next(
        (tmp_path / "bridge-data" / "exports").glob(
            "trafficsupervision-export-*"
        )
    )
    manifest = json.loads(
        (bundle / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["provenance"]["pipeline_job_id"] == str(
        PIPELINE_JOB_ID
    )
    assert manifest["provenance"]["analysis_job_id"] == str(
        ANALYSIS_JOB_ID
    )
    assert manifest["record_counts"]["incidents"] == 2


def test_derived_outputs_reject_incident_outside_selected_question_set(
    tmp_path,
):
    service, store, records = _fixture(tmp_path)
    leaked = dict(store.incidents[0])
    leaked["candidate_id"] = "c" * 20
    leaked["question_id"] = str(uuid4())
    leaked["incident_id"] = leaked["question_id"]
    store.incidents.append(leaked)

    with pytest.raises(
        DerivedOutputsConflict,
        match="selected pipeline question set",
    ):
        service.generate(_request(records))

    assert list(
        (tmp_path / "bridge-data" / "exports").iterdir()
    ) == []


def test_exact_sample_binding_gap_is_explicit_blocked_not_completed(
    tmp_path,
):
    report_preparer = ExactBindingUnavailablePreparer()
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

    assert result.checkpoint["state"] == "blocked"
    assert result.checkpoint["report"] == {
        "state": "blocked",
        "blocking_reasons": [
            "exact_sample_plan_binding_unavailable"
        ],
        "provider_called": False,
        "document_edit_plan_prepared": False,
        "com_invoked": False,
    }
    assert len(report_preparer.calls) == 1


def test_missing_current_route_and_date_block_before_provider_but_export(
    tmp_path,
):
    report_preparer = FakeReportPreparer()
    service, _store, records = _fixture(
        tmp_path, report_preparer=report_preparer
    )
    request = _request(
        records,
        report_transmission_consent=True,
        template_semantic_map_ready=True,
        report_provider_ready=True,
        report_route_name="",
        report_inspection_date="",
    )

    result = service.generate(request)

    assert result.checkpoint["state"] == "blocked"
    assert result.checkpoint["report"] == {
        "state": "blocked",
        "blocking_reasons": [
            "report_route_name_required",
            "report_inspection_date_required",
        ],
        "provider_called": False,
        "document_edit_plan_prepared": False,
        "com_invoked": False,
    }
    assert report_preparer.calls == []
    assert result.metrics["incident_count"] == 1
    assert result.metrics["evidence_file_count"] == 2
    assert len(
        list(
            (tmp_path / "bridge-data" / "exports").glob(
                "trafficsupervision-export-*"
            )
        )
    ) == 1


def test_verified_timed_gpx_binds_nearest_in_video_point_only(
    tmp_path,
):
    report_preparer = FakeReportPreparer()
    service, _store, records = _fixture(
        tmp_path,
        report_preparer=report_preparer,
        gpx_tracks=FakeGpxTracks(),
    )
    request = _request(
        records,
        report_transmission_consent=True,
        template_semantic_map_ready=True,
        report_provider_ready=True,
    )

    result = service.generate(request)

    assert result.checkpoint["state"] == "completed"
    incident_id = records[0]["question_id"]
    assert report_preparer.calls[0][
        "locations_by_incident_id"
    ] == {
        incident_id: (
            "영상 00:00:01.000 · GPS "
            "37.5123457, 127.0987654"
        )
    }
    bundle = next(
        (tmp_path / "bridge-data" / "exports").glob(
            "trafficsupervision-export-*"
        )
    )
    with (bundle / "reviewed_incidents.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as source:
        rows = list(csv.DictReader(source))
    assert rows[0]["latitude"] == "37.51234567"
    assert rows[0]["longitude"] == "127.09876543"
    assert rows[0]["location_validation"] == (
        "timed_gpx_attached"
    )


@pytest.mark.parametrize(
    "safe_code",
    [
        "provider_deidentified_evidence_required",
        "deidentified_evidence_required",
        "current_report_content_invalid",
    ],
)
def test_report_binder_safe_error_code_is_preserved(
    tmp_path,
    safe_code,
):
    report_preparer = SafeCodeFailurePreparer(safe_code)
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

    assert result.checkpoint["state"] == "blocked"
    assert result.checkpoint["report"]["blocking_reasons"] == [
        safe_code
    ]
    assert result.checkpoint["report"]["provider_called"] is False
    assert len(report_preparer.calls) == 1


def test_custom_output_location_is_not_treated_as_a_path(tmp_path):
    service, _store, records = _fixture(tmp_path)
    request = _request(records, output_location_id="renderer-selected")

    with pytest.raises(
        DerivedOutputsConflict,
        match="server-owned resolver",
    ):
        service.generate(request)

    assert list((tmp_path / "bridge-data" / "exports").iterdir()) == []


def test_server_bound_custom_output_location_writes_heavy_bundle_externally(
    tmp_path,
):
    service, _store, records = _fixture(tmp_path)
    request = _request(
        records,
        output_location_id="project-output-" + ("5" * 32),
    )
    external_root = (tmp_path / "job-derived-outputs").resolve()
    external_root.mkdir()
    authorization = ResolvedDerivedOutputsArtifactRoot(
        category_root=external_root,
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        source_sha256=SOURCE_SHA256,
        output_location_id=request.output_location_id,
        job_root_identity_sha256="6" * 64,
    )

    first = service.generate(request, artifact_root=authorization)
    replay = service.generate(request, artifact_root=authorization)
    restarted = DerivedOutputsService(
        data_root=service._data_root,
        candidate_labels=service._candidate_labels,
        store=service._store,
        analysis_jobs=service._analysis_jobs,
        experimental_proposals=service._experimental_proposals,
        candidate_evidence=service._candidate_evidence,
        gpx_tracks=service._gpx_tracks,
        report_preparer=service._report_preparer,
        report_deidentified_evidence=(
            service._report_deidentified_evidence
        ),
        approved_reference_resolver=(
            service._approved_reference_resolver
        ),
    )
    restart_replay = restarted.generate(
        request, artifact_root=authorization
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert restart_replay.replayed is True
    bundles = list(
        external_root.glob("trafficsupervision-export-*")
    )
    assert len(bundles) == 1
    assert list((tmp_path / "bridge-data" / "exports").iterdir()) == []
    manifest = json.loads(
        (bundles[0] / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["provenance"]["output_location_id"] == (
        request.output_location_id
    )
    assert manifest["provenance"]["artifact_storage"][
        "mode"
    ] == "external_category_root"
    assert str(external_root) not in json.dumps(
        manifest["provenance"]["artifact_storage"]
    )
    record = next(
        (
            tmp_path
            / "bridge-data"
            / "derived-outputs"
            / str(PROJECT_ID)
            / str(PIPELINE_JOB_ID)
        ).glob("*.json")
    )
    record_text = record.read_text(encoding="utf-8")
    assert str(external_root) not in record_text
    assert str(external_root) not in json.dumps(first.checkpoint)


def test_custom_output_root_mismatch_and_artifact_tamper_fail_closed(
    tmp_path,
):
    service, _store, records = _fixture(tmp_path)
    request = _request(
        records,
        output_location_id="project-output-" + ("7" * 32),
    )
    external_root = (tmp_path / "job-derived-outputs").resolve()
    external_root.mkdir()
    wrong = ResolvedDerivedOutputsArtifactRoot(
        category_root=external_root,
        project_id=PROJECT_ID,
        pipeline_job_id=uuid4(),
        analysis_job_id=ANALYSIS_JOB_ID,
        source_sha256=SOURCE_SHA256,
        output_location_id=request.output_location_id,
        job_root_identity_sha256="8" * 64,
    )
    with pytest.raises(
        DerivedOutputsConflict, match="exact PipelineJob binding"
    ):
        service.generate(request, artifact_root=wrong)

    authorization = ResolvedDerivedOutputsArtifactRoot(
        category_root=external_root,
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        source_sha256=SOURCE_SHA256,
        output_location_id=request.output_location_id,
        job_root_identity_sha256="8" * 64,
    )
    service.generate(request, artifact_root=authorization)
    bundle = next(
        external_root.glob("trafficsupervision-export-*")
    )
    (bundle / "raw_detections.csv").write_text(
        "tampered", encoding="utf-8"
    )
    with pytest.raises(
        DerivedOutputsCorrupt,
        match="integrity verification",
    ):
        service.generate(request, artifact_root=authorization)
