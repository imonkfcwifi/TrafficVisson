"""Launch the private TrafficSuperVision bridge on an OS-assigned loopback port."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from typing import Optional, Tuple

BRIDGE_ROOT = Path(__file__).resolve().parent
if str(BRIDGE_ROOT) not in sys.path:
    # Python is launched with -I, so only this verified application directory is added.
    sys.path.insert(0, str(BRIDGE_ROOT))

from app.external_model_runtime import (  # noqa: E402
    activate_external_model_runtime_from_environment,
)

# The frozen bridge remains independently usable when no runtime pack is
# configured. A configured pack is verified and activated before app modules
# import any optional ML dependency; invalid packs fail closed by capability
# while the core bridge can still report the error and serve non-ML workflows.
MODEL_RUNTIME_STATUS = activate_external_model_runtime_from_environment()

import uvicorn

from app.main import create_app


def _run_internal_worker() -> bool:
    """Dispatch fixed internal subprocess modes in frozen and source builds."""
    if (
        len(sys.argv) >= 2
        and sys.argv[1]
        in {"--vision-worker", "--grounding-dino-pack-worker"}
        and os.environ.get(
            "TRAFFICSUPERVISION_EXTERNAL_MODEL_RUNTIME_PACK"
        )
        and MODEL_RUNTIME_STATUS.get("state") != "ready"
    ):
        # The parent bridge remains available to report a failed runtime pack,
        # but model workers must never continue with partially imported or
        # ambient dependencies after activation failed.
        raise SystemExit(78)
    if len(sys.argv) >= 2 and sys.argv[1] == "--vision-worker":
        from app.vision_worker import main as vision_worker_main

        raise SystemExit(vision_worker_main(sys.argv[2:]))
    if (
        len(sys.argv) >= 2
        and sys.argv[1] == "--grounding-dino-pack-worker"
    ):
        from app.grounding_dino_pack_worker import (
            main as grounding_dino_worker_main,
        )

        raise SystemExit(grounding_dino_worker_main())
    return False


def _take_token() -> str:
    token = os.environ.pop("TRAFFICSUPERVISION_BRIDGE_TOKEN", "")
    if len(token) < 43:
        raise RuntimeError("Bridge token is missing or invalid")
    return token


def _take_data_root() -> Optional[str]:
    value = os.environ.pop("TRAFFICSUPERVISION_BRIDGE_DATA_ROOT", "").strip()
    if not value:
        return None
    selected = Path(value)
    if not selected.is_absolute():
        raise RuntimeError("Bridge data root must be absolute")
    return str(selected.resolve())


def _frozen_media_tool_paths() -> Tuple[Optional[str], Optional[str]]:
    """Resolve only the FFmpeg copies shipped beside the frozen bridge."""
    if not getattr(sys, "frozen", False):
        return None, None
    bundle_root = Path(sys.executable).resolve(strict=True).parent
    resolved_tools = []
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        candidate = bundle_root / name
        if candidate.is_symlink() or not candidate.is_file():
            raise RuntimeError(f"Bundled {name} is missing or invalid")
        resolved = candidate.resolve(strict=True)
        if resolved.parent != bundle_root or resolved.name.casefold() != name:
            raise RuntimeError(f"Bundled {name} escaped the bridge directory")
        resolved_tools.append(str(resolved))
    return resolved_tools[0], resolved_tools[1]


async def _run() -> None:
    token = _take_token()
    bridge_data_root = _take_data_root()
    ffmpeg_path, ffprobe_path = _frozen_media_tool_paths()
    application = create_app(
        token,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        bridge_data_root=bridge_data_root,
    )
    listen_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen_socket.bind(("127.0.0.1", 0))
    listen_socket.listen(128)
    listen_socket.setblocking(False)
    port = listen_socket.getsockname()[1]

    config = uvicorn.Config(
        application,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        log_config=None,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[listen_socket]))
    while not server.started and not task.done():
        await asyncio.sleep(0.01)
    if task.done():
        await task
        raise RuntimeError("Bridge server stopped before readiness")

    ready = {
        "host": "127.0.0.1",
        "port": port,
        "protocol": "http",
    }
    print(
        "TRAFFICSUPERVISION_BRIDGE_READY " + json.dumps(ready, separators=(",", ":")),
        flush=True,
    )
    await task


if __name__ == "__main__":
    _run_internal_worker()
    asyncio.run(_run())
