import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.media_intake import (
    MediaIntakeService,
    is_allowlisted_video_name,
    validate_explicit_directory_syntax,
)
from app.store import DEMO_PROJECT_ID
from conftest import TEST_TOKEN


def _capture(client, auth, directory):
    response = client.post(
        f"/projects/{DEMO_PROJECT_ID}/media/snapshots",
        headers=auth,
        json={
            "project_id": str(DEMO_PROJECT_ID),
            "directory": str(directory),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _confirm_stable(client, auth, directory):
    snapshots = [_capture(client, auth, directory) for _ in range(3)]
    response = client.post(
        f"/projects/{DEMO_PROJECT_ID}/media/stability",
        headers=auth,
        json={
            "project_id": str(DEMO_PROJECT_ID),
            "snapshot_ids": [item["snapshot_id"] for item in snapshots],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["stable"] is True
    return response.json()["stable_snapshot_id"]


def _probe_client(tmp_path):
    fake_ffprobe = tmp_path / "ffprobe.exe"
    fake_ffprobe.write_bytes(b"test fixture; subprocess.run is monkeypatched")
    return TestClient(
        create_app(
            TEST_TOKEN,
            fixture_mode=True,
            allow_test_client=True,
            ffprobe_path=str(fake_ffprobe),
        )
    )


def _valid_probe_output(size):
    return {
        "format": {
            "duration": "12.5",
            "size": str(size),
            "format_name": "mov,mp4",
        },
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "30000/1001",
            },
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
            },
        ],
    }


def _wait_for_hash_job(client, auth, job_id, timeout_seconds=3):
    deadline = time.monotonic() + timeout_seconds
    endpoint = (
        f"/projects/{DEMO_PROJECT_ID}/media/hash-jobs/{job_id}"
    )
    last_response = None
    while time.monotonic() < deadline:
        last_response = client.get(endpoint, headers=auth)
        assert last_response.status_code == 200, last_response.text
        if last_response.json()["status"] in {
            "completed",
            "cancelled",
            "failed",
        }:
            return last_response.json()
        time.sleep(0.01)
    raise AssertionError(
        "hash job did not reach a terminal state: "
        + (last_response.text if last_response is not None else "no response")
    )


def test_snapshot_is_authenticated_allowlisted_repeatable_and_path_private(
    client, auth, tmp_path
):
    media_directory = tmp_path / "현장 영상"
    media_directory.mkdir()
    (media_directory / "A.MP4").write_bytes(b"video-a")
    (media_directory / "b.mov").write_bytes(b"video-b")
    (media_directory / "notes.txt").write_text("not media", encoding="utf-8")
    (media_directory / "installer.exe").write_bytes(b"MZ")

    denied = client.post(
        f"/projects/{DEMO_PROJECT_ID}/media/snapshots",
        json={
            "project_id": str(DEMO_PROJECT_ID),
            "directory": str(media_directory),
        },
    )
    assert denied.status_code == 401

    first = _capture(client, auth, media_directory)
    second = _capture(client, auth, media_directory)

    assert first["snapshot_digest"] == second["snapshot_digest"]
    assert first["digest_basis"] == "name_size_creation_mtime_sha256_v1"
    assert first["content_hash_status"] == "not_computed"
    assert first["file_count"] == 2
    assert first["total_size_bytes"] == 14
    assert [item["name"] for item in first["files"]] == ["A.MP4", "b.mov"]
    assert [item["extension"] for item in first["files"]] == [".mp4", ".mov"]
    assert all(item["created_time_ns"] > 0 for item in first["files"])
    assert all(item["modified_time_ns"] > 0 for item in first["files"])
    assert str(media_directory) not in json.dumps(first, ensure_ascii=False)
    assert "directory" not in first
    assert "content_hash" not in first


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("drive.MP4", True),
        ("dashcam.m2ts", True),
        ("clip & whoami.mp4", True),
        ("report.txt", False),
        ("payload.exe", False),
        ("../escape.mp4", False),
        (r"folder\escape.mp4", False),
    ],
)
def test_video_extension_and_plain_filename_allowlist(name, expected):
    assert is_allowlisted_video_name(name) is expected


def test_directory_syntax_accepts_unc_and_rejects_relative_device_and_traversal():
    unc = r"\\192.168.0.234\도로연구실\점검 영상"
    assert validate_explicit_directory_syntax(unc) == unc

    for invalid in (
        "relative\\folder",
        "C:relative\\folder",
        r"\\.\pipe\danger",
        r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1",
        r"C:\safe\..\escape",
        r"C:\safe\*.mp4",
    ):
        with pytest.raises(ValueError):
            validate_explicit_directory_syntax(invalid)


def test_snapshot_rejects_a_file_path_and_ignores_unsupported_files(
    client, auth, tmp_path
):
    media_file = tmp_path / "video.mp4"
    media_file.write_bytes(b"video")
    response = client.post(
        f"/projects/{DEMO_PROJECT_ID}/media/snapshots",
        headers=auth,
        json={
            "project_id": str(DEMO_PROJECT_ID),
            "directory": str(media_file),
        },
    )
    assert response.status_code == 422
    assert str(media_file) not in response.text

    empty_directory = tmp_path / "unsupported only"
    empty_directory.mkdir()
    (empty_directory / "run.cmd").write_text("calc", encoding="utf-8")
    snapshot = _capture(client, auth, empty_directory)
    assert snapshot["file_count"] == 0
    assert snapshot["files"] == []


def test_local_file_registration_is_stable_path_private_and_project_scoped(
    client, auth, tmp_path
):
    media_directory = tmp_path / "native picker source"
    media_directory.mkdir()
    media = media_directory / "selected.mov"
    media.write_bytes(b"real local selection")
    endpoint = f"/projects/{DEMO_PROJECT_ID}/media/local-file"
    payload = {
        "project_id": str(DEMO_PROJECT_ID),
        "selected_path": str(media),
    }

    assert client.post(endpoint, json=payload).status_code == 401
    response = client.post(endpoint, headers=auth, json=payload)
    assert response.status_code == 201, response.text
    snapshot = response.json()
    assert snapshot["file_count"] == 1
    assert snapshot["files"][0]["name"] == media.name
    assert snapshot["total_size_bytes"] == media.stat().st_size
    assert str(media_directory) not in response.text
    assert "selected_path" not in response.text

    # Native selections are immediately eligible for probe/hash, while every
    # later filesystem access still checks the captured metadata.
    started = client.post(
        f"/projects/{DEMO_PROJECT_ID}/media/hash-jobs",
        headers=auth,
        json={
            "project_id": str(DEMO_PROJECT_ID),
            "snapshot_id": snapshot["snapshot_id"],
            "file_name": media.name,
        },
    )
    assert started.status_code == 202, started.text
    completed = _wait_for_hash_job(client, auth, started.json()["job_id"])
    assert completed["status"] == "completed"
    assert completed["sha256_lower"] == hashlib.sha256(
        media.read_bytes()
    ).hexdigest()

    mismatched = client.post(
        endpoint,
        headers=auth,
        json={**payload, "project_id": str(uuid4())},
    )
    assert mismatched.status_code == 409
    assert str(media_directory) not in mismatched.text


def test_local_file_registration_rejects_non_video_and_changed_file(
    client, auth, tmp_path
):
    unsupported = tmp_path / "payload.exe"
    unsupported.write_bytes(b"MZ")
    endpoint = f"/projects/{DEMO_PROJECT_ID}/media/local-file"
    rejected = client.post(
        endpoint,
        headers=auth,
        json={
            "project_id": str(DEMO_PROJECT_ID),
            "selected_path": str(unsupported),
        },
    )
    assert rejected.status_code == 422
    assert str(tmp_path) not in rejected.text

    media = tmp_path / "mutable.mp4"
    media.write_bytes(b"first")
    registered = client.post(
        endpoint,
        headers=auth,
        json={
            "project_id": str(DEMO_PROJECT_ID),
            "selected_path": str(media),
        },
    )
    assert registered.status_code == 201
    media.write_bytes(b"upload resumed after selection")
    changed = client.post(
        f"/projects/{DEMO_PROJECT_ID}/media/hash-jobs",
        headers=auth,
        json={
            "project_id": str(DEMO_PROJECT_ID),
            "snapshot_id": registered.json()["snapshot_id"],
            "file_name": media.name,
        },
    )
    assert changed.status_code == 409
    assert str(tmp_path) not in changed.text


def test_three_snapshot_stability_requires_both_intervals_unchanged(
    client, auth, tmp_path
):
    directory = tmp_path / "upload"
    directory.mkdir()
    media = directory / "trip.mp4"
    media.write_bytes(b"a")

    first = _capture(client, auth, directory)
    media.write_bytes(b"expanded upload")
    second = _capture(client, auth, directory)
    third = _capture(client, auth, directory)

    response = client.post(
        f"/projects/{DEMO_PROJECT_ID}/media/stability",
        headers=auth,
        json={
            "project_id": str(DEMO_PROJECT_ID),
            "snapshot_ids": [
                first["snapshot_id"],
                second["snapshot_id"],
                third["snapshot_id"],
            ],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["observed_interval_seconds"][0] >= 0
    assert payload["observed_interval_seconds"][1] >= 0
    assert {
        key: value
        for key, value in payload.items()
        if key
        not in {"minimum_interval_seconds", "observed_interval_seconds"}
    } == {
        "snapshot_ids": [
            first["snapshot_id"],
            second["snapshot_id"],
            third["snapshot_id"],
        ],
        "first_interval_unchanged": False,
        "second_interval_unchanged": True,
        "stable": False,
        "reason": "first_interval_changed",
        "file_count": 1,
        "total_size_bytes": len(b"expanded upload"),
        "stable_snapshot_id": None,
    }

    stable_snapshot_id = _confirm_stable(client, auth, directory)
    assert stable_snapshot_id


def test_production_minimum_interval_prevents_immediate_stability(tmp_path):
    directory = tmp_path / "minimum interval"
    directory.mkdir()
    (directory / "trip.mp4").write_bytes(b"video")
    project_id = uuid4()
    service = MediaIntakeService(minimum_interval_seconds=60)
    snapshots = [
        service.capture_snapshot(project_id, str(directory)) for _ in range(3)
    ]
    comparison = service.compare_snapshots(
        project_id, [item["snapshot_id"] for item in snapshots]
    )
    assert comparison["first_interval_unchanged"] is True
    assert comparison["second_interval_unchanged"] is True
    assert comparison["stable"] is False
    assert comparison["reason"] == "interval_too_short"
    assert comparison["minimum_interval_seconds"] == 60


def test_unchanged_empty_directory_is_not_promoted_to_stable(
    client, auth, tmp_path
):
    directory = tmp_path / "empty"
    directory.mkdir()
    snapshots = [_capture(client, auth, directory) for _ in range(3)]
    response = client.post(
        f"/projects/{DEMO_PROJECT_ID}/media/stability",
        headers=auth,
        json={
            "project_id": str(DEMO_PROJECT_ID),
            "snapshot_ids": [item["snapshot_id"] for item in snapshots],
        },
    )
    assert response.status_code == 200
    assert response.json()["first_interval_unchanged"] is True
    assert response.json()["second_interval_unchanged"] is True
    assert response.json()["stable"] is False
    assert response.json()["reason"] == "no_supported_media"


def test_stability_rejects_duplicate_out_of_order_and_cross_directory_snapshots(
    client, auth, tmp_path
):
    first_directory = tmp_path / "first"
    second_directory = tmp_path / "second"
    first_directory.mkdir()
    second_directory.mkdir()
    (first_directory / "one.mp4").write_bytes(b"one")
    (second_directory / "two.mp4").write_bytes(b"two")

    first = _capture(client, auth, first_directory)
    second = _capture(client, auth, first_directory)
    third = _capture(client, auth, first_directory)
    other = _capture(client, auth, second_directory)
    endpoint = f"/projects/{DEMO_PROJECT_ID}/media/stability"

    duplicate = client.post(
        endpoint,
        headers=auth,
        json={
            "project_id": str(DEMO_PROJECT_ID),
            "snapshot_ids": [
                first["snapshot_id"],
                second["snapshot_id"],
                second["snapshot_id"],
            ],
        },
    )
    assert duplicate.status_code == 422

    out_of_order = client.post(
        endpoint,
        headers=auth,
        json={
            "project_id": str(DEMO_PROJECT_ID),
            "snapshot_ids": [
                third["snapshot_id"],
                second["snapshot_id"],
                first["snapshot_id"],
            ],
        },
    )
    assert out_of_order.status_code == 409

    cross_directory = client.post(
        endpoint,
        headers=auth,
        json={
            "project_id": str(DEMO_PROJECT_ID),
            "snapshot_ids": [
                first["snapshot_id"],
                second["snapshot_id"],
                other["snapshot_id"],
            ],
        },
    )
    assert cross_directory.status_code == 409
    assert str(first_directory) not in cross_directory.text
    assert str(second_directory) not in cross_directory.text


def test_media_snapshots_do_not_cross_project_boundaries(
    client, auth, tmp_path
):
    directory = tmp_path / "project isolation"
    directory.mkdir()
    (directory / "trip.mp4").write_bytes(b"video")
    snapshots = [_capture(client, auth, directory) for _ in range(3)]

    created = client.post(
        "/projects",
        headers=auth,
        json={"name": "격리 프로젝트"},
    )
    assert created.status_code == 201
    other_project = created.json()["id"]
    response = client.post(
        f"/projects/{other_project}/media/stability",
        headers=auth,
        json={
            "project_id": other_project,
            "snapshot_ids": [item["snapshot_id"] for item in snapshots],
        },
    )
    assert response.status_code == 404
    assert "not found in this project" in response.json()["detail"]


def test_ffprobe_uses_one_argv_item_for_injection_like_filename_and_no_shell(
    auth, tmp_path, monkeypatch
):
    directory = tmp_path / "share & whoami"
    directory.mkdir()
    media_name = "clip & whoami.mp4"
    media = directory / media_name
    media.write_bytes(b"real bytes")

    with _probe_client(tmp_path) as client:
        snapshot_id = _confirm_stable(client, auth, directory)
        observed = {}

        def fake_run(argv, **kwargs):
            observed["argv"] = argv
            observed["kwargs"] = kwargs
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(_valid_probe_output(media.stat().st_size)),
                stderr="",
            )

        monkeypatch.setattr("app.media_intake.subprocess.run", fake_run)
        response = client.post(
            f"/projects/{DEMO_PROJECT_ID}/media/probe",
            headers=auth,
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "snapshot_id": snapshot_id,
                "file_name": media_name,
                "timeout_seconds": 5,
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["ffprobe_status"] == "validated"
    assert response.json()["file_name"] == media_name
    assert response.json()["duration_seconds"] == 12.5
    assert response.json()["content_hash_status"] == "not_computed"
    assert isinstance(observed["argv"], list)
    assert observed["argv"][-2] == "-i"
    assert Path(observed["argv"][-1]).name == media_name
    assert observed["kwargs"]["shell"] is False
    assert observed["kwargs"]["timeout"] == 5
    assert not any(
        item.casefold() in {"cmd", "cmd.exe", "powershell", "powershell.exe"}
        for item in observed["argv"]
    )


def test_probe_rejects_path_traversal_unsupported_extension_and_unstable_snapshot(
    client, auth, tmp_path
):
    directory = tmp_path / "probe guards"
    directory.mkdir()
    (directory / "trip.mp4").write_bytes(b"video")
    snapshot = _capture(client, auth, directory)
    endpoint = f"/projects/{DEMO_PROJECT_ID}/media/probe"

    for file_name in ("../trip.mp4", r"..\trip.mp4", "payload.exe"):
        response = client.post(
            endpoint,
            headers=auth,
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "snapshot_id": snapshot["snapshot_id"],
                "file_name": file_name,
            },
        )
        assert response.status_code == 422

    unstable = client.post(
        endpoint,
        headers=auth,
        json={
            "project_id": str(DEMO_PROJECT_ID),
            "snapshot_id": snapshot["snapshot_id"],
            "file_name": "trip.mp4",
        },
    )
    assert unstable.status_code == 409


def test_ffprobe_timeout_is_bounded_and_does_not_leak_paths(
    auth, tmp_path, monkeypatch
):
    directory = tmp_path / "timeout"
    directory.mkdir()
    media = directory / "trip.mp4"
    media.write_bytes(b"video")

    with _probe_client(tmp_path) as client:
        snapshot_id = _confirm_stable(client, auth, directory)

        def timeout(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        monkeypatch.setattr("app.media_intake.subprocess.run", timeout)
        response = client.post(
            f"/projects/{DEMO_PROJECT_ID}/media/probe",
            headers=auth,
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "snapshot_id": snapshot_id,
                "file_name": media.name,
                "timeout_seconds": 1,
            },
        )

    assert response.status_code == 504
    assert "timed out" in response.json()["detail"]
    assert str(directory) not in response.text


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(
            {"streams": [{"index": 0, "codec_type": "audio", "codec_name": "aac"}]}
        ),
        lambda payload: payload["format"].update({"duration": "nan"}),
        lambda payload: payload["format"].update({"size": "999999"}),
        lambda payload: payload["streams"][0].update({"width": 0}),
    ],
)
def test_ffprobe_rejects_invalid_stream_duration_size_or_dimensions(
    auth, tmp_path, monkeypatch, mutate
):
    directory = tmp_path / "invalid metadata"
    directory.mkdir()
    media = directory / "trip.mp4"
    media.write_bytes(b"video bytes")

    with _probe_client(tmp_path) as client:
        snapshot_id = _confirm_stable(client, auth, directory)
        payload = _valid_probe_output(media.stat().st_size)
        mutate(payload)

        monkeypatch.setattr(
            "app.media_intake.subprocess.run",
            lambda argv, **kwargs: subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps(payload), stderr=""
            ),
        )
        response = client.post(
            f"/projects/{DEMO_PROJECT_ID}/media/probe",
            headers=auth,
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "snapshot_id": snapshot_id,
                "file_name": media.name,
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "ffprobe returned invalid media metadata"


def test_probe_refuses_media_that_changed_after_stability(
    auth, tmp_path, monkeypatch
):
    directory = tmp_path / "changed"
    directory.mkdir()
    media = directory / "trip.mp4"
    media.write_bytes(b"before")

    with _probe_client(tmp_path) as client:
        snapshot_id = _confirm_stable(client, auth, directory)
        media.write_bytes(b"after upload continued")

        def must_not_run(*args, **kwargs):
            raise AssertionError("ffprobe must not run for changed media")

        monkeypatch.setattr("app.media_intake.subprocess.run", must_not_run)
        response = client.post(
            f"/projects/{DEMO_PROJECT_ID}/media/probe",
            headers=auth,
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "snapshot_id": snapshot_id,
                "file_name": media.name,
            },
        )

    assert response.status_code == 409
    assert "changed" in response.json()["detail"]


def test_hash_job_requires_stable_snapshot_and_rejects_nested_or_script_name(
    client, auth, tmp_path
):
    directory = tmp_path / "hash guards"
    directory.mkdir()
    (directory / "trip.mp4").write_bytes(b"video")
    snapshot = _capture(client, auth, directory)
    endpoint = f"/projects/{DEMO_PROJECT_ID}/media/hash-jobs"

    unstable = client.post(
        endpoint,
        headers=auth,
        json={
            "project_id": str(DEMO_PROJECT_ID),
            "snapshot_id": snapshot["snapshot_id"],
            "file_name": "trip.mp4",
        },
    )
    assert unstable.status_code == 409

    for file_name in ("../trip.mp4", r"..\trip.mp4", "run.cmd"):
        rejected = client.post(
            endpoint,
            headers=auth,
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "snapshot_id": snapshot["snapshot_id"],
                "file_name": file_name,
            },
        )
        assert rejected.status_code == 422


def test_explicit_hash_job_streams_chunks_and_returns_both_hex_forms(
    client, auth, tmp_path, monkeypatch
):
    directory = tmp_path / "hash & whoami"
    directory.mkdir()
    media_name = "trip & whoami.mp4"
    payload = (b"TrafficSuperVision-real-media-" * 200_000) + b"tail"
    media = directory / media_name
    media.write_bytes(payload)
    stable_snapshot_id = _confirm_stable(client, auth, directory)

    progress_updates = []
    service = client.app.state.media_intake
    original_progress = service._update_hash_progress

    def record_progress(job_id, bytes_hashed):
        progress_updates.append(bytes_hashed)
        original_progress(job_id, bytes_hashed)

    monkeypatch.setattr(service, "_update_hash_progress", record_progress)
    started = client.post(
        f"/projects/{DEMO_PROJECT_ID}/media/hash-jobs",
        headers=auth,
        json={
            "project_id": str(DEMO_PROJECT_ID),
            "snapshot_id": stable_snapshot_id,
            "file_name": media_name,
        },
    )
    assert started.status_code == 202, started.text
    assert started.json()["status"] == "queued"
    assert started.json()["bytes_hashed"] == 0
    assert started.json()["sha256_lower"] is None
    assert started.json()["sha256_upper"] is None

    completed = _wait_for_hash_job(
        client, auth, started.json()["job_id"]
    )
    expected = hashlib.sha256(payload).hexdigest()
    assert completed["status"] == "completed"
    assert completed["content_hash_status"] == "computed"
    assert completed["hash_algorithm"] == "sha256"
    assert completed["chunk_size_bytes"] == 4_194_304
    assert completed["bytes_hashed"] == len(payload)
    assert completed["total_bytes"] == len(payload)
    assert completed["sha256_lower"] == expected
    assert completed["sha256_upper"] == expected.upper()
    assert completed["error_code"] is None
    assert len(progress_updates) >= 2
    assert progress_updates == sorted(progress_updates)
    assert progress_updates[-1] == len(payload)
    assert str(directory) not in json.dumps(completed, ensure_ascii=False)


def test_hash_start_rejects_file_changed_after_stable_snapshot(
    client, auth, tmp_path
):
    directory = tmp_path / "changed before hash"
    directory.mkdir()
    media = directory / "trip.mp4"
    media.write_bytes(b"before")
    stable_snapshot_id = _confirm_stable(client, auth, directory)
    media.write_bytes(b"after upload resumed")

    response = client.post(
        f"/projects/{DEMO_PROJECT_ID}/media/hash-jobs",
        headers=auth,
        json={
            "project_id": str(DEMO_PROJECT_ID),
            "snapshot_id": stable_snapshot_id,
            "file_name": media.name,
        },
    )
    assert response.status_code == 409
    assert str(directory) not in response.text


@pytest.mark.parametrize("guard", ["symlink", "reparse"])
def test_hash_start_rejects_symlink_or_reparse_candidate(
    client, auth, tmp_path, monkeypatch, guard
):
    directory = tmp_path / f"{guard} guard"
    directory.mkdir()
    media = directory / "trip.mp4"
    media.write_bytes(b"video")
    stable_snapshot_id = _confirm_stable(client, auth, directory)

    if guard == "symlink":
        original_is_symlink = Path.is_symlink

        def selected_file_is_symlink(path):
            if path == media:
                return True
            return original_is_symlink(path)

        monkeypatch.setattr(Path, "is_symlink", selected_file_is_symlink)
    else:
        monkeypatch.setattr(
            "app.media_intake._is_reparse_point", lambda _stat: True
        )

    response = client.post(
        f"/projects/{DEMO_PROJECT_ID}/media/hash-jobs",
        headers=auth,
        json={
            "project_id": str(DEMO_PROJECT_ID),
            "snapshot_id": stable_snapshot_id,
            "file_name": media.name,
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "The selected media file changed after the stable snapshot"
    )


def test_hash_job_fails_closed_when_file_metadata_changes_midstream(
    client, auth, tmp_path, monkeypatch
):
    directory = tmp_path / "changed during hash"
    directory.mkdir()
    media = directory / "trip.mp4"
    media.write_bytes(b"x" * (4_194_304 + 1_024))
    stable_snapshot_id = _confirm_stable(client, auth, directory)

    service = client.app.state.media_intake
    original_progress = service._update_hash_progress
    changed = threading.Event()

    def change_mtime_after_first_chunk(job_id, bytes_hashed):
        original_progress(job_id, bytes_hashed)
        if not changed.is_set():
            changed.set()
            stat_result = media.stat()
            os.utime(
                media,
                ns=(
                    stat_result.st_atime_ns,
                    stat_result.st_mtime_ns + 1_000_000_000,
                ),
            )

    monkeypatch.setattr(
        service, "_update_hash_progress", change_mtime_after_first_chunk
    )
    started = client.post(
        f"/projects/{DEMO_PROJECT_ID}/media/hash-jobs",
        headers=auth,
        json={
            "project_id": str(DEMO_PROJECT_ID),
            "snapshot_id": stable_snapshot_id,
            "file_name": media.name,
        },
    )
    assert started.status_code == 202
    failed = _wait_for_hash_job(client, auth, started.json()["job_id"])
    assert changed.is_set()
    assert failed["status"] == "failed"
    assert failed["error_code"] == "file_changed"
    assert failed["content_hash_status"] == "not_available"
    assert failed["sha256_lower"] is None
    assert failed["sha256_upper"] is None
    assert 0 < failed["bytes_hashed"] <= failed["total_bytes"]


def test_running_hash_job_can_be_cancelled_without_fake_completion(
    client, auth, tmp_path, monkeypatch
):
    directory = tmp_path / "cancel hash"
    directory.mkdir()
    media = directory / "trip.mp4"
    with media.open("wb") as stream:
        stream.truncate(8 * 1024 * 1024)
    stable_snapshot_id = _confirm_stable(client, auth, directory)

    service = client.app.state.media_intake
    original_progress = service._update_hash_progress
    worker_paused = threading.Event()
    release_worker = threading.Event()

    def pause_after_progress(job_id, bytes_hashed):
        original_progress(job_id, bytes_hashed)
        worker_paused.set()
        release_worker.wait(timeout=2)

    monkeypatch.setattr(service, "_update_hash_progress", pause_after_progress)
    started = client.post(
        f"/projects/{DEMO_PROJECT_ID}/media/hash-jobs",
        headers=auth,
        json={
            "project_id": str(DEMO_PROJECT_ID),
            "snapshot_id": stable_snapshot_id,
            "file_name": media.name,
        },
    )
    assert started.status_code == 202
    assert worker_paused.wait(timeout=2)
    try:
        cancelled = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/media/hash-jobs/"
                f"{started.json()['job_id']}/cancel"
            ),
            headers=auth,
            json={},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] in {"cancelling", "cancelled"}
        assert cancelled.json()["sha256_lower"] is None
    finally:
        release_worker.set()

    terminal = _wait_for_hash_job(
        client, auth, started.json()["job_id"]
    )
    assert terminal["status"] == "cancelled"
    assert terminal["content_hash_status"] == "not_available"
    assert terminal["sha256_lower"] is None
    assert terminal["bytes_hashed"] < terminal["total_bytes"]
