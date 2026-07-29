"""Verify persisted production analysis records through the authenticated API."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient


DESKTOP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DESKTOP_ROOT / "bridge"))

from app.main import create_app  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument(
        "--project-id",
        default="11111111-1111-4111-8111-111111111111",
    )
    args = parser.parse_args()
    project_id = UUID(args.project_id)
    token = "trafficvisson-verification-token-" + "x" * 32
    app = create_app(
        token,
        allow_test_client=True,
        bridge_data_root=str(Path(args.data_root).resolve()),
    )
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        response = client.get(
            f"/projects/{project_id}/analysis/jobs?limit=100",
            headers=headers,
        )
        response.raise_for_status()
        jobs = response.json()["jobs"]
        completed = [
            job
            for job in jobs
            if job["status"] == "completed" and job["result"] is not None
        ]
        if not completed:
            raise SystemExit("no completed analysis records were recovered")
        latest = completed[0]
        candidate = latest["result"]["candidates"][0]
        evidence = client.get(
            (
                f"/projects/{project_id}/analysis/jobs/{latest['job_id']}"
                f"/candidates/{candidate['candidate_id']}/image"
            ),
            headers=headers,
        )
        evidence.raise_for_status()
        clip = client.get(
            (
                f"/projects/{project_id}/analysis/jobs/{latest['job_id']}"
                f"/candidates/{candidate['candidate_id']}/clip"
            ),
            headers=headers,
        )
        clip.raise_for_status()
        clip_range = client.get(
            (
                f"/projects/{project_id}/analysis/jobs/{latest['job_id']}"
                f"/candidates/{candidate['candidate_id']}/clip"
            ),
            headers={**headers, "Range": "bytes=0-1023"},
        )
        clip_range.raise_for_status()
        print(
            json.dumps(
                {
                    "job_count": len(jobs),
                    "completed_count": len(completed),
                    "candidate_total": sum(
                        item["result"]["candidate_count"]
                        for item in completed
                    ),
                    "artifact_total": sum(
                        item["result"]["artifact_count"]
                        for item in completed
                    ),
                    "source_start_location_count": sum(
                        1
                        for item in completed
                        if item["result"]["provenance"].get("source_location")
                        and item["result"]["provenance"]["source_location"].get(
                            "scope"
                        )
                        == "capture_start_only"
                    ),
                    "latest_file": latest["file_name"],
                    "evidence_status": evidence.status_code,
                    "evidence_type": evidence.headers.get("content-type"),
                    "evidence_bytes": len(evidence.content),
                    "clip_status": clip.status_code,
                    "clip_type": clip.headers.get("content-type"),
                    "clip_bytes": len(clip.content),
                    "clip_range_status": clip_range.status_code,
                    "clip_content_range": clip_range.headers.get("content-range"),
                    "clip_range_bytes": len(clip_range.content),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
