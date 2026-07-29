from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from app.analysis_jobs import ResolvedAnalysisArtifact
from app.auto_labeling import compile_labeling_command
from app.command_proposal_generation import (
    RUNNER_REQUEST_SCHEMA,
    RUNNER_RESPONSE_SCHEMA,
    CommandProposalGenerationError,
    FixedManifestCommandProposalGenerator,
    UnavailableCommandProposalGenerator,
)


PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
JOB_ID = UUID("22222222-2222-4222-8222-222222222222")
CANDIDATE_ID = "0123456789abcdefabcd"
RECIPE_ID = "a" * 64


def _write_json(path: Path, value: object) -> bytes:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    path.write_bytes(raw)
    return raw


def _runner_fixture(tmp_path: Path):
    root = (
        tmp_path
        / "TrafficSuperVision"
        / "CommandProposalRunner"
        / "grounding-dino-v1"
    )
    model_root = root / "model-pack"
    model_root.mkdir(parents=True)
    executable = root / "trafficsupervision-command-proposal-runner.exe"
    executable.write_bytes(b"trusted-test-runner")
    model_artifact = model_root / "model.onnx"
    model_artifact.write_bytes(b"trusted-test-model")
    notice_artifact = model_root / "THIRD-PARTY-NOTICES.md"
    notice_artifact.write_text("trusted test notice", encoding="utf-8")
    model_manifest_raw = _write_json(
        model_root / "manifest.json",
        {
            "schema_version": (
                "trafficvisson.command-proposal-model-pack.v2"
            ),
            "pack_id": "trafficvisson-grounding-dino-test",
            "pack_version": "0.1.0",
            "model": {
                "name": "Grounding DINO test model",
                "version": "0123456789abcdef0123456789abcdef01234567",
                "task": "zero_shot_object_detection",
                "format": "onnx",
                "selected_artifact": "model.onnx",
            },
            "source": {
                "repository_url": (
                    "https://models.example.test/grounding-dino"
                ),
                "repo_id": "example/grounding-dino",
                "resolved_revision": (
                    "0123456789abcdef0123456789abcdef01234567"
                ),
                "selected_model": "model.onnx",
            },
            "license": {
                "weights_spdx": "Apache-2.0",
                "runtime_spdx": "MIT",
                "license_url": (
                    "https://www.apache.org/licenses/LICENSE-2.0"
                ),
                "commercial_use_confirmed": True,
                "redistribution_allowed": False,
                "notice_artifact": "THIRD-PARTY-NOTICES.md",
            },
            "gpu_requirements": {
                "required": False,
                "cpu_fallback": True,
                "recommended_min_vram_mb": 4096,
                "cuda_min_version": None,
            },
            "inference": {
                "task": "zero_shot_object_detection",
            },
            "review_policy": {
                "review_only": True,
                "automatic_labeling_allowed": False,
                "incident_creation_allowed": False,
                "model_promotion_allowed": False,
            },
            "artifacts": [
                {
                    "path": "model.onnx",
                    "size_bytes": model_artifact.stat().st_size,
                    "sha256": hashlib.sha256(
                        model_artifact.read_bytes()
                    ).hexdigest(),
                },
                {
                    "path": "THIRD-PARTY-NOTICES.md",
                    "size_bytes": notice_artifact.stat().st_size,
                    "sha256": hashlib.sha256(
                        notice_artifact.read_bytes()
                    ).hexdigest(),
                },
            ],
        },
    )
    model_manifest_sha256 = hashlib.sha256(
        model_manifest_raw
    ).hexdigest()
    runner_manifest_raw = _write_json(
        root / "runner-manifest.json",
        {
            "schema_version": "trafficvisson.command-proposal-runner.v1",
            "runner_id": "trafficvisson-grounding-dino-test",
            "runner_version": "0.1.0",
            "protocol_version": RUNNER_REQUEST_SCHEMA,
            "executable_sha256": hashlib.sha256(
                executable.read_bytes()
            ).hexdigest(),
            "model_manifest_sha256": model_manifest_sha256,
            "model": {
                "provider": "transformers_grounding_dino",
                "pack_id": "trafficvisson-grounding-dino-test",
                "pack_version": "0.1.0",
                "manifest_sha256": model_manifest_sha256,
            },
            "evaluation": {
                "evaluation_id": "runner-contract-test",
                "producer": "trafficvisson-test-runner",
                "producer_version": "0.1.0",
            },
        },
    )
    image = tmp_path / "candidate.jpg"
    image.write_bytes(b"\xff\xd8\xfftrusted-test-image")
    image_sha256 = hashlib.sha256(image.read_bytes()).hexdigest()
    return {
        "root": root.resolve(),
        "manifest_sha256": hashlib.sha256(
            runner_manifest_raw
        ).hexdigest(),
        "image": image.resolve(),
        "image_sha256": image_sha256,
    }


def _resolver(fixture):
    def resolve(project_id, analysis_job_id, candidate_id):
        assert project_id == PROJECT_ID
        assert analysis_job_id == JOB_ID
        assert candidate_id == CANDIDATE_ID
        return ResolvedAnalysisArtifact(
            path=fixture["image"],
            media_type="image/jpeg",
            size_bytes=fixture["image"].stat().st_size,
            sha256=fixture["image_sha256"],
        )

    return resolve


def _response_for_request(request: dict, *, classified: bool = True) -> bytes:
    prediction = {
        "issue_type": request["issue_type"],
        "relative_score": None,
        "model_score": 0.71,
        "box": {
            "x": 0.1,
            "y": 0.2,
            "width": 0.3,
            "height": 0.4,
        },
        "source_label": request["positive_prompts"][0],
        "calibration": "model_score",
    }
    return json.dumps(
        {
            "schema_version": RUNNER_RESPONSE_SCHEMA,
            "project_id": request["project_id"],
            "analysis_job_id": request["analysis_job_id"],
            "recipe_id": request["recipe_id"],
            "runner_manifest_sha256": request[
                "runner_manifest_sha256"
            ],
            "model_manifest_sha256": request["model_manifest_sha256"],
            "proposals": [
                {
                    "candidate_id": CANDIDATE_ID,
                    "status": (
                        "classified" if classified else "no_suggestion"
                    ),
                    "predictions": [prediction] if classified else [],
                }
            ],
        },
        separators=(",", ":"),
    ).encode("utf-8")


def test_fixed_runner_uses_pinned_manifest_and_never_receives_raw_command(
    tmp_path,
):
    fixture = _runner_fixture(tmp_path)
    captured = {}

    def process_runner(argv, *, input, cwd, env, timeout):
        request = json.loads(input.decode("utf-8"))
        captured.update(
            argv=argv,
            request=request,
            input_text=input.decode("utf-8"),
            cwd=cwd,
            env=env,
            timeout=timeout,
        )
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout=_response_for_request(request),
            stderr=b"",
        )

    generator = FixedManifestCommandProposalGenerator(
        fixture["root"],
        trusted_manifest_sha256=fixture["manifest_sha256"],
        process_runner=process_runner,
    )
    result = generator.generate(
        project_id=PROJECT_ID,
        analysis_job_id=JOB_ID,
        recipe_id=RECIPE_ID,
        command_plan=compile_labeling_command(
            "표지판을 가린 수풀을 찾아줘"
        ),
        candidates=[{"candidate_id": CANDIDATE_ID}],
        resolve_image=_resolver(fixture),
    )

    assert result.available is True
    assert result.generated is True
    assert result.evaluated_candidate_count == 1
    assert len(result.proposals) == 1
    assert result.proposals[0]["status"] == "classified"
    assert result.model["provider"] == "transformers_grounding_dino"
    assert result.evaluation["evaluation_id"] == f"cmd-{RECIPE_ID}"
    assert captured["argv"] == [
        str(
            fixture["root"]
            / "trafficsupervision-command-proposal-runner.exe"
        )
    ]
    assert captured["request"]["project_id"] == str(PROJECT_ID)
    assert captured["request"]["analysis_job_id"] == str(JOB_ID)
    assert captured["request"]["recipe_id"] == RECIPE_ID
    assert "command" not in captured["request"]
    assert "표지판을 가린 수풀을 찾아줘" not in captured["input_text"]
    assert captured["env"]["TRANSFORMERS_OFFLINE"] == "1"


def test_no_suggestion_is_counted_but_never_returned_for_sidecar_storage(
    tmp_path,
):
    fixture = _runner_fixture(tmp_path)

    def process_runner(argv, *, input, cwd, env, timeout):
        del cwd, env, timeout
        request = json.loads(input.decode("utf-8"))
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout=_response_for_request(request, classified=False),
            stderr=b"",
        )

    result = FixedManifestCommandProposalGenerator(
        fixture["root"],
        trusted_manifest_sha256=fixture["manifest_sha256"],
        process_runner=process_runner,
    ).generate(
        project_id=PROJECT_ID,
        analysis_job_id=JOB_ID,
        recipe_id=RECIPE_ID,
        command_plan=compile_labeling_command("포트홀을 찾아줘"),
        candidates=[{"candidate_id": CANDIDATE_ID}],
        resolve_image=_resolver(fixture),
    )

    assert result.evaluated_candidate_count == 1
    assert result.proposals == ()


def test_unpinned_or_tampered_runner_manifest_cannot_execute(tmp_path):
    fixture = _runner_fixture(tmp_path)
    called = False

    def process_runner(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("untrusted runner must not execute")

    manifest_path = fixture["root"] / "runner-manifest.json"
    manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")
    generator = FixedManifestCommandProposalGenerator(
        fixture["root"],
        trusted_manifest_sha256=fixture["manifest_sha256"],
        process_runner=process_runner,
    )
    with pytest.raises(
        CommandProposalGenerationError,
        match="not trusted",
    ):
        generator.generate(
            project_id=PROJECT_ID,
            analysis_job_id=JOB_ID,
            recipe_id=RECIPE_ID,
            command_plan=compile_labeling_command("포트홀을 찾아줘"),
            candidates=[{"candidate_id": CANDIDATE_ID}],
            resolve_image=_resolver(fixture),
        )
    assert called is False


def test_runner_response_cannot_cross_job_scope(tmp_path):
    fixture = _runner_fixture(tmp_path)

    def process_runner(argv, *, input, cwd, env, timeout):
        del cwd, env, timeout
        request = json.loads(input.decode("utf-8"))
        response = json.loads(_response_for_request(request))
        response["analysis_job_id"] = (
            "33333333-3333-4333-8333-333333333333"
        )
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout=json.dumps(response).encode("utf-8"),
            stderr=b"",
        )

    generator = FixedManifestCommandProposalGenerator(
        fixture["root"],
        trusted_manifest_sha256=fixture["manifest_sha256"],
        process_runner=process_runner,
    )
    with pytest.raises(
        CommandProposalGenerationError,
        match="crossed its requested scope",
    ):
        generator.generate(
            project_id=PROJECT_ID,
            analysis_job_id=JOB_ID,
            recipe_id=RECIPE_ID,
            command_plan=compile_labeling_command("포트홀을 찾아줘"),
            candidates=[{"candidate_id": CANDIDATE_ID}],
            resolve_image=_resolver(fixture),
        )


def test_program_data_discovery_is_honestly_unavailable_without_build_pin(
    tmp_path,
):
    generator = FixedManifestCommandProposalGenerator.from_program_data(
        program_data=str(tmp_path.resolve())
    )
    assert isinstance(generator, UnavailableCommandProposalGenerator)
