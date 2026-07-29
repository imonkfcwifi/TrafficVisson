"""Install a pinned, evaluation-only Grounding DINO Tiny model pack.

The pack is intentionally excluded from the TrafficVisson installer and active
model registry.  It exists only for local, review-only research evaluations.
No downloaded file is executed and Transformers is later required to load the
snapshot with ``local_files_only=True`` and ``trust_remote_code=False``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import stat
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence


PACK_SCHEMA = "trafficvisson.grounding-dino-evaluation-pack.v1"
PACK_ID = "trafficvisson-idea-grounding-dino-tiny-evaluation"
PACK_VERSION = "0.1.0"
REPO_ID = "IDEA-Research/grounding-dino-tiny"
IMMUTABLE_REVISION = "a2bb814dd30d776dcf7e30523b00659f4f141c71"
MODEL_CARD_URL = "https://huggingface.co/IDEA-Research/grounding-dino-tiny"
TRANSFORMERS_DOC_URL = (
    "https://huggingface.co/docs/transformers/model_doc/grounding-dino"
)
MODEL_FILE_SHA256 = (
    "1a2412ef99bd74bcd3c2a246fa1e48581f8889a1300c9051974741314fc042f3"
)
MAX_DOWNLOAD_SECONDS = 1_800.0
MIN_FREE_RESERVE_BYTES = 1 * 1024**3
MAX_PACK_BYTES = 2 * 1024**3

SNAPSHOT_FILES = {
    "README.md": 2_580,
    "added_tokens.json": 82,
    "config.json": 1_644,
    "model.safetensors": 689_359_096,
    "preprocessor_config.json": 457,
    "special_tokens_map.json": 125,
    "tokenizer.json": 711_396,
    "tokenizer_config.json": 1_237,
    "vocab.txt": 231_508,
}
SNAPSHOT_TOTAL_BYTES = sum(SNAPSHOT_FILES.values())

QUERY_CONTRACT = [
    {
        "query_id": "pothole",
        "prompt": "a pothole in the road",
        "candidate_type": "pothole_candidate",
    },
    {
        "query_id": "road_surface_damage",
        "prompt": "a broken or cracked road surface",
        "candidate_type": "road_surface_damage_candidate",
    },
    {
        "query_id": "road_debris",
        "prompt": "a fallen object or debris on the road",
        "candidate_type": "road_debris_candidate",
    },
    {
        "query_id": "barrier_damage",
        "prompt": "a damaged median barrier or damaged guardrail",
        "candidate_type": "barrier_damage_candidate",
    },
    {
        "query_id": "traffic_sign",
        "prompt": "a traffic sign",
        "candidate_type": "traffic_sign_candidate",
    },
    {
        "query_id": "vegetation_obstruction",
        "prompt": "excessive roadside vegetation blocking the view",
        "candidate_type": "vegetation_obstruction_candidate",
    },
    {
        "query_id": "tunnel_entrance_sign",
        "prompt": "a sign at a tunnel entrance",
        "candidate_type": "tunnel_entrance_sign_candidate",
    },
]


class PackInstallError(RuntimeError):
    """Safe operator-facing install failure."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _safe_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise PackInstallError(f"{label} is unavailable") from error
    if path.is_symlink() or _is_reparse_point(path) or not resolved.is_dir():
        raise PackInstallError(f"{label} is unsafe")
    return resolved


def _safe_file(path: Path, *, maximum_bytes: int) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = path.stat()
    except OSError as error:
        raise PackInstallError("A pack file is unavailable") from error
    if (
        path.is_symlink()
        or _is_reparse_point(path)
        or not resolved.is_file()
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= maximum_bytes
    ):
        raise PackInstallError("A pack file is unsafe")
    return resolved


def _sha256(path: Path, *, maximum_bytes: int = MAX_PACK_BYTES) -> str:
    before = path.stat()
    if before.st_size > maximum_bytes:
        raise PackInstallError("A pack file exceeds its size bound")
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while block := stream.read(4 * 1024 * 1024):
                total += len(block)
                if total > maximum_bytes:
                    raise PackInstallError("A pack file exceeds its size bound")
                digest.update(block)
        after = path.stat()
    except OSError as error:
        raise PackInstallError("A pack file could not be read") from error
    if (
        total != before.st_size
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise PackInstallError("A pack file changed while it was hashed")
    return digest.hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _runtime_provenance() -> dict[str, Any]:
    versions: dict[str, Optional[str]] = {}
    modules: dict[str, Any] = {}
    for name in ("torch", "transformers", "huggingface_hub", "safetensors"):
        try:
            module = __import__(name)
        except Exception:
            versions[name] = None
            continue
        modules[name] = module
        versions[name] = str(getattr(module, "__version__", "unknown"))
    torch = modules.get("torch")
    cuda_available = False
    cuda_version = None
    device_count = 0
    device_name = None
    device_total_memory_bytes = None
    if torch is not None:
        try:
            cuda_available = bool(torch.cuda.is_available())
            cuda_version = torch.version.cuda
            device_count = int(torch.cuda.device_count())
            if cuda_available:
                device_name = str(torch.cuda.get_device_name(0))
                device_total_memory_bytes = int(
                    torch.cuda.get_device_properties(0).total_memory
                )
        except Exception:
            cuda_available = False
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "versions": versions,
        "torch_cuda_available": cuda_available,
        "torch_cuda_version": cuda_version,
        "cuda_device_count": device_count,
        "preferred_cuda_device_name": device_name,
        "preferred_cuda_total_memory_bytes": device_total_memory_bytes,
    }


def _model_info() -> dict[str, Any]:
    try:
        from huggingface_hub import HfApi
    except Exception as error:
        raise PackInstallError("huggingface_hub is required for provenance") from error
    try:
        info = HfApi().model_info(
            REPO_ID,
            revision=IMMUTABLE_REVISION,
            files_metadata=True,
        )
    except Exception as error:
        raise PackInstallError("The pinned Hugging Face revision is unavailable") from error
    siblings = {
        row.rfilename: int(row.size)
        for row in info.siblings
        if row.rfilename in SNAPSHOT_FILES and row.size is not None
    }
    return {
        "repo_id": info.id,
        "resolved_revision": info.sha,
        "private": bool(info.private),
        "gated": bool(info.gated),
        "tags": list(info.tags or []),
        "files": siblings,
    }


def _validate_model_info(value: Mapping[str, Any]) -> None:
    if (
        value.get("repo_id") != REPO_ID
        or value.get("resolved_revision") != IMMUTABLE_REVISION
        or value.get("private") is not False
        or value.get("gated") not in {False, None}
        or value.get("files") != SNAPSHOT_FILES
        or "license:apache-2.0" not in value.get("tags", [])
    ):
        raise PackInstallError(
            "The pinned Hugging Face snapshot metadata did not match"
        )


def _download_file(
    remote_name: str,
    destination: Path,
    *,
    expected_size: int,
    deadline: float,
) -> None:
    quoted_name = "/".join(
        urllib.parse.quote(part, safe="") for part in remote_name.split("/")
    )
    url = (
        f"https://huggingface.co/{REPO_ID}/resolve/"
        f"{IMMUTABLE_REVISION}/{quoted_name}?download=true"
    )
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "TrafficVisson-evaluation-pack/0.1.0"},
    )
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status != 200:
                raise PackInstallError("A pinned model file download failed")
            with destination.open("xb") as stream:
                while block := response.read(4 * 1024 * 1024):
                    if time.monotonic() > deadline:
                        raise PackInstallError(
                            "The model download exceeded its time bound"
                        )
                    total += len(block)
                    if total > expected_size:
                        raise PackInstallError(
                            "A downloaded model file exceeded its declared size"
                        )
                    stream.write(block)
                stream.flush()
                os.fsync(stream.fileno())
    except PackInstallError:
        raise
    except Exception as error:
        raise PackInstallError("A pinned model file download failed") from error
    if total != expected_size:
        raise PackInstallError("A downloaded model file size did not match")


def _inventory_file(root: Path, path: Path) -> dict[str, Any]:
    safe = _safe_file(path, maximum_bytes=MAX_PACK_BYTES)
    return {
        "path": safe.relative_to(root).as_posix(),
        "size_bytes": safe.stat().st_size,
        "sha256": _sha256(safe),
    }


def _verify_existing(target: Path) -> dict[str, Any]:
    manifest_path = _safe_file(
        target / "trafficvisson-grounding-dino-pack.json",
        maximum_bytes=512 * 1024,
    )
    try:
        payload = json.loads(manifest_path.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PackInstallError("The existing evaluation pack manifest is invalid") from error
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != PACK_SCHEMA
        or payload.get("pack_id") != PACK_ID
        or payload.get("pack_version") != PACK_VERSION
        or payload.get("source", {}).get("resolved_revision")
        != IMMUTABLE_REVISION
        or payload.get("review_only") is not True
        or payload.get("automatic_labeling_allowed") is not False
        or payload.get("sidecar_recording_allowed") is not False
    ):
        raise PackInstallError("The existing evaluation pack identity is invalid")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise PackInstallError("The existing evaluation pack inventory is invalid")
    seen: set[str] = set()
    for row in artifacts:
        if not isinstance(row, dict) or set(row) != {
            "path",
            "size_bytes",
            "sha256",
        }:
            raise PackInstallError("An existing pack artifact row is invalid")
        relative = row["path"]
        if (
            not isinstance(relative, str)
            or relative in seen
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
        ):
            raise PackInstallError("An existing pack artifact path is invalid")
        seen.add(relative)
        path = _safe_file(target / relative, maximum_bytes=MAX_PACK_BYTES)
        if (
            path.stat().st_size != row["size_bytes"]
            or _sha256(path) != row["sha256"]
        ):
            raise PackInstallError("An existing pack artifact failed verification")
    return payload


def install_pack(
    *,
    program_data: Optional[Path | str] = None,
    maximum_download_seconds: float = MAX_DOWNLOAD_SECONDS,
    info_provider: Callable[[], Mapping[str, Any]] = _model_info,
    downloader: Callable[..., None] = _download_file,
) -> tuple[Path, dict[str, Any]]:
    if (
        isinstance(maximum_download_seconds, bool)
        or not isinstance(maximum_download_seconds, (int, float))
        or not math.isfinite(float(maximum_download_seconds))
        or not 30 <= float(maximum_download_seconds) <= 7_200
    ):
        raise PackInstallError("The download time bound is invalid")
    base = (
        Path(program_data)
        if program_data is not None
        else Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    )
    if not base.is_absolute():
        raise PackInstallError("PROGRAMDATA must be absolute")
    pack_parent = base / "TrafficVisson" / "ModelPacks"
    pack_parent.mkdir(parents=True, exist_ok=True)
    pack_parent = _safe_directory(pack_parent, "TrafficVisson model-pack directory")
    target = pack_parent / f"{PACK_ID}-{PACK_VERSION}"
    if target.exists() or target.is_symlink():
        target = _safe_directory(target, "Grounding DINO evaluation pack")
        return target, _verify_existing(target)

    remote_info = dict(info_provider())
    _validate_model_info(remote_info)
    required_free = SNAPSHOT_TOTAL_BYTES + MIN_FREE_RESERVE_BYTES
    if shutil.disk_usage(pack_parent).free < required_free:
        raise PackInstallError(
            "ProgramData does not have enough free space for the pinned snapshot"
        )
    deadline = time.monotonic() + float(maximum_download_seconds)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{PACK_ID}-",
            dir=str(pack_parent),
        )
    )
    published = False
    try:
        temporary = _safe_directory(temporary, "Temporary evaluation pack")
        snapshot = temporary / "snapshot"
        snapshot.mkdir(exist_ok=False)
        for remote_name, expected_size in SNAPSHOT_FILES.items():
            downloader(
                remote_name,
                snapshot / remote_name,
                expected_size=expected_size,
                deadline=deadline,
            )
        model_path = _safe_file(
            snapshot / "model.safetensors",
            maximum_bytes=MAX_PACK_BYTES,
        )
        if _sha256(model_path) != MODEL_FILE_SHA256:
            raise PackInstallError("The pinned Safetensors SHA-256 did not match")

        runtime = _runtime_provenance()
        source_markdown = f"""# Grounding DINO evaluation pack provenance

- Repository: `{REPO_ID}`
- Immutable revision: `{IMMUTABLE_REVISION}`
- Model card: {MODEL_CARD_URL}
- Transformers documentation: {TRANSFORMERS_DOC_URL}
- Declared model-card license: Apache-2.0
- Snapshot selection: Safetensors weights and required processor/tokenizer files only
- `pytorch_model.bin`: intentionally excluded
- TrafficVisson installer redistribution: disabled
- Use: local review-only proposal evaluation

The downloaded snapshot is loaded offline with `local_files_only=True`,
`trust_remote_code=False`, and `use_safetensors=True`. It must not create
automatic labels, incidents, report facts, model promotions, or sidecars.
"""
        source_path = temporary / "MODEL-SOURCE.md"
        source_path.write_text(source_markdown, encoding="utf-8")
        snapshot_inventory = [
            _inventory_file(temporary, snapshot / name)
            for name in SNAPSHOT_FILES
        ]
        artifacts = [
            *snapshot_inventory,
            _inventory_file(temporary, source_path),
        ]
        manifest = {
            "schema_version": PACK_SCHEMA,
            "pack_id": PACK_ID,
            "pack_version": PACK_VERSION,
            "created_at": _utc_now(),
            "purpose": "local_open_vocabulary_detector_evaluation_only",
            "review_only": True,
            "automatic_labeling_allowed": False,
            "incident_creation_allowed": False,
            "model_promotion_allowed": False,
            "sidecar_recording_allowed": False,
            "installer_distribution": "excluded",
            "source": {
                "repo_id": REPO_ID,
                "requested_revision": IMMUTABLE_REVISION,
                "resolved_revision": remote_info["resolved_revision"],
                "model_card_url": MODEL_CARD_URL,
                "transformers_documentation_url": TRANSFORMERS_DOC_URL,
                "license_spdx": "Apache-2.0",
                "snapshot_total_bytes": SNAPSHOT_TOTAL_BYTES,
                "snapshot_file_count": len(SNAPSHOT_FILES),
            },
            "license": {
                "weights_spdx": "Apache-2.0",
                "runtime_spdx": "Apache-2.0",
                "redistribution_enabled": False,
                "notice": (
                    "The official Hugging Face model card declares Apache-2.0. "
                    "TrafficVisson installer redistribution remains disabled "
                    "for this research pack."
                ),
            },
            "runtime_provenance_at_install": runtime,
            "device_policy": {
                "preferred": "cuda",
                "cpu_fallback": True,
                "target_device": "NVIDIA RTX A2000 12GB",
                "maximum_cuda_allocator_bytes": 10 * 1024**3,
                "maximum_frames_per_run": 349,
                "maximum_predictions_per_frame": 32,
            },
            "inference_contract": {
                "processor_class": "AutoProcessor",
                "model_class": "AutoModelForZeroShotObjectDetection",
                "snapshot_directory": "snapshot",
                "local_files_only": True,
                "trust_remote_code": False,
                "use_safetensors": True,
                "score_semantics": "model_score_not_probability",
                "queries": QUERY_CONTRACT,
                "excluded_state_claims": [
                    "lane_marking_repaint_needed",
                    "colored_guidance_line_repaint_needed",
                    "mowing_required",
                    "maintenance_action_confirmed",
                ],
            },
            "snapshot_files": snapshot_inventory,
            "artifacts": artifacts,
        }
        _atomic_json(
            temporary / "trafficvisson-grounding-dino-pack.json",
            manifest,
        )
        if target.exists() or target.is_symlink():
            raise PackInstallError("The target evaluation pack appeared concurrently")
        os.replace(temporary, target)
        published = True
        verified = _verify_existing(
            _safe_directory(target, "Published Grounding DINO evaluation pack")
        )
        return target, verified
    finally:
        if not published and temporary.exists():
            try:
                if temporary.parent == pack_parent:
                    shutil.rmtree(temporary)
            except OSError:
                pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Install the pinned Grounding DINO Tiny snapshot as a "
            "TrafficVisson evaluation-only local pack."
        )
    )
    parser.add_argument(
        "--maximum-download-seconds",
        type=float,
        default=MAX_DOWNLOAD_SECONDS,
    )
    arguments = parser.parse_args(argv)
    try:
        target, manifest = install_pack(
            maximum_download_seconds=arguments.maximum_download_seconds,
        )
    except (PackInstallError, OSError) as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "message": str(error),
                    "automatic_labels_created": 0,
                    "incidents_created": 0,
                    "model_promoted": False,
                    "sidecars_created": 0,
                },
                ensure_ascii=False,
            ),
            file=os.sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "status": "installed",
                "pack_id": manifest["pack_id"],
                "pack_version": manifest["pack_version"],
                "resolved_revision": manifest["source"]["resolved_revision"],
                "snapshot_file_count": manifest["source"][
                    "snapshot_file_count"
                ],
                "snapshot_total_bytes": manifest["source"][
                    "snapshot_total_bytes"
                ],
                "review_only": True,
                "automatic_labels_created": 0,
                "incidents_created": 0,
                "model_promoted": False,
                "sidecars_created": 0,
                "target": str(target),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
