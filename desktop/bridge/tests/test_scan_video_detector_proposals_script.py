from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "scan_video_detector_proposals.py"
)
SPEC = importlib.util.spec_from_file_location(
    "trafficvisson_dense_detector_scan",
    SCRIPT,
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakePack:
    def __init__(self, manifest: Path, model: Path):
        self.engine = "onnx_yolo_v8"
        self.pack_id = "fixture-pothole-pack"
        self.pack_version = "0.1.1"
        self.manifest_sha256 = _sha256(manifest)
        self.artifacts = {
            "model.onnx": SimpleNamespace(
                path=model,
                size_bytes=model.stat().st_size,
                sha256=_sha256(model),
            )
        }


class FakeService:
    def __init__(
        self,
        manifest_sha256: str,
        *,
        fail_on_call: int | None = None,
        bad_prediction: bool = False,
    ):
        self.manifest_sha256 = manifest_sha256
        self.fail_on_call = fail_on_call
        self.bad_prediction = bad_prediction
        self.calls = 0

    def status(self):
        return {
            "enabled": True,
            "mode": "local_model",
            "warning_code": None,
            "engine": "onnx_yolo_v8",
            "pack_id": "fixture-pothole-pack",
            "pack_version": "0.1.1",
            "manifest_sha256": self.manifest_sha256,
            "capabilities": ["defect_detection"],
            "runtime_spdx": "MIT",
            "weights_spdx": "Apache-2.0",
            "redistribution_allowed": False,
        }

    def infer_candidates(self, candidates, run_directory, *, cancel=None):
        self.calls += 1
        if self.fail_on_call == self.calls:
            raise MODULE.DenseScanError("fixture interruption")
        assert cancel is not None and cancel() is False
        results = {}
        for local_index, candidate in enumerate(candidates):
            image = run_directory / candidate["representative_image"]["relative_path"]
            assert image.is_file()
            prediction = []
            if local_index < 2:
                prediction = [
                    {
                        "issue_type": "pothole",
                        "confidence": True if self.bad_prediction else 0.61,
                        "box": {
                            "x": 0.2 + local_index * 0.01,
                            "y": 0.5,
                            "width": 0.2,
                            "height": 0.15,
                        },
                        "source_label": "pothole fixture proposal",
                        "calibration": "model_score",
                    }
                ]
            results[candidate["candidate_id"]] = {
                "schema_version": "trafficvisson.vision-inference.v1",
                "mode": "local_model",
                "status": "classified" if prediction else "no_detection",
                "provider": "onnx_yolo_v8",
                "pack_id": "fixture-pothole-pack",
                "pack_version": "0.1.1",
                "manifest_sha256": self.manifest_sha256,
                "capabilities": ["defect_detection"],
                "predictions": prediction,
                "ocr_observations": [],
                "duration_ms": 5,
                "warning_code": None,
            }
        return results, {
            "mode": "local_model",
            "engine": "onnx_yolo_v8",
            "pack_id": "fixture-pothole-pack",
            "pack_version": "0.1.1",
            "manifest_sha256": self.manifest_sha256,
            "capabilities": ["defect_detection"],
            "runtime_spdx": "MIT",
            "weights_spdx": "Apache-2.0",
            "redistribution_allowed": False,
            "status_counts": {
                "classified": sum(
                    bool(row["predictions"]) for row in results.values()
                )
            },
        }


def _fixture(tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"read-only-source-video-fixture")
    manifest = tmp_path / "trafficvisson-model-pack.json"
    manifest.write_text('{"fixture":"manifest"}\n', encoding="utf-8")
    model = tmp_path / "model.onnx"
    model.write_bytes(b"fixture-onnx-model")
    tool = tmp_path / "media-tool.exe"
    tool.write_bytes(b"fixture-tool")
    program_data = tmp_path / "ProgramData"
    sample_calls = []

    def probe(source_video, **_):
        assert source_video == source.resolve()
        return MODULE.VideoProbe(
            duration_seconds=4.0,
            width=320,
            height=240,
            video_codec="h264",
            format_name="mov,mp4",
        )

    def sample(
        source_video,
        staging_root,
        *,
        sample_interval_seconds,
        max_frames,
        **_,
    ):
        assert source_video == source.resolve()
        sample_calls.append(source_video)
        frames_root = staging_root / "frames"
        frames_root.mkdir()
        rows = []
        for frame_index in range(1, max_frames + 1):
            path = frames_root / f"frame-{frame_index:06d}.jpg"
            Image.new(
                "RGB",
                (320, 240),
                color=(frame_index * 20, 50, 90),
            ).save(path, format="JPEG", quality=85)
            digest = _sha256(path)
            frame_id = hashlib.sha256(
                f"{frame_index}\0{digest}".encode("ascii")
            ).hexdigest()[:20]
            rows.append(
                MODULE.SampledFrame(
                    frame_id=frame_id,
                    frame_index=frame_index,
                    timestamp_seconds=round(
                        (frame_index - 1) * sample_interval_seconds,
                        6,
                    ),
                    relative_path=f".staging/frames/{path.name}",
                    size_bytes=path.stat().st_size,
                    sha256=digest,
                )
            )
        return rows

    def pack_loader(selected_manifest):
        assert selected_manifest == manifest.resolve()
        return FakePack(manifest, model)

    return {
        "source": source,
        "manifest": manifest,
        "model": model,
        "tool": tool,
        "program_data": program_data,
        "probe": probe,
        "sample": sample,
        "sample_calls": sample_calls,
        "pack_loader": pack_loader,
    }


def _run_kwargs(fixture, *, run_name="fixture-dense-scan"):
    return {
        "source_video": fixture["source"],
        "model_manifest": fixture["manifest"],
        "run_name": run_name,
        "fps": 1.0,
        "interval_seconds": None,
        "max_frames": 4,
        "max_video_seconds": 4.0,
        "max_wall_seconds": 60.0,
        "max_temporary_bytes": 16 * 1024 * 1024,
        "max_output_bytes": 16 * 1024 * 1024,
        "batch_size": 4,
        "max_evidence_frames": 4,
        "temporal_gap_seconds": 1.5,
        "temporal_iou_threshold": 0.1,
        "program_data": fixture["program_data"],
        "ffmpeg_path": str(fixture["tool"]),
        "ffprobe_path": str(fixture["tool"]),
        "model_pack_loader": fixture["pack_loader"],
        "tool_version_function": lambda *_, **__: "fixture media tool 1.0",
        "probe_function": fixture["probe"],
        "sample_function": fixture["sample"],
    }


def test_dense_scan_exports_bounded_review_only_artifacts_without_mutating_inputs(
    tmp_path,
):
    fixture = _fixture(tmp_path)
    source_before = fixture["source"].read_bytes()
    model_before = fixture["model"].read_bytes()
    manifest_digest = _sha256(fixture["manifest"])
    service = FakeService(manifest_digest)
    envelope = MODULE.run_dense_scan(
        **_run_kwargs(fixture),
        service_factory=lambda *_: service,
    )

    assert envelope["status"] == "completed"
    assert envelope["summary"] == {
        "sampled_frame_count": 4,
        "proposal_frame_count": 2,
        "prediction_count": 2,
        "temporal_group_count": 1,
        "evidence_frame_count": 2,
        "status_counts": {"classified": 2, "no_detection": 2},
    }
    assert envelope["score_semantics"] == {
        "calibration": "model_score",
        "label": "model_score_not_probability",
        "is_probability": False,
        "is_accuracy": False,
        "is_label": False,
        "is_maintenance_decision": False,
    }
    assert envelope["side_effects"] == {
        "analysis_records_read": 0,
        "analysis_records_modified": 0,
        "label_records_read": 0,
        "label_records_modified": 0,
        "sqlite_databases_read": 0,
        "sqlite_databases_modified": 0,
        "automatic_labels_created": 0,
        "incidents_created": 0,
        "model_promoted": False,
        "experimental_sidecars_created": 0,
    }
    assert fixture["source"].read_bytes() == source_before
    assert fixture["model"].read_bytes() == model_before
    assert envelope["source"]["verified_unchanged"] is True
    assert envelope["model"]["artifacts_verified_unchanged"] is True

    run_root = (
        fixture["program_data"]
        / "TrafficVisson"
        / "ModelEvaluations"
        / "fixture-dense-scan"
    )
    assert not (run_root / ".staging").exists()
    for relative in (
        "predictions.json",
        "frame_scores.csv",
        "README.md",
        "contact-sheet-001.jpg",
        "checkpoint.json",
        "manifest.json",
    ):
        assert (run_root / relative).is_file()
    evidence = sorted((run_root / "evidence").glob("*.jpg"))
    assert len(evidence) == 2
    for result in envelope["results"]:
        assert result["sampled_frame_ref"] is None
        if result["review_required"]:
            assert result["evidence_ref"].startswith("evidence/")
            assert all(
                prediction["is_probability"] is False
                and prediction["score_semantics"]
                == "model_score_not_probability"
                for prediction in result["predictions"]
            )
    assert (
        envelope["temporal_groups"][0]["representative"]["evidence_ref"]
        .startswith("evidence/")
    )

    with (run_root / "frame_scores.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 4
    assert {row["is_probability"] for row in rows} == {"False"}
    assert {row["automatic_label_created"] for row in rows} == {"False"}
    assert {row["incident_created"] for row in rows} == {"False"}
    assert {row["model_promoted"] for row in rows} == {"False"}
    assert {row["experimental_sidecar_created"] for row in rows} == {"False"}

    artifact_manifest = json.loads((run_root / "manifest.json").read_text("utf-8"))
    assert artifact_manifest["score_semantics"] == "model_score_not_probability"
    assert artifact_manifest["automatic_labels_created"] == 0
    assert artifact_manifest["incidents_created"] == 0
    for artifact in artifact_manifest["artifacts"]:
        path = run_root / artifact["path"]
        assert path.stat().st_size == artifact["size_bytes"]
        assert _sha256(path) == artifact["sha256"]


def test_interrupted_scan_resumes_from_checkpoint_without_resampling(tmp_path):
    fixture = _fixture(tmp_path)
    manifest_digest = _sha256(fixture["manifest"])
    first_service = FakeService(manifest_digest, fail_on_call=2)
    kwargs = _run_kwargs(fixture, run_name="resume-dense-scan")
    kwargs["batch_size"] = 1

    with pytest.raises(MODULE.DenseScanError, match="fixture interruption"):
        MODULE.run_dense_scan(
            **kwargs,
            service_factory=lambda *_: first_service,
        )
    run_root = (
        fixture["program_data"]
        / "TrafficVisson"
        / "ModelEvaluations"
        / "resume-dense-scan"
    )
    checkpoint = json.loads((run_root / "checkpoint.json").read_text("utf-8"))
    assert checkpoint["status"] == "inferring"
    assert checkpoint["completed_frame_count"] == 1
    assert len(fixture["sample_calls"]) == 1

    resumed_service = FakeService(manifest_digest)
    resumed_kwargs = dict(kwargs)
    resumed_kwargs["probe_function"] = lambda *_args, **_kwargs: pytest.fail(
        "resume must not probe"
    )
    resumed_kwargs["sample_function"] = lambda *_args, **_kwargs: pytest.fail(
        "resume must not resample"
    )
    envelope = MODULE.run_dense_scan(
        **resumed_kwargs,
        resume=True,
        service_factory=lambda *_: resumed_service,
    )
    assert envelope["status"] == "completed"
    assert envelope["summary"]["sampled_frame_count"] == 4
    assert len(fixture["sample_calls"]) == 1
    assert not (run_root / ".staging").exists()


def test_completed_scan_resume_returns_published_result_without_inference(tmp_path):
    fixture = _fixture(tmp_path)
    manifest_digest = _sha256(fixture["manifest"])
    kwargs = _run_kwargs(fixture, run_name="completed-resume")
    expected = MODULE.run_dense_scan(
        **kwargs,
        service_factory=lambda *_: FakeService(manifest_digest),
    )
    resumed_kwargs = dict(kwargs)
    resumed_kwargs["probe_function"] = lambda *_args, **_kwargs: pytest.fail(
        "a completed run must not probe"
    )
    resumed_kwargs["sample_function"] = lambda *_args, **_kwargs: pytest.fail(
        "a completed run must not resample"
    )
    resumed = MODULE.run_dense_scan(
        **resumed_kwargs,
        resume=True,
        service_factory=lambda *_: pytest.fail(
            "a completed run must not invoke inference"
        ),
    )
    assert resumed == expected


def test_invalid_detector_score_is_rejected_before_publication(tmp_path):
    fixture = _fixture(tmp_path)
    manifest_digest = _sha256(fixture["manifest"])
    with pytest.raises(
        MODULE.DenseScanError,
        match="violates the ONNX contract",
    ):
        MODULE.run_dense_scan(
            **_run_kwargs(fixture, run_name="invalid-contract"),
            service_factory=lambda *_: FakeService(
                manifest_digest,
                bad_prediction=True,
            ),
        )
    run_root = (
        fixture["program_data"]
        / "TrafficVisson"
        / "ModelEvaluations"
        / "invalid-contract"
    )
    assert not (run_root / "predictions.json").exists()
    assert not (run_root / "frame_scores.csv").exists()
    assert not (run_root / "evidence").exists()


def test_run_name_cannot_escape_programdata_model_evaluations(tmp_path):
    fixture = _fixture(tmp_path)
    with pytest.raises(MODULE.DenseScanError, match="run name is invalid"):
        MODULE.run_dense_scan(
            **_run_kwargs(fixture, run_name="../escape"),
            service_factory=lambda *_: pytest.fail("must fail before inference"),
        )
    assert not (fixture["program_data"].parent / "escape").exists()
