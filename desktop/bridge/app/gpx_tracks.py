from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat as stat_module
import xml.etree.ElementTree as element_tree
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence
from uuid import UUID, uuid5


GPX_TRACK_SCHEMA = "trafficvisson.gpx-track.v1"
GPX_EXPORT_PROVENANCE_SCHEMA = "trafficvisson.gpx-export-provenance.v1"
MAX_GPX_BYTES = 25 * 1024 * 1024
MAX_GPX_POINTS = 50_000
MAX_RESPONSE_POINTS = 8_000
MAX_RECORD_BYTES = 32 * 1024 * 1024
MAX_VIDEO_OFFSET_SECONDS = 86_400.0
GPS_GAP_SECONDS = 10.0

_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_UNSAFE_XML_DECLARATION = re.compile(
    br"<!\s*(?:DOCTYPE|ENTITY)\b",
    flags=re.IGNORECASE,
)


class GpxTrackError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ParsedGpxTrack:
    source_name: str
    source_payload: bytes
    source_size_bytes: int
    source_sha256: str
    started_at: datetime
    ended_at: datetime
    route_points: Sequence[Mapping[str, Any]]
    total_distance_meters: float


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_uuid(value: UUID | str, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as error:
        raise GpxTrackError("invalid_identity", f"{label} is invalid") from error


def _canonical_offset(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GpxTrackError(
            "invalid_video_offset",
            "Video offset must be a finite number of seconds",
        )
    offset = float(value)
    if not math.isfinite(offset) or abs(offset) > MAX_VIDEO_OFFSET_SECONDS:
        raise GpxTrackError(
            "invalid_video_offset",
            "Video offset must be within plus or minus 24 hours",
        )
    return round(offset, 3)


def _safe_source_name(source_path: Path) -> str:
    name = source_path.name.replace("\x00", "\uFFFD").strip()
    return name[:255] or "route.gpx"


def _read_regular_file(source_path: Path) -> bytes:
    if not source_path.is_absolute():
        raise GpxTrackError(
            "invalid_source_path",
            "The selected GPX file must use an absolute local path",
        )
    if source_path.suffix.lower() != ".gpx":
        raise GpxTrackError(
            "unsupported_extension",
            "Only .gpx route files are supported",
        )
    try:
        before = source_path.lstat()
    except OSError as error:
        raise GpxTrackError(
            "source_unavailable",
            "The selected GPX file is no longer available",
        ) from error
    if stat_module.S_ISLNK(before.st_mode) or not stat_module.S_ISREG(
        before.st_mode
    ):
        raise GpxTrackError(
            "source_not_regular",
            "The selected GPX source must be a regular file",
        )
    if before.st_size < 1 or before.st_size > MAX_GPX_BYTES:
        raise GpxTrackError(
            "source_size_invalid",
            "The selected GPX file is empty or exceeds the 25 MB limit",
        )

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(source_path), flags)
    except OSError as error:
        raise GpxTrackError(
            "source_unavailable",
            "The selected GPX file could not be opened safely",
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat_module.S_ISREG(opened.st_mode)
            or opened.st_size != before.st_size
            or (
                hasattr(before, "st_ino")
                and before.st_ino
                and opened.st_ino
                and before.st_ino != opened.st_ino
            )
            or (
                hasattr(before, "st_dev")
                and before.st_dev != opened.st_dev
            )
        ):
            raise GpxTrackError(
                "source_changed",
                "The selected GPX file changed while it was being opened",
            )
        chunks: List[bytes] = []
        consumed = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, MAX_GPX_BYTES + 1))
            if not chunk:
                break
            consumed += len(chunk)
            if consumed > MAX_GPX_BYTES:
                raise GpxTrackError(
                    "source_size_invalid",
                    "The selected GPX file exceeds the 25 MB limit",
                )
            chunks.append(chunk)
        data = b"".join(chunks)
        if len(data) != opened.st_size:
            raise GpxTrackError(
                "source_changed",
                "The selected GPX file changed while it was being read",
            )
        return data
    finally:
        os.close(descriptor)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith(("Z", "z")):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise GpxTrackError(
            "invalid_timestamp",
            "A GPX track point contains an invalid timestamp",
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GpxTrackError(
            "timezone_missing",
            "GPX timestamps must include UTC or an explicit timezone",
        )
    return parsed.astimezone(timezone.utc)


def _finite_coordinate(
    value: object,
    *,
    minimum: float,
    maximum: float,
    label: str,
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise GpxTrackError(
            "invalid_coordinate",
            f"A GPX point has an invalid {label}",
        ) from error
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise GpxTrackError(
            "invalid_coordinate",
            f"A GPX point has an out-of-range {label}",
        )
    return number


def _optional_finite_text(
    value: Optional[str],
    *,
    minimum: float,
    maximum: float,
) -> Optional[float]:
    if value is None or not value.strip():
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        return None
    return parsed


def _point_children(point: element_tree.Element) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for child in point.iter():
        if child is point or child.text is None:
            continue
        name = _local_name(child.tag)
        if name in {"time", "ele", "speed"} and name not in values:
            values[name] = child.text
    return values


def _haversine_meters(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    radius = 6_371_008.8
    lat_a = math.radians(latitude_a)
    lat_b = math.radians(latitude_b)
    delta_lat = lat_b - lat_a
    delta_lon = math.radians(longitude_b - longitude_a)
    haversine = (
        math.sin(delta_lat / 2.0) ** 2
        + math.cos(lat_a)
        * math.cos(lat_b)
        * math.sin(delta_lon / 2.0) ** 2
    )
    return radius * 2.0 * math.atan2(
        math.sqrt(haversine),
        math.sqrt(max(0.0, 1.0 - haversine)),
    )


def parse_gpx(source_path: Path) -> ParsedGpxTrack:
    payload = _read_regular_file(source_path)
    if _UNSAFE_XML_DECLARATION.search(payload):
        raise GpxTrackError(
            "unsafe_xml",
            "DOCTYPE and ENTITY declarations are not allowed in GPX files",
        )
    try:
        root = element_tree.fromstring(payload)
    except element_tree.ParseError as error:
        raise GpxTrackError(
            "invalid_xml",
            "The selected file is not well-formed GPX XML",
        ) from error
    if _local_name(root.tag) != "gpx":
        raise GpxTrackError(
            "invalid_gpx_root",
            "The selected XML document is not a GPX file",
        )

    raw_points: List[Dict[str, Any]] = []
    observed_points = 0
    for element in root.iter():
        if _local_name(element.tag) not in {"trkpt", "rtept"}:
            continue
        observed_points += 1
        if observed_points > MAX_GPX_POINTS:
            raise GpxTrackError(
                "too_many_points",
                f"GPX files may contain at most {MAX_GPX_POINTS:,} route points",
            )
        children = _point_children(element)
        timestamp_text = children.get("time")
        if timestamp_text is None:
            continue
        latitude = _finite_coordinate(
            element.attrib.get("lat"),
            minimum=-90.0,
            maximum=90.0,
            label="latitude",
        )
        longitude = _finite_coordinate(
            element.attrib.get("lon"),
            minimum=-180.0,
            maximum=180.0,
            label="longitude",
        )
        altitude = _optional_finite_text(
            children.get("ele"),
            minimum=-1_000.0,
            maximum=20_000.0,
        )
        speed_mps = _optional_finite_text(
            children.get("speed"),
            minimum=0.0,
            maximum=100.0,
        )
        raw_points.append(
            {
                "recorded_at": _parse_timestamp(timestamp_text),
                "latitude": latitude,
                "longitude": longitude,
                "altitude_meters": altitude,
                "explicit_speed_kph": (
                    round(speed_mps * 3.6, 3)
                    if speed_mps is not None
                    else None
                ),
            }
        )

    if len(raw_points) < 2:
        raise GpxTrackError(
            "insufficient_timed_points",
            "The GPX file must contain at least two points with timezone-aware timestamps",
        )

    raw_points.sort(key=lambda item: item["recorded_at"])
    points: List[Dict[str, Any]] = []
    total_distance = 0.0
    for item in raw_points:
        if points and item["recorded_at"] == points[-1]["_recorded_at"]:
            continue
        previous = points[-1] if points else None
        if previous is None:
            elapsed_seconds = 0.0
            distance_from_previous = 0.0
            gap_from_previous = False
            derived_speed_kph = None
        else:
            elapsed_seconds = (
                item["recorded_at"] - raw_points[0]["recorded_at"]
            ).total_seconds()
            delta_seconds = (
                item["recorded_at"] - previous["_recorded_at"]
            ).total_seconds()
            if delta_seconds <= 0:
                continue
            distance_from_previous = _haversine_meters(
                previous["latitude"],
                previous["longitude"],
                item["latitude"],
                item["longitude"],
            )
            total_distance += distance_from_previous
            gap_from_previous = delta_seconds > GPS_GAP_SECONDS
            derived_speed_kph = min(
                300.0,
                max(0.0, distance_from_previous / delta_seconds * 3.6),
            )
        speed_kph = item["explicit_speed_kph"]
        if speed_kph is None:
            speed_kph = derived_speed_kph
        points.append(
            {
                "sequence": len(points),
                "_recorded_at": item["recorded_at"],
                "recorded_at": item["recorded_at"].isoformat(),
                "elapsed_seconds": round(elapsed_seconds, 3),
                "latitude": round(item["latitude"], 8),
                "longitude": round(item["longitude"], 8),
                "altitude_meters": (
                    round(item["altitude_meters"], 3)
                    if item["altitude_meters"] is not None
                    else None
                ),
                "speed_kph": (
                    round(speed_kph, 3)
                    if speed_kph is not None
                    else None
                ),
                "distance_from_previous_meters": round(
                    distance_from_previous,
                    3,
                ),
                "gap_from_previous": gap_from_previous,
            }
        )

    if len(points) < 2:
        raise GpxTrackError(
            "insufficient_timed_points",
            "The GPX file does not contain two distinct timed points",
        )
    for point in points:
        point.pop("_recorded_at", None)
    return ParsedGpxTrack(
        source_name=_safe_source_name(source_path),
        source_payload=payload,
        source_size_bytes=len(payload),
        source_sha256=hashlib.sha256(payload).hexdigest(),
        started_at=raw_points[0]["recorded_at"],
        ended_at=raw_points[-1]["recorded_at"],
        route_points=tuple(points),
        total_distance_meters=round(total_distance, 3),
    )


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{id(payload):x}.tmp"
    )
    try:
        with temporary.open("xb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sample_indices(
    points: Sequence[Mapping[str, Any]],
    limit: int,
) -> List[int]:
    if len(points) <= limit:
        return list(range(len(points)))
    indices = {0, len(points) - 1}
    stride = (len(points) - 1) / float(limit - 1)
    indices.update(round(index * stride) for index in range(limit))
    for index, point in enumerate(points):
        if point.get("gap_from_previous"):
            indices.add(index)
            if index > 0:
                indices.add(index - 1)
    ordered = sorted(indices)
    if len(ordered) <= limit:
        return ordered
    stride = (len(ordered) - 1) / float(limit - 1)
    return sorted(
        {ordered[round(index * stride)] for index in range(limit)}
    )


class GpxTrackStore:
    """Durable, project-scoped GPX originals and normalized route records."""

    def __init__(
        self,
        data_root: Path,
        resolve_trip: Callable[[UUID, UUID], Mapping[str, Any]],
    ) -> None:
        self._data_root = data_root.resolve()
        self._resolve_trip = resolve_trip
        self._lock = RLock()

    def _track_directory(self, project_id: str) -> Path:
        return self._data_root / "projects" / project_id / "gps-tracks"

    def _record_path(self, project_id: str, trip_id: str) -> Path:
        return self._track_directory(project_id) / f"{trip_id}.json"

    def _digest_path(self, project_id: str, trip_id: str) -> Path:
        return self._track_directory(project_id) / f"{trip_id}.json.sha256"

    def _source_path(
        self,
        project_id: str,
        trip_id: str,
        source_sha256: str,
    ) -> Path:
        return (
            self._track_directory(project_id)
            / "originals"
            / f"{trip_id}-{source_sha256[:16]}.gpx"
        )

    def _read_record(self, project_id: str, trip_id: str) -> Dict[str, Any]:
        record_path = self._record_path(project_id, trip_id)
        digest_path = self._digest_path(project_id, trip_id)
        try:
            file_stat = record_path.stat()
            digest_text = digest_path.read_text(encoding="ascii").strip()
        except OSError as error:
            raise GpxTrackError(
                "track_not_found",
                "No GPX track is attached to this trip",
            ) from error
        if (
            not stat_module.S_ISREG(file_stat.st_mode)
            or file_stat.st_size < 2
            or file_stat.st_size > MAX_RECORD_BYTES
            or not _SHA256_PATTERN.fullmatch(digest_text)
        ):
            raise GpxTrackError(
                "track_record_invalid",
                "The saved GPX track record is invalid",
            )
        payload = record_path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest_text:
            raise GpxTrackError(
                "track_record_hash_mismatch",
                "The saved GPX track record failed its integrity check",
            )
        try:
            record = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GpxTrackError(
                "track_record_invalid",
                "The saved GPX track record is not valid JSON",
            ) from error
        if (
            not isinstance(record, dict)
            or record.get("schema_version") != GPX_TRACK_SCHEMA
            or record.get("project_id") != project_id
            or record.get("trip_id") != trip_id
            or not isinstance(record.get("route_points"), list)
            or len(record["route_points"]) < 2
        ):
            raise GpxTrackError(
                "track_record_invalid",
                "The saved GPX track record crossed its project or trip boundary",
            )
        return record

    def _response(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        points = record["route_points"]
        indices = _sample_indices(points, MAX_RESPONSE_POINTS)
        offset = _canonical_offset(record["video_offset_seconds"])
        sampled = []
        for display_sequence, index in enumerate(indices):
            point = dict(points[index])
            point["source_sequence"] = point.pop("sequence")
            point["sequence"] = display_sequence
            point["video_time_seconds"] = round(
                float(point["elapsed_seconds"]) + offset,
                3,
            )
            sampled.append(point)
        response = {
            key: value
            for key, value in record.items()
            if key not in {"route_points"}
        }
        response.update(
            {
                "route_points": sampled,
                "displayed_point_count": len(sampled),
                "sampled_for_display": len(sampled) < len(points),
            }
        )
        return response

    def import_track(
        self,
        project_id: UUID,
        trip_id: UUID,
        selected_path: str,
        video_offset_seconds: float = 0.0,
    ) -> Dict[str, Any]:
        project_text = _canonical_uuid(project_id, "project_id")
        trip_text = _canonical_uuid(trip_id, "trip_id")
        offset = _canonical_offset(video_offset_seconds)
        trip = self._resolve_trip(UUID(project_text), UUID(trip_text))
        if not isinstance(trip, Mapping):
            raise GpxTrackError(
                "trip_unavailable",
                "The selected trip could not be verified",
            )
        parsed = parse_gpx(Path(selected_path))
        source_path = self._source_path(
            project_text,
            trip_text,
            parsed.source_sha256,
        )
        track_id = str(
            uuid5(
                UUID(trip_text),
                f"trafficvisson-gpx:{parsed.source_sha256}",
            )
        )

        with self._lock:
            previous: Optional[Dict[str, Any]]
            try:
                previous = self._read_record(project_text, trip_text)
            except GpxTrackError as error:
                if error.code != "track_not_found":
                    raise
                previous = None
            revision = int(previous.get("revision", 0)) + 1 if previous else 1
            imported_at = _utc_now()
            record = {
                "schema_version": GPX_TRACK_SCHEMA,
                "track_id": track_id,
                "project_id": project_text,
                "trip_id": trip_text,
                "source_name": parsed.source_name,
                "source_size_bytes": parsed.source_size_bytes,
                "source_sha256": parsed.source_sha256,
                "point_count": len(parsed.route_points),
                "started_at": parsed.started_at.isoformat(),
                "ended_at": parsed.ended_at.isoformat(),
                "duration_seconds": round(
                    (
                        parsed.ended_at - parsed.started_at
                    ).total_seconds(),
                    3,
                ),
                "total_distance_meters": parsed.total_distance_meters,
                "video_offset_seconds": offset,
                "revision": revision,
                "replaces_track_id": (
                    previous.get("track_id") if previous else None
                ),
                "imported_at": imported_at.isoformat(),
                "updated_at": imported_at.isoformat(),
                "route_points": list(parsed.route_points),
            }
            if source_path.exists():
                existing_source = _read_regular_file(source_path)
                if (
                    len(existing_source) != parsed.source_size_bytes
                    or hashlib.sha256(existing_source).hexdigest()
                    != parsed.source_sha256
                ):
                    raise GpxTrackError(
                        "source_archive_hash_mismatch",
                        "The saved GPX original failed its integrity check",
                    )
            else:
                _write_atomic(source_path, parsed.source_payload)
            payload = _json_bytes(record)
            digest = hashlib.sha256(payload).hexdigest()
            _write_atomic(
                self._record_path(project_text, trip_text),
                payload,
            )
            _write_atomic(
                self._digest_path(project_text, trip_text),
                f"{digest}\n".encode("ascii"),
            )
            return self._response(record)

    def get_track(
        self,
        project_id: UUID,
        trip_id: UUID,
    ) -> Dict[str, Any]:
        project_text = _canonical_uuid(project_id, "project_id")
        trip_text = _canonical_uuid(trip_id, "trip_id")
        self._resolve_trip(UUID(project_text), UUID(trip_text))
        with self._lock:
            return self._response(
                self._read_record(project_text, trip_text)
            )

    def export_snapshot(
        self,
        project_id: UUID,
        trip_id: UUID,
    ) -> Dict[str, Any]:
        """Return every verified GPX point and path-free export provenance.

        Renderer responses are deliberately sampled to ``MAX_RESPONSE_POINTS``.
        Artifact export must instead preserve the complete normalized track, so
        this method uses the integrity-checked record directly after resolving
        the project/trip boundary again.
        """

        project_text = _canonical_uuid(project_id, "project_id")
        trip_text = _canonical_uuid(trip_id, "trip_id")
        trip = self._resolve_trip(UUID(project_text), UUID(trip_text))
        if not isinstance(trip, Mapping):
            raise GpxTrackError(
                "trip_unavailable",
                "The selected trip could not be verified",
            )
        with self._lock:
            record = self._read_record(project_text, trip_text)
            source_sha256_for_path = record.get("source_sha256")
            source_size_for_check = record.get("source_size_bytes")
            if (
                not isinstance(source_sha256_for_path, str)
                or not _SHA256_PATTERN.fullmatch(source_sha256_for_path)
                or isinstance(source_size_for_check, bool)
                or not isinstance(source_size_for_check, int)
                or source_size_for_check < 1
            ):
                raise GpxTrackError(
                    "track_record_invalid",
                    "The saved GPX track metadata is invalid",
                )
            try:
                original_payload = _read_regular_file(
                    self._source_path(
                        project_text,
                        trip_text,
                        source_sha256_for_path,
                    )
                )
            except (KeyError, GpxTrackError) as error:
                raise GpxTrackError(
                    "source_changed",
                    "The saved GPX original failed its integrity check",
                ) from error
            if (
                len(original_payload) != source_size_for_check
                or hashlib.sha256(original_payload).hexdigest()
                != source_sha256_for_path
            ):
                raise GpxTrackError(
                    "source_changed",
                    "The saved GPX original failed its integrity check",
                )

        try:
            track_id = _canonical_uuid(record["track_id"], "track_id")
            revision = record["revision"]
            point_count = record["point_count"]
            source_size_bytes = record["source_size_bytes"]
            source_sha256 = record["source_sha256"]
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
                or isinstance(point_count, bool)
                or not isinstance(point_count, int)
                or point_count != len(record["route_points"])
                or isinstance(source_size_bytes, bool)
                or not isinstance(source_size_bytes, int)
                or source_size_bytes < 1
                or not isinstance(source_sha256, str)
                or not _SHA256_PATTERN.fullmatch(source_sha256)
            ):
                raise ValueError
            video_duration_seconds = float(trip["duration_seconds"])
            if not math.isfinite(video_duration_seconds) or video_duration_seconds <= 0:
                raise ValueError
            video_offset_seconds = _canonical_offset(
                record["video_offset_seconds"]
            )
        except (
            GpxTrackError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            if isinstance(error, GpxTrackError):
                raise
            raise GpxTrackError(
                "track_record_invalid",
                "The saved GPX track metadata is invalid",
            ) from error

        route_points: List[Dict[str, Any]] = []
        points_before_video = 0
        points_after_video = 0
        gap_point_count = 0
        track_uuid = UUID(track_id)
        for expected_sequence, raw_point in enumerate(record["route_points"]):
            try:
                if not isinstance(raw_point, Mapping):
                    raise ValueError
                sequence = raw_point["sequence"]
                if (
                    isinstance(sequence, bool)
                    or not isinstance(sequence, int)
                    or sequence != expected_sequence
                ):
                    raise ValueError
                elapsed_seconds = float(raw_point["elapsed_seconds"])
                latitude = float(raw_point["latitude"])
                longitude = float(raw_point["longitude"])
                altitude = raw_point.get("altitude_meters")
                speed = raw_point.get("speed_kph")
                gap_from_previous = raw_point["gap_from_previous"]
                if (
                    not math.isfinite(elapsed_seconds)
                    or elapsed_seconds < 0
                    or not math.isfinite(latitude)
                    or not -90 <= latitude <= 90
                    or not math.isfinite(longitude)
                    or not -180 <= longitude <= 180
                    or not isinstance(gap_from_previous, bool)
                ):
                    raise ValueError
                if altitude is not None:
                    altitude = float(altitude)
                    if not math.isfinite(altitude):
                        raise ValueError
                if speed is not None:
                    speed = float(speed)
                    if not math.isfinite(speed) or not 0 <= speed <= 300:
                        raise ValueError
            except (KeyError, TypeError, ValueError) as error:
                raise GpxTrackError(
                    "track_record_invalid",
                    "The saved GPX route points are invalid",
                ) from error

            video_time_seconds = round(
                elapsed_seconds + video_offset_seconds,
                3,
            )
            if video_time_seconds < 0:
                points_before_video += 1
            elif video_time_seconds > video_duration_seconds:
                points_after_video += 1
            if gap_from_previous:
                gap_point_count += 1
            route_points.append(
                {
                    "route_point_id": str(
                        uuid5(track_uuid, f"route-point:{sequence}")
                    ),
                    "trip_id": trip_text,
                    "sequence": sequence,
                    "timestamp_ms": int(round(video_time_seconds * 1_000)),
                    "latitude": latitude,
                    "longitude": longitude,
                    "altitude_m": altitude,
                    "speed_kph": speed,
                    "heading_deg": None,
                    "accuracy_m": None,
                    "source": "gpx",
                }
            )

        record_sha256 = hashlib.sha256(_json_bytes(record)).hexdigest()
        return {
            "route_points": route_points,
            "provenance": {
                "schema_version": GPX_EXPORT_PROVENANCE_SCHEMA,
                "status": "attached",
                "project_id": project_text,
                "trip_id": trip_text,
                "track_id": track_id,
                "track_schema_version": record["schema_version"],
                "track_revision": revision,
                "track_record_sha256": record_sha256,
                "source_name": record["source_name"],
                "source_size_bytes": source_size_bytes,
                "source_sha256": source_sha256,
                "source_integrity_verified": True,
                "point_count": point_count,
                "exported_point_count": len(route_points),
                "sampled_for_export": False,
                "video_offset_seconds": video_offset_seconds,
                "video_duration_seconds": round(video_duration_seconds, 3),
                "points_before_video_count": points_before_video,
                "points_after_video_count": points_after_video,
                "gap_point_count": gap_point_count,
                "started_at": record["started_at"],
                "ended_at": record["ended_at"],
                "imported_at": record["imported_at"],
                "updated_at": record["updated_at"],
                "timestamp_basis": "gpx_elapsed_seconds_plus_video_offset",
            },
        }

    def update_video_offset(
        self,
        project_id: UUID,
        trip_id: UUID,
        video_offset_seconds: float,
    ) -> Dict[str, Any]:
        project_text = _canonical_uuid(project_id, "project_id")
        trip_text = _canonical_uuid(trip_id, "trip_id")
        offset = _canonical_offset(video_offset_seconds)
        self._resolve_trip(UUID(project_text), UUID(trip_text))
        with self._lock:
            record = self._read_record(project_text, trip_text)
            record["video_offset_seconds"] = offset
            record["revision"] = int(record.get("revision", 0)) + 1
            record["updated_at"] = _utc_now().isoformat()
            payload = _json_bytes(record)
            digest = hashlib.sha256(payload).hexdigest()
            _write_atomic(
                self._record_path(project_text, trip_text),
                payload,
            )
            _write_atomic(
                self._digest_path(project_text, trip_text),
                f"{digest}\n".encode("ascii"),
            )
            return self._response(record)
