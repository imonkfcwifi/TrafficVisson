"""Install the pinned official D-FINE-small base as a local training Model Pack.

Only allowlisted Apache-2.0 repository files are downloaded.  The installer
copies them into a closed directory, inventories every byte with SHA-256,
declares TrafficSuperVision's canonical road-review project head, and validates the
result with the production loader before publishing it atomically.

The installed base remains COCO-pretrained.  The local CUDA trainer verifies
that immutable backbone and deliberately reinitializes only the project label
head when the user explicitly starts training.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from huggingface_hub import hf_hub_download


REPO_ID = "ustc-community/dfine-small-coco"
REVISION = "f79e65b5fbb33ceb9d3ebba042955d7410c608f8"
PACK_ID = "trafficvisson-ustc-dfine-small-road-training"
PACK_VERSION = "0.1.0"
PACK_DIRECTORY = f"{PACK_ID}-{PACK_VERSION}"
MANIFEST_NAME = "trafficsupervision-detector-training-pack.json"
ALLOWLIST = (
    "README.md",
    "config.json",
    "model.safetensors",
    "preprocessor_config.json",
)
ROAD_LABELS = (
    "pothole",
    "road_surface_damage",
    "road_debris",
    "median_barrier_damage",
    "sign_damage",
    "sign_vegetation_occlusion",
    "roadside_mowing_required",
    "guide_sign_identity",
    "tunnel_identity",
    "tunnel_name_occlusion",
    "lane_marking_faded",
    "colored_guidance_faded",
)


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            block = stream.read(4 * 1024 * 1024)
            if not block:
                break
            size += len(block)
            digest.update(block)
    return digest.hexdigest(), size


def _artifact(path: Path, root: Path) -> dict[str, object]:
    digest, size = _sha256(path)
    return {
        "path": path.relative_to(root).as_posix(),
        "size_bytes": size,
        "sha256": digest,
    }


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def install(destination_root: Path) -> Path:
    destination_root.mkdir(parents=True, exist_ok=True)
    destination_root = destination_root.resolve(strict=True)
    target = destination_root / PACK_DIRECTORY
    if target.exists():
        manifest = target / MANIFEST_NAME
        if not manifest.is_file():
            raise RuntimeError(
                "existing detector training pack is incomplete; "
                "refusing to overwrite it"
            )
        import sys

        bridge_root = Path(__file__).resolve().parents[1] / "bridge"
        sys.path.insert(0, str(bridge_root))
        from app.local_object_detector_trainer import (
            load_local_detector_model_pack,
        )

        load_local_detector_model_pack(manifest)
        return manifest

    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{PACK_ID}-",
            dir=str(destination_root),
        )
    )
    try:
        model_root = temporary / "model"
        model_root.mkdir()
        for name in ALLOWLIST:
            cached = Path(
                hf_hub_download(
                    repo_id=REPO_ID,
                    filename=name,
                    revision=REVISION,
                )
            ).resolve(strict=True)
            destination = model_root / name
            shutil.copyfile(cached, destination)
            if _sha256(destination) != _sha256(cached):
                raise RuntimeError(
                    f"downloaded detector file failed copy verification: {name}"
                )

        notice = temporary / "MODEL-SOURCE.md"
        with notice.open(
            "w",
            encoding="utf-8",
            newline="\n",
        ) as stream:
            stream.write(
                "\n".join(
                    (
                    "# D-FINE small training base",
                    "",
                    f"- Model: {REPO_ID}",
                    f"- Immutable revision: {REVISION}",
                    f"- Source: https://huggingface.co/{REPO_ID}",
                    "- Weights license: Apache-2.0",
                    "- Runtime: Hugging Face Transformers, Apache-2.0",
                    "- Original head: COCO classes",
                    (
                        "- TrafficSuperVision use: the verified backbone is "
                        "loaded offline and only the local project head is "
                        "reinitialized during explicitly approved CUDA training."
                    ),
                    "- No Hub upload and no remote code execution.",
                    "",
                    )
                )
            )
        inventoried_files = [
            notice,
            *(model_root / name for name in ALLOWLIST),
        ]
        artifacts = [
            _artifact(path, temporary)
            for path in sorted(
                inventoried_files,
                key=lambda item: item.relative_to(
                    temporary
                ).as_posix(),
            )
        ]
        manifest_value = {
            "schema_version": (
                "trafficvisson.local-object-detector-training-pack.v1"
            ),
            "pack_id": PACK_ID,
            "pack_version": PACK_VERSION,
            "architecture": "dfine-small",
            "model_id": REPO_ID,
            "model_version": REVISION,
            "source": {
                "repository": f"https://huggingface.co/{REPO_ID}",
                "revision": REVISION,
            },
            "license": {
                "weights_spdx": "Apache-2.0",
                "runtime_spdx": "Apache-2.0",
                "commercial_use_confirmed": True,
                "redistribution_allowed": True,
                "license_url": (
                    "https://www.apache.org/licenses/LICENSE-2.0"
                ),
                "notice_artifact": "MODEL-SOURCE.md",
            },
            "artifacts": artifacts,
            "model": {
                "directory": "model",
                "labels": [
                    {
                        "class_id": class_id,
                        "issue_type": issue_type,
                    }
                    for class_id, issue_type in enumerate(ROAD_LABELS)
                ],
            },
            "hardware": {
                "requires_cuda": True,
                "minimum_cuda_version": "12.1",
                "minimum_vram_bytes": 8 * 1024 * 1024 * 1024,
            },
            "training": {
                "image_size": 640,
                "train_batch_size": 1,
                "gradient_accumulation_steps": 8,
                "epochs": 30,
                "learning_rate": 0.00005,
                "score_threshold": 0.25,
                "iou_threshold": 0.5,
            },
        }
        manifest = temporary / MANIFEST_NAME
        manifest.write_bytes(_canonical_json(manifest_value))

        import sys

        bridge_root = Path(__file__).resolve().parents[1] / "bridge"
        sys.path.insert(0, str(bridge_root))
        from app.local_object_detector_trainer import (
            load_local_detector_model_pack,
        )

        pack = load_local_detector_model_pack(manifest)
        if (
            pack.pack_id != PACK_ID
            or pack.source_revision != REVISION
            or tuple(value for _key, value in pack.labels) != ROAD_LABELS
        ):
            raise RuntimeError("detector Model Pack contract mismatch")
        os.replace(temporary, target)
        return target / MANIFEST_NAME
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--destination-root",
        default=str(
            Path(
                os.environ.get("PROGRAMDATA", r"C:\ProgramData")
            )
            / "TrafficVisson"
            / "ModelPacks"
        ),
    )
    args = parser.parse_args()
    print(install(Path(args.destination_root)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
