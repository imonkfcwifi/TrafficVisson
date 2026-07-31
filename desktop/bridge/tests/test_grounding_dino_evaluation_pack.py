from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest

from app.analysis_jobs import ResolvedAnalysisArtifact
from app.auto_labeling import compile_labeling_command
from app.command_proposal_generation import (
    CommandProposalGenerationError,
)
from app import grounding_dino_evaluation_pack as module
from app.grounding_dino_evaluation_pack import (
    SCORE_SEMANTICS,
    WORKER_RESPONSE_SCHEMA,
    VerifiedGroundingDinoEvaluationPackProposalGenerator,
)


PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
JOB_ID = UUID("22222222-2222-4222-8222-222222222222")
CANDIDATE_ID = "0123456789abcdefabcd"


def _write_pack(tmp_path: Path, monkeypatch):
    root = (
        tmp_path
        / "trafficvisson-idea-grounding-dino-tiny-evaluation-0.1.0"
    )
    snapshot = root / "snapshot"
    snapshot.mkdir(parents=True)
    artifacts = {
        "snapshot/config.json": b'{"model_type":"grounding-dino"}\n',
        "snapshot/model.safetensors": b"fixture model",
        "snapshot/tokenizer.json": b'{"fixture":"tokenizer"}\n',
        "MODEL-SOURCE.md": b"fixture source\n",
    }
    rows = []
    for relative, body in artifacts.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        rows.append(
            {
                "path": relative,
                "size_bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        )
    queries = [
        {
            "query_id": "pothole",
            "prompt": "a pothole in the road",
            "candidate_type": "pothole_candidate",
        },
        {
            "query_id": "road_surface_damage",
            "prompt": "a broken or cracked road surface",
            "candidate_type": "road_surface_damage_candidate",
        },
        {
            "query_id": "road_debris",
            "prompt": "a fallen object or debris on the road",
            "candidate_type": "road_debris_candidate",
        },
        {
            "query_id": "barrier_damage",
            "prompt": "a damaged median barrier or damaged guardrail",
            "candidate_type": "barrier_damage_candidate",
        },
        {
            "query_id": "traffic_sign",
            "prompt": "a traffic sign",
            "candidate_type": "traffic_sign_candidate",
        },
        {
            "query_id": "vegetation_obstruction",
            "prompt": "excessive roadside vegetation blocking the view",
            "candidate_type": "vegetation_obstruction_candidate",
        },
        {
            "query_id": "tunnel_entrance_sign",
            "prompt": "a sign at a tunnel entrance",
            "candidate_type": "tunnel_entrance_sign_candidate",
        },
    ]
    manifest = {
        "artifacts": rows,
        "automatic_labeling_allowed": False,
        "created_at": "2026-07-28T11:32:56Z",
        "device_policy": {
            "cpu_fallback": True,
            "maximum_cuda_allocator_bytes": 4 * 1024**3,
            "maximum_frames_per_run": 32,
            "maximum_predictions_per_frame": 8,
            "preferred": "cuda",
            "target_device": "fixture",
        },
        "incident_creation_allowed": False,
        "inference_contract": {
            "excluded_state_claims": [
                "maintenance_action_confirmed"
            ],
            "local_files_only": True,
            "model_class": "AutoModelForZeroShotObjectDetection",
            "processor_class": "AutoProcessor",
            "queries": queries,
            "score_semantics": SCORE_SEMANTICS,
            "snapshot_directory": "snapshot",
            "trust_remote_code": False,
            "use_safetensors": True,
        },
        "installer_distribution": "excluded",
        "license": {
            "notice": "fixture Apache-2.0",
            "redistribution_enabled": False,
            "runtime_spdx": "Apache-2.0",
            "weights_spdx": "Apache-2.0",
        },
        "model_promotion_allowed": False,
        "pack_id": module.PACK_ID,
        "pack_version": module.PACK_VERSION,
        "purpose": "local_open_vocabulary_detector_evaluation_only",
        "review_only": True,
        "runtime_provenance_at_install": {
            "fixture": True,
        },
        "schema_version": module.EVALUATION_PACK_SCHEMA,
        "sidecar_recording_allowed": False,
        "snapshot_files": [
            row for row in rows if row["path"].startswith("snapshot/")
        ],
        "source": {
            "license_spdx": "Apache-2.0",
            "model_card_url": "https://example.test/model",
            "repo_id": module.SOURCE_REPO_ID,
            "requested_revision": module.SOURCE_REVISION,
            "resolved_revision": module.SOURCE_REVISION,
            "snapshot_file_count": 3,
            "snapshot_total_bytes": sum(
                row["size_bytes"]
                for row in rows
                if row["path"].startswith("snapshot/")
            ),
            "transformers_documentation_url": (
                "https://example.test/docs"
            ),
        },
    }
    manifest_path = root / module.PACK_MANIFEST_NAME
    raw = json.dumps(
        manifest,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    manifest_path.write_bytes(raw)
    monkeypatch.setattr(
        module,
        "MODEL_ARTIFACT_SHA256",
        hashlib.sha256(artifacts["snapshot/model.safetensors"]).hexdigest(),
    )
    return {
        "root": root.resolve(),
        "manifest": manifest_path.resolve(),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
    }


def test_verified_evaluation_pack_batches_fixed_queries_as_review_only(
    tmp_path,
    monkeypatch,
):
    fixture = _write_pack(tmp_path, monkeypatch)
    image = tmp_path / "candidate.jpg"
    image.write_bytes(b"\xff\xd8\xfffixture")
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    captured = {}

    def worker(request):
        captured.update(request)
        return {
            "schema_version": WORKER_RESPONSE_SCHEMA,
            "project_id": str(PROJECT_ID),
            "analysis_job_id": str(JOB_ID),
            "recipe_id": "a" * 64,
            "pack_manifest_sha256": fixture["manifest_sha256"],
            "score_semantics": SCORE_SEMANTICS,
            "results": [
                {
                    "candidate_id": CANDIDATE_ID,
                    "status": "classified",
                    "predictions": [
                        {
                            "issue_type": "road_debris",
                            "relative_score": None,
                            "model_score": 0.71,
                            "box": {
                                "x": 0.1,
                                "y": 0.2,
                                "width": 0.3,
                                "height": 0.4,
                            },
                            "source_label": "debris on road",
                            "calibration": "model_score",
                        }
                    ],
                }
            ],
        }

    generator = (
        VerifiedGroundingDinoEvaluationPackProposalGenerator(
            fixture["manifest"],
            trusted_manifest_sha256=fixture["manifest_sha256"],
            worker_runner=worker,
        )
    )

    def resolve(*_args):
        return ResolvedAnalysisArtifact(
            path=image.resolve(),
            media_type="image/jpeg",
            size_bytes=image.stat().st_size,
            sha256=digest,
        )

    result = generator.generate_taxonomy(
        project_id=PROJECT_ID,
        analysis_job_id=JOB_ID,
        recipe_id="a" * 64,
        command_plans=[
            compile_labeling_command(
                "review road debris",
                explicit_issue_type="road_debris",
            ),
            compile_labeling_command(
                "review traffic sign",
                explicit_issue_type="guide_sign_identity",
            ),
        ],
        candidates=[{"candidate_id": CANDIDATE_ID}],
        resolve_image=resolve,
    )

    assert result.available is True
    assert result.generated is True
    assert result.evaluated_candidate_count == 1
    assert len(result.proposals) == 1
    assert result.proposals[0]["predictions"][0]["issue_type"] == (
        "road_debris"
    )
    assert {row["issue_type"] for row in captured["queries"]} == {
        "road_debris",
        "guide_sign_identity",
    }
    assert captured["pack_manifest_sha256"] == fixture["manifest_sha256"]
    assert captured["candidates"][0]["image_sha256"] == digest
    status = generator.status()
    assert status["state"] == "ready"
    assert status["review_only"] is True
    assert status["model_pack"]["provenance_verified"] is True
    assert status["model_pack"]["model_artifact_sha256"] == (
        module.MODEL_ARTIFACT_SHA256
    )
    assert "path" not in json.dumps(status)


def test_evaluation_pack_manifest_is_out_of_band_pinned(
    tmp_path,
    monkeypatch,
):
    fixture = _write_pack(tmp_path, monkeypatch)
    fixture["manifest"].write_bytes(
        fixture["manifest"].read_bytes() + b"\n"
    )
    with pytest.raises(
        CommandProposalGenerationError,
        match="not trusted",
    ):
        VerifiedGroundingDinoEvaluationPackProposalGenerator(
            fixture["manifest"],
            trusted_manifest_sha256=fixture["manifest_sha256"],
            worker_runner=lambda _request: {},
        )


def test_evaluation_pack_never_supports_relationship_state_prompts(
    tmp_path,
    monkeypatch,
):
    fixture = _write_pack(tmp_path, monkeypatch)
    generator = (
        VerifiedGroundingDinoEvaluationPackProposalGenerator(
            fixture["manifest"],
            trusted_manifest_sha256=fixture["manifest_sha256"],
            worker_runner=lambda _request: {},
        )
    )
    with pytest.raises(
        CommandProposalGenerationError,
        match="unsupported",
    ):
        generator.generate_taxonomy(
            project_id=PROJECT_ID,
            analysis_job_id=JOB_ID,
            recipe_id="b" * 64,
            command_plans=[
                compile_labeling_command(
                    "review vegetation relation",
                    explicit_issue_type="sign_vegetation_occlusion",
                )
            ],
            candidates=[{"candidate_id": CANDIDATE_ID}],
            resolve_image=lambda *_args: (_ for _ in ()).throw(
                AssertionError("unsupported plans must fail before images")
            ),
        )
