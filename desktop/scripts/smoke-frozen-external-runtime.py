"""Smoke the frozen bridge with a tiny verified external runtime pack.

This fixture proves the packaging boundary only. It contains pure-Python
stand-ins for NumPy/OpenCV and does not claim real inference or CUDA.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request


READY_PREFIX = "TRAFFICSUPERVISION_BRIDGE_READY "


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_pack(root: Path) -> Path:
    site = root / "runtime" / "site-packages"
    site.mkdir(parents=True)
    (root / "NOTICE.txt").write_text(
        "Frozen external runtime smoke only.\n",
        encoding="utf-8",
    )
    packages = []
    for name, version, module_name in (
        ("numpy", "2.2.6", "numpy"),
        ("opencv-python-headless", "4.11.0.86", "cv2"),
    ):
        module = site / module_name
        module.mkdir()
        (module / "__init__.py").write_text(
            f'__version__ = "{version}"\n',
            encoding="utf-8",
        )
        dist = site / f"{name.replace('-', '_')}-{version}.dist-info"
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
            f"{name} smoke licence\n",
            encoding="utf-8",
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
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    manifest = {
        "schema_version": (
            "trafficvisson.external-model-runtime-pack.v1"
        ),
        "runtime_id": "trafficvisson-frozen-smoke-runtime",
        "runtime_version": "0.1.0",
        "source": {
            "repository": "https://example.invalid/frozen-smoke-runtime",
            "revision": "3" * 40,
        },
        "license": {
            "runtime_spdx": "BSD-3-Clause",
            "commercial_use_confirmed": True,
            "redistribution_allowed": False,
            "license_url": "https://example.invalid/frozen-smoke-license",
            "notice_artifact": "NOTICE.txt",
        },
        "python": {
            "implementation": "cpython",
            "cache_tag": sys.implementation.cache_tag,
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
        "packages": packages,
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
            for path in files
        ],
    }
    manifest_path = (
        root / "trafficsupervision-external-model-runtime-pack.json"
    )
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return manifest_path


def _readline_with_timeout(
    process: subprocess.Popen[str],
    timeout_seconds: float,
) -> str:
    lines: queue.Queue[str] = queue.Queue(maxsize=1)

    def read() -> None:
        lines.put(process.stdout.readline() if process.stdout else "")

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    try:
        return lines.get(timeout=timeout_seconds)
    except queue.Empty as error:
        raise RuntimeError("frozen external-runtime readiness timed out") from error


def main() -> int:
    desktop_root = Path(__file__).resolve().parents[1]
    bundle = (
        desktop_root
        / "build"
        / "bridge"
        / "TrafficSuperVisionBridge"
    )
    executable = bundle / "TrafficSuperVisionBridge.exe"
    if not executable.is_file():
        raise RuntimeError("frozen bridge executable is missing")
    smoke_root = Path(
        tempfile.mkdtemp(prefix="TrafficSuperVisionExternalRuntimeSmoke-")
    )
    process: subprocess.Popen[str] | None = None
    try:
        manifest = _create_pack(smoke_root / "runtime-pack")
        local_data = smoke_root / "local-data"
        local_data.mkdir()
        token = (
            "frozen-external-runtime-smoke-token-with-at-least-256-bits"
        )
        environment = dict(os.environ)
        environment.update(
            {
                "TRAFFICSUPERVISION_BRIDGE_TOKEN": token,
                "TRAFFICSUPERVISION_EXTERNAL_MODEL_RUNTIME_PACK": (
                    str(manifest)
                ),
                "LOCALAPPDATA": str(local_data),
                "PATH": f"{bundle}{os.pathsep}{environment.get('PATH', '')}",
            }
        )
        environment.pop("TRAFFICSUPERVISION_VISION_MODEL_PACK", None)
        environment.pop("TRAFFICSUPERVISION_DFINE_TRAINING_RUNTIME", None)
        environment.pop(
            "TRAFFICSUPERVISION_DFINE_TRAINING_RUNTIME_SHA256",
            None,
        )
        process = subprocess.Popen(
            [str(executable)],
            cwd=str(bundle),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        line = _readline_with_timeout(process, 30.0).strip()
        if not line.startswith(READY_PREFIX):
            stderr = process.stderr.read(500) if process.stderr else ""
            raise RuntimeError(
                f"frozen external-runtime readiness failed: {stderr}"
            )
        ready = json.loads(line[len(READY_PREFIX) :])
        request = urllib.request.Request(
            (
                f"http://127.0.0.1:{int(ready['port'])}"
                "/runtime/readiness"
            ),
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        components = {
            row["component_id"]: row for row in payload["components"]
        }
        if components["opencv"]["state"] != "ready":
            raise RuntimeError(
                "frozen bridge did not activate the external runtime pack"
            )
        if str(smoke_root) in repr(payload):
            raise RuntimeError(
                "runtime readiness leaked the external runtime path"
            )
        print(
            json.dumps(
                {
                    "frozen_bridge": "ready",
                    "external_runtime": "ready",
                    "opencv": components["opencv"]["state"],
                    "pytorch_cuda": components["pytorch_cuda"]["state"],
                    "path_leaked": False,
                },
                separators=(",", ":"),
            )
        )
        return 0
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        shutil.rmtree(smoke_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
