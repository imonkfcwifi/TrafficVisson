"""Resume-safe acceptance processing for every video in one trusted folder.

The production Electron UI performs the same intake, probe, SHA-256 and
candidate-extraction calls. This harness exists so a newly delivered field
folder can be verified in one unattended pass before installer acceptance.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from uuid import UUID


DESKTOP_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_ROOT = DESKTOP_ROOT / "bridge"
sys.path.insert(0, str(BRIDGE_ROOT))

from app.analysis_jobs import AnalysisJobService  # noqa: E402
from app.candidate_extraction import ExtractionLimits  # noqa: E402
from app.media_intake import (  # noqa: E402
    MediaIntakeService,
    is_allowlisted_video_name,
)


def emit(event: str, **fields: object) -> None:
    print(
        json.dumps(
            {"event": event, **fields},
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ),
        flush=True,
    )


def wait_for_terminal(getter, job_id, timeout_seconds: float) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = getter(job_id)
        if state["status"] in {"completed", "failed", "cancelled"}:
            return state
        time.sleep(0.2)
    raise TimeoutError(f"job {job_id} exceeded the batch timeout")


def matching_completed_jobs(analysis: AnalysisJobService, project_id: UUID) -> set:
    matches = set()
    for job in analysis.list(project_id, limit=128):
        if job["status"] != "completed" or job["result"] is None:
            continue
        provenance = job["result"].get("provenance")
        if not isinstance(provenance, dict):
            continue
        matches.add(
            (
                job["file_name"],
                provenance.get("source_size_bytes"),
                provenance.get("source_modified_time_ns"),
            )
        )
    return matches


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-directory", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument(
        "--project-id",
        default="11111111-1111-4111-8111-111111111111",
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--per-file-timeout-seconds", type=float, default=3_600)
    parser.add_argument("--maximum-files", type=int, default=10_000)
    args = parser.parse_args()

    project_id = UUID(args.project_id)
    source_directory = Path(args.source_directory)
    data_root = Path(args.data_root).resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    if not source_directory.is_dir() or source_directory.is_symlink():
        raise SystemExit("source directory is unavailable or is a symbolic link")

    files = []
    with os.scandir(source_directory) as entries:
        for entry in entries:
            if (
                len(files) >= args.maximum_files
                or not is_allowlisted_video_name(entry.name)
                or entry.is_symlink()
                or not entry.is_file(follow_symlinks=False)
            ):
                continue
            stat_result = entry.stat(follow_symlinks=False)
            files.append(
                (
                    int(stat_result.st_size),
                    entry.name.casefold(),
                    entry.name,
                    int(stat_result.st_mtime_ns),
                    Path(entry.path),
                )
            )
    files.sort()
    emit(
        "batch_discovered",
        file_count=len(files),
        total_size_bytes=sum(item[0] for item in files),
    )

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
            cadence_seconds=3.0,
            scene_threshold=0.25,
            group_gap_seconds=1.5,
            preview_duration_seconds=2.0,
            max_sample_frames=8,
            max_candidate_groups=8,
            max_frame_width=960,
            max_frame_bytes=8 * 1024 * 1024,
            max_clip_bytes=48 * 1024 * 1024,
            max_output_bytes=448 * 1024 * 1024,
            min_free_disk_bytes=15 * 1024 * 1024 * 1024,
            scan_timeout_seconds=min(
                args.per_file_timeout_seconds, 7_200.0
            ),
            artifact_timeout_seconds=min(
                args.per_file_timeout_seconds, 1_800.0
            ),
            max_total_seconds=min(
                args.per_file_timeout_seconds, 7_200.0
            ),
        ),
    )
    completed_signatures = matching_completed_jobs(analysis, project_id)
    completed = 0
    skipped = 0
    failed = 0
    started_at = time.monotonic()
    try:
        for index, (size, _, file_name, modified_ns, path) in enumerate(
            files, start=1
        ):
            signature = (file_name, size, modified_ns)
            if signature in completed_signatures:
                skipped += 1
                emit(
                    "file_skipped",
                    index=index,
                    file_count=len(files),
                    file_name=file_name,
                    reason="matching_completed_analysis",
                )
                continue
            file_started_at = time.monotonic()
            emit(
                "file_started",
                index=index,
                file_count=len(files),
                file_name=file_name,
                size_bytes=size,
            )
            try:
                snapshot = media.register_local_file(project_id, str(path))
                probe = media.probe(
                    project_id,
                    snapshot["snapshot_id"],
                    file_name,
                    min(120, max(1, int(args.per_file_timeout_seconds))),
                )
                hash_job = media.start_hash_job(
                    project_id, snapshot["snapshot_id"], file_name
                )
                hash_result = wait_for_terminal(
                    lambda job_id: media.get_hash_job(project_id, job_id),
                    hash_job["job_id"],
                    args.per_file_timeout_seconds,
                )
                if hash_result["status"] != "completed":
                    raise RuntimeError(
                        "hash verification failed: "
                        f"{hash_result.get('error_code')}"
                    )
                analysis_job = analysis.start(
                    project_id,
                    snapshot["snapshot_id"],
                    file_name,
                    hash_result["job_id"],
                    hash_result["sha256_lower"],
                )
                terminal = wait_for_terminal(
                    lambda job_id: analysis.get(project_id, job_id),
                    analysis_job["job_id"],
                    args.per_file_timeout_seconds,
                )
                if terminal["status"] != "completed":
                    raise RuntimeError(
                        "candidate extraction failed: "
                        f"{terminal.get('error_code')}"
                    )
                completed += 1
                result = terminal["result"]
                completed_signatures.add(signature)
                emit(
                    "file_completed",
                    index=index,
                    file_count=len(files),
                    file_name=file_name,
                    duration_seconds=probe["duration_seconds"],
                    sha256=hash_result["sha256_lower"],
                    job_id=terminal["job_id"],
                    candidate_count=result["candidate_count"],
                    artifact_count=result["artifact_count"],
                    elapsed_seconds=round(
                        time.monotonic() - file_started_at, 3
                    ),
                )
            except Exception as error:
                failed += 1
                emit(
                    "file_failed",
                    index=index,
                    file_count=len(files),
                    file_name=file_name,
                    error_type=type(error).__name__,
                    elapsed_seconds=round(
                        time.monotonic() - file_started_at, 3
                    ),
                )
        emit(
            "batch_completed",
            file_count=len(files),
            completed_count=completed,
            skipped_count=skipped,
            failed_count=failed,
            elapsed_seconds=round(time.monotonic() - started_at, 3),
        )
        return 1 if failed else 0
    finally:
        analysis.shutdown()
        media.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
