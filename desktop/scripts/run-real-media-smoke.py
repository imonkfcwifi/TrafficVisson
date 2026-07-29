"""Run TrafficSuperVision's real intake -> hash -> candidate path on one video.

This is a bounded developer acceptance harness. It never copies or modifies the
source video and prints only path-free result metadata.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from uuid import UUID


DESKTOP_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_ROOT = DESKTOP_ROOT / "bridge"
sys.path.insert(0, str(BRIDGE_ROOT))

from app.analysis_jobs import AnalysisJobService  # noqa: E402
from app.candidate_extraction import ExtractionLimits  # noqa: E402
from app.media_intake import MediaIntakeService  # noqa: E402


def wait_for_hash(
    media: MediaIntakeService,
    project_id: UUID,
    job_id: UUID,
    timeout_seconds: float,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = media.get_hash_job(project_id, job_id)
        if state["status"] in {"completed", "failed", "cancelled"}:
            return state
        time.sleep(0.1)
    media.cancel_hash_job(project_id, job_id)
    raise TimeoutError("SHA-256 verification exceeded the smoke-test timeout")


def wait_for_analysis(
    analysis: AnalysisJobService,
    project_id: UUID,
    job_id: UUID,
    timeout_seconds: float,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = analysis.get(project_id, job_id)
        if state["status"] in {"completed", "failed", "cancelled"}:
            return state
        time.sleep(0.2)
    analysis.cancel(project_id, job_id)
    raise TimeoutError("Candidate extraction exceeded the smoke-test timeout")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument(
        "--project-id",
        default="11111111-1111-4111-8111-111111111111",
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    args = parser.parse_args()

    project_id = UUID(args.project_id)
    source = str(Path(args.source))
    data_root = Path(args.data_root).resolve()
    data_root.mkdir(parents=True, exist_ok=True)

    media = MediaIntakeService(
        ffprobe_path=args.ffprobe,
        minimum_interval_seconds=0,
    )
    analysis = AnalysisJobService(
        media,
        data_root=data_root,
        ffmpeg_path=args.ffmpeg,
        ffprobe_path=args.ffprobe,
        extraction_limits=ExtractionLimits(
            cadence_seconds=1.0,
            scene_threshold=0.2,
            group_gap_seconds=0.75,
            preview_duration_seconds=2.0,
            max_sample_frames=8,
            max_candidate_groups=8,
            max_frame_width=960,
            max_frame_bytes=8 * 1024 * 1024,
            max_clip_bytes=48 * 1024 * 1024,
            max_output_bytes=448 * 1024 * 1024,
            min_free_disk_bytes=128 * 1024 * 1024,
            scan_timeout_seconds=min(args.timeout_seconds, 1_800.0),
            max_total_seconds=min(args.timeout_seconds, 3_600.0),
        ),
    )
    started_at = time.monotonic()
    try:
        snapshot = media.register_local_file(project_id, source)
        file_name = snapshot["files"][0]["name"]
        probe = media.probe(
            project_id,
            snapshot["snapshot_id"],
            file_name,
            min(60, max(1, int(args.timeout_seconds))),
        )
        hash_started = media.start_hash_job(
            project_id, snapshot["snapshot_id"], file_name
        )
        hash_result = wait_for_hash(
            media,
            project_id,
            hash_started["job_id"],
            args.timeout_seconds,
        )
        if hash_result["status"] != "completed":
            raise RuntimeError(
                f"SHA-256 verification failed: {hash_result['error_code']}"
            )
        job = analysis.start(
            project_id,
            snapshot["snapshot_id"],
            file_name,
            hash_result["job_id"],
            hash_result["sha256_lower"],
        )
        terminal = wait_for_analysis(
            analysis,
            project_id,
            job["job_id"],
            args.timeout_seconds,
        )
        summary = {
            "status": terminal["status"],
            "job_id": str(terminal["job_id"]),
            "project_id": str(project_id),
            "file_name": file_name,
            "size_bytes": probe["size_bytes"],
            "duration_seconds": probe["duration_seconds"],
            "video_streams": probe["video_streams"],
            "source_sha256": hash_result["sha256_lower"],
            "candidate_count": (
                terminal["result"]["candidate_count"]
                if terminal["result"] is not None
                else 0
            ),
            "artifact_count": (
                terminal["result"]["artifact_count"]
                if terminal["result"] is not None
                else 0
            ),
            "error_code": terminal["error_code"],
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return 0 if terminal["status"] == "completed" else 1
    finally:
        analysis.shutdown()
        media.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
