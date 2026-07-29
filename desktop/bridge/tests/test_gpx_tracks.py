from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from app.gpx_tracks import (
    MAX_RESPONSE_POINTS,
    GpxTrackError,
    GpxTrackStore,
    parse_gpx,
)


PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
TRIP_ID = UUID("22222222-2222-4222-8222-222222222222")
OTHER_PROJECT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def _gpx(*, naive_time: bool = False, unsafe: bool = False) -> bytes:
    declaration = '<!DOCTYPE gpx [<!ENTITY xxe "unsafe">]>' if unsafe else ""
    timezone_suffix = "" if naive_time else "Z"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
{declaration}
<gpx version="1.1" creator="TrafficSuperVision test"
  xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>현장 점검</name><trkseg>
    <trkpt lat="37.500000" lon="127.000000">
      <ele>10.5</ele><time>2026-07-29T00:00:00{timezone_suffix}</time>
    </trkpt>
    <trkpt lat="37.500500" lon="127.000500">
      <ele>11.0</ele><time>2026-07-29T00:00:05{timezone_suffix}</time>
    </trkpt>
    <trkpt lat="37.501000" lon="127.001000">
      <ele>12.0</ele><time>2026-07-29T00:00:20{timezone_suffix}</time>
    </trkpt>
  </trkseg></trk>
</gpx>
""".encode("utf-8")


def test_parse_gpx_normalizes_timed_points_and_marks_real_gaps(
    tmp_path: Path,
) -> None:
    source = tmp_path / "route.gpx"
    source.write_bytes(_gpx())

    track = parse_gpx(source)

    assert track.source_name == "route.gpx"
    assert track.source_sha256 == hashlib.sha256(_gpx()).hexdigest()
    assert len(track.route_points) == 3
    assert track.route_points[0]["elapsed_seconds"] == 0.0
    assert track.route_points[1]["elapsed_seconds"] == 5.0
    assert track.route_points[2]["elapsed_seconds"] == 20.0
    assert track.route_points[1]["gap_from_previous"] is False
    assert track.route_points[2]["gap_from_previous"] is True
    assert track.route_points[1]["speed_kph"] > 0
    assert track.total_distance_meters > 0


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (_gpx(unsafe=True), "unsafe_xml"),
        (_gpx(naive_time=True), "timezone_missing"),
        (b"<html><body>not gpx</body></html>", "invalid_gpx_root"),
    ],
)
def test_parse_gpx_rejects_unsafe_or_unusable_xml(
    tmp_path: Path,
    payload: bytes,
    code: str,
) -> None:
    source = tmp_path / "route.gpx"
    source.write_bytes(payload)

    with pytest.raises(GpxTrackError) as raised:
        parse_gpx(source)

    assert raised.value.code == code


def test_track_store_is_project_scoped_durable_and_offset_versioned(
    tmp_path: Path,
) -> None:
    source = tmp_path / "route.gpx"
    source.write_bytes(_gpx())
    resolved = []

    def resolve_trip(project_id: UUID, trip_id: UUID):
        resolved.append((project_id, trip_id))
        if project_id != PROJECT_ID or trip_id != TRIP_ID:
            raise GpxTrackError("trip_unavailable", "trip is unavailable")
        return {
            "project_id": str(project_id),
            "trip_id": str(trip_id),
            "duration_seconds": 60.0,
        }

    store = GpxTrackStore(tmp_path / "data", resolve_trip)
    imported = store.import_track(
        PROJECT_ID,
        TRIP_ID,
        str(source),
        video_offset_seconds=2.5,
    )

    assert imported["project_id"] == str(PROJECT_ID)
    assert imported["trip_id"] == str(TRIP_ID)
    assert imported["point_count"] == 3
    assert imported["video_offset_seconds"] == 2.5
    assert imported["route_points"][0]["video_time_seconds"] == 2.5
    assert imported["revision"] == 1
    assert resolved[-1] == (PROJECT_ID, TRIP_ID)

    restarted = GpxTrackStore(tmp_path / "data", resolve_trip)
    restored = restarted.get_track(PROJECT_ID, TRIP_ID)
    assert restored["track_id"] == imported["track_id"]
    assert restored["source_sha256"] == imported["source_sha256"]

    shifted = restarted.update_video_offset(PROJECT_ID, TRIP_ID, -1.25)
    assert shifted["revision"] == 2
    assert shifted["video_offset_seconds"] == -1.25
    assert shifted["route_points"][1]["video_time_seconds"] == 3.75

    exported = restarted.export_snapshot(PROJECT_ID, TRIP_ID)
    assert len(exported["route_points"]) == 3
    assert exported["route_points"][0] == {
        "route_point_id": exported["route_points"][0]["route_point_id"],
        "trip_id": str(TRIP_ID),
        "sequence": 0,
        "timestamp_ms": -1250,
        "latitude": 37.5,
        "longitude": 127.0,
        "altitude_m": 10.5,
        "speed_kph": None,
        "heading_deg": None,
        "accuracy_m": None,
        "source": "gpx",
    }
    assert UUID(exported["route_points"][0]["route_point_id"])
    assert len(
        {point["route_point_id"] for point in exported["route_points"]}
    ) == 3
    provenance = exported["provenance"]
    assert provenance["status"] == "attached"
    assert provenance["project_id"] == str(PROJECT_ID)
    assert provenance["trip_id"] == str(TRIP_ID)
    assert provenance["track_id"] == imported["track_id"]
    assert provenance["track_revision"] == 2
    assert provenance["source_sha256"] == imported["source_sha256"]
    assert provenance["video_offset_seconds"] == -1.25
    assert provenance["point_count"] == 3
    assert provenance["exported_point_count"] == 3
    assert provenance["sampled_for_export"] is False
    assert provenance["points_before_video_count"] == 1
    assert provenance["points_after_video_count"] == 0
    assert provenance["gap_point_count"] == 1
    record_digest = (
        tmp_path
        / "data"
        / "projects"
        / str(PROJECT_ID)
        / "gps-tracks"
        / f"{TRIP_ID}.json.sha256"
    ).read_text(encoding="ascii").strip()
    assert provenance["track_record_sha256"] == record_digest
    assert provenance["source_integrity_verified"] is True
    assert str(tmp_path) not in json.dumps(exported, ensure_ascii=False)

    with pytest.raises(GpxTrackError, match="unavailable"):
        restarted.get_track(OTHER_PROJECT_ID, TRIP_ID)


def test_track_store_detects_record_tampering(tmp_path: Path) -> None:
    source = tmp_path / "route.gpx"
    source.write_bytes(_gpx())

    def resolve_trip(project_id: UUID, trip_id: UUID):
        return {"project_id": str(project_id), "trip_id": str(trip_id)}

    store = GpxTrackStore(tmp_path / "data", resolve_trip)
    store.import_track(PROJECT_ID, TRIP_ID, str(source))
    record_path = (
        tmp_path
        / "data"
        / "projects"
        / str(PROJECT_ID)
        / "gps-tracks"
        / f"{TRIP_ID}.json"
    )
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["video_offset_seconds"] = 999.0
    record_path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(GpxTrackError) as raised:
        store.get_track(PROJECT_ID, TRIP_ID)

    assert raised.value.code == "track_record_hash_mismatch"


def test_export_snapshot_detects_saved_original_tampering(
    tmp_path: Path,
) -> None:
    source = tmp_path / "route.gpx"
    source.write_bytes(_gpx())

    def resolve_trip(project_id: UUID, trip_id: UUID):
        return {
            "project_id": str(project_id),
            "trip_id": str(trip_id),
            "duration_seconds": 60.0,
        }

    store = GpxTrackStore(tmp_path / "data", resolve_trip)
    imported = store.import_track(PROJECT_ID, TRIP_ID, str(source))
    saved_original = (
        tmp_path
        / "data"
        / "projects"
        / str(PROJECT_ID)
        / "gps-tracks"
        / "originals"
        / f"{TRIP_ID}-{imported['source_sha256'][:16]}.gpx"
    )
    saved_original.write_bytes(b"<gpx>tampered</gpx>")

    with pytest.raises(GpxTrackError) as raised:
        store.export_snapshot(PROJECT_ID, TRIP_ID)

    assert raised.value.code == "source_changed"


def test_export_snapshot_keeps_every_point_beyond_renderer_sample_limit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "dense-route.gpx"
    base_time = datetime(2026, 7, 29, tzinfo=timezone.utc)
    point_count = MAX_RESPONSE_POINTS + 1
    points = []
    for index in range(point_count):
        timestamp = (
            base_time + timedelta(seconds=index)
        ).isoformat().replace("+00:00", "Z")
        points.append(
            (
                f'<trkpt lat="{37.5 + index * 0.000001:.6f}" '
                f'lon="{127.0 + index * 0.000001:.6f}">'
                f"<time>{timestamp}</time></trkpt>"
            )
        )
    source.write_text(
        (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<gpx version="1.1" creator="TrafficSuperVision test" '
            'xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
            + "".join(points)
            + "</trkseg></trk></gpx>"
        ),
        encoding="utf-8",
    )

    def resolve_trip(project_id: UUID, trip_id: UUID):
        return {
            "project_id": str(project_id),
            "trip_id": str(trip_id),
            "duration_seconds": float(point_count),
        }

    store = GpxTrackStore(tmp_path / "data", resolve_trip)
    store.import_track(PROJECT_ID, TRIP_ID, str(source))

    renderer_track = store.get_track(PROJECT_ID, TRIP_ID)
    assert renderer_track["sampled_for_display"] is True
    assert len(renderer_track["route_points"]) == MAX_RESPONSE_POINTS

    exported = store.export_snapshot(PROJECT_ID, TRIP_ID)
    assert len(exported["route_points"]) == point_count
    assert exported["route_points"][-1]["sequence"] == point_count - 1
    assert exported["provenance"]["point_count"] == point_count
    assert exported["provenance"]["exported_point_count"] == point_count
    assert exported["provenance"]["sampled_for_export"] is False


def test_track_store_rejects_tampered_archived_original(
    tmp_path: Path,
) -> None:
    source = tmp_path / "route.gpx"
    source.write_bytes(_gpx())

    def resolve_trip(project_id: UUID, trip_id: UUID):
        return {"project_id": str(project_id), "trip_id": str(trip_id)}

    store = GpxTrackStore(tmp_path / "data", resolve_trip)
    store.import_track(PROJECT_ID, TRIP_ID, str(source))
    originals = (
        tmp_path
        / "data"
        / "projects"
        / str(PROJECT_ID)
        / "gps-tracks"
        / "originals"
    )
    archived = next(originals.glob("*.gpx"))
    archived.write_bytes(b"tampered")

    with pytest.raises(GpxTrackError) as raised:
        store.import_track(PROJECT_ID, TRIP_ID, str(source))

    assert raised.value.code == "source_archive_hash_mismatch"
