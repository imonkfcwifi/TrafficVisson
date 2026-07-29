from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

from PIL import Image


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "evaluate_existing_candidates.py"
)
SPEC = importlib.util.spec_from_file_location(
    "trafficvisson_evaluate_existing_candidates",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture_data_root(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    data_root = tmp_path / "bridge-data"
    project_id = "11111111-1111-4111-8111-111111111111"
    job_id = "22222222-2222-4222-8222-222222222222"
    project = data_root / "projects" / project_id
    records = project / "analysis-job-records"
    artifacts = (
        project
        / "candidate-extractions"
        / "candidate-extraction-evaluation"
    )
    labels = data_root / "candidate-labels"
    records.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    labels.mkdir(parents=True)

    image_path = artifacts / "candidate-0001.jpg"
    Image.new("RGB", (96, 54), (80, 120, 60)).save(
        image_path,
        format="JPEG",
        quality=90,
    )
    image_digest = _sha256(image_path)
    candidate_id = "a" * 20
    record = {
        "schema_version": "trafficvisson.analysis-job.v1",
        "status": "completed",
        "project_id": project_id,
        "job_id": job_id,
        "snapshot_id": "33333333-3333-4333-8333-333333333333",
        "hash_job_id": "44444444-4444-4444-8444-444444444444",
        "file_name": "field-drive.mp4",
        "artifact_directory_name": "candidate-extraction-evaluation",
        "result": {
            "candidate_count": 1,
            "candidates": [
                {
                    "candidate_id": candidate_id,
                    "representative_timestamp_seconds": 12.5,
                    "scene_score": 0.4,
                    "sharpness_score": 0.8,
                    "triggers": ["scene_change"],
                    "representative_image": {
                        "relative_path": image_path.name,
                        "size_bytes": image_path.stat().st_size,
                        "sha256": image_digest,
                    },
                }
            ],
        },
    }
    record_path = records / f"{job_id}.json"
    record_path.write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    record_path.with_suffix(".sha256").write_text(
        _sha256(record_path),
        encoding="ascii",
    )
    label_path = labels / "labels.json"
    label_path.write_text('{"labels":[]}', encoding="utf-8")
    sqlite_path = data_root / "catalog.sqlite3"
    with sqlite3.connect(sqlite_path) as connection:
        connection.execute("CREATE TABLE source_guard (value TEXT)")
        connection.execute("INSERT INTO source_guard VALUES ('unchanged')")
    model_manifest = tmp_path / "trafficsupervision-model-pack.json"
    model_manifest.write_text("{}", encoding="utf-8")
    return data_root, model_manifest, record_path, image_path


class _FakeClipService:
    def __init__(self, manifest: Path, timeout_seconds: float) -> None:
        assert manifest.is_file()
        assert timeout_seconds >= 5

    @staticmethod
    def status():
        return {
            "enabled": True,
            "engine": "transformers_clip_zero_shot",
            "pack_id": "fake-clip",
            "pack_version": "0.0.1",
            "manifest_sha256": "f" * 64,
        }

    @staticmethod
    def infer_candidates(candidates, run_directory):
        assert run_directory.is_dir()
        results = {}
        for candidate in candidates:
            image = run_directory / candidate["representative_image"]["relative_path"]
            assert image.is_file()
            results[candidate["candidate_id"]] = {
                "schema_version": "trafficvisson.vision-inference.v1",
                "mode": "local_model",
                "status": "classified",
                "predictions": [
                    {
                        "issue_type": "roadside_mowing_required",
                        "confidence": 0.12345678,
                        "box": None,
                        "source_label": "overgrown grass",
                        "calibration": "zero_shot_relative",
                    }
                ],
                "ocr_observations": [],
                "duration_ms": 17,
                "warning_code": None,
            }
        return results, {
            "schema_version": "trafficvisson.vision-inference.v1",
            "mode": "local_model",
            "engine": "transformers_clip_zero_shot",
            "pack_id": "fake-clip",
            "pack_version": "0.0.1",
            "manifest_sha256": "f" * 64,
            "runtime_spdx": "Apache-2.0",
            "weights_spdx": "MIT",
            "candidate_count": len(candidates),
            "status_counts": {"classified": len(candidates)},
        }


def test_evaluation_is_non_mutating_and_marks_clip_scores_as_relative(
    tmp_path: Path,
) -> None:
    data_root, model_manifest, record_path, image_path = _fixture_data_root(
        tmp_path
    )
    guarded = {
        path: path.read_bytes()
        for path in (
            record_path,
            record_path.with_suffix(".sha256"),
            image_path,
            data_root / "candidate-labels" / "labels.json",
            data_root / "catalog.sqlite3",
        )
    }
    output = tmp_path / "evaluation"
    result = MODULE.run_evaluation(
        data_root=data_root,
        model_manifest=model_manifest,
        output_directory=output,
        service_factory=_FakeClipService,
    )

    assert result["score_semantics"] == {
        "calibration": "zero_shot_relative",
        "label": "zero_shot_relative_not_probability",
        "is_probability": False,
        "is_accuracy": False,
        "is_maintenance_decision": False,
    }
    assert result["side_effects"] == {
        "analysis_records_modified": 0,
        "label_records_modified": 0,
        "sqlite_databases_modified": 0,
        "automatic_labels_created": 0,
        "incidents_created": 0,
        "model_promoted": False,
        "experimental_sidecar_runs_created": 0,
        "experimental_sidecar_proposals_created": 0,
    }
    assert result["results"][0]["review_state"] == "unreviewed"
    assert result["results"][0]["predictions"][0]["score"] == 0.12345678
    assert result["results"][0]["predictions"][0]["relative_score"] == 0.12345678
    assert result["results"][0]["predictions"][0]["model_score"] is None
    assert not (output / ".staging").exists()
    assert (output / "predictions.json").is_file()
    assert (output / "candidate_scores.csv").is_file()
    assert (output / "EVALUATION.md").is_file()
    assert (output / "manifest.json").is_file()
    assert (output / "sidecar_job_payloads.json").is_file()
    assert (output / "review-contact-sheet-001.jpg").is_file()
    assert "실제 결함 확률" in (output / "EVALUATION.md").read_text(
        encoding="utf-8"
    )
    assert all(path.read_bytes() == value for path, value in guarded.items())


def test_existing_output_directory_is_refused(tmp_path: Path) -> None:
    data_root, model_manifest, _, _ = _fixture_data_root(tmp_path)
    output = tmp_path / "existing"
    output.mkdir()
    try:
        MODULE.run_evaluation(
            data_root=data_root,
            model_manifest=model_manifest,
            output_directory=output,
            service_factory=_FakeClipService,
        )
    except MODULE.EvaluationError as error:
        assert "already exists" in str(error)
    else:
        raise AssertionError("existing evaluation directory was not refused")


class _FakeOnnxService:
    def __init__(self, manifest: Path, timeout_seconds: float) -> None:
        assert manifest.is_file()
        assert timeout_seconds >= 5

    @staticmethod
    def status():
        return {
            "enabled": True,
            "engine": "onnx_yolo_v8",
            "pack_id": "fake-onnx-pothole",
            "pack_version": "0.0.1",
            "manifest_sha256": "e" * 64,
        }

    @staticmethod
    def infer_candidates(candidates, run_directory):
        results = {}
        for candidate in candidates:
            results[candidate["candidate_id"]] = {
                "schema_version": "trafficvisson.vision-inference.v1",
                "mode": "local_model",
                "status": "classified",
                "predictions": [
                    {
                        "issue_type": "pothole",
                        "confidence": 0.7654321,
                        "box": {
                            "x": 0.1,
                            "y": 0.2,
                            "width": 0.3,
                            "height": 0.4,
                        },
                        "source_label": "포트홀 실험 제안",
                        "calibration": "model_score",
                    }
                ],
                "ocr_observations": [],
                "duration_ms": 11,
                "warning_code": None,
            }
        return results, {
            "schema_version": "trafficvisson.vision-inference.v1",
            "mode": "local_model",
            "engine": "onnx_yolo_v8",
            "pack_id": "fake-onnx-pothole",
            "pack_version": "0.0.1",
            "manifest_sha256": "e" * 64,
            "runtime_spdx": "MIT",
            "weights_spdx": "Apache-2.0",
            "candidate_count": len(candidates),
            "status_counts": {"classified": len(candidates)},
        }


def test_onnx_model_scores_and_boxes_remain_unreviewed_proposals(
    tmp_path: Path,
) -> None:
    data_root, model_manifest, _, _ = _fixture_data_root(tmp_path)
    output = tmp_path / "onnx-evaluation"
    note = (
        "2프레임 사전 스모크 중 1건은 노면 그림자를 포트홀로 본 오탐 "
        "의심 사례이며 hard negative 확인이 필요하다."
    )
    result = MODULE.run_evaluation(
        data_root=data_root,
        model_manifest=model_manifest,
        output_directory=output,
        service_factory=_FakeOnnxService,
        operator_note=note,
    )

    prediction = result["results"][0]["predictions"][0]
    assert result["score_semantics"]["calibration"] == "model_score"
    assert result["score_semantics"]["label"] == "model_score_not_probability"
    assert result["score_semantics"]["is_probability"] is False
    assert prediction["score"] == 0.7654321
    assert prediction["relative_score"] is None
    assert prediction["model_score"] == 0.7654321
    assert prediction["box"] == {
        "x": 0.1,
        "y": 0.2,
        "width": 0.3,
        "height": 0.4,
    }
    assert result["results"][0]["review_state"] == "unreviewed"
    report = (output / "EVALUATION.md").read_text(encoding="utf-8")
    assert note in report
    assert "hard negative" in report
    assert "보정되지 않은 모델 출력 점수" in report
    with (output / "candidate_scores.csv").open(
        encoding="utf-8-sig", newline=""
    ) as stream:
        row = next(csv.DictReader(stream))
    assert row["model_score"] == "0.7654321"
    assert json.loads(row["box_json"])["x"] == 0.1


def test_opt_in_sidecar_recorder_receives_job_scoped_production_payloads(
    tmp_path: Path,
) -> None:
    data_root, model_manifest, _, _ = _fixture_data_root(tmp_path)
    output = tmp_path / "sidecar-evaluation"
    captured = {}

    def recorder(root, payloads):
        captured["root"] = root
        captured["payloads"] = list(payloads)
        return {
            "requested": True,
            "recorded": True,
            "run_count": len(payloads),
            "proposal_count": sum(
                len(payload["proposals"]) for payload in payloads
            ),
            "run_ids": ["66666666-6666-4666-8666-666666666666"],
            "review_only": True,
            "automatic_labeling_allowed": False,
        }

    result = MODULE.run_evaluation(
        data_root=data_root,
        model_manifest=model_manifest,
        output_directory=output,
        service_factory=_FakeOnnxService,
        record_sidecars=True,
        sidecar_evaluation_id="onnx-pothole-fixture-v1",
        sidecar_recorder=recorder,
    )

    assert captured["root"] == data_root.resolve()
    assert len(captured["payloads"]) == 1
    payload = captured["payloads"][0]
    assert payload["model"] == {
        "provider": "onnx_yolo_v8",
        "pack_id": "fake-onnx-pothole",
        "pack_version": "0.0.1",
        "manifest_sha256": "e" * 64,
    }
    assert payload["evaluation"] == {
        "evaluation_id": "onnx-pothole-fixture-v1",
        "producer": "trafficvisson-candidate-evaluator",
        "producer_version": "0.2.0",
    }
    assert payload["dataset"] is None
    proposal = payload["proposals"][0]
    assert proposal["candidate_id"] == "a" * 20
    assert proposal["status"] == "classified"
    assert proposal["predictions"][0] == {
        "issue_type": "pothole",
        "relative_score": None,
        "model_score": 0.7654321,
        "box": {
            "x": 0.1,
            "y": 0.2,
            "width": 0.3,
            "height": 0.4,
        },
        "source_label": "포트홀 실험 제안",
        "calibration": "model_score",
    }
    assert result["side_effects"][
        "experimental_sidecar_runs_created"
    ] == 1
    assert result["side_effects"][
        "experimental_sidecar_proposals_created"
    ] == 1
    exported = json.loads(
        (output / "sidecar_job_payloads.json").read_text(encoding="utf-8")
    )
    assert exported["recorded"] is True
    assert exported["jobs"] == captured["payloads"]


def test_no_suggestion_is_omitted_so_it_cannot_hide_another_model_proposal() -> None:
    payloads = MODULE.build_sidecar_job_payloads(
        [
            {
                "project_id": "11111111-1111-4111-8111-111111111111",
                "job_id": "22222222-2222-4222-8222-222222222222",
                "source_candidate_id": "a" * 20,
                "inference_mode": "local_model",
                "inference_status": "no_detection",
                "predictions": [],
            }
        ],
        model_summary={
            "engine": "onnx_yolo_v8",
            "pack_id": "fake-onnx-pothole",
            "pack_version": "0.0.1",
            "manifest_sha256": "e" * 64,
        },
        evaluation_id="onnx-no-suggestion-v1",
    )

    # ExperimentalProposalStore.latest_for_job chooses the newest recorded
    # row per candidate. An empty ONNX row would mask an older classified CLIP
    # row, so no-suggestion candidates must create no sidecar payload at all.
    assert payloads == []


def test_duplicate_candidate_ids_across_jobs_get_distinct_stable_ids(
    tmp_path: Path,
) -> None:
    data_root, _, record_path, image_path = _fixture_data_root(tmp_path)
    first = json.loads(record_path.read_text(encoding="utf-8"))
    second_job_id = "55555555-5555-4555-8555-555555555555"
    second = dict(first)
    second["job_id"] = second_job_id
    second["artifact_directory_name"] = "candidate-extraction-evaluation-two"
    second_artifacts = (
        data_root
        / "projects"
        / first["project_id"]
        / "candidate-extractions"
        / second["artifact_directory_name"]
    )
    second_artifacts.mkdir()
    second_image = second_artifacts / image_path.name
    shutil_copy = image_path.read_bytes()
    second_image.write_bytes(shutil_copy)
    second["result"] = json.loads(json.dumps(first["result"]))
    second["result"]["candidates"][0]["representative_image"][
        "sha256"
    ] = _sha256(second_image)
    second_record = record_path.with_name(f"{second_job_id}.json")
    second_record.write_text(
        json.dumps(second, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    second_record.with_suffix(".sha256").write_text(
        _sha256(second_record),
        encoding="ascii",
    )

    discovery = MODULE.discover_candidates(data_root)
    assert len(discovery.candidates) == 2
    ids = [item.evaluation_candidate_id for item in discovery.candidates]
    assert len(ids) == len(set(ids))
    assert all(len(value) == 20 for value in ids)


def test_invalid_later_candidate_discards_the_whole_record_even_with_limit(
    tmp_path: Path,
) -> None:
    data_root, _, record_path, _ = _fixture_data_root(tmp_path)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    invalid = json.loads(json.dumps(record["result"]["candidates"][0]))
    invalid["candidate_id"] = "not-a-candidate-id"
    record["result"]["candidates"].append(invalid)
    record["result"]["candidate_count"] = 2
    record_path.write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    record_path.with_suffix(".sha256").write_text(
        _sha256(record_path),
        encoding="ascii",
    )

    try:
        MODULE.discover_candidates(data_root, limit=1)
    except MODULE.EvaluationError as error:
        assert "No valid persisted candidates" in str(error)
    else:
        raise AssertionError("a partially invalid analysis record was accepted")
