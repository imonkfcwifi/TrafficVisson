"""Install a pinned, evaluation-only pothole ONNX model pack.

This helper deliberately does not activate the pack and does not edit an
existing TrafficVisson analysis record.  It downloads one safelisted ONNX file
from an immutable Hugging Face revision, verifies the published digest, and
creates a hash-inventoried model-pack directory for local review experiments.

The upstream repository declares Apache-2.0 metadata but does not currently
ship a standalone licence file.  TrafficVisson therefore marks redistribution
as disabled and keeps this pack out of release builds pending a separate legal
and in-domain validation review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterable


REPOSITORY_ID = "peterhdd/pothole-detection-yolov8"
REPOSITORY_REVISION = "da7747eea7abb4319a0f55961f31809a16c1b10a"
REMOTE_FILENAME = "best.onnx"
EXPECTED_MODEL_SHA256 = (
    "91dd7de7a110c61314ea19a958fcc85c7b3461cc6d60d87fefc08b41ac6e32c5"
)
EXPECTED_OPSET21_SHA256 = (
    "5680df5b80b8eabe6efa55237255426d6b533362ec27fdadcae78ed303ef98d9"
)
PACK_DIRECTORY_NAME = "trafficvisson-peterhdd-yolov8s-pothole-research-0.1.1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _artifact(path: Path, relative_path: str) -> dict[str, object]:
    return {
        "path": relative_path,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _default_destination_root() -> Path:
    program_data = os.environ.get("PROGRAMDATA")
    if not program_data:
        raise RuntimeError("PROGRAMDATA is unavailable")
    return Path(program_data) / "TrafficVisson" / "ModelPacks"


def _source_notice() -> str:
    return f"""# Experimental pothole detector source

- Repository: https://huggingface.co/{REPOSITORY_ID}
- Pinned revision: `{REPOSITORY_REVISION}`
- File: `{REMOTE_FILENAME}`
- Published SHA-256: `{EXPECTED_MODEL_SHA256}`
- Local compatibility transform: ONNX opset 22 to opset 21 with `onnx==1.19.1`
- Derived model SHA-256: `{EXPECTED_OPSET21_SHA256}`
- Repository metadata licence: `Apache-2.0`
- Runtime used by TrafficVisson: ONNX Runtime (`MIT`)

The upstream model card describes a one-class YOLOv8s pothole detector.  The
repository metadata declares Apache-2.0, but the repository snapshot inspected
on 2026-07-28 did not include a standalone licence file.  For that reason this
pack is limited to local research evaluation, is not redistributed, is not
included in the installer, and cannot be promoted without legal review and a
fixed in-domain Korean-road validation set.

Model output is an uncalibrated proposal.  It never becomes an approved
incident, maintenance decision, training label, or report input without a
user's explicit answer in the question review flow.
"""


def _manifest(model_path: Path, notice_path: Path) -> dict[str, object]:
    notice = (
        "Pinned one-class pothole ONNX detector from "
        f"{REPOSITORY_ID}@{REPOSITORY_REVISION}. Repository metadata declares "
        "Apache-2.0; redistribution is disabled pending legal review. "
        "Evaluation-only proposals require explicit user confirmation."
    )
    return {
        "schema_version": "trafficvisson.vision-model-pack.v1",
        "pack_id": "trafficvisson-peterhdd-yolov8s-pothole-research",
        "pack_version": "0.1.1",
        "engine": "onnx_yolo_v8",
        "license": {
            "runtime_spdx": "MIT",
            "weights_spdx": "Apache-2.0",
            "commercial_use_confirmed": True,
            "redistribution_allowed": False,
            "notice": notice,
        },
        "artifacts": [
            _artifact(model_path, "model.onnx"),
            _artifact(notice_path, "MODEL-SOURCE.md"),
        ],
        "model": {
            "file": "model.onnx",
            "input_width": 640,
            "input_height": 640,
            "labels": [
                {
                    "class_id": 0,
                    "issue_type": "pothole",
                    "display_name": "포트홀 실험 제안",
                }
            ],
            "confidence_threshold": 0.25,
            "nms_iou_threshold": 0.45,
            "max_detections": 16,
            "execution_provider": "cpu",
        },
    }


def _verify_existing(target: Path) -> Path:
    manifest = target / "trafficvisson-model-pack.json"
    model = target / "model.onnx"
    notice = target / "MODEL-SOURCE.md"
    if not manifest.is_file() or not model.is_file() or not notice.is_file():
        raise RuntimeError(
            f"Refusing to overwrite incomplete existing pack: {target}"
        )
    if _sha256(model) != EXPECTED_OPSET21_SHA256:
        raise RuntimeError(
            f"Existing model digest does not match the pinned transform: {target}"
        )
    return manifest


def install(destination_root: Path) -> Path:
    destination_root = destination_root.resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    target = destination_root / PACK_DIRECTORY_NAME
    if target.exists():
        return _verify_existing(target)

    try:
        from huggingface_hub import hf_hub_download
    except ImportError as error:
        raise RuntimeError("huggingface_hub is required for this installer") from error
    try:
        import onnx
        from onnx import checker, version_converter
    except ImportError as error:
        raise RuntimeError(
            "onnx==1.19.1 is required to create the opset-21 evaluation pack"
        ) from error
    if onnx.__version__ != "1.19.1":
        raise RuntimeError(
            "onnx==1.19.1 is required for a reproducible model transform"
        )

    temporary = Path(
        tempfile.mkdtemp(prefix=f".{PACK_DIRECTORY_NAME}-", dir=destination_root)
    )
    try:
        downloaded = Path(
            hf_hub_download(
                repo_id=REPOSITORY_ID,
                filename=REMOTE_FILENAME,
                revision=REPOSITORY_REVISION,
            )
        )
        if _sha256(downloaded) != EXPECTED_MODEL_SHA256:
            raise RuntimeError("Downloaded model digest did not match the pinned source")
        try:
            source_model = onnx.load(str(downloaded), load_external_data=False)
            converted_model = version_converter.convert_version(source_model, 21)
            checker.check_model(converted_model)
        except Exception as error:
            raise RuntimeError(
                "The pinned model could not be converted to ONNX opset 21"
            ) from error
        model_path = temporary / "model.onnx"
        onnx.save_model(converted_model, str(model_path))
        if _sha256(model_path) != EXPECTED_OPSET21_SHA256:
            raise RuntimeError(
                "Converted model digest did not match the reviewed transform"
            )

        notice_path = temporary / "MODEL-SOURCE.md"
        notice_path.write_text(
            _source_notice(),
            encoding="utf-8",
        )
        manifest_path = temporary / "trafficvisson-model-pack.json"
        _write_json(manifest_path, _manifest(model_path, notice_path))
        temporary.replace(target)
        return target / manifest_path.name
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install the pinned evaluation-only pothole ONNX model pack."
    )
    parser.add_argument(
        "--destination-root",
        type=Path,
        default=None,
        help="ModelPacks parent directory (defaults to ProgramData).",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    destination = arguments.destination_root or _default_destination_root()
    manifest = install(destination)
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
