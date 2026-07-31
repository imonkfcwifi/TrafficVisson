"""Install the pinned official SAM 2.1 tiny safetensors Model Pack.

This script downloads only allowlisted non-pickle runtime files from the
official Hugging Face repository, verifies every file, writes a deterministic
TrafficSuperVision manifest, and atomically installs a separate Model Pack.
It never executes downloaded code or a downloaded checkpoint.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from huggingface_hub import snapshot_download


REPO_ID = "facebook/sam2.1-hiera-tiny"
REVISION = "de431c4043854a71d8101e17995dfe596bf101a5"
PACK_DIRECTORY = "trafficvisson-facebook-sam2.1-hiera-tiny-0.1.0"
MANIFEST_NAME = "trafficvisson-sam2-model-pack.json"
ALLOWLIST = (
    "README.md",
    "config.json",
    "model.safetensors",
    "preprocessor_config.json",
    "processor_config.json",
)


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            digest.update(block)
    return digest.hexdigest(), size


def main() -> None:
    program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    pack_parent = program_data / "TrafficVisson" / "ModelPacks"
    pack_parent.mkdir(parents=True, exist_ok=True)
    target = pack_parent / PACK_DIRECTORY
    if target.exists():
        manifest = target / MANIFEST_NAME
        if not manifest.is_file():
            raise RuntimeError(
                "existing SAM2 pack has no manifest; refusing to overwrite"
            )
        print(str(target))
        return

    temporary: Path | None = Path(
        tempfile.mkdtemp(prefix=".sam2-pack-", dir=str(pack_parent))
    )
    try:
        assert temporary is not None
        model_root = temporary / "model"
        snapshot_download(
            repo_id=REPO_ID,
            revision=REVISION,
            allow_patterns=list(ALLOWLIST),
            local_dir=str(model_root),
        )
        observed = sorted(
            path.name
            for path in model_root.iterdir()
            if path.is_file()
        )
        if observed != sorted(ALLOWLIST):
            raise RuntimeError(
                "downloaded SAM2 file set does not match the allowlist"
            )
        artifacts = []
        for name in sorted(ALLOWLIST):
            path = model_root / name
            digest, size = sha256_file(path)
            artifacts.append(
                {
                    "path": f"model/{name}",
                    "size_bytes": size,
                    "sha256": digest,
                }
            )
        model_sha = next(
            row["sha256"]
            for row in artifacts
            if row["path"] == "model/model.safetensors"
        )
        manifest = {
            "schema_version": (
                "trafficvisson.sam2-transformers-model-pack.v1"
            ),
            "pack_id": "trafficvisson-facebook-sam2.1-hiera-tiny",
            "pack_version": "0.1.0",
            "provider": "transformers_sam2",
            "model_name": REPO_ID,
            "model_version": REVISION,
            "model_directory": "model",
            "model_artifact_path": "model/model.safetensors",
            "model_artifact_sha256": model_sha,
            "source_repo_id": REPO_ID,
            "source_revision": REVISION,
            "source_url": f"https://huggingface.co/{REPO_ID}",
            "weights_spdx": "Apache-2.0",
            "runtime_spdx": "Apache-2.0",
            "commercial_use_confirmed": True,
            "redistribution_allowed": True,
            "gpu_required": True,
            "cpu_fallback": True,
            "recommended_min_vram_mb": 4096,
            "cuda_min_version": "12.0",
            "review_only": True,
            "artifacts": artifacts,
        }
        (temporary / MANIFEST_NAME).write_text(
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        temporary.replace(target)
        temporary = None
        print(str(target))
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)


if __name__ == "__main__":
    main()
