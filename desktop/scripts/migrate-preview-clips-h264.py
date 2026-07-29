"""Migrate persisted TrafficSuperVision preview clips to Chromium-compatible H.264.

Run this only while the desktop bridge is stopped. The script accepts one
project directory, verifies every source clip against its persisted digest,
transcodes MPEG-4 Part 2 clips to H.264, and atomically updates the job record
and its adjacent SHA-256 sidecar.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_checked(argv: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        argv,
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip()[-500:]
        raise RuntimeError(f"{Path(argv[0]).name} failed: {detail}")
    return completed


def video_codec(ffprobe: str, path: Path) -> str:
    completed = run_checked(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    return completed.stdout.strip().lower()


def canonical_record_bytes(record: dict) -> bytes:
    return json.dumps(
        record,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def validate_project_root(project_root: Path) -> tuple[Path, Path]:
    resolved = project_root.resolve(strict=True)
    records_root = resolved / "analysis-job-records"
    extractions_root = resolved / "candidate-extractions"
    if (
        not resolved.is_dir()
        or resolved.is_symlink()
        or not records_root.is_dir()
        or records_root.is_symlink()
        or not extractions_root.is_dir()
        or extractions_root.is_symlink()
    ):
        raise ValueError("project root does not contain safe persisted analysis data")
    return records_root.resolve(strict=True), extractions_root.resolve(strict=True)


def migrate_record(
    *,
    record_path: Path,
    extractions_root: Path,
    ffmpeg: str,
    ffprobe: str,
) -> tuple[int, int]:
    original_bytes = record_path.read_bytes()
    digest_path = record_path.with_suffix(".sha256")
    expected_record_digest = digest_path.read_text(encoding="ascii").strip()
    if hashlib.sha256(original_bytes).hexdigest() != expected_record_digest:
        raise ValueError(f"record digest mismatch: {record_path.name}")

    record = json.loads(original_bytes)
    if record.get("status") != "completed":
        return 0, 0
    result = record.get("result")
    candidates = result.get("candidates") if isinstance(result, dict) else None
    directory_name = record.get("artifact_directory_name")
    if not isinstance(candidates, list) or not isinstance(directory_name, str):
        raise ValueError(f"invalid completed record: {record_path.name}")

    run_directory = (extractions_root / directory_name).resolve(strict=True)
    if run_directory.parent != extractions_root or run_directory.is_symlink():
        raise ValueError(f"unsafe artifact directory: {record_path.name}")

    pending: list[tuple[Path, Path, Path, dict, int, str]] = []
    skipped = 0
    for candidate in candidates:
        artifact = candidate.get("preview_clip") if isinstance(candidate, dict) else None
        if not isinstance(artifact, dict):
            raise ValueError(f"missing preview clip: {record_path.name}")
        relative_path = artifact.get("relative_path")
        if not isinstance(relative_path, str) or Path(relative_path).name != relative_path:
            raise ValueError(f"unsafe preview path: {record_path.name}")
        clip_path = (run_directory / relative_path).resolve(strict=True)
        if clip_path.parent != run_directory or clip_path.is_symlink():
            raise ValueError(f"unsafe preview artifact: {record_path.name}")
        actual_size = clip_path.stat().st_size
        actual_digest = sha256_file(clip_path)
        if actual_size != artifact.get("size_bytes") or actual_digest != artifact.get("sha256"):
            raise ValueError(f"preview integrity mismatch: {clip_path.name}")
        if video_codec(ffprobe, clip_path) == "h264":
            skipped += 1
            continue

        temporary = clip_path.with_name(f".{clip_path.stem}.{uuid.uuid4().hex}.h264.mp4")
        backup = clip_path.with_name(f".{clip_path.name}.{uuid.uuid4().hex}.backup")
        run_checked(
            [
                ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(clip_path),
                "-map",
                "0:v:0",
                "-an",
                "-sn",
                "-dn",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "26",
                "-profile:v",
                "main",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                "-y",
                str(temporary),
            ]
        )
        if video_codec(ffprobe, temporary) != "h264":
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"transcoded preview is not H.264: {clip_path.name}")
        pending.append(
            (
                clip_path,
                temporary,
                backup,
                artifact,
                temporary.stat().st_size,
                sha256_file(temporary),
            )
        )

    if not pending:
        return 0, skipped

    replaced: list[tuple[Path, Path]] = []
    try:
        for clip_path, temporary, backup, artifact, size, digest in pending:
            os.replace(clip_path, backup)
            os.replace(temporary, clip_path)
            replaced.append((clip_path, backup))
            artifact["size_bytes"] = size
            artifact["sha256"] = digest

        result["total_artifact_bytes"] = sum(
            item[kind]["size_bytes"]
            for item in candidates
            for kind in ("representative_image", "preview_clip")
        )
        updated_bytes = canonical_record_bytes(record)
        atomic_write(record_path, updated_bytes)
        atomic_write(
            digest_path,
            hashlib.sha256(updated_bytes).hexdigest().encode("ascii"),
        )
    except Exception:
        for clip_path, backup in reversed(replaced):
            clip_path.unlink(missing_ok=True)
            if backup.exists():
                os.replace(backup, clip_path)
        raise
    finally:
        for _clip_path, temporary, backup, _artifact, _size, _digest in pending:
            temporary.unlink(missing_ok=True)
            backup.unlink(missing_ok=True)

    return len(pending), skipped


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_root", type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args()
    records_root, extractions_root = validate_project_root(args.project_root)

    migrated = 0
    skipped = 0
    records = sorted(records_root.glob("*.json"))
    for index, record_path in enumerate(records, start=1):
        changed, already_h264 = migrate_record(
            record_path=record_path,
            extractions_root=extractions_root,
            ffmpeg=args.ffmpeg,
            ffprobe=args.ffprobe,
        )
        migrated += changed
        skipped += already_h264
        print(
            f"[{index}/{len(records)}] {record_path.stem}: "
            f"migrated={changed}, already_h264={already_h264}",
            flush=True,
        )
    print(
        json.dumps(
            {
                "records": len(records),
                "migrated_clips": migrated,
                "already_h264": skipped,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
