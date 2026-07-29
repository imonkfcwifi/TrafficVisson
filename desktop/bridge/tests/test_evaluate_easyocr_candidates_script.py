from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from uuid import UUID

import numpy
from PIL import Image


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "evaluate_easyocr_candidates.py"
)
SPEC = importlib.util.spec_from_file_location(
    "trafficvisson_evaluate_easyocr_candidates",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path):
    data_root = tmp_path / "bridge-data"
    project_id = "11111111-1111-4111-8111-111111111111"
    project_root = data_root / "projects" / project_id
    records_root = project_root / "analysis-job-records"
    records_root.mkdir(parents=True)
    protected = []
    for index in range(4):
        job_id = str(
            UUID(f"22222222-2222-4222-8222-{index + 1:012d}")
        )
        candidate_id = f"{index + 1:020x}"
        run_name = f"candidate-extraction-ocr-{index + 1:02d}"
        artifact_root = (
            project_root / "candidate-extractions" / run_name
        )
        artifact_root.mkdir(parents=True)
        image_path = artifact_root / f"{candidate_id}.jpg"
        Image.new(
            "RGB",
            (160, 90),
            (40 + index * 20, 90, 120),
        ).save(image_path, format="JPEG", quality=90)
        image_sha256 = _sha256(image_path)
        record = {
            "schema_version": "trafficvisson.analysis-job.v1",
            "status": "completed",
            "project_id": project_id,
            "job_id": job_id,
            "snapshot_id": str(
                UUID(f"33333333-3333-4333-8333-{index + 1:012d}")
            ),
            "hash_job_id": str(
                UUID(f"44444444-4444-4444-8444-{index + 1:012d}")
            ),
            "file_name": f"drive-{index + 1:02d}.mp4",
            "artifact_directory_name": run_name,
            "result": {
                "candidate_count": 1,
                "candidates": [
                    {
                        "candidate_id": candidate_id,
                        "representative_timestamp_seconds": 10.0 + index,
                        "scene_score": 0.4,
                        "sharpness_score": 0.8,
                        "triggers": ["scene_change"],
                        "representative_image": {
                            "relative_path": image_path.name,
                            "size_bytes": image_path.stat().st_size,
                            "sha256": image_sha256,
                        },
                    }
                ],
            },
        }
        record_path = records_root / f"{job_id}.json"
        record_path.write_text(
            json.dumps(record, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        digest_path = record_path.with_suffix(".sha256")
        digest_path.write_text(_sha256(record_path), encoding="ascii")
        protected.extend((image_path, record_path, digest_path))

    labels = data_root / "candidate-labels"
    labels.mkdir()
    label_path = labels / "labels.json"
    label_path.write_text('{"labels":[]}', encoding="utf-8")
    sidecars = data_root / "experimental-proposals"
    sidecars.mkdir()
    sidecar_path = sidecars / "guard.json"
    sidecar_path.write_text('{"review_only":true}', encoding="utf-8")
    database_path = data_root / "catalog.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE guard (value TEXT)")
        connection.execute("INSERT INTO guard VALUES ('unchanged')")
    protected.extend((label_path, sidecar_path, database_path))

    model_root = tmp_path / "easyocr-model"
    model_root.mkdir()
    detector = model_root / "craft_mlt_25k.pth"
    recognizer = model_root / "korean_g2.pth"
    detector.write_bytes(b"fake-craft-model")
    recognizer.write_bytes(b"fake-korean-model")
    protected.extend((detector, recognizer))
    return data_root, model_root, protected


class _FakeTorch:
    @staticmethod
    def set_num_threads(_value):
        return None

    @staticmethod
    def set_num_interop_threads(_value):
        return None

    @staticmethod
    def manual_seed(_value):
        return None

    @staticmethod
    def use_deterministic_algorithms(_enabled, warn_only=True):
        assert warn_only is True


class _FakeReader:
    def readtext(self, image, **options):
        assert image.shape == (90, 160, 3)
        assert options["detail"] == 1
        assert options["paragraph"] is False
        assert options["batch_size"] == 1
        assert options["workers"] == 0
        return [
            (
                [[10, 10], [100, 10], [100, 35], [10, 35]],
                "천안 12",
                0.87,
            ),
            (
                [[20, 45], [120, 45], [120, 70], [20, 70]],
                "TUNNEL",
                0.61,
            ),
        ]


def _fake_runtime():
    return MODULE.RuntimeBundle(
        easyocr_version="1.7.1-test",
        torch_version="2.7.0-test",
        numpy_module=numpy,
        torch_module=_FakeTorch(),
        reader_factory=lambda model_root: (
            _FakeReader()
            if model_root.name == "easyocr-model"
            else (_ for _ in ()).throw(AssertionError("wrong model root"))
        ),
    )


def test_cpu_easyocr_smoke_is_distributed_review_only_and_non_mutating(
    tmp_path: Path,
) -> None:
    data_root, model_root, protected_paths = _fixture(tmp_path)
    guarded = {path: path.read_bytes() for path in protected_paths}
    output = tmp_path / "easyocr-smoke"

    result = MODULE.run_evaluation(
        data_root=data_root,
        model_directory=model_root,
        output_directory=output,
        sample_count=3,
        max_wall_seconds=60,
        max_output_bytes=32 * 1024 * 1024,
        cpu_threads=2,
        runtime_factory=_fake_runtime,
    )

    assert result["discovery"]["selected_frame_count"] == 3
    assert result["discovery"]["selected_job_count"] == 3
    assert len({frame["job_id"] for frame in result["frames"]}) == 3
    assert result["runtime"]["device"] == "cpu"
    assert result["runtime"]["gpu_access_disabled"] is True
    assert result["runtime"]["configured_vram_budget_bytes"] == 0
    assert result["runtime"]["download_enabled"] is False
    assert result["score_semantics"]["label"] == (
        "ocr_score_not_verified_probability"
    )
    assert result["score_semantics"]["is_probability"] is False
    assert result["review_only"] is True
    assert result["automatic_labeling_allowed"] is False
    assert result["sidecars_created"] == 0
    assert result["annotations_created"] == 0
    assert result["incidents_created"] == 0
    assert result["model_promoted"] is False
    assert result["summary"]["raw_observation_count"] == 6
    assert result["summary"]["filtered_observation_count"] == 3
    assert result["summary"]["hangul_observation_count"] == 3
    assert result["summary"]["digit_observation_count"] == 3
    assert all(
        frame["facility_interpretation"]["facility_kind"] is None
        and frame["facility_interpretation"]["facility_name"] is None
        and frame["facility_interpretation"][
            "facility_identity_verified"
        ]
        is False
        for frame in result["frames"]
    )

    assert (output / "ocr_results.json").is_file()
    assert (output / "ocr_observations.csv").is_file()
    assert (output / "README.md").is_file()
    assert (output / "contact-sheet-001.jpg").is_file()
    assert (output / "manifest.json").is_file()
    with (output / "ocr_observations.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 6
    hangul = [row for row in rows if row["raw_text"] == "천안 12"]
    english = [row for row in rows if row["raw_text"] == "TUNNEL"]
    assert len(hangul) == 3
    assert all(row["review_filter_match"] == "true" for row in hangul)
    assert all(row["contains_hangul"] == "true" for row in hangul)
    assert all(row["contains_digit"] == "true" for row in hangul)
    assert all(row["review_filter_match"] == "false" for row in english)
    assert all(
        row["score_semantics"]
        == "ocr_score_not_verified_probability"
        for row in rows
    )
    assert all(row["is_probability"] == "false" for row in rows)
    assert all(row["review_only"] == "true" for row in rows)
    assert all(
        row["automatic_labeling_allowed"] == "false" for row in rows
    )

    manifest = json.loads(
        (output / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["review_only"] is True
    assert manifest["automatic_labeling_allowed"] is False
    assert manifest["sidecars_created"] == 0
    assert manifest["source_integrity"]["analysis_json_modified"] == 0
    assert manifest["source_integrity"]["label_files_modified"] == 0
    assert manifest["source_integrity"]["sqlite_files_modified"] == 0
    assert (
        manifest["source_integrity"]["experimental_sidecars_modified"]
        == 0
    )
    model_by_name = {
        row["file_name"]: row for row in manifest["model_files"]
    }
    assert model_by_name["craft_mlt_25k.pth"]["sha256"] == _sha256(
        model_root / "craft_mlt_25k.pth"
    )
    assert model_by_name["korean_g2.pth"]["sha256"] == _sha256(
        model_root / "korean_g2.pth"
    )
    assert "시설 유형·시설명·지리 위치 확정: `0건`" in (
        output / "README.md"
    ).read_text(encoding="utf-8")
    assert all(path.read_bytes() == content for path, content in guarded.items())
