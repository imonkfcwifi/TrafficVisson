from __future__ import annotations

from uuid import UUID


PROJECT_ID = "11111111-1111-4111-8111-111111111111"
TRIP_ID = "22222222-2222-4222-8222-222222222222"


def _track(offset: float = 0.0):
    return {
        "schema_version": "trafficvisson.gpx-track.v1",
        "track_id": "33333333-3333-4333-8333-333333333333",
        "project_id": PROJECT_ID,
        "trip_id": TRIP_ID,
        "source_name": "route.gpx",
        "source_size_bytes": 1024,
        "source_sha256": "a" * 64,
        "point_count": 2,
        "displayed_point_count": 2,
        "sampled_for_display": False,
        "started_at": "2026-07-29T00:00:00+00:00",
        "ended_at": "2026-07-29T00:00:05+00:00",
        "duration_seconds": 5.0,
        "total_distance_meters": 50.0,
        "video_offset_seconds": offset,
        "revision": 1,
        "replaces_track_id": None,
        "imported_at": "2026-07-29T00:01:00+00:00",
        "updated_at": "2026-07-29T00:01:00+00:00",
        "route_points": [
            {
                "sequence": 0,
                "source_sequence": 0,
                "recorded_at": "2026-07-29T00:00:00+00:00",
                "elapsed_seconds": 0.0,
                "video_time_seconds": offset,
                "latitude": 37.5,
                "longitude": 127.0,
                "altitude_meters": 10.0,
                "speed_kph": None,
                "distance_from_previous_meters": 0.0,
                "gap_from_previous": False,
            },
            {
                "sequence": 1,
                "source_sequence": 1,
                "recorded_at": "2026-07-29T00:00:05+00:00",
                "elapsed_seconds": 5.0,
                "video_time_seconds": 5.0 + offset,
                "latitude": 37.5005,
                "longitude": 127.0005,
                "altitude_meters": 11.0,
                "speed_kph": 36.0,
                "distance_from_previous_meters": 50.0,
                "gap_from_previous": False,
            },
        ],
    }


class _FakeGpxStore:
    def __init__(self):
        self.calls = []

    def import_track(
        self,
        project_id: UUID,
        trip_id: UUID,
        selected_path: str,
        video_offset_seconds: float,
    ):
        self.calls.append(
            ("import", project_id, trip_id, selected_path, video_offset_seconds)
        )
        return _track(video_offset_seconds)

    def get_track(self, project_id: UUID, trip_id: UUID):
        self.calls.append(("get", project_id, trip_id))
        return _track()

    def update_video_offset(
        self,
        project_id: UUID,
        trip_id: UUID,
        video_offset_seconds: float,
    ):
        self.calls.append(
            ("offset", project_id, trip_id, video_offset_seconds)
        )
        return _track(video_offset_seconds)


def test_gpx_routes_are_authenticated_project_scoped_and_schema_validated(
    client,
    auth,
):
    fake = _FakeGpxStore()
    client.app.state.gpx_tracks = fake
    base = f"/projects/{PROJECT_ID}/trips/{TRIP_ID}/gpx"

    assert client.get(base).status_code == 401
    imported = client.post(
        base,
        headers=auth,
        json={
            "selected_path": "C:\\selected\\route.gpx",
            "video_offset_seconds": -2.5,
        },
    )
    assert imported.status_code == 201
    assert imported.json()["video_offset_seconds"] == -2.5
    assert imported.json()["route_points"][0]["video_time_seconds"] == -2.5

    restored = client.get(base, headers=auth)
    assert restored.status_code == 200
    assert restored.json()["project_id"] == PROJECT_ID
    assert restored.json()["trip_id"] == TRIP_ID

    shifted = client.post(
        f"{base}/offset",
        headers=auth,
        json={"video_offset_seconds": 3.25},
    )
    assert shifted.status_code == 200
    assert shifted.json()["route_points"][1]["video_time_seconds"] == 8.25
    assert [call[0] for call in fake.calls] == ["import", "get", "offset"]


def test_gpx_routes_reject_unbounded_offsets_before_store_access(client, auth):
    fake = _FakeGpxStore()
    client.app.state.gpx_tracks = fake
    base = f"/projects/{PROJECT_ID}/trips/{TRIP_ID}/gpx"

    response = client.post(
        base,
        headers=auth,
        json={
            "selected_path": "C:\\selected\\route.gpx",
            "video_offset_seconds": 100_000,
        },
    )

    assert response.status_code == 422
    assert fake.calls == []
