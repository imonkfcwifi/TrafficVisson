import csv
import hashlib
import json
import os
import zipfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import pytest

from app.artifact_export import (
    CSV_CONTRACTS,
    ArtifactExportCancelled,
    ArtifactExportError,
    ArtifactExportLimitError,
    EvidenceExportSource,
    MAX_EVIDENCE_FILES,
    MAX_EVIDENCE_TOTAL_BYTES,
    export_artifacts,
)


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _evidence_pair(tmp_path, *, incident_id=None, image_bytes=None, clip_bytes=None):
    incident_id = incident_id or str(uuid4())
    image_path = tmp_path / f"{incident_id}-image.jpg"
    clip_path = tmp_path / f"{incident_id}-clip.mp4"
    image_path.write_bytes(
        image_bytes
        if image_bytes is not None
        else b"\xff\xd8\xff\xe0" + b"trafficvisson-image"
    )
    clip_path.write_bytes(
        clip_bytes
        if clip_bytes is not None
        else b"\x00\x00\x00\x18ftypisom" + b"trafficvisson-clip"
    )
    return (
        EvidenceExportSource(
            incident_id=incident_id,
            kind="image",
            path=image_path.resolve(),
            media_type="image/jpeg",
            size_bytes=image_path.stat().st_size,
            sha256=_sha256(image_path),
        ),
        EvidenceExportSource(
            incident_id=incident_id,
            kind="clip",
            path=clip_path.resolve(),
            media_type="video/mp4",
            size_bytes=clip_path.stat().st_size,
            sha256=_sha256(clip_path),
        ),
    )


def _payload():
    return {
        "trip": {
            "trip_id": "TRIP-2025-09-16",
            "video_name": "천안논산 천안방향.mp4",
        },
        "detections": [
            {
                "detection_id": "DET-1",
                "trip_id": "TRIP-2025-09-16",
                "timestamp_ms": 222_000,
                "type": "TUNNEL_IDENTITY",
                "confidence": 0.42,
                "bbox": {"x": 0.2, "y": 0.1, "w": 0.4, "h": 0.2},
                "source": "=unsafe spreadsheet formula",
            }
        ],
        "incidents": [
            {
                "incident_id": "INC-1",
                "trip_id": "TRIP-2025-09-16",
                "detection_ids": ["DET-1"],
                "event_type": "식생 가림",
                "maintenance_required": True,
                "recommended_action": "삭초 필요",
                "review_status": "approved",
            }
        ],
        "label_questions": [
            {
                "question_id": "Q-1",
                "prompt": "이 구간은 삭초가 필요한가요?",
                "mandatory": True,
            }
        ],
        "labels": [
            {
                "annotation_id": "ANN-1",
                "question_id": "Q-1",
                "type": "VEGETATION_TRIM",
                "answer": "yes",
                "geometry": {"polygon": [[1, 2], [3, 4]]},
            }
        ],
        "ocr_observations": [
            {
                "ocr_observation_id": "OCR-1",
                "raw_text": "○○터널",
                "verified": False,
            }
        ],
        "route_points": [
            {
                "route_point_id": "RP-1",
                "sequence": 1,
                "latitude": 36.1234,
                "longitude": 127.1234,
            }
        ],
        "maintenance_actions": [
            {
                "action_id": "ACT-1",
                "incident_id": "INC-1",
                "action_type": "TRIM",
                "description": "터널명 주변 삭초",
                "priority": "high",
            }
        ],
        "report_payload": {
            "title": "도로 점검 결과",
            "sections": [{"heading": "점검 결과", "incident_ids": ["INC-1"]}],
        },
        "provenance": {
            "input_sha256": "e263ad878eed64e980df26c2347e4482196aa"
            "e0b13865761138727e9e5858d74",
            "model_version_id": "MODEL-1",
            "dataset_version_id": "DATASET-1",
        },
    }


def _payload_with_gpx():
    payload = _payload()
    project_id = "11111111-1111-4111-8111-111111111111"
    trip_id = "22222222-2222-4222-8222-222222222222"
    track_id = "33333333-3333-4333-8333-333333333333"
    route_point_id = str(
        uuid5(UUID(track_id), "route-point:0")
    )
    payload["trip"]["project_id"] = project_id
    payload["trip"]["trip_id"] = trip_id
    payload["route_points"] = [
        {
            "route_point_id": route_point_id,
            "trip_id": trip_id,
            "sequence": 0,
            "timestamp_ms": -250,
            "latitude": 37.5,
            "longitude": -127.0,
            "altitude_m": -10.5,
            "speed_kph": 12.5,
            "heading_deg": None,
            "accuracy_m": None,
            "source": "gpx",
        },
        {
            "route_point_id": str(
                uuid5(UUID(track_id), "route-point:1")
            ),
            "trip_id": trip_id,
            "sequence": 1,
            "timestamp_ms": 750,
            "latitude": 37.6,
            "longitude": -127.1,
            "altitude_m": -9.5,
            "speed_kph": 13.5,
            "heading_deg": None,
            "accuracy_m": None,
            "source": "gpx",
        },
    ]
    payload["provenance"]["gpx_track"] = {
        "schema_version": "trafficvisson.gpx-export-provenance.v1",
        "status": "attached",
        "project_id": project_id,
        "trip_id": trip_id,
        "track_id": track_id,
        "track_revision": 2,
        "point_count": 2,
        "exported_point_count": 2,
        "video_offset_seconds": -0.25,
        "sampled_for_export": False,
        "source_integrity_verified": True,
        "source_sha256": "a" * 64,
        "track_record_sha256": "b" * 64,
    }
    return payload


def test_export_writes_complete_verified_korean_excel_package(tmp_path):
    root = tmp_path / "exports"
    root.mkdir()
    fixed_time = datetime(2026, 7, 28, 5, 0, tzinfo=timezone.utc)

    result = export_artifacts(root, _payload(), now=lambda: fixed_time)

    expected_data_names = [contract[1] for contract in CSV_CONTRACTS] + [
        "report_payload.json",
        "manifest.json",
        "trafficsupervision-export.zip",
    ]
    assert [item.name for item in result.artifacts] == expected_data_names
    assert result.output_directory.parent == root
    assert result.output_directory.name.startswith("trafficsupervision-export-")
    assert result.archive_path.is_file()
    assert not list(result.output_directory.glob("*.tmp"))

    for artifact in result.artifacts:
        path = result.output_directory / artifact.name
        assert path.is_file()
        assert path.stat().st_size == artifact.size_bytes
        assert _sha256(path) == artifact.sha256

    raw_csv = result.output_directory / "raw_detections.csv"
    assert raw_csv.read_bytes().startswith(b"\xef\xbb\xbf")
    with raw_csv.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        assert tuple(reader.fieldnames) == CSV_CONTRACTS[0][2]
        row = next(reader)
    assert json.loads(row["bbox"]) == {"h": 0.2, "w": 0.4, "x": 0.2, "y": 0.1}
    assert row["source"] == "'=unsafe spreadsheet formula"

    annotation_csv = result.output_directory / "annotations.csv"
    assert "VEGETATION_TRIM" in annotation_csv.read_text(encoding="utf-8-sig")
    assert "삭초 필요" in (
        result.output_directory / "reviewed_incidents.csv"
    ).read_text(encoding="utf-8-sig")

    manifest = json.loads(
        (result.output_directory / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["schema_version"] == "trafficvisson-artifact-export-v3"
    assert manifest["generated_at"] == "2026-07-28T05:00:00+00:00"
    assert manifest["trip_id"] == "TRIP-2025-09-16"
    assert manifest["record_counts"]["annotations"] == 1
    assert manifest["record_counts"]["detections"] == 1
    manifest_artifacts = {item["name"]: item for item in manifest["artifacts"]}
    assert set(manifest_artifacts) == set(expected_data_names[:-2])
    for name, entry in manifest_artifacts.items():
        assert _sha256(result.output_directory / name) == entry["sha256"]
        assert (result.output_directory / name).stat().st_size == entry["size_bytes"]

    with zipfile.ZipFile(result.archive_path) as archive:
        assert archive.namelist() == expected_data_names[:-1]
        assert all(".." not in Path(name).parts for name in archive.namelist())
        for name in archive.namelist():
            assert archive.read(name) == (result.output_directory / name).read_bytes()


def test_export_creates_unique_private_directories_and_header_only_csvs(tmp_path):
    root = tmp_path / "exports"
    root.mkdir()
    first = export_artifacts(root, {"trip": {}, "report_payload": {}})
    second = export_artifacts(root, {"trip": {}, "report_payload": {}})

    assert first.output_directory != second.output_directory
    if os.name != "nt":
        assert first.output_directory.stat().st_mode & 0o777 == 0o700
    for _key, file_name, columns in CSV_CONTRACTS:
        with (first.output_directory / file_name).open(
            "r", encoding="utf-8-sig", newline=""
        ) as source:
            reader = csv.reader(source)
            assert next(reader) == list(columns)
            assert list(reader) == []


def test_export_rejects_unsafe_roots_and_link_components(tmp_path):
    with pytest.raises(ArtifactExportError):
        export_artifacts(Path("relative"), {})
    with pytest.raises(ArtifactExportError):
        export_artifacts(Path(tmp_path.anchor), {})

    exports = tmp_path / "exports"
    exports.mkdir()
    file_root = tmp_path / "not-a-directory"
    file_root.write_text("x", encoding="utf-8")
    with pytest.raises(ArtifactExportError):
        export_artifacts(file_root, {})

    traversal = exports / ".." / "escape"
    with pytest.raises(ArtifactExportError):
        export_artifacts(traversal, {})

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked-export"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is unavailable on this host")
    with pytest.raises(ArtifactExportError):
        export_artifacts(link, {})


def test_cancellation_never_leaves_a_success_package(tmp_path):
    root = tmp_path / "exports"
    root.mkdir()
    calls = {"count": 0}

    def cancelled():
        calls["count"] += 1
        return calls["count"] >= 4

    with pytest.raises(ArtifactExportCancelled):
        export_artifacts(
            root,
            {"detections": [{"detection_id": str(index)} for index in range(500)]},
            cancelled=cancelled,
        )
    assert list(root.iterdir()) == []


def test_export_rejects_non_object_records_non_finite_json_and_oversize_cell(
    tmp_path,
):
    root = tmp_path / "exports"
    root.mkdir()

    with pytest.raises(ArtifactExportError):
        export_artifacts(root, {"detections": ["not an object"]})
    with pytest.raises(ArtifactExportError):
        export_artifacts(root, {"report_payload": {"confidence": float("nan")}})
    with pytest.raises(ArtifactExportLimitError):
        export_artifacts(
            root,
            {"detections": [{"source": "x" * (1024 * 1024 + 1)}]},
        )
    assert list(root.iterdir()) == []


def test_export_streams_deterministic_incident_evidence_into_verified_zip(
    tmp_path,
):
    root = tmp_path / "exports"
    root.mkdir()
    later_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    earlier_id = "11111111-1111-4111-8111-111111111111"
    later = _evidence_pair(tmp_path, incident_id=later_id)
    earlier = _evidence_pair(tmp_path, incident_id=earlier_id)

    result = export_artifacts(
        root,
        _payload(),
        evidence_sources=(
            later[1],
            earlier[1],
            later[0],
            earlier[0],
        ),
    )

    expected_evidence_names = [
        f"evidence/{earlier_id}/representative.jpg",
        f"evidence/{earlier_id}/preview.mp4",
        f"evidence/{later_id}/representative.jpg",
        f"evidence/{later_id}/preview.mp4",
    ]
    assert [item.name for item in result.artifacts][8:12] == (
        expected_evidence_names
    )
    assert not (result.output_directory / "evidence").exists()
    manifest = json.loads(
        (result.output_directory / "manifest.json").read_text("utf-8")
    )
    assert manifest["schema_version"] == "trafficvisson-artifact-export-v3"
    assert manifest["evidence"]["incident_count"] == 2
    assert manifest["evidence"]["file_count"] == 4
    assert [
        item["name"] for item in manifest["evidence"]["members"]
    ] == expected_evidence_names
    serialized_manifest = json.dumps(manifest, ensure_ascii=False)
    assert str(tmp_path) not in serialized_manifest

    expected_zip_names = [
        contract[1] for contract in CSV_CONTRACTS
    ] + [
        "report_payload.json",
        *expected_evidence_names,
        "manifest.json",
    ]
    with zipfile.ZipFile(result.archive_path) as archive:
        assert archive.namelist() == expected_zip_names
        for source in (*earlier, *later):
            archived = archive.read(source.archive_name)
            assert hashlib.sha256(archived).hexdigest() == source.sha256
            assert len(archived) == source.size_bytes


def test_evidence_hash_magic_pair_and_bounds_fail_atomically(tmp_path):
    root = tmp_path / "exports"
    root.mkdir()
    image, clip = _evidence_pair(tmp_path)

    bad_hash = EvidenceExportSource(
        **{**image.__dict__, "sha256": "0" * 64}
    )
    with pytest.raises(ArtifactExportError):
        export_artifacts(
            root,
            {},
            evidence_sources=(bad_hash, clip),
        )
    assert list(root.iterdir()) == []

    bad_magic_path = tmp_path / "not-jpeg.jpg"
    bad_magic_path.write_bytes(b"not-a-jpeg")
    bad_magic = EvidenceExportSource(
        incident_id=image.incident_id,
        kind="image",
        path=bad_magic_path.resolve(),
        media_type="image/jpeg",
        size_bytes=bad_magic_path.stat().st_size,
        sha256=_sha256(bad_magic_path),
    )
    with pytest.raises(ArtifactExportError):
        export_artifacts(
            root,
            {},
            evidence_sources=(bad_magic, clip),
        )
    assert list(root.iterdir()) == []

    with pytest.raises(ArtifactExportError):
        export_artifacts(root, {}, evidence_sources=(image,))
    assert list(root.iterdir()) == []

    with pytest.raises(ArtifactExportLimitError):
        export_artifacts(
            root,
            {},
            evidence_sources=(image,) * (MAX_EVIDENCE_FILES + 1),
        )
    assert list(root.iterdir()) == []

    oversized_sources = []
    for _index in range(5):
        incident_id = str(uuid4())
        oversized_sources.extend(
            (
                EvidenceExportSource(
                    incident_id=incident_id,
                    kind="image",
                    path=image.path,
                    media_type="image/jpeg",
                    size_bytes=image.size_bytes,
                    sha256=image.sha256,
                ),
                EvidenceExportSource(
                    incident_id=incident_id,
                    kind="clip",
                    path=clip.path,
                    media_type="video/mp4",
                    size_bytes=2 * 1024**3,
                    sha256=clip.sha256,
                ),
            )
        )
    assert sum(item.size_bytes for item in oversized_sources) > (
        MAX_EVIDENCE_TOTAL_BYTES
    )
    with pytest.raises(ArtifactExportLimitError):
        export_artifacts(
            root,
            {},
            evidence_sources=tuple(oversized_sources),
        )
    assert list(root.iterdir()) == []


def test_gpx_contract_preserves_negative_numbers_and_rejects_boundary_drift(
    tmp_path,
):
    root = tmp_path / "exports"
    root.mkdir()

    result = export_artifacts(root, _payload_with_gpx())
    with (result.output_directory / "route_points.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as source:
        rows = list(csv.DictReader(source))
    assert rows[0]["timestamp_ms"] == "-250"
    assert rows[0]["longitude"] == "-127.0"
    assert rows[0]["altitude_m"] == "-10.5"
    assert not rows[0]["timestamp_ms"].startswith("'")

    for mutate in (
        lambda payload: payload["provenance"]["gpx_track"].update(
            {"project_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}
        ),
        lambda payload: payload["provenance"]["gpx_track"].update(
            {"point_count": 3}
        ),
        lambda payload: payload["route_points"][1].update(
            {"trip_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}
        ),
        lambda payload: payload["provenance"]["gpx_track"].update(
            {"track_record_sha256": "invalid"}
        ),
    ):
        unsafe = deepcopy(_payload_with_gpx())
        mutate(unsafe)
        before = set(root.iterdir())
        with pytest.raises(ArtifactExportError):
            export_artifacts(root, unsafe)
        assert set(root.iterdir()) == before


def test_evidence_symlink_and_cancellation_leave_no_package(tmp_path):
    root = tmp_path / "exports"
    root.mkdir()
    image, clip = _evidence_pair(
        tmp_path,
        image_bytes=b"\xff\xd8\xff\xe0" + b"x" * (12 * 1024 * 1024),
    )
    link = tmp_path / "linked-image.jpg"
    try:
        link.symlink_to(image.path)
    except (OSError, NotImplementedError):
        link = None
    if link is not None:
        linked_image = EvidenceExportSource(
            **{**image.__dict__, "path": link.absolute()}
        )
        with pytest.raises(ArtifactExportError):
            export_artifacts(
                root,
                {},
                evidence_sources=(linked_image, clip),
            )
        assert list(root.iterdir()) == []

    calls_while_zipping = {"count": 0}

    def cancelled():
        if list(root.rglob("trafficsupervision-export.zip.tmp")):
            calls_while_zipping["count"] += 1
            return calls_while_zipping["count"] >= 20
        return False

    with pytest.raises(ArtifactExportCancelled):
        export_artifacts(
            root,
            {},
            evidence_sources=(image, clip),
            cancelled=cancelled,
        )
    assert calls_while_zipping["count"] >= 20
    assert list(root.iterdir()) == []
