from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.project_artifact_roots import (
    DEFAULT_OUTPUT_LOCATION_ID,
    ProjectArtifactRootConflict,
    ProjectArtifactRootNotFound,
    ProjectArtifactRootRegistry,
    ProjectArtifactRootUnavailable,
)
from app.source_candidate_probe import SourceCandidateProbe
from app.store import DEMO_PROJECT_ID


TEST_TOKEN = "artifact-root-api-test-token-" + ("a" * 40)


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def test_default_and_custom_locations_are_project_scoped_and_path_private(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "bridge").resolve()
    selected = (tmp_path / "large-output").resolve()
    selected.mkdir()
    registry = ProjectArtifactRootRegistry(data_root=data_root)
    project_a = uuid4()
    project_b = uuid4()

    defaults = registry.list(project_a)
    custom_a = registry.register(
        project_a, selected_directory=selected
    )
    custom_b = registry.register(
        project_b, selected_directory=selected
    )

    assert defaults[0]["output_location_id"] == DEFAULT_OUTPUT_LOCATION_ID
    assert defaults[0]["path_exposed"] is False
    assert custom_a["output_location_id"] != custom_b["output_location_id"]
    assert custom_a["state"] == "ready"
    public_json = json.dumps(
        [defaults, custom_a, custom_b], ensure_ascii=False
    )
    assert str(data_root) not in public_json
    assert str(selected) not in public_json

    resolved_a = registry.resolve(
        project_a,
        output_location_id=custom_a["output_location_id"],
    )
    resolved_b = registry.resolve(
        project_b,
        output_location_id=custom_b["output_location_id"],
    )
    assert resolved_a.root != resolved_b.root
    assert resolved_a.root.is_relative_to(selected)
    assert resolved_b.root.is_relative_to(selected)
    with pytest.raises(ProjectArtifactRootNotFound):
        registry.resolve(
            project_b,
            output_location_id=custom_a["output_location_id"],
        )


def test_profile_and_job_binding_survive_restart_and_remain_pinned(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "bridge").resolve()
    first_base = (tmp_path / "first").resolve()
    second_base = (tmp_path / "second").resolve()
    first_base.mkdir()
    second_base.mkdir()
    project_id = uuid4()
    registry = ProjectArtifactRootRegistry(data_root=data_root)
    first = registry.register(
        project_id, selected_directory=first_base
    )
    registry.capture_profile_binding(
        project_id,
        profile_revision=1,
        output_location_id=first["output_location_id"],
    )
    old_job = registry.capture_job_binding(
        project_id,
        profile_revision=1,
        output_location_id=first["output_location_id"],
    )
    second = registry.register(
        project_id, selected_directory=second_base
    )
    registry.capture_profile_binding(
        project_id,
        profile_revision=2,
        output_location_id=second["output_location_id"],
    )

    restarted = ProjectArtifactRootRegistry(data_root=data_root)
    old_resolved = restarted.resolve_job_binding(
        project_id, old_job.binding
    )
    new_resolved = restarted.capture_job_binding(
        project_id,
        profile_revision=2,
        output_location_id=second["output_location_id"],
    )

    assert old_resolved.root.is_relative_to(first_base)
    assert new_resolved.root.is_relative_to(second_base)
    assert old_resolved.root != new_resolved.root
    assert "artifact_root_path" not in old_job.binding
    assert old_job.binding["path_exposed"] is False


def test_registry_and_marker_tampering_fail_closed(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "bridge").resolve()
    selected = (tmp_path / "output").resolve()
    selected.mkdir()
    project_id = uuid4()
    registry = ProjectArtifactRootRegistry(data_root=data_root)
    location = registry.register(
        project_id, selected_directory=selected
    )
    location_id = location["output_location_id"]
    record_path = (
        data_root
        / "project-artifact-root-registry"
        / str(project_id)
        / "locations"
        / f"{location_id}.json"
    )
    record_path.write_bytes(record_path.read_bytes() + b" ")

    with pytest.raises(ProjectArtifactRootConflict):
        registry.resolve(
            project_id, output_location_id=location_id
        )

    record_raw = record_path.read_bytes().rstrip()
    record_path.write_bytes(record_raw)
    record_path.with_suffix(".json.sha256").write_text(
        hashlib.sha256(record_raw).hexdigest() + "\n",
        encoding="ascii",
    )
    record = json.loads(record_raw)
    marker_path = (
        Path(record["artifact_root_path"])
        / ".trafficsupervision-root.json"
    )
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["project_id"] = str(uuid4())
    marker_raw = _json_bytes(marker)
    marker_path.write_bytes(marker_raw)
    marker_path.with_suffix(".json.sha256").write_text(
        hashlib.sha256(marker_raw).hexdigest() + "\n",
        encoding="ascii",
    )
    with pytest.raises(ProjectArtifactRootConflict):
        registry.resolve(
            project_id, output_location_id=location_id
        )


def test_linked_selected_directory_is_rejected(tmp_path: Path) -> None:
    target = (tmp_path / "target").resolve()
    target.mkdir()
    linked = tmp_path / "linked"
    try:
        os.symlink(target, linked, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable")

    registry = ProjectArtifactRootRegistry(
        data_root=(tmp_path / "bridge").resolve()
    )
    with pytest.raises(ProjectArtifactRootConflict):
        registry.register(uuid4(), selected_directory=linked)


def test_identity_stat_race_is_normalized_as_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    data_root = (tmp_path / "bridge").resolve()
    selected = (tmp_path / "output").resolve()
    selected.mkdir()
    project_id = uuid4()
    registry = ProjectArtifactRootRegistry(data_root=data_root)
    location = registry.register(
        project_id, selected_directory=selected
    )
    resolved = registry.resolve(
        project_id,
        output_location_id=location["output_location_id"],
    )
    original_read = registry._read_verified

    def disconnect_after_marker(path, label):
        value = original_read(path, label)
        if label == "project artifact-root marker":
            shutil.rmtree(resolved.root)
        return value

    monkeypatch.setattr(
        registry, "_read_verified", disconnect_after_marker
    )
    with pytest.raises(ProjectArtifactRootUnavailable):
        registry.resolve(
            project_id,
            output_location_id=location["output_location_id"],
        )


def test_reservation_scope_stat_race_is_normalized_as_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    data_root = (tmp_path / "bridge").resolve()
    selected = (tmp_path / "output").resolve()
    selected.mkdir()
    project_id = uuid4()
    registry = ProjectArtifactRootRegistry(data_root=data_root)
    location = registry.register(
        project_id, selected_directory=selected
    )
    resolved = registry.resolve(
        project_id,
        output_location_id=location["output_location_id"],
    )
    monkeypatch.setattr(
        registry,
        "resolve",
        lambda *_args, **_kwargs: resolved,
    )
    shutil.rmtree(resolved.root)

    with pytest.raises(ProjectArtifactRootUnavailable):
        registry.reservation_scope(
            project_id, resolved.binding
        )


def test_output_location_api_never_returns_the_private_selected_path(
    tmp_path: Path,
) -> None:
    bridge_root = (tmp_path / "bridge-data").resolve()
    selected = (tmp_path / "private large output").resolve()
    selected.mkdir()
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
    base = f"/projects/{DEMO_PROJECT_ID}/output-locations"

    with TestClient(
        create_app(
            TEST_TOKEN,
            fixture_mode=True,
            allow_test_client=True,
            bridge_data_root=str(bridge_root),
            source_candidate_probe=SourceCandidateProbe(None),
        )
    ) as client:
        registered = client.post(
            base,
            headers=headers,
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "selected_directory": str(selected),
            },
        )
        assert registered.status_code == 201, registered.text
        listed = client.get(base, headers=headers)
        assert listed.status_code == 200, listed.text

        serialized = json.dumps(
            [registered.json(), listed.json()],
            ensure_ascii=False,
        )
        assert str(selected) not in serialized
        assert str(bridge_root) not in serialized
        assert "selected_directory" not in serialized
        assert "artifact_root_path" not in serialized
        assert registered.json()["location"]["path_exposed"] is False
        assert listed.json()["path_exposed"] is False
