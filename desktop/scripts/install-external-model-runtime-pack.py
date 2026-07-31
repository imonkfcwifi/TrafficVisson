"""Publish a closed external ML runtime pack without downloading code.

The source site-packages tree must already have been created from a reviewed,
locked wheelhouse for the same CPython ABI as the frozen bridge.  This command
copies it into a versioned ProgramData directory, inventories every byte, and
validates the resulting pack with the production loader before atomic publish.

The build descriptor intentionally supplies licence decisions and package
notices; the installer does not guess legal facts from package names.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import sys
import tempfile
from typing import Any, Mapping, Sequence


BUILD_SCHEMA = "trafficvisson.external-model-runtime-build.v1"
MANIFEST_NAME = "trafficsupervision-external-model-runtime-pack.json"
_WINDOWS_REPARSE_ATTRIBUTE = 0x400


class RuntimePackInstallError(RuntimeError):
    pass


def _is_link_or_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return True
    if stat.S_ISLNK(details.st_mode):
        return True
    return bool(
        getattr(details, "st_file_attributes", 0)
        & _WINDOWS_REPARSE_ATTRIBUTE
    )


def _strict_object(path: Path) -> Mapping[str, Any]:
    def pairs(values: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise RuntimePackInstallError(
                    "runtime build descriptor contains duplicate keys"
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                RuntimePackInstallError(
                    "runtime build descriptor is non-finite"
                )
            ),
        )
    except RuntimePackInstallError:
        raise
    except Exception as error:
        raise RuntimePackInstallError(
            "runtime build descriptor is invalid"
        ) from error
    if not isinstance(value, Mapping):
        raise RuntimePackInstallError(
            "runtime build descriptor must be an object"
        )
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


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


def _copy_closed_tree(source: Path, destination: Path) -> None:
    source = source.resolve(strict=True)
    if not source.is_dir() or _is_link_or_reparse(source):
        raise RuntimePackInstallError(
            "source site-packages must be a real directory"
        )
    destination.mkdir(parents=True)
    for candidate in source.rglob("*"):
        if _is_link_or_reparse(candidate):
            raise RuntimePackInstallError(
                "source site-packages contains a link or reparse point"
            )
        relative = candidate.relative_to(source)
        target = destination / relative
        if candidate.is_dir():
            target.mkdir()
        elif candidate.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(candidate, target)
            if _sha256(candidate) != _sha256(target):
                raise RuntimePackInstallError(
                    "a runtime package file failed copy verification"
                )
        else:
            raise RuntimePackInstallError(
                "source site-packages contains an unsupported entry"
            )


def install(
    source_site_packages: Path,
    descriptor_path: Path,
    destination_root: Path,
) -> Path:
    descriptor_path = descriptor_path.resolve(strict=True)
    if _is_link_or_reparse(descriptor_path) or not descriptor_path.is_file():
        raise RuntimePackInstallError(
            "runtime build descriptor is unavailable"
        )
    descriptor = _strict_object(descriptor_path)
    expected = {
        "schema_version",
        "runtime_id",
        "runtime_version",
        "source",
        "license",
        "hardware",
        "capabilities",
        "packages",
        "dll_directories",
    }
    if (
        set(descriptor) != expected
        or descriptor.get("schema_version") != BUILD_SCHEMA
        or not isinstance(descriptor.get("runtime_id"), str)
        or not isinstance(descriptor.get("runtime_version"), str)
    ):
        raise RuntimePackInstallError(
            "runtime build descriptor fields are invalid"
        )
    folder_name = (
        f"{descriptor['runtime_id']}-{descriptor['runtime_version']}"
    )
    if (
        not folder_name
        or folder_name in {".", ".."}
        or "/" in folder_name
        or "\\" in folder_name
    ):
        raise RuntimePackInstallError(
            "runtime build destination identity is invalid"
        )
    destination_root.mkdir(parents=True, exist_ok=True)
    destination_root = destination_root.resolve(strict=True)
    if _is_link_or_reparse(destination_root):
        raise RuntimePackInstallError(
            "runtime destination root is unsafe"
        )
    target = destination_root / folder_name
    manifest_name = MANIFEST_NAME
    if target.exists():
        manifest = target / manifest_name
        if not manifest.is_file():
            raise RuntimePackInstallError(
                "existing runtime pack is incomplete"
            )
        bridge_root = Path(__file__).resolve().parents[1] / "bridge"
        sys.path.insert(0, str(bridge_root))
        from app.external_model_runtime import (
            load_external_model_runtime_pack,
        )

        load_external_model_runtime_pack(manifest)
        return manifest

    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{descriptor['runtime_id']}-",
            dir=str(destination_root),
        )
    )
    try:
        runtime_root = temporary / "runtime"
        site_packages = runtime_root / "site-packages"
        runtime_root.mkdir()
        _copy_closed_tree(source_site_packages, site_packages)

        manifest = {
            "schema_version": (
                "trafficvisson.external-model-runtime-pack.v1"
            ),
            "runtime_id": descriptor["runtime_id"],
            "runtime_version": descriptor["runtime_version"],
            "source": descriptor["source"],
            "license": descriptor["license"],
            "python": {
                "implementation": "cpython",
                "cache_tag": sys.implementation.cache_tag,
                "version": (
                    f"{sys.version_info.major}.{sys.version_info.minor}"
                ),
            },
            "platform": {
                "system": platform.system(),
                "machine": (
                    "AMD64"
                    if platform.machine().casefold()
                    in {"amd64", "x86_64"}
                    else platform.machine()
                ),
            },
            "hardware": descriptor["hardware"],
            "capabilities": descriptor["capabilities"],
            "packages": descriptor["packages"],
            "paths": {
                "site_packages": "runtime/site-packages",
                "dll_directories": [
                    f"runtime/site-packages/{value}"
                    for value in descriptor["dll_directories"]
                ],
            },
            "artifacts": [],
        }
        inventoried = sorted(
            (
                path
                for path in temporary.rglob("*")
                if path.is_file()
            ),
            key=lambda path: path.relative_to(temporary).as_posix(),
        )
        manifest["artifacts"] = [
            _artifact(path, temporary) for path in inventoried
        ]
        manifest_path = temporary / manifest_name
        manifest_path.write_bytes(_canonical_json(manifest))

        bridge_root = Path(__file__).resolve().parents[1] / "bridge"
        sys.path.insert(0, str(bridge_root))
        from app.external_model_runtime import (
            load_external_model_runtime_pack,
        )

        verified = load_external_model_runtime_pack(manifest_path)
        if (
            verified.runtime_id != descriptor["runtime_id"]
            or verified.runtime_version != descriptor["runtime_version"]
        ):
            raise RuntimePackInstallError(
                "published runtime identity changed"
            )
        os.replace(temporary, target)
        return target / manifest_name
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-site-packages", required=True)
    parser.add_argument("--descriptor", required=True)
    parser.add_argument(
        "--destination-root",
        default=str(
            Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
            / "TrafficSuperVision"
            / "ModelRuntime"
        ),
    )
    args = parser.parse_args()
    manifest = install(
        Path(args.source_site_packages),
        Path(args.descriptor),
        Path(args.destination_root),
    )
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
