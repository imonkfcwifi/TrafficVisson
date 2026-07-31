"""Install the already-present EasyOCR Korean weights as a verified Model Pack.

The installer is deliberately offline.  It copies only the two EasyOCR files
needed by ``Reader(['ko', 'en'])`` from the user's existing EasyOCR cache,
records their hashes, and publishes an immutable TrafficVisson vision pack.
It never downloads or executes content from a webpage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path


PACK_ID = "trafficvisson-jaidedai-easyocr-ko-en"
PACK_VERSION = "0.1.0"
MODEL_FILES = ("craft_mlt_25k.pth", "korean_g2.pth")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(4 * 1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _artifact(path: Path, root: Path) -> dict:
    return {
        "path": path.relative_to(root).as_posix(),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def install(source: Path, destination_root: Path) -> Path:
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise RuntimeError("EasyOCR model cache is not a directory")
    sources = []
    for name in MODEL_FILES:
        path = (source / name).resolve(strict=True)
        if path.parent != source or not path.is_file():
            raise RuntimeError(f"EasyOCR model file is missing: {name}")
        sources.append(path)

    destination_root.mkdir(parents=True, exist_ok=True)
    destination_root = destination_root.resolve(strict=True)
    final = destination_root / (
        f"{PACK_ID}-{PACK_VERSION}"
    )
    if final.exists():
        manifest = final / "trafficvisson-model-pack.json"
        if manifest.is_file():
            return manifest
        raise RuntimeError("Existing EasyOCR pack is incomplete")

    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{PACK_ID}-",
            dir=str(destination_root),
        )
    )
    try:
        model_root = temporary / "models"
        model_root.mkdir()
        for source_path in sources:
            destination = model_root / source_path.name
            shutil.copyfile(source_path, destination)
            if (
                destination.stat().st_size
                != source_path.stat().st_size
                or _sha256(destination) != _sha256(source_path)
            ):
                raise RuntimeError("EasyOCR model copy verification failed")
        source_note = temporary / "MODEL-SOURCE.md"
        with source_note.open(
            "w",
            encoding="utf-8",
            newline="\n",
        ) as stream:
            stream.write(
                "\n".join(
                    (
                        "# EasyOCR Korean/English model source",
                        "",
                        "- Runtime: JaidedAI/EasyOCR (Apache-2.0)",
                        "- Repository: https://github.com/JaidedAI/EasyOCR",
                        "- Model hub: https://www.jaided.ai/easyocr/modelhub/",
                        "- Files: craft_mlt_25k.pth, korean_g2.pth",
                        "- Installation: copied from an already-present local "
                        "EasyOCR cache; no network download was performed.",
                        "- Redistribution: disabled pending project legal review.",
                        "",
                    )
                )
            )
        artifacts = [
            _artifact(path, temporary)
            for path in sorted(
                (
                    source_note,
                    *(model_root / name for name in MODEL_FILES),
                ),
                key=lambda item: item.relative_to(
                    temporary
                ).as_posix(),
            )
        ]
        manifest = {
            "schema_version": "trafficvisson.vision-model-pack.v1",
            "pack_id": PACK_ID,
            "pack_version": PACK_VERSION,
            "engine": "easyocr_ocr_only",
            "license": {
                "runtime_spdx": "Apache-2.0",
                "weights_spdx": "Apache-2.0",
                "commercial_use_confirmed": True,
                "redistribution_allowed": False,
                "notice": (
                    "The official EasyOCR repository is Apache-2.0 and "
                    "publishes the Korean/CRAFT weights through its model "
                    "hub. Pack redistribution stays disabled pending legal "
                    "review."
                ),
            },
            "artifacts": artifacts,
            "ocr": {
                "engine": "easyocr",
                "model_storage_directory": "models",
                "languages": ["ko", "en"],
                "minimum_confidence": 0.25,
                "max_observations": 32,
                "device": "cuda",
            },
        }
        manifest_path = temporary / "trafficvisson-model-pack.json"
        manifest_path.write_bytes(_canonical_json(manifest))

        # Validate with the same production loader before publishing.
        import sys

        bridge_root = Path(__file__).resolve().parents[1] / "bridge"
        sys.path.insert(0, str(bridge_root))
        from app.vision_inference import load_model_pack

        pack = load_model_pack(manifest_path)
        if (
            pack.pack_id != PACK_ID
            or pack.pack_version != PACK_VERSION
            or pack.capabilities != ("ocr",)
        ):
            raise RuntimeError("EasyOCR Model Pack contract mismatch")
        os.replace(temporary, final)
        return final / "trafficvisson-model-pack.json"
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        default=str(Path.home() / ".EasyOCR" / "model"),
    )
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
    manifest = install(
        Path(args.source),
        Path(args.destination_root),
    )
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
