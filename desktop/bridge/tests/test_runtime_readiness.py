from __future__ import annotations

from importlib import metadata

from fastapi.testclient import TestClient

from app.main import create_app
from app.provider_status import CommandResult, ProviderStatusService
from app.runtime_readiness import (
    RUNTIME_READINESS_SCHEMA,
    RuntimeReadinessService,
)


def _verified_pack(**overrides):
    value = {
        "provenance_verified": True,
        "provider": "transformers_grounding_dino",
        "pack_id": "trafficvisson-grounding-dino",
        "pack_version": "0.2.0-review",
        "manifest_sha256": "a" * 64,
        "model_name": "Grounding DINO Tiny ONNX Q4",
        "model_version": "1" * 40,
        "model_artifact_sha256": "b" * 64,
        "source_repo_id": "onnx-community/grounding-dino-tiny-ONNX",
        "source_revision": "2" * 40,
        "weights_spdx": "Apache-2.0",
        "runtime_spdx": "MIT",
        "commercial_use_confirmed": True,
        "redistribution_allowed": True,
        "gpu_required": False,
        "cpu_fallback": True,
        "recommended_min_vram_mb": 4096,
        "cuda_min_version": None,
        "review_only": True,
    }
    value.update(overrides)
    return value


def _provider_service(tmp_path):
    codex = tmp_path / "codex.exe"
    claude = tmp_path / "claude.exe"
    codex.write_bytes(b"MZ")
    claude.write_bytes(b"MZ")

    def runner(argv, **_kwargs):
        if argv[-1] == "--version":
            return CommandResult(returncode=0, stdout=b"codex-cli 0.136.0")
        if "codex.exe" in argv[0]:
            return CommandResult(
                returncode=0, stdout=b"Logged in using ChatGPT"
            )
        return CommandResult(
            returncode=0,
            stdout=b'{"loggedIn":false,"version":"2.1.145"}',
        )

    return ProviderStatusService(
        explicit_paths={"codex": codex, "claude": claude},
        runner=runner,
        environ={"OPENAI_API_KEY": "must-not-pass"},
    )


def test_readiness_keeps_local_detection_and_cli_judgment_boundaries(
    tmp_path,
):
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"MZ")
    versions = {
        "torch": "2.7.1+cu128",
        "opencv-python-headless": "4.11.0.86",
        "easyocr": "1.7.2",
        "transformers": "4.53.2",
    }

    def distribution_version(name):
        if name not in versions:
            raise metadata.PackageNotFoundError(name)
        return versions[name]

    service = RuntimeReadinessService(
        provider_status=_provider_service(tmp_path),
        vision_status=lambda: {
            "enabled": False,
            "mode": "scene_fallback",
            "warning_code": "model_pack_not_configured",
        },
        command_model_status=lambda: {
            "state": "ready",
            "provider": "transformers_grounding_dino",
            "pack_id": "trafficvisson-grounding-dino",
            "pack_version": "0.2.0-review",
            "manifest_sha256": "a" * 64,
            "review_only": True,
            "model_pack": _verified_pack(),
        },
        hwp_preflight=lambda: {
            "available": False,
            "message": "한컴오피스 설치가 필요합니다.",
        },
        ffmpeg_path=str(ffmpeg),
        distribution_version=distribution_version,
        python_version="3.9.13",
        electron_version="43.2.0",
    )

    snapshot = service.snapshot()

    assert snapshot["schema_version"] == RUNTIME_READINESS_SCHEMA
    assert snapshot["provider_policy"] == {
        "bulk_frame_detection": "local_models_only",
        "structured_judgment_providers": ["codex", "claude"],
        "provider_authentication_owner": "official_cli",
        "authentication_files_read_by_app": False,
        "local_model_provider": "disabled",
    }
    assert snapshot["pipeline_outcome_contract"] == {
        "model_unavailable": "unavailable",
        "model_runtime_failed": "failed",
        "successful_zero_predictions": "zero_detections",
    }
    rows = {row["component_id"]: row for row in snapshot["components"]}
    assert len(rows) == 13
    assert rows["grounding_dino"]["state"] == "ready"
    assert rows["grounding_dino"]["model_pack"]["manifest_sha256"] == "a" * 64
    assert rows["grounding_dino"]["model_pack"]["source_revision"] == "2" * 40
    assert (
        rows["grounding_dino"]["model_pack"]["recommended_min_vram_mb"]
        == 4096
    )
    assert rows["sam2"]["state"] == "unavailable"
    assert rows["pytorch_cuda"]["state"] == "incomplete"
    assert rows["korean_ocr"]["state"] == "incomplete"
    assert rows["embedding_search"]["state"] == "incomplete"
    assert rows["codex_cli"]["state"] == "ready"
    assert rows["claude_code"]["state"] == "external_required"
    assert rows["local_model_provider"]["state"] == "disabled"
    assert "OPENAI_API_KEY" not in repr(snapshot)
    assert str(tmp_path) not in repr(snapshot)


def test_ready_claim_without_full_model_pack_provenance_is_not_accepted(
    tmp_path,
):
    service = RuntimeReadinessService(
        provider_status=ProviderStatusService(which=lambda _name: None),
        vision_status=lambda: {
            "enabled": True,
            "capabilities": ["ocr"],
            "pack_id": "legacy-ocr-pack",
            "pack_version": "1.0.0",
            "manifest_sha256": "c" * 64,
            "weights_spdx": "Apache-2.0",
            "runtime_spdx": "MIT",
        },
        command_model_status=lambda: {
            "state": "ready",
            "pack_id": "legacy-dino-pack",
            "pack_version": "1.0.0",
            "manifest_sha256": "d" * 64,
            "review_only": True,
        },
        sam2_status=lambda: {
            "state": "ready",
            "model_pack": {
                "pack_id": "legacy-sam2-pack",
                "pack_version": "1.0.0",
                "manifest_sha256": "e" * 64,
            },
        },
        hwp_preflight=lambda: {"available": False},
        ffmpeg_path=None,
        distribution_version=lambda name: (_ for _ in ()).throw(
            metadata.PackageNotFoundError(name)
        ),
        which=lambda _name: None,
    )

    rows = {
        row["component_id"]: row
        for row in service.snapshot()["components"]
    }

    assert rows["grounding_dino"]["state"] == "incomplete"
    assert rows["grounding_dino"]["error_code"] == (
        "model_pack_provenance_incomplete"
    )
    assert rows["korean_ocr"]["state"] == "incomplete"
    assert rows["korean_ocr"]["error_code"] == (
        "model_pack_provenance_incomplete"
    )
    assert rows["sam2"]["state"] == "failed"
    assert rows["sam2"]["error_code"] == "model_pack_invalid"
    assert all(
        "model_pack" not in rows[component_id]
        for component_id in ("grounding_dino", "korean_ocr", "sam2")
    )


def test_invalid_model_pack_is_failed_not_unavailable_or_zero_detection(
    tmp_path,
):
    service = RuntimeReadinessService(
        provider_status=ProviderStatusService(which=lambda _name: None),
        vision_status=lambda: {
            "enabled": False,
            "warning_code": "model_pack_invalid",
        },
        command_model_status=lambda: {
            "state": "failed",
            "error_code": "model_pack_invalid",
        },
        hwp_preflight=lambda: {"available": True},
        ffmpeg_path=None,
        distribution_version=lambda name: (_ for _ in ()).throw(
            metadata.PackageNotFoundError(name)
        ),
        which=lambda _name: None,
    )

    rows = {
        row["component_id"]: row
        for row in service.snapshot()["components"]
    }

    assert rows["grounding_dino"]["state"] == "failed"
    assert rows["grounding_dino"]["error_code"] == "model_pack_invalid"
    assert rows["grounding_dino"]["state"] != "unavailable"
    assert rows["grounding_dino"]["state"] != "zero_detections"
    assert rows["hancom_com"]["state"] == "ready"
    assert rows["electron_host"]["state"] == "incomplete"
    assert rows["electron_host"]["error_code"] == (
        "electron_version_not_reported"
    )


def test_verified_external_runtime_is_required_for_pytorch_cuda_ready(
    tmp_path,
):
    versions = {"torch": "2.7.0+cu128"}

    def distribution_version(name):
        if name not in versions:
            raise metadata.PackageNotFoundError(name)
        return versions[name]

    common = {
        "provider_status": ProviderStatusService(which=lambda _name: None),
        "vision_status": lambda: {"enabled": False},
        "command_model_status": lambda: {"state": "unavailable"},
        "hwp_preflight": lambda: {"available": False},
        "distribution_version": distribution_version,
        "which": lambda _name: None,
    }
    ready = RuntimeReadinessService(
        **common,
        model_runtime_status=lambda: {
            "state": "ready",
            "capabilities": ["pytorch_cuda", "detector_training"],
            "manifest_sha256": "f" * 64,
            "cuda_verified": True,
        },
    )
    failed = RuntimeReadinessService(
        **common,
        model_runtime_status=lambda: {
            "state": "failed",
            "error_code": "runtime_artifact_hash_mismatch",
        },
    )

    ready_row = {
        row["component_id"]: row
        for row in ready.snapshot()["components"]
    }["pytorch_cuda"]
    failed_row = {
        row["component_id"]: row
        for row in failed.snapshot()["components"]
    }["pytorch_cuda"]

    assert ready_row["state"] == "ready"
    assert ready_row["version"] == "2.7.0+cu128"
    assert ready_row.get("error_code") is None
    assert failed_row["state"] == "failed"
    assert failed_row["error_code"] == "model_runtime_pack_invalid"
    assert str(tmp_path) not in repr(ready_row)


def test_runtime_status_probe_failures_are_failed_without_leaking_details(
    tmp_path,
):
    def broken_vision_status():
        raise RuntimeError(r"C:\private\vision-pack\secret.bin")

    service = RuntimeReadinessService(
        provider_status=ProviderStatusService(which=lambda _name: None),
        vision_status=broken_vision_status,
        command_model_status=lambda: (_ for _ in ()).throw(
            RuntimeError("token=must-not-leak")
        ),
        hwp_preflight=lambda: {"available": False},
        distribution_version=lambda name: (_ for _ in ()).throw(
            metadata.PackageNotFoundError(name)
        ),
        which=lambda _name: None,
    )

    snapshot = service.snapshot()
    rows = {
        row["component_id"]: row for row in snapshot["components"]
    }

    assert rows["grounding_dino"]["state"] == "failed"
    assert rows["grounding_dino"]["error_code"] == (
        "model_pack_status_failed"
    )
    assert rows["korean_ocr"]["state"] == "failed"
    assert rows["embedding_search"]["state"] == "failed"
    assert "private" not in repr(snapshot)
    assert "token" not in repr(snapshot).casefold()


def test_runtime_readiness_endpoint_is_authenticated_and_schema_bounded(
    tmp_path,
):
    base_service = RuntimeReadinessService(
        provider_status=ProviderStatusService(which=lambda _name: None),
        vision_status=lambda: {
            "enabled": False,
            "warning_code": "model_pack_not_configured",
        },
        command_model_status=lambda: {"state": "unavailable"},
        hwp_preflight=lambda: {"available": False},
        distribution_version=lambda name: (_ for _ in ()).throw(
            metadata.PackageNotFoundError(name)
        ),
        which=lambda _name: None,
    )
    token = "runtime-readiness-test-token-with-at-least-32-bytes"
    app = create_app(
        token,
        fixture_mode=True,
        allow_test_client=True,
        bridge_data_root=str(tmp_path / "bridge-data"),
        runtime_readiness_service=base_service,
    )

    with TestClient(app) as client:
        assert client.get("/runtime/readiness").status_code == 401
        response = client.get(
            "/runtime/readiness",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == RUNTIME_READINESS_SCHEMA
    assert len(payload["components"]) == 13
    assert payload["provider_policy"]["bulk_frame_detection"] == (
        "local_models_only"
    )
