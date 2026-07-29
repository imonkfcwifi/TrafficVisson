"""Attach real source-container start coordinates to persisted analysis records.

This migration reads only bounded ffprobe metadata.  It never infers a route:
valid ISO 6709 format tags are stored with ``scope=capture_start_only`` and
zero coordinates are ignored.  Source size and modification time must still
match the immutable provenance recorded by the analysis job.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4


DESKTOP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DESKTOP_ROOT / "bridge"))

from app.candidate_extraction import _parse_source_location  # noqa: E402


def _probe_location(ffprobe: Path, source: Path):
    completed = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_entries",
            (
                "format_tags=location,location-eng,"
                "com.apple.quicktime.location.ISO6709"
            ),
            "-of",
            "json",
            str(source),
        ],
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0 or len(completed.stdout) > 64 * 1024:
        raise RuntimeError("ffprobe metadata inspection failed")
    decoded = json.loads(completed.stdout.decode("utf-8", errors="strict"))
    if not isinstance(decoded, dict):
        raise RuntimeError("ffprobe returned an invalid metadata root")
    raw_format = decoded.get("format")
    if not isinstance(raw_format, dict):
        return None
    return _parse_source_location(raw_format)


def _atomic_record_write(record_path: Path, payload: dict) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest().encode("ascii")
    digest_path = record_path.with_suffix(".sha256")
    token = uuid4().hex
    temporary_record = record_path.with_name(f".{record_path.stem}.{token}.tmp")
    temporary_digest = record_path.with_name(
        f".{record_path.stem}.{token}.sha256.tmp"
    )
    try:
        with temporary_record.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        with temporary_digest.open("xb") as stream:
            stream.write(digest)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_record, record_path)
        os.replace(temporary_digest, digest_path)
    finally:
        temporary_record.unlink(missing_ok=True)
        temporary_digest.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--ffprobe", required=True, type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write records. Without this flag the command is a dry run.",
    )
    args = parser.parse_args()

    source_root = args.source_root.resolve(strict=True)
    data_root = args.data_root.resolve(strict=True)
    ffprobe = args.ffprobe.resolve(strict=True)
    if not source_root.is_dir() or not data_root.is_dir():
        raise SystemExit("source-root and data-root must be existing directories")
    if ffprobe.name.casefold() not in {"ffprobe", "ffprobe.exe"} or not ffprobe.is_file():
        raise SystemExit("ffprobe must resolve to an ffprobe executable")

    sources_by_name: dict[str, list[Path]] = {}
    for source in source_root.glob("*.mp4"):
        if source.is_file() and not source.is_symlink():
            sources_by_name.setdefault(source.name.casefold(), []).append(source)

    counters = {
        "records": 0,
        "already_present": 0,
        "source_missing_or_ambiguous": 0,
        "source_changed": 0,
        "no_valid_coordinate": 0,
        "would_update": 0,
        "updated": 0,
        "errors": 0,
    }
    records = sorted(
        data_root.glob("projects/*/analysis-job-records/*.json"),
        key=lambda path: str(path).casefold(),
    )
    for record_path in records:
        counters["records"] += 1
        try:
            payload = json.loads(record_path.read_text(encoding="utf-8"))
            provenance = payload["result"]["provenance"]
            if provenance.get("source_location") is not None:
                counters["already_present"] += 1
                continue
            matches = sources_by_name.get(str(payload["file_name"]).casefold(), [])
            if len(matches) != 1:
                counters["source_missing_or_ambiguous"] += 1
                continue
            source = matches[0]
            source_stat = source.stat()
            if (
                source_stat.st_size != payload["source_size_bytes"]
                or source_stat.st_mtime_ns != payload["source_modified_time_ns"]
            ):
                counters["source_changed"] += 1
                continue
            location = _probe_location(ffprobe, source)
            if location is None:
                counters["no_valid_coordinate"] += 1
                continue
            counters["would_update"] += 1
            if args.apply:
                provenance["source_location"] = dict(location.__dict__)
                _atomic_record_write(record_path, payload)
                counters["updated"] += 1
        except Exception as error:
            counters["errors"] += 1
            print(f"ERROR {record_path.name}: {type(error).__name__}", file=sys.stderr)

    print(json.dumps(counters, ensure_ascii=False, sort_keys=True))
    return 0 if counters["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
