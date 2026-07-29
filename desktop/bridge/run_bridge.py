"""Launch the private TrafficVisson bridge on an OS-assigned loopback port."""

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

import uvicorn

from app.main import create_app


def _run_internal_worker() -> bool:
    """Dispatch fixed internal subprocess modes in frozen and source builds."""
    if len(sys.argv) >= 2 and sys.argv[1] == "--vision-worker":
        from app.vision_worker import main as vision_worker_main

        raise SystemExit(vision_worker_main(sys.argv[2:]))
    return False


def _take_token() -> str:
    token = os.environ.pop("TRAFFICVISSON_BRIDGE_TOKEN", "")
    if len(token) < 43:
        raise RuntimeError("Bridge token is missing or invalid")
    return token


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
    ffmpeg_path, ffprobe_path = _frozen_media_tool_paths()
    application = create_app(
        token,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
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
        "TRAFFICVISSON_BRIDGE_READY " + json.dumps(ready, separators=(",", ":")),
        flush=True,
    )
    await task


if __name__ == "__main__":
    _run_internal_worker()
    asyncio.run(_run())
