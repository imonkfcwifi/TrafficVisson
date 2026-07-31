from __future__ import annotations

import hashlib
import json
from pathlib import Path
import platform
import sys

import pytest

from app.external_model_runtime import (
    ExternalModelRuntimeError,
    MODEL_RUNTIME_ENVIRONMENT_KEY,
    activate_external_model_runtime,
    activate_external_model_runtime_from_environment,
    external_model_runtime_status,
    load_external_model_runtime_pack,
    _reset_external_model_runtime_for_tests,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_pack(root: Path, *, cache_tag: str | None = None) -> Path:
    site = root / "runtime" / "site-packages"
    site.mkdir(parents=True)
    (root / "RUNTIME-NOTICE.txt").write_text(
        "Reviewed local runtime notice.\n",
        encoding="utf-8",
    )
    package_rows = []
    for name, version, import_name in (
        ("numpy", "2.2.6", "numpy"),
        ("opencv-python-headless", "4.11.0.86", "cv2"),
    ):
        module = site / import_name
        module.mkdir()
        (module / "__init__.py").write_text(
            f'__version__ = "{version}"\n',
            encoding="utf-8",
        )
        dist = site / f"{name.replace('-', '_')}-{version}.dist-info"
        dist.mkdir()
        (dist / "METADATA").write_text(
            "\n".join(
                (
                    "Metadata-Version: 2.4",
                    f"Name: {name}",
                    f"Version: {version}",
                    "",
                )
            ),
            encoding="utf-8",
        )
        (dist / "LICENSE").write_text(
            f"{name} fixture licence\n",
            encoding="utf-8",
        )
        package_rows.append(
            {
                "name": name,
                "version": version,
                "spdx": "BSD-3-Clause",
                "notice_artifact": (
                    f"runtime/site-packages/{dist.name}/LICENSE"
                ),
            }
        )
    artifact_paths = sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file()
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    manifest = {
        "schema_version": (
            "trafficvisson.external-model-runtime-pack.v1"
        ),
        "runtime_id": "trafficvisson-test-opencv-runtime",
        "runtime_version": "0.1.0",
        "source": {
            "repository": "https://example.invalid/runtime-build",
            "revision": "1" * 40,
        },
        "license": {
            "runtime_spdx": "BSD-3-Clause",
            "commercial_use_confirmed": True,
            "redistribution_allowed": False,
            "license_url": "https://example.invalid/runtime-license",
            "notice_artifact": "RUNTIME-NOTICE.txt",
        },
        "python": {
            "implementation": "cpython",
            "cache_tag": cache_tag or sys.implementation.cache_tag,
            "version": f"{sys.version_info.major}.{sys.version_info.minor}",
        },
        "platform": {
            "system": platform.system(),
            "machine": (
                "AMD64"
                if platform.machine().casefold() in {"amd64", "x86_64"}
                else platform.machine()
            ),
        },
        "hardware": {
            "requires_cuda": False,
            "cpu_fallback": True,
            "minimum_cuda_version": None,
            "minimum_vram_bytes": 0,
        },
        "capabilities": ["opencv"],
        "packages": package_rows,
        "paths": {
            "site_packages": "runtime/site-packages",
            "dll_directories": [],
        },
        "artifacts": [
            {
                "path": path.relative_to(root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path in artifact_paths
        ],
    }
    manifest_path = (
        root / "trafficsupervision-external-model-runtime-pack.json"
    )
    manifest_path.write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return manifest_path


@pytest.fixture(autouse=True)
def _reset_runtime():
    _reset_external_model_runtime_for_tests()
    yield
    _reset_external_model_runtime_for_tests()


def test_closed_runtime_pack_verifies_abi_packages_licenses_and_hashes(
    tmp_path,
):
    manifest = _write_pack(tmp_path / "pack")

    pack = load_external_model_runtime_pack(manifest)

    assert pack.runtime_id == "trafficvisson-test-opencv-runtime"
    assert pack.capabilities == ("opencv",)
    assert pack.python_cache_tag == sys.implementation.cache_tag
    assert pack.runtime_spdx == "BSD-3-Clause"
    assert pack.commercial_use_confirmed is True
    assert len(pack.artifacts) == 7
    assert pack.manifest_sha256 == _sha256(manifest)
    assert str(tmp_path) not in repr(dict(pack.public_identity))


def test_runtime_pack_rejects_tamper_and_unlisted_files(tmp_path):
    first_manifest = _write_pack(tmp_path / "tampered")
    target = (
        first_manifest.parent
        / "runtime"
        / "site-packages"
        / "cv2"
        / "__init__.py"
    )
    target.write_text("tampered = True\n", encoding="utf-8")

    with pytest.raises(
        ExternalModelRuntimeError,
        match="failed verification",
    ) as tampered:
        load_external_model_runtime_pack(first_manifest)
    assert tampered.value.code == "runtime_artifact_hash_mismatch"

    second_manifest = _write_pack(tmp_path / "unlisted")
    (
        second_manifest.parent
        / "runtime"
        / "site-packages"
        / "unexpected.py"
    ).write_text("raise RuntimeError\n", encoding="utf-8")
    with pytest.raises(
        ExternalModelRuntimeError,
        match="inventory is not closed",
    ) as unlisted:
        load_external_model_runtime_pack(second_manifest)
    assert unlisted.value.code == "runtime_inventory_mismatch"


def test_runtime_pack_rejects_python_abi_mismatch(tmp_path):
    manifest = _write_pack(tmp_path / "pack", cache_tag="cp999")

    with pytest.raises(
        ExternalModelRuntimeError,
        match="ABI does not match",
    ) as mismatch:
        load_external_model_runtime_pack(manifest)
    assert mismatch.value.code == "runtime_python_abi_mismatch"


def test_activation_is_path_free_and_forwards_only_configured_manifest(
    tmp_path,
):
    manifest = _write_pack(tmp_path / "pack")

    status = activate_external_model_runtime(
        manifest,
        runtime_probe=lambda _pack: {
            "cuda_verified": False,
            "cuda_version": None,
            "cuda_total_memory_bytes": None,
        },
    )

    assert status["state"] == "ready"
    assert status["manifest_sha256"] == _sha256(manifest)
    assert status["capabilities"] == ["opencv"]
    assert str(tmp_path) not in repr(status)
    assert str(manifest.parent / "runtime" / "site-packages") in sys.path

    _reset_external_model_runtime_for_tests()
    from_environment = activate_external_model_runtime_from_environment(
        {MODEL_RUNTIME_ENVIRONMENT_KEY: str(manifest)}
    )
    # The default probe imports the two tiny fixture modules from the verified
    # runtime directory and does not need ambient ML packages.
    assert from_environment["state"] == "ready"
    assert external_model_runtime_status()["state"] == "ready"


def test_invalid_configured_runtime_fails_closed_without_leaking_path(
    tmp_path,
):
    manifest = _write_pack(tmp_path / "private-pack")
    manifest.write_text("{}", encoding="utf-8")

    status = activate_external_model_runtime_from_environment(
        {MODEL_RUNTIME_ENVIRONMENT_KEY: str(manifest)}
    )

    assert status == {
        "state": "failed",
        "error_code": "runtime_manifest_invalid",
    }
    assert str(tmp_path) not in repr(status)


def test_frozen_worker_dispatch_stops_after_runtime_activation_failure(
    monkeypatch,
):
    import run_bridge

    monkeypatch.setenv(
        MODEL_RUNTIME_ENVIRONMENT_KEY,
        r"C:\invalid\runtime-pack.json",
    )
    monkeypatch.setattr(
        run_bridge,
        "MODEL_RUNTIME_STATUS",
        {"state": "failed"},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["TrafficSuperVisionBridge.exe", "--vision-worker"],
    )

    with pytest.raises(SystemExit) as stopped:
        run_bridge._run_internal_worker()

    assert stopped.value.code == 78
