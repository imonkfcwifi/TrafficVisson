"""Safe, deterministic TrafficVisson evidence-package export.

The bridge resolves project authorization before calling this module.  This
module deliberately has no knowledge of Electron, HTTP, HWP COM, or fixture
data: it receives already-authorized structured records and writes a bounded
package below an explicit export root.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import stat as stat_module
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from uuid import UUID, uuid4, uuid5


SCHEMA_VERSION = "trafficvisson-artifact-export-v3"
CSV_ENCODING = "utf-8-sig"
MAX_ROWS_PER_DATASET = 250_000
MAX_TOTAL_ROWS = 500_000
MAX_CELL_UTF8_BYTES = 1 * 1024 * 1024
MAX_JSON_UTF8_BYTES = 64 * 1024 * 1024
HASH_CHUNK_BYTES = 4 * 1024 * 1024
MAX_EVIDENCE_INCIDENTS = 512
MAX_EVIDENCE_FILES = MAX_EVIDENCE_INCIDENTS * 2
MAX_EVIDENCE_TOTAL_BYTES = 8 * 1024**3
MAX_EVIDENCE_IMAGE_BYTES = 256 * 1024 * 1024
MAX_EVIDENCE_CLIP_BYTES = 2 * 1024**3
MIN_EXPORT_FREE_BYTES = 256 * 1024 * 1024
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_ARCHIVE_PART_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_EVIDENCE_KIND_CONTRACT = {
    "image": ("representative.jpg", "image/jpeg", MAX_EVIDENCE_IMAGE_BYTES),
    "clip": ("preview.mp4", "video/mp4", MAX_EVIDENCE_CLIP_BYTES),
}

CSV_CONTRACTS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    (
        "detections",
        "raw_detections.csv",
        (
            "detection_id",
            "trip_id",
            "video_id",
            "timestamp_ms",
            "frame_index",
            "type",
            "confidence",
            "score_value",
            "score_semantics",
            "score_calibration",
            "proposal_origin",
            "review_only",
            "automatic_labeling_allowed",
            "model_provider",
            "model_pack_id",
            "model_pack_version",
            "model_manifest_sha256",
            "dataset_id",
            "dataset_version",
            "dataset_sha256",
            "proposal_run_id",
            "evaluation_id",
            "evaluation_producer",
            "evaluation_producer_version",
            "analysis_candidate_sha256",
            "representative_image_sha256",
            "severity",
            "bbox",
            "mask_ref",
            "ocr_observation_id",
            "latitude",
            "longitude",
            "model_version_id",
            "source",
        ),
    ),
    (
        "incidents",
        "reviewed_incidents.csv",
        (
            "incident_id",
            "trip_id",
            "detection_ids",
            "event_type",
            "severity",
            "maintenance_required",
            "recommended_action",
            "review_status",
            "start_ms",
            "end_ms",
            "latitude",
            "longitude",
            "evidence_image_ref",
            "preview_clip_ref",
            "ocr_text",
            "location_validation",
            "model_version_id",
            "reviewed_at",
        ),
    ),
    (
        "label_questions",
        "label_questions.csv",
        (
            "question_id",
            "project_id",
            "trip_id",
            "candidate_incident_id",
            "prompt",
            "policy",
            "proposed_type",
            "proposed_confidence",
            "proposed_score_value",
            "proposed_score_semantics",
            "proposed_score_calibration",
            "proposal_origin",
            "review_only",
            "automatic_labeling_allowed",
            "model_provider",
            "model_pack_id",
            "model_pack_version",
            "model_manifest_sha256",
            "dataset_id",
            "dataset_version",
            "dataset_sha256",
            "proposal_run_id",
            "evaluation_id",
            "evaluation_producer",
            "evaluation_producer_version",
            "analysis_candidate_sha256",
            "representative_image_sha256",
            "proposed_geometry",
            "evidence_image_ref",
            "preview_clip_ref",
            "gps_latitude",
            "gps_longitude",
            "mandatory",
            "created_at",
        ),
    ),
    (
        "annotations",
        "annotations.csv",
        (
            "annotation_id",
            "question_id",
            "project_id",
            "trip_id",
            "type",
            "geometry",
            "ocr_text",
            "action_label",
            "severity",
            "answer",
            "review_status",
            "label_source",
            "created_at",
            "dataset_split",
        ),
    ),
    (
        "ocr_observations",
        "ocr_observations.csv",
        (
            "ocr_observation_id",
            "trip_id",
            "incident_id",
            "timestamp_ms",
            "raw_text",
            "normalized_text",
            "candidate_name",
            "kind",
            "confidence",
            "verified",
            "verification_source",
            "region",
            "model_version_id",
        ),
    ),
    (
        "route_points",
        "route_points.csv",
        (
            "route_point_id",
            "trip_id",
            "sequence",
            "timestamp_ms",
            "latitude",
            "longitude",
            "altitude_m",
            "speed_kph",
            "heading_deg",
            "accuracy_m",
            "source",
        ),
    ),
    (
        "maintenance_actions",
        "maintenance_actions.csv",
        (
            "action_id",
            "incident_id",
            "trip_id",
            "action_type",
            "description",
            "priority",
            "severity",
            "status",
            "due_date",
            "latitude",
            "longitude",
            "confirmed_by",
            "confirmed_at",
        ),
    ),
)


class ArtifactExportError(RuntimeError):
    """Base exception for a package that was not safely completed."""


class ArtifactExportCancelled(ArtifactExportError):
    """Raised when a caller cancellation hook requests a stop."""


class ArtifactExportLimitError(ArtifactExportError):
    """Raised before an input or output exceeds the bounded export contract."""


@dataclass(frozen=True)
class ArtifactDigest:
    name: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class EvidenceExportSource:
    """An authorized evidence handle that is never serialized with its path."""

    incident_id: str
    kind: str
    path: Path
    media_type: str
    size_bytes: int
    sha256: str

    @property
    def archive_name(self) -> str:
        file_name = _EVIDENCE_KIND_CONTRACT[self.kind][0]
        return f"evidence/{self.incident_id}/{file_name}"


@dataclass(frozen=True)
class _ArchiveMember:
    path: Path
    digest: ArtifactDigest
    media_type: Optional[str] = None


@dataclass(frozen=True)
class ArtifactExportResult:
    export_id: str
    output_directory: Path
    archive_path: Path
    artifacts: Tuple[ArtifactDigest, ...]


def _is_reparse_point(stat_result: os.stat_result) -> bool:
    attributes = getattr(stat_result, "st_file_attributes", 0)
    flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _check_cancelled(cancelled: Optional[Callable[[], bool]]) -> None:
    if cancelled is not None and cancelled():
        raise ArtifactExportCancelled("Artifact export was cancelled")


def _assert_safe_archive_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or len(name.encode("utf-8")) > 768
        or "\\" in name
        or "\x00" in name
        or any(ord(character) < 32 for character in name)
    ):
        raise ArtifactExportError("An archive member name is invalid")
    parsed = PurePosixPath(name)
    if parsed.is_absolute() or parsed.as_posix() != name:
        raise ArtifactExportError("An archive member name is invalid")
    parts = parsed.parts
    if (
        not parts
        or any(part in {"", ".", ".."} for part in parts)
        or any(not _ARCHIVE_PART_PATTERN.fullmatch(part) for part in parts)
    ):
        raise ArtifactExportError("An archive member name is invalid")
    return name


def _prepare_evidence_sources(
    sources: Sequence[EvidenceExportSource],
) -> Tuple[EvidenceExportSource, ...]:
    if isinstance(sources, (str, bytes, bytearray)) or not isinstance(
        sources, Sequence
    ):
        raise ArtifactExportError("Evidence sources must be a sequence")
    if len(sources) > MAX_EVIDENCE_FILES:
        raise ArtifactExportLimitError("Evidence exceeds the file-count limit")

    seen: set[Tuple[str, str]] = set()
    kinds_by_incident: Dict[str, set[str]] = {}
    total_bytes = 0
    prepared: List[EvidenceExportSource] = []
    for source in sources:
        if not isinstance(source, EvidenceExportSource):
            raise ArtifactExportError("Evidence contains an invalid source")
        try:
            canonical_incident_id = str(UUID(source.incident_id))
        except (ValueError, AttributeError, TypeError) as error:
            raise ArtifactExportError(
                "Evidence incident identity is invalid"
            ) from error
        if canonical_incident_id != source.incident_id:
            raise ArtifactExportError("Evidence incident identity is invalid")
        contract = _EVIDENCE_KIND_CONTRACT.get(source.kind)
        if contract is None or source.media_type != contract[1]:
            raise ArtifactExportError("Evidence media contract is invalid")
        if (
            isinstance(source.size_bytes, bool)
            or not isinstance(source.size_bytes, int)
            or not 0 < source.size_bytes <= contract[2]
            or not isinstance(source.sha256, str)
            or not _SHA256_PATTERN.fullmatch(source.sha256)
        ):
            raise ArtifactExportError("Evidence integrity metadata is invalid")
        source_path = Path(source.path)
        if (
            not source_path.is_absolute()
            or any(part in {".", ".."} for part in source_path.parts)
        ):
            raise ArtifactExportError("Evidence source is invalid")
        identity = (canonical_incident_id, source.kind)
        if identity in seen:
            raise ArtifactExportError("Evidence contains a duplicate member")
        seen.add(identity)
        kinds_by_incident.setdefault(canonical_incident_id, set()).add(
            source.kind
        )
        total_bytes += source.size_bytes
        if total_bytes > MAX_EVIDENCE_TOTAL_BYTES:
            raise ArtifactExportLimitError(
                "Evidence exceeds the total byte limit"
            )
        _assert_safe_archive_name(source.archive_name)
        prepared.append(source)

    if len(kinds_by_incident) > MAX_EVIDENCE_INCIDENTS:
        raise ArtifactExportLimitError("Evidence exceeds the incident limit")
    if any(kinds != {"image", "clip"} for kinds in kinds_by_incident.values()):
        raise ArtifactExportError(
            "Every evidence incident requires one image and one clip"
        )
    return tuple(
        sorted(
            prepared,
            key=lambda item: (
                item.incident_id,
                0 if item.kind == "image" else 1,
            ),
        )
    )


def _assert_safe_export_root(export_root: Path) -> Path:
    raw = os.fspath(export_root)
    if not raw or "\x00" in raw or any(ord(character) < 32 for character in raw):
        raise ArtifactExportError("Export root contains invalid characters")
    raw_path = Path(raw)
    if any(part in {".", ".."} for part in raw_path.parts):
        raise ArtifactExportError("Export root must not contain dot segments")
    if not raw_path.is_absolute():
        raise ArtifactExportError("Export root must be an absolute path")

    absolute = Path(os.path.abspath(raw))
    if absolute == Path(absolute.anchor):
        raise ArtifactExportError("A filesystem root is not a safe export root")
    try:
        if absolute == Path.home():
            raise ArtifactExportError("The user home directory is too broad for export")
    except RuntimeError:
        pass

    # Check every existing component with lstat so resolving the final path
    # cannot silently cross a symlink or Windows junction/reparse point.
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            component_stat = os.lstat(current)
        except FileNotFoundError as error:
            raise ArtifactExportError("Export root does not exist") from error
        if stat_module.S_ISLNK(component_stat.st_mode) or _is_reparse_point(
            component_stat
        ):
            raise ArtifactExportError("Export root must not cross a link or reparse point")

    root_stat = os.stat(absolute, follow_symlinks=False)
    if not stat_module.S_ISDIR(root_stat.st_mode):
        raise ArtifactExportError("Export root must be a directory")
    return absolute


def _records(
    payload: Mapping[str, Any], key: str
) -> Sequence[Mapping[str, Any]]:
    value = payload.get(key, ())
    if key == "annotations" and value == () and "labels" in payload:
        value = payload["labels"]
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ArtifactExportError(f"{key} must be a sequence of objects")
    if len(value) > MAX_ROWS_PER_DATASET:
        raise ArtifactExportLimitError(f"{key} exceeds the row limit")
    for record in value:
        if not isinstance(record, Mapping):
            raise ArtifactExportError(f"{key} contains a non-object record")
    return value


def _validate_gpx_export_contract(
    trip: Mapping[str, Any],
    route_points: Sequence[Mapping[str, Any]],
    provenance: Mapping[str, Any],
) -> None:
    """Fail closed when a production GPX snapshot crosses export boundaries."""

    gpx_track = provenance.get("gpx_track")
    if gpx_track is None:
        return
    if not isinstance(gpx_track, Mapping):
        raise ArtifactExportError("GPX provenance must be an object")
    if gpx_track.get("schema_version") != (
        "trafficvisson.gpx-export-provenance.v1"
    ):
        raise ArtifactExportError("GPX provenance schema is invalid")

    status = gpx_track.get("status")
    if status not in {"attached", "not_attached"}:
        raise ArtifactExportError("GPX provenance status is invalid")
    trip_id = trip.get("trip_id")
    project_id = trip.get("project_id")
    if (
        not isinstance(trip_id, str)
        or gpx_track.get("trip_id") != trip_id
        or not isinstance(project_id, str)
        or gpx_track.get("project_id") != project_id
    ):
        raise ArtifactExportError("GPX provenance crossed the project or trip boundary")

    if status == "not_attached":
        if route_points:
            raise ArtifactExportError(
                "A GPX-free export cannot contain route points"
            )
        return

    try:
        track_id = str(UUID(str(gpx_track["track_id"])))
        track_revision = gpx_track["track_revision"]
        point_count = gpx_track["point_count"]
        exported_point_count = gpx_track["exported_point_count"]
        offset = float(gpx_track["video_offset_seconds"])
        if (
            track_id != gpx_track["track_id"]
            or isinstance(track_revision, bool)
            or not isinstance(track_revision, int)
            or track_revision < 1
            or isinstance(point_count, bool)
            or not isinstance(point_count, int)
            or point_count < 2
            or point_count > 50_000
            or isinstance(exported_point_count, bool)
            or not isinstance(exported_point_count, int)
            or exported_point_count != point_count
            or len(route_points) != point_count
            or not math.isfinite(offset)
            or abs(offset) > 86_400
            or gpx_track.get("sampled_for_export") is not False
            or gpx_track.get("source_integrity_verified") is not True
            or not isinstance(gpx_track.get("source_sha256"), str)
            or not _SHA256_PATTERN.fullmatch(gpx_track["source_sha256"])
            or not isinstance(gpx_track.get("track_record_sha256"), str)
            or not _SHA256_PATTERN.fullmatch(
                gpx_track["track_record_sha256"]
            )
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError) as error:
        raise ArtifactExportError("Attached GPX provenance is invalid") from error

    seen_point_ids = set()
    track_uuid = UUID(track_id)
    for expected_sequence, point in enumerate(route_points):
        try:
            point_id = str(UUID(str(point["route_point_id"])))
            sequence = point["sequence"]
            timestamp_ms = point["timestamp_ms"]
            latitude = float(point["latitude"])
            longitude = float(point["longitude"])
            if (
                point_id != point["route_point_id"]
                or point_id
                != str(uuid5(track_uuid, f"route-point:{expected_sequence}"))
                or point_id in seen_point_ids
                or point.get("trip_id") != trip_id
                or isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence != expected_sequence
                or isinstance(timestamp_ms, bool)
                or not isinstance(timestamp_ms, int)
                or not math.isfinite(latitude)
                or not -90 <= latitude <= 90
                or not math.isfinite(longitude)
                or not -180 <= longitude <= 180
                or point.get("source") != "gpx"
            ):
                raise ValueError
            for optional_name in ("altitude_m", "speed_kph"):
                optional_value = point.get(optional_name)
                if optional_value is not None and not math.isfinite(
                    float(optional_value)
                ):
                    raise ValueError
        except (KeyError, TypeError, ValueError) as error:
            raise ArtifactExportError("A GPX route point is invalid") from error
        seen_point_ids.add(point_id)


def _json_value(value: Any) -> str:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ArtifactExportError("A CSV value is not JSON serializable") from error
    return text


def _csv_value(value: Any) -> str:
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ArtifactExportError("A CSV numeric value is not finite")
        text = str(value)
    elif isinstance(value, (dict, list, tuple)):
        text = _json_value(value)
    else:
        text = str(value)

    if len(text.encode("utf-8")) > MAX_CELL_UTF8_BYTES:
        raise ArtifactExportLimitError("A CSV cell exceeds the byte limit")

    # Preserve the value while preventing spreadsheet formula execution.
    if isinstance(value, str) and text.startswith(
        ("=", "+", "-", "@", "\t", "\r", "\n")
    ):
        text = "'" + text
    return text


def _atomic_write_csv(
    destination: Path,
    columns: Tuple[str, ...],
    rows: Sequence[Mapping[str, Any]],
    cancelled: Optional[Callable[[], bool]],
) -> None:
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        with temporary.open("w", encoding=CSV_ENCODING, newline="") as output:
            writer = csv.DictWriter(
                output,
                fieldnames=list(columns),
                extrasaction="ignore",
                lineterminator="\r\n",
            )
            writer.writeheader()
            for index, record in enumerate(rows):
                if index % 128 == 0:
                    _check_cancelled(cancelled)
                writer.writerow(
                    {column: _csv_value(record.get(column)) for column in columns}
                )
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ArtifactExportError("JSON payload is not serializable") from error
    if len(encoded) > MAX_JSON_UTF8_BYTES:
        raise ArtifactExportLimitError("A JSON artifact exceeds the byte limit")
    return encoded


def _atomic_write_bytes(destination: Path, content: bytes) -> None:
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        with temporary.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _digest_file(
    path: Path, cancelled: Optional[Callable[[], bool]]
) -> ArtifactDigest:
    file_stat = os.stat(path, follow_symlinks=False)
    if (
        not stat_module.S_ISREG(file_stat.st_mode)
        or _is_reparse_point(file_stat)
        or path.is_symlink()
    ):
        raise ArtifactExportError("An artifact is not a regular file")

    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while True:
            _check_cancelled(cancelled)
            chunk = source.read(HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    if size != file_stat.st_size:
        raise ArtifactExportError("An artifact changed while it was verified")
    return ArtifactDigest(path.name, size, digest.hexdigest())


def _same_open_file(
    before: os.stat_result, opened: os.stat_result
) -> bool:
    if (
        int(before.st_size) != int(opened.st_size)
        or int(before.st_mtime_ns) != int(opened.st_mtime_ns)
    ):
        return False
    before_inode = int(getattr(before, "st_ino", 0))
    opened_inode = int(getattr(opened, "st_ino", 0))
    before_device = int(getattr(before, "st_dev", 0))
    opened_device = int(getattr(opened, "st_dev", 0))
    if before_inode and opened_inode and before_inode != opened_inode:
        return False
    if before_device and opened_device and before_device != opened_device:
        return False
    return True


def _validate_media_header(header: bytes, media_type: Optional[str]) -> None:
    if media_type == "image/jpeg" and not header.startswith(b"\xff\xd8\xff"):
        raise ArtifactExportError("Evidence JPEG signature verification failed")
    if media_type == "video/mp4" and (
        len(header) < 12 or header[4:8] != b"ftyp"
    ):
        raise ArtifactExportError("Evidence MP4 signature verification failed")


def _build_zip_atomic(
    destination: Path,
    members: Iterable[_ArchiveMember],
    cancelled: Optional[Callable[[], bool]],
) -> None:
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        with zipfile.ZipFile(
            temporary, mode="x", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            for member in members:
                _check_cancelled(cancelled)
                archive_name = _assert_safe_archive_name(member.digest.name)
                try:
                    before = os.lstat(member.path)
                except OSError as error:
                    raise ArtifactExportError(
                        "An archive source is unavailable"
                    ) from error
                if (
                    stat_module.S_ISLNK(before.st_mode)
                    or _is_reparse_point(before)
                    or not stat_module.S_ISREG(before.st_mode)
                    or int(before.st_size) != member.digest.size_bytes
                ):
                    raise ArtifactExportError(
                        "An archive source failed integrity verification"
                    )
                info = zipfile.ZipInfo(
                    archive_name, date_time=(1980, 1, 1, 0, 0, 0)
                )
                info.compress_type = (
                    zipfile.ZIP_STORED
                    if member.media_type is not None
                    else zipfile.ZIP_DEFLATED
                )
                info.external_attr = 0o100600 << 16
                digest = hashlib.sha256()
                size = 0
                try:
                    with member.path.open("rb") as source:
                        opened = os.fstat(source.fileno())
                        if not _same_open_file(before, opened):
                            raise ArtifactExportError(
                                "An archive source changed before it was read"
                            )
                        header = source.read(32)
                        _validate_media_header(header, member.media_type)
                        with archive.open(
                            info,
                            "w",
                            force_zip64=(
                                member.digest.size_bytes
                                >= zipfile.ZIP64_LIMIT
                            ),
                        ) as target:
                            if header:
                                digest.update(header)
                                size += len(header)
                                target.write(header)
                            while True:
                                _check_cancelled(cancelled)
                                chunk = source.read(HASH_CHUNK_BYTES)
                                if not chunk:
                                    break
                                digest.update(chunk)
                                size += len(chunk)
                                if size > member.digest.size_bytes:
                                    raise ArtifactExportError(
                                        "An archive source changed while it was read"
                                    )
                                target.write(chunk)
                        after = os.lstat(member.path)
                except ArtifactExportError:
                    raise
                except OSError as error:
                    raise ArtifactExportError(
                        "An archive source could not be read"
                    ) from error
                if (
                    not _same_open_file(before, after)
                    or stat_module.S_ISLNK(after.st_mode)
                    or _is_reparse_point(after)
                    or not stat_module.S_ISREG(after.st_mode)
                    or size != member.digest.size_bytes
                    or digest.hexdigest() != member.digest.sha256
                ):
                    raise ArtifactExportError(
                        "An archive source failed integrity verification"
                    )
        # Windows does not permit fsync on a read-only CRT descriptor.
        with temporary.open("ab") as archive_source:
            os.fsync(archive_source.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _verify_zip(
    archive_path: Path,
    expected_members: Sequence[ArtifactDigest],
    cancelled: Optional[Callable[[], bool]],
) -> None:
    expected_by_name = {item.name: item for item in expected_members}
    try:
        with zipfile.ZipFile(archive_path, mode="r") as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if (
                len(names) != len(set(names))
                or names != [item.name for item in expected_members]
                or set(names) != set(expected_by_name)
            ):
                raise ArtifactExportError("ZIP member contract verification failed")
            for info in infos:
                if info.is_dir():
                    raise ArtifactExportError("ZIP contains an unsafe member name")
                _assert_safe_archive_name(info.filename)
                expected = expected_by_name[info.filename]
                if info.file_size != expected.size_bytes:
                    raise ArtifactExportError("ZIP member size verification failed")
                digest = hashlib.sha256()
                size = 0
                with archive.open(info, "r") as source:
                    while True:
                        _check_cancelled(cancelled)
                        chunk = source.read(HASH_CHUNK_BYTES)
                        if not chunk:
                            break
                        digest.update(chunk)
                        size += len(chunk)
                if size != expected.size_bytes or digest.hexdigest() != expected.sha256:
                    raise ArtifactExportError("ZIP member hash verification failed")
    except (zipfile.BadZipFile, OSError) as error:
        raise ArtifactExportError("ZIP archive verification failed") from error


def _safe_cleanup(job_directory: Optional[Path], export_root: Path) -> None:
    if job_directory is None or not job_directory.exists():
        return
    try:
        if job_directory.parent != export_root:
            return
        job_stat = os.lstat(job_directory)
        if stat_module.S_ISLNK(job_stat.st_mode) or _is_reparse_point(job_stat):
            job_directory.unlink(missing_ok=True)
        else:
            shutil.rmtree(job_directory)
    except OSError:
        # Preserve the original export error.  A startup janitor can remove a
        # private incomplete directory if an external process locked it.
        return


def export_artifacts(
    export_root: Path,
    payload: Mapping[str, Any],
    *,
    evidence_sources: Sequence[EvidenceExportSource] = (),
    cancelled: Optional[Callable[[], bool]] = None,
    now: Optional[Callable[[], datetime]] = None,
) -> ArtifactExportResult:
    """Write and verify one complete export package.

    ``payload`` accepts ``trip``, ``detections``, ``incidents``,
    ``label_questions``, ``annotations`` (or ``labels``),
    ``ocr_observations``, ``route_points``, ``maintenance_actions``,
    ``report_payload``, and ``provenance``.  Missing record lists are exported
    as header-only CSV files so consumers always receive the full contract.
    Authorized evidence paths are accepted only through ``evidence_sources``;
    those paths are streamed into the ZIP and are never serialized.
    """

    if not isinstance(payload, Mapping):
        raise ArtifactExportError("Export payload must be an object")
    root = _assert_safe_export_root(Path(export_root))
    _check_cancelled(cancelled)
    prepared_evidence = _prepare_evidence_sources(evidence_sources)

    trip = payload.get("trip", {})
    report_payload = payload.get("report_payload", {})
    provenance = payload.get("provenance", {})
    for name, value in (
        ("trip", trip),
        ("report_payload", report_payload),
        ("provenance", provenance),
    ):
        if not isinstance(value, Mapping):
            raise ArtifactExportError(f"{name} must be an object")

    datasets: Dict[str, Sequence[Mapping[str, Any]]] = {
        key: _records(payload, key) for key, _file_name, _columns in CSV_CONTRACTS
    }
    _validate_gpx_export_contract(
        trip,
        datasets["route_points"],
        provenance,
    )
    total_rows = sum(len(rows) for rows in datasets.values())
    if total_rows > MAX_TOTAL_ROWS:
        raise ArtifactExportLimitError("Export exceeds the total row limit")

    export_id = str(uuid4())
    job_directory: Optional[Path] = None
    try:
        job_directory = Path(
            tempfile.mkdtemp(
                prefix=f"trafficvisson-export-{export_id}-", dir=os.fspath(root)
            )
        )
        os.chmod(job_directory, 0o700)
        if job_directory.parent != root:
            raise ArtifactExportError("Export directory escaped its root")

        data_paths: List[Path] = []
        for key, file_name, columns in CSV_CONTRACTS:
            _check_cancelled(cancelled)
            destination = job_directory / file_name
            _atomic_write_csv(destination, columns, datasets[key], cancelled)
            data_paths.append(destination)

        report_document = {"trip": dict(trip), "report": dict(report_payload)}
        report_path = job_directory / "report_payload.json"
        _atomic_write_bytes(report_path, _canonical_json_bytes(report_document))
        data_paths.append(report_path)

        data_digests = tuple(
            _digest_file(path, cancelled) for path in data_paths
        )
        evidence_digests = tuple(
            ArtifactDigest(
                name=source.archive_name,
                size_bytes=source.size_bytes,
                sha256=source.sha256,
            )
            for source in prepared_evidence
        )
        content_digests = (*data_digests, *evidence_digests)
        generated_at = (now or (lambda: datetime.now(timezone.utc)))()
        if generated_at.tzinfo is None:
            raise ArtifactExportError("Export timestamp must be timezone-aware")
        incident_ids = sorted(
            {source.incident_id for source in prepared_evidence}
        )
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "export_id": export_id,
            "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
            "trip_id": trip.get("trip_id"),
            "record_counts": {
                key: len(datasets[key]) for key, _name, _columns in CSV_CONTRACTS
            },
            "provenance": dict(provenance),
            "artifacts": [
                {
                    "name": item.name,
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                }
                for item in content_digests
            ],
            "evidence": {
                "incident_count": len(incident_ids),
                "file_count": len(prepared_evidence),
                "total_size_bytes": sum(
                    item.size_bytes for item in prepared_evidence
                ),
                "members": [
                    {
                        "incident_id": source.incident_id,
                        "kind": (
                            "representative_image"
                            if source.kind == "image"
                            else "preview_clip"
                        ),
                        "name": source.archive_name,
                        "media_type": source.media_type,
                        "size_bytes": source.size_bytes,
                        "sha256": source.sha256,
                    }
                    for source in prepared_evidence
                ],
            },
        }
        manifest_path = job_directory / "manifest.json"
        _atomic_write_bytes(manifest_path, _canonical_json_bytes(manifest))
        manifest_digest = _digest_file(manifest_path, cancelled)

        required_free_bytes = (
            sum(item.size_bytes for item in content_digests)
            + manifest_digest.size_bytes
            + MIN_EXPORT_FREE_BYTES
        )
        try:
            available_bytes = shutil.disk_usage(root).free
        except OSError as error:
            raise ArtifactExportError(
                "Export destination capacity could not be verified"
            ) from error
        if available_bytes < required_free_bytes:
            raise ArtifactExportLimitError(
                "Export destination does not have enough free space"
            )

        archive_path = job_directory / "trafficvisson-export.zip"
        archive_members = [
            _ArchiveMember(path=path, digest=digest)
            for path, digest in zip(data_paths, data_digests)
        ]
        archive_members.extend(
            _ArchiveMember(
                path=source.path,
                digest=digest,
                media_type=source.media_type,
            )
            for source, digest in zip(
                prepared_evidence, evidence_digests
            )
        )
        archive_members.append(
            _ArchiveMember(path=manifest_path, digest=manifest_digest)
        )
        _build_zip_atomic(
            archive_path, archive_members, cancelled
        )
        _verify_zip(
            archive_path, (*content_digests, manifest_digest), cancelled
        )
        archive_digest = _digest_file(archive_path, cancelled)

        all_digests = (*content_digests, manifest_digest, archive_digest)
        # Final verification is intentionally separate from write-time hashing:
        # success is returned only while every completed file still matches.
        for expected in (*data_digests, manifest_digest, archive_digest):
            actual = _digest_file(job_directory / expected.name, cancelled)
            if actual != expected:
                raise ArtifactExportError(
                    f"Artifact verification failed for {expected.name}"
                )
        _check_cancelled(cancelled)
        return ArtifactExportResult(
            export_id=export_id,
            output_directory=job_directory,
            archive_path=archive_path,
            artifacts=tuple(all_digests),
        )
    except BaseException:
        _safe_cleanup(job_directory, root)
        raise
