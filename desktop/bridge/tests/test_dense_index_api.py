from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.dense_index_jobs import (
    DenseIndexJobNotFound,
    ResolvedDenseIndexArtifact,
)
from app.main import create_app
from app.store import DEMO_PROJECT_ID


TOKEN = "d" * 64
ANALYSIS_JOB_ID = UUID("22222222-2222-4222-8222-222222222222")
SOURCE_SHA256 = "a" * 64


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _analysis_row(status: str = "completed") -> dict:
    return {
        "job_id": ANALYSIS_JOB_ID,
        "project_id": DEMO_PROJECT_ID,
        "source_sha256": SOURCE_SHA256,
        "status": status,
        "result": {} if status == "completed" else None,
    }


def _safety() -> dict[str, bool]:
    return {
        "review_only": True,
        "automatic_incident_creation": False,
        "automatic_annotation_creation": False,
        "automatic_training_ingest": False,
        "automatic_maintenance_decision": False,
        "automatic_model_promotion": False,
    }


class FakeDenseIndexJobs:
    def __init__(self, artifact_path: Path):
        self.artifact_path = artifact_path
        self.job_id = UUID("33333333-3333-4333-8333-333333333333")
        self.calls: list[tuple] = []
        self.shutdown_called = False

    def _row(self, status: str = "queued") -> dict:
        now = datetime.now(timezone.utc)
        result = None
        record_sha256 = None
        if status == "completed":
            record_sha256 = "b" * 64
            result = {
                "artifact_set_id": str(self.job_id),
                "frame_count": 12,
                "total_frame_bytes": 4096,
                "index": {
                    "artifact_ref": (
                        f"dense-index:{self.job_id}:frame-index"
                    )
                },
            }
        return {
            "job_id": self.job_id,
            "project_id": DEMO_PROJECT_ID,
            "opaque_source_identity": str(ANALYSIS_JOB_ID),
            "source_sha256": SOURCE_SHA256,
            "sampling_fps": 2,
            "status": status,
            "stage": status,
            "progress_percent": 100 if status == "completed" else 0,
            "progress_state": (
                "determinate" if status == "completed" else "waiting"
            ),
            "progress_basis": "stage_only_no_fabricated_frame_progress",
            "pause_supported": False,
            "error_code": None,
            "created_at": now,
            "updated_at": now,
            "record_sha256": record_sha256,
            "safety": _safety(),
            "result": result,
        }

    def start(
        self,
        project_id,
        opaque_source_identity,
        source_sha256,
        *,
        sampling_fps,
    ):
        self.calls.append(
            (
                "start",
                project_id,
                opaque_source_identity,
                source_sha256,
                sampling_fps,
            )
        )
        return self._row()

    def list(self, project_id, *, limit):
        self.calls.append(("list", project_id, limit))
        return [self._row("completed")]

    def get(self, project_id, job_id):
        self.calls.append(("get", project_id, job_id))
        if project_id != DEMO_PROJECT_ID or job_id != self.job_id:
            raise DenseIndexJobNotFound("not found")
        return self._row("completed")

    def cancel(self, project_id, job_id):
        self.calls.append(("cancel", project_id, job_id))
        return self._row("completed")

    def resolve_artifact(self, project_id, job_id, artifact_id):
        self.calls.append(
            ("artifact", project_id, job_id, artifact_id)
        )
        body = self.artifact_path.read_bytes()
        return ResolvedDenseIndexArtifact(
            path=self.artifact_path,
            media_type="application/json",
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
        )

    def shutdown(self):
        self.shutdown_called = True


def _app(tmp_path):
    return create_app(
        TOKEN,
        fixture_mode=True,
        allow_test_client=True,
        bridge_data_root=str(tmp_path / "bridge-data"),
        media_stability_min_interval_seconds=0,
    )


def test_dense_index_api_is_opaque_review_only_and_project_scoped(
    tmp_path,
):
    app = _app(tmp_path)
    artifact = tmp_path / "frame-index.json"
    artifact.write_text('{"schema_version":"test"}', encoding="utf-8")
    fake = FakeDenseIndexJobs(artifact)
    app.state.dense_index_jobs.shutdown()
    app.state.dense_index_jobs = fake
    app.state.analysis_jobs.get = (
        lambda project_id, job_id: _analysis_row()
        if project_id == DEMO_PROJECT_ID and job_id == ANALYSIS_JOB_ID
        else (_ for _ in ()).throw(
            HTTPException(status_code=404, detail="not found")
        )
    )

    with TestClient(app) as client:
        start = client.post(
            f"/projects/{DEMO_PROJECT_ID}/dense-index/jobs",
            headers=_headers(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": str(ANALYSIS_JOB_ID),
                "sampling_fps": 2,
                "confirm_review_only": True,
            },
        )
        assert start.status_code == 202, start.text
        payload = start.json()
        assert payload["analysis_job_id"] == str(ANALYSIS_JOB_ID)
        assert payload["safety"] == _safety()
        assert "path" not in start.text.casefold()
        assert fake.calls[0] == (
            "start",
            DEMO_PROJECT_ID,
            str(ANALYSIS_JOB_ID),
            SOURCE_SHA256,
            2,
        )

        listed = client.get(
            f"/projects/{DEMO_PROJECT_ID}/dense-index/jobs",
            headers=_headers(),
        )
        assert listed.status_code == 200
        completed = listed.json()["jobs"][0]
        assert completed["result"] == {
            "artifact_set_id": str(fake.job_id),
            "frame_count": 12,
            "total_frame_bytes": 4096,
            "index_artifact_ref": (
                f"dense-index:{fake.job_id}:frame-index"
            ),
        }

        served = client.get(
            (
                f"/projects/{DEMO_PROJECT_ID}/dense-index/jobs/"
                f"{fake.job_id}/artifacts/frame-index"
            ),
            headers=_headers(),
        )
        assert served.status_code == 200
        assert served.content == artifact.read_bytes()
        assert served.headers["cache-control"] == "no-store"
        assert str(artifact) not in str(served.headers)

        other_project = uuid4()
        hidden = client.get(
            (
                f"/projects/{other_project}/dense-index/jobs/"
                f"{fake.job_id}"
            ),
            headers=_headers(),
        )
        assert hidden.status_code == 404

    assert fake.shutdown_called is True


def test_dense_index_api_rejects_unconfirmed_or_renderer_source_fields(
    tmp_path,
):
    app = _app(tmp_path)
    app.state.analysis_jobs.get = lambda *_: _analysis_row()
    with TestClient(app) as client:
        base = {
            "project_id": str(DEMO_PROJECT_ID),
            "analysis_job_id": str(ANALYSIS_JOB_ID),
            "sampling_fps": 2,
            "confirm_review_only": False,
        }
        assert client.post(
            f"/projects/{DEMO_PROJECT_ID}/dense-index/jobs",
            headers=_headers(),
            json=base,
        ).status_code == 422
        assert client.post(
            f"/projects/{DEMO_PROJECT_ID}/dense-index/jobs",
            headers=_headers(),
            json={
                **base,
                "confirm_review_only": True,
                "source_path": "C:\\secret\\road.mp4",
            },
        ).status_code == 422
        assert client.post(
            f"/projects/{DEMO_PROJECT_ID}/dense-index/jobs",
            headers=_headers(),
            json={**base, "sampling_fps": 6},
        ).status_code == 422


def test_dense_index_restart_without_live_source_requires_reselection(
    tmp_path,
):
    app = _app(tmp_path)
    app.state.analysis_jobs.get = lambda *_: _analysis_row()

    def source_unavailable(*_args):
        raise HTTPException(
            status_code=409,
            detail="stable snapshot is not present in this runtime",
        )

    app.state.analysis_jobs.resolve_completed_source = source_unavailable
    with TestClient(app) as client:
        response = client.post(
            f"/projects/{DEMO_PROJECT_ID}/dense-index/jobs",
            headers=_headers(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": str(ANALYSIS_JOB_ID),
                "sampling_fps": 2,
                "confirm_review_only": True,
            },
        )
    assert response.status_code == 409
    assert "source_unavailable_reselect_required" in response.text
