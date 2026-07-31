from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from app.external_model_runtime import load_external_model_runtime_pack


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "install-external-model-runtime-pack.py"
)


def _installer_module():
    spec = importlib.util.spec_from_file_location(
        "trafficvisson_external_runtime_installer",
        SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source_tree(root: Path) -> tuple[Path, list[dict[str, str]]]:
    root.mkdir()
    (root / "RUNTIME-NOTICE.txt").write_text(
        "runtime notice\n", encoding="utf-8"
    )
    packages = []
    for name, version, import_name in (
        ("numpy", "2.2.6", "numpy"),
        ("opencv-python-headless", "4.11.0.86", "cv2"),
    ):
        module = root / import_name
        module.mkdir()
        (module / "__init__.py").write_text(
            f'__version__ = "{version}"\n',
            encoding="utf-8",
        )
        dist = root / f"{name.replace('-', '_')}-{version}.dist-info"
        dist.mkdir()
        (dist / "METADATA").write_text(
            (
                "Metadata-Version: 2.4\n"
                f"Name: {name}\n"
                f"Version: {version}\n"
            ),
            encoding="utf-8",
        )
        (dist / "LICENSE").write_text(
            f"{name} licence\n", encoding="utf-8"
        )
        packages.append(
            {
                "name": name,
                "version": version,
                "spdx": "BSD-3-Clause",
                "notice_artifact": (
                    f"runtime/site-packages/{dist.name}/LICENSE"
                ),
            }
        )
    return root, packages


def test_installer_publishes_versioned_closed_runtime_atomically(tmp_path):
    source, packages = _source_tree(tmp_path / "site-packages")
    descriptor = {
        "schema_version": (
            "trafficvisson.external-model-runtime-build.v1"
        ),
        "runtime_id": "trafficvisson-opencv-runtime",
        "runtime_version": "0.1.0",
        "source": {
            "repository": "https://example.invalid/locked-wheelhouse",
            "revision": "2" * 40,
        },
        "license": {
            "runtime_spdx": "BSD-3-Clause",
            "commercial_use_confirmed": True,
            "redistribution_allowed": False,
            "license_url": "https://example.invalid/runtime-license",
            "notice_artifact": (
                "runtime/site-packages/RUNTIME-NOTICE.txt"
            ),
        },
        "hardware": {
            "requires_cuda": False,
            "cpu_fallback": True,
            "minimum_cuda_version": None,
            "minimum_vram_bytes": 0,
        },
        "capabilities": ["opencv"],
        "packages": packages,
        "dll_directories": [],
    }
    descriptor_path = tmp_path / "runtime-build.json"
    descriptor_path.write_text(
        json.dumps(descriptor, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    destination = tmp_path / "installed"
    installer = _installer_module()

    manifest = installer.install(
        source,
        descriptor_path,
        destination,
    )
    verified = load_external_model_runtime_pack(manifest)
    repeated = installer.install(
        source,
        descriptor_path,
        destination,
    )

    assert repeated == manifest
    assert verified.runtime_id == "trafficvisson-opencv-runtime"
    assert manifest.parent.name == "trafficvisson-opencv-runtime-0.1.0"
    assert not any(
        path.name.startswith(".trafficvisson-opencv-runtime-")
        for path in destination.iterdir()
    )
