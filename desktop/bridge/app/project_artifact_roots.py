from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


LOCATION_SCHEMA = "trafficvisson.project-artifact-root.v1"
PROFILE_BINDING_SCHEMA = (
    "trafficvisson.project-artifact-root-profile-binding.v1"
)
JOB_BINDING_SCHEMA = "trafficvisson.project-artifact-root-job-binding.v1"
MARKER_SCHEMA = "trafficvisson.project-artifact-root-marker.v1"
DEFAULT_OUTPUT_LOCATION_ID = "project-default"
MAX_RECORD_BYTES = 64 * 1024
_LOCATION_ID = re.compile(
    r"^(?:project-default|project-output-[0-9a-f]{32})$"
)
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_CAPACITY_RESERVATION_ID = re.compile(
    r"^(?:folder-batch|pipeline-job|current-report):"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class ProjectArtifactRootError(RuntimeError):
    pass


class ProjectArtifactRootNotFound(ProjectArtifactRootError):
    pass


class ProjectArtifactRootConflict(ProjectArtifactRootError):
    pass


class ProjectArtifactRootUnavailable(ProjectArtifactRootError):
    pass


class ProjectOutputLocationRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    selected_directory: str = Field(min_length=3, max_length=32_767)

    @field_validator("selected_directory")
    @classmethod
    def absolute_directory(cls, value: str) -> str:
        if "\x00" in value or not Path(value).is_absolute():
            raise ValueError(
                "selected_directory must be an absolute private path"
            )
        return value


@dataclass(frozen=True)
class ResolvedProjectArtifactRoot:
    root: Path
    binding: dict[str, Any]
    available_free_bytes: int
    safe_summary: dict[str, Any]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _hash_payload(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(
        attributes
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _safe_volume_label(path: Path) -> str:
    anchor = path.anchor.rstrip("\\/")
    if re.fullmatch(r"[A-Za-z]:", anchor):
        return anchor.upper()
    if anchor.startswith("\\\\"):
        return "네트워크 위치"
    return "로컬 저장소"


def _safe_folder_name(
    selected_base: Path, *, is_default: bool
) -> str:
    if is_default:
        return "프로젝트 기본 저장소"
    name = selected_base.name
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "\uFFFD", name).strip()
    return cleaned[:120] or "선택한 결과 폴더"


class ProjectArtifactRootRegistry:
    """Server-owned project artifact-root registry.

    Raw paths live only in the bridge's hash-verified private ledger. Public
    serializers expose an opaque location ID plus bounded capacity metadata.
    A job persists only a path-free binding, then resolves and revalidates the
    same marker/identity on every run or resume.
    """

    def __init__(
        self,
        *,
        data_root: Path,
        disk_usage=shutil.disk_usage,
    ) -> None:
        root = Path(data_root)
        if not root.is_absolute():
            raise ValueError("artifact-root registry data_root must be absolute")
        root.mkdir(parents=True, exist_ok=True)
        self._data_root = root.resolve()
        self._validate_real_directory(self._data_root)
        self._registry_root = (
            self._data_root / "project-artifact-root-registry"
        )
        self._default_roots = self._data_root / "project-artifacts"
        self._registry_root.mkdir(parents=True, exist_ok=True)
        self._default_roots.mkdir(parents=True, exist_ok=True)
        self._validate_real_directory(self._registry_root)
        self._validate_real_directory(self._default_roots)
        if not callable(disk_usage):
            raise ValueError("artifact-root disk usage resolver is invalid")
        self._disk_usage = disk_usage
        self._lock = threading.RLock()
        # Future-output reservations are process-local projections of the
        # durable FolderBatch/PipelineJob ledgers. Both services restore them
        # from their verified records after restart. Keeping the arbitration
        # here gives batch preflight and exact stage replan one atomic lock
        # keyed by the physical volume, not by an opaque location alias.
        self._capacity_reservations: dict[str, dict[str, int]] = {}

    @staticmethod
    def _validate_capacity_reservation(
        reservation_id: str,
        reserved_bytes: int,
        safety_reserve_bytes: int,
    ) -> None:
        if (
            not isinstance(reservation_id, str)
            or _CAPACITY_RESERVATION_ID.fullmatch(
                reservation_id
            )
            is None
            or isinstance(reserved_bytes, bool)
            or not isinstance(reserved_bytes, int)
            or not 0 <= reserved_bytes <= 8 * 1024**4
            or isinstance(safety_reserve_bytes, bool)
            or not isinstance(safety_reserve_bytes, int)
            or not 0 <= safety_reserve_bytes <= 128 * 1024**3
        ):
            raise ValueError("capacity reservation is invalid")

    def adjust_capacity_reservation(
        self,
        project_id: UUID,
        binding: Mapping[str, Any],
        *,
        reservation_id: str,
        reserved_bytes: int,
        safety_reserve_bytes: int,
    ) -> dict[str, Any]:
        """Atomically set one same-volume future-output reservation."""

        self._validate_capacity_reservation(
            reservation_id,
            reserved_bytes,
            safety_reserve_bytes,
        )
        with self._lock:
            resolved = self.resolve(
                project_id,
                output_location_id=str(
                    binding.get("output_location_id", "")
                ),
                expected_binding=binding,
            )
            scope = self.reservation_scope(project_id, binding)
            reservations = self._capacity_reservations.setdefault(
                scope, {}
            )
            previous = int(reservations.get(reservation_id, 0))
            other_reserved = sum(reservations.values()) - previous
            try:
                available = int(self._disk_usage(resolved.root).free)
            except (OSError, TypeError, ValueError) as error:
                raise ProjectArtifactRootUnavailable(
                    "artifact-root capacity is unavailable"
                ) from error
            required = (
                other_reserved
                + reserved_bytes
                + safety_reserve_bytes
            )
            if available < required:
                if not reservations:
                    self._capacity_reservations.pop(scope, None)
                raise ProjectArtifactRootUnavailable(
                    "artifact-root capacity reservation cannot fit"
                )
            if reserved_bytes:
                reservations[reservation_id] = reserved_bytes
            else:
                reservations.pop(reservation_id, None)
            if not reservations:
                self._capacity_reservations.pop(scope, None)
            return {
                "schema_version": (
                    "trafficvisson.capacity-reservation-observation.v1"
                ),
                "reservation_id": reservation_id,
                "reservation_scope_sha256": scope,
                "reserved_bytes": reserved_bytes,
                "other_reserved_bytes": other_reserved,
                "available_free_bytes": available,
                "safety_reserve_bytes": safety_reserve_bytes,
                "path_exposed": False,
            }

    def transfer_capacity_reservation(
        self,
        project_id: UUID,
        binding: Mapping[str, Any],
        *,
        from_reservation_id: str,
        to_reservation_id: str,
        reserved_bytes: int,
    ) -> dict[str, Any]:
        """Atomically move part of a batch hold to one PipelineJob."""

        self._validate_capacity_reservation(
            from_reservation_id, reserved_bytes, 0
        )
        self._validate_capacity_reservation(
            to_reservation_id, reserved_bytes, 0
        )
        if from_reservation_id == to_reservation_id:
            raise ValueError("capacity reservation transfer is invalid")
        with self._lock:
            resolved = self.resolve(
                project_id,
                output_location_id=str(
                    binding.get("output_location_id", "")
                ),
                expected_binding=binding,
            )
            scope = self.reservation_scope(project_id, binding)
            reservations = self._capacity_reservations.setdefault(
                scope, {}
            )
            source_bytes = reservations.get(from_reservation_id)
            destination_bytes = reservations.get(to_reservation_id)
            if destination_bytes is not None:
                if destination_bytes != reserved_bytes:
                    raise ProjectArtifactRootConflict(
                        "pipeline capacity reservation changed"
                    )
                return {
                    "schema_version": (
                        "trafficvisson.capacity-reservation-observation.v1"
                    ),
                    "reservation_id": to_reservation_id,
                    "reservation_scope_sha256": scope,
                    "reserved_bytes": reserved_bytes,
                    "other_reserved_bytes": (
                        sum(reservations.values()) - reserved_bytes
                    ),
                    "available_free_bytes": int(
                        self._disk_usage(resolved.root).free
                    ),
                    "safety_reserve_bytes": 0,
                    "path_exposed": False,
                }
            if (
                isinstance(source_bytes, bool)
                or not isinstance(source_bytes, int)
                or source_bytes < reserved_bytes
            ):
                raise ProjectArtifactRootConflict(
                    "folder capacity reservation is unavailable"
                )
            remainder = source_bytes - reserved_bytes
            if remainder:
                reservations[from_reservation_id] = remainder
            else:
                reservations.pop(from_reservation_id, None)
            reservations[to_reservation_id] = reserved_bytes
            return {
                "schema_version": (
                    "trafficvisson.capacity-reservation-observation.v1"
                ),
                "reservation_id": to_reservation_id,
                "reservation_scope_sha256": scope,
                "reserved_bytes": reserved_bytes,
                "other_reserved_bytes": (
                    sum(reservations.values()) - reserved_bytes
                ),
                "available_free_bytes": int(
                    self._disk_usage(resolved.root).free
                ),
                "safety_reserve_bytes": 0,
                "path_exposed": False,
            }

    def release_capacity_reservation(
        self,
        project_id: UUID,
        binding: Mapping[str, Any],
        *,
        reservation_id: str,
    ) -> None:
        self._validate_capacity_reservation(
            reservation_id, 0, 0
        )
        with self._lock:
            scope = self.reservation_scope(project_id, binding)
            reservations = self._capacity_reservations.get(scope)
            if reservations is None:
                return
            reservations.pop(reservation_id, None)
            if not reservations:
                self._capacity_reservations.pop(scope, None)

    def register(
        self,
        project_id: UUID,
        *,
        selected_directory: Path,
    ) -> dict[str, Any]:
        self._validate_project_id(project_id)
        selected = Path(selected_directory)
        if not selected.is_absolute():
            raise ValueError("selected output directory must be absolute")
        self._validate_no_link_chain(selected)
        selected = selected.resolve(strict=True)
        self._validate_dedicated_base(selected)
        with self._lock:
            for record in self._read_project_records(project_id):
                if (
                    record["is_default"] is False
                    and Path(record["selected_base_path"]) == selected
                ):
                    resolved = self._resolve_record(project_id, record)
                    return self._public_location(resolved)

            location_id = f"project-output-{uuid4().hex}"
            artifact_root = (
                selected
                / ".trafficsupervision-artifacts"
                / str(project_id)
                / location_id
            )
            artifact_root.mkdir(parents=True, exist_ok=False)
            self._validate_real_directory_chain(
                artifact_root, stop_at=selected
            )
            record = self._create_record(
                project_id=project_id,
                location_id=location_id,
                artifact_root=artifact_root,
                selected_base_path=selected,
                is_default=False,
                revision=self._next_revision(project_id),
            )
            self._persist_record(project_id, record)
            return self._public_location(
                self._resolve_record(project_id, record)
            )

    def list(self, project_id: UUID) -> list[dict[str, Any]]:
        self._validate_project_id(project_id)
        with self._lock:
            self._ensure_default(project_id)
            rows: list[dict[str, Any]] = []
            for record in self._read_project_records(project_id):
                try:
                    rows.append(
                        self._public_location(
                            self._resolve_record(project_id, record)
                        )
                    )
                except ProjectArtifactRootError:
                    rows.append(
                        self._public_unavailable_location(record)
                    )
            return sorted(
                rows,
                key=lambda item: (
                    item["output_location_id"]
                    != DEFAULT_OUTPUT_LOCATION_ID,
                    int(item["registry_revision"]),
                ),
            )

    def capture_profile_binding(
        self,
        project_id: UUID,
        *,
        profile_revision: int,
        output_location_id: str,
    ) -> ResolvedProjectArtifactRoot:
        self._validate_project_id(project_id)
        if (
            isinstance(profile_revision, bool)
            or not isinstance(profile_revision, int)
            or profile_revision < 1
        ):
            raise ValueError("profile revision is invalid")
        self._validate_location_id(output_location_id)
        with self._lock:
            resolved = self.resolve(
                project_id,
                output_location_id=output_location_id,
            )
            now = _utc_now()
            binding = {
                "schema_version": PROFILE_BINDING_SCHEMA,
                "project_id": str(project_id),
                "profile_revision": profile_revision,
                "output_location_id": output_location_id,
                "registry_revision": resolved.binding[
                    "registry_revision"
                ],
                "root_identity_sha256": resolved.binding[
                    "root_identity_sha256"
                ],
                "marker_sha256": resolved.binding["marker_sha256"],
                "configured_free_bytes": resolved.available_free_bytes,
                "configured_at": now,
            }
            path = self._profile_binding_path(
                project_id, profile_revision
            )
            if path.exists():
                current = self._read_verified(
                    path, "artifact-root profile binding"
                )
                stable_fields = (
                    "schema_version",
                    "project_id",
                    "profile_revision",
                    "output_location_id",
                    "registry_revision",
                    "root_identity_sha256",
                    "marker_sha256",
                )
                if any(
                    current.get(field) != binding[field]
                    for field in stable_fields
                ):
                    raise ProjectArtifactRootConflict(
                        "profile revision is already bound to another artifact root"
                    )
                binding = current
            else:
                self._persist_verified(path, binding)
            return ResolvedProjectArtifactRoot(
                root=resolved.root,
                binding=binding,
                available_free_bytes=resolved.available_free_bytes,
                safe_summary=resolved.safe_summary,
            )

    def resolve_profile_binding(
        self,
        project_id: UUID,
        *,
        profile_revision: int,
        output_location_id: str,
    ) -> ResolvedProjectArtifactRoot:
        self._validate_project_id(project_id)
        path = self._profile_binding_path(project_id, profile_revision)
        if not path.exists():
            raise ProjectArtifactRootNotFound(
                "pipeline profile artifact-root binding is missing"
            )
        profile_binding = self._read_verified(
            path, "artifact-root profile binding"
        )
        expected_keys = {
            "schema_version",
            "project_id",
            "profile_revision",
            "output_location_id",
            "registry_revision",
            "root_identity_sha256",
            "marker_sha256",
            "configured_free_bytes",
            "configured_at",
        }
        if (
            set(profile_binding) != expected_keys
            or profile_binding.get("schema_version")
            != PROFILE_BINDING_SCHEMA
            or profile_binding.get("project_id") != str(project_id)
            or profile_binding.get("profile_revision")
            != profile_revision
            or profile_binding.get("output_location_id")
            != output_location_id
        ):
            raise ProjectArtifactRootConflict(
                "pipeline profile artifact-root binding is invalid"
            )
        resolved = self.resolve(
            project_id,
            output_location_id=output_location_id,
            expected_binding=profile_binding,
        )
        return ResolvedProjectArtifactRoot(
            root=resolved.root,
            binding=dict(profile_binding),
            available_free_bytes=resolved.available_free_bytes,
            safe_summary=resolved.safe_summary,
        )

    def capture_job_binding(
        self,
        project_id: UUID,
        *,
        profile_revision: int,
        output_location_id: str,
    ) -> ResolvedProjectArtifactRoot:
        profile = self.resolve_profile_binding(
            project_id,
            profile_revision=profile_revision,
            output_location_id=output_location_id,
        )
        binding = {
            "schema_version": JOB_BINDING_SCHEMA,
            "project_id": str(project_id),
            "profile_revision": profile_revision,
            "output_location_id": output_location_id,
            "registry_revision": profile.binding[
                "registry_revision"
            ],
            "root_identity_sha256": profile.binding[
                "root_identity_sha256"
            ],
            "marker_sha256": profile.binding["marker_sha256"],
            "configured_free_bytes": profile.binding[
                "configured_free_bytes"
            ],
            "configured_at": profile.binding["configured_at"],
            "job_start_free_bytes": profile.available_free_bytes,
            "job_start_at": _utc_now(),
            "path_exposed": False,
        }
        return ResolvedProjectArtifactRoot(
            root=profile.root,
            binding=binding,
            available_free_bytes=profile.available_free_bytes,
            safe_summary=profile.safe_summary,
        )

    def resolve_job_binding(
        self,
        project_id: UUID,
        binding: Mapping[str, Any],
    ) -> ResolvedProjectArtifactRoot:
        expected_keys = {
            "schema_version",
            "project_id",
            "profile_revision",
            "output_location_id",
            "registry_revision",
            "root_identity_sha256",
            "marker_sha256",
            "configured_free_bytes",
            "configured_at",
            "job_start_free_bytes",
            "job_start_at",
            "path_exposed",
        }
        if (
            not isinstance(binding, Mapping)
            or set(binding) != expected_keys
            or binding.get("schema_version") != JOB_BINDING_SCHEMA
            or binding.get("project_id") != str(project_id)
            or binding.get("path_exposed") is not False
        ):
            raise ProjectArtifactRootConflict(
                "pipeline job artifact-root binding is invalid"
            )
        return self.resolve(
            project_id,
            output_location_id=str(binding["output_location_id"]),
            expected_binding=binding,
        )

    def reservation_scope(
        self,
        project_id: UUID,
        binding: Mapping[str, Any],
    ) -> str:
        """Return a path-free physical-volume identity for capacity holds."""

        output_location_id = binding.get("output_location_id")
        if not isinstance(output_location_id, str):
            raise ProjectArtifactRootConflict(
                "artifact-root reservation binding is invalid"
            )
        resolved = self.resolve(
            project_id,
            output_location_id=output_location_id,
            expected_binding=binding,
        )
        try:
            stat_result = resolved.root.stat()
        except OSError as error:
            raise ProjectArtifactRootUnavailable(
                "project artifact-root volume identity is unavailable"
            ) from error
        return _hash_payload(
            {
                "anchor": os.path.normcase(
                    resolved.root.anchor.rstrip("\\/")
                ),
                "device": int(stat_result.st_dev),
            }
        )

    def resolve(
        self,
        project_id: UUID,
        *,
        output_location_id: str,
        expected_binding: Optional[Mapping[str, Any]] = None,
    ) -> ResolvedProjectArtifactRoot:
        self._validate_project_id(project_id)
        self._validate_location_id(output_location_id)
        with self._lock:
            if output_location_id == DEFAULT_OUTPUT_LOCATION_ID:
                self._ensure_default(project_id)
            path = self._location_path(project_id, output_location_id)
            if not path.exists():
                raise ProjectArtifactRootNotFound(
                    "project output location is not registered"
                )
            record = self._read_verified(
                path, "project artifact-root record"
            )
            resolved = self._resolve_record(project_id, record)
            if expected_binding is not None:
                for field in (
                    "output_location_id",
                    "registry_revision",
                    "root_identity_sha256",
                    "marker_sha256",
                ):
                    if expected_binding.get(field) != resolved.binding[field]:
                        raise ProjectArtifactRootConflict(
                            "project artifact root changed after it was bound"
                        )
            return resolved

    def _ensure_default(self, project_id: UUID) -> None:
        path = self._location_path(
            project_id, DEFAULT_OUTPUT_LOCATION_ID
        )
        if path.exists():
            return
        artifact_root = self._default_roots / str(project_id)
        artifact_root.mkdir(parents=True, exist_ok=True)
        self._validate_real_directory_chain(
            artifact_root, stop_at=self._data_root
        )
        record = self._create_record(
            project_id=project_id,
            location_id=DEFAULT_OUTPUT_LOCATION_ID,
            artifact_root=artifact_root,
            selected_base_path=self._data_root,
            is_default=True,
            revision=self._next_revision(project_id),
        )
        self._persist_record(project_id, record)

    def _create_record(
        self,
        *,
        project_id: UUID,
        location_id: str,
        artifact_root: Path,
        selected_base_path: Path,
        is_default: bool,
        revision: int,
    ) -> dict[str, Any]:
        self._validate_writable(artifact_root)
        root_nonce = uuid4().hex
        try:
            stat_result = artifact_root.stat()
            identity = self._identity_hash(
                artifact_root,
                root_nonce=root_nonce,
                stat_result=stat_result,
            )
        except OSError as error:
            raise ProjectArtifactRootUnavailable(
                "project artifact-root identity is unavailable"
            ) from error
        marker = {
            "schema_version": MARKER_SCHEMA,
            "project_id": str(project_id),
            "output_location_id": location_id,
            "root_nonce": root_nonce,
            "root_identity_sha256": identity,
            "created_at": _utc_now(),
        }
        marker_path = self._marker_path(artifact_root)
        self._persist_verified(marker_path, marker)
        marker_sha256 = _hash_payload(marker)
        return {
            "schema_version": LOCATION_SCHEMA,
            "project_id": str(project_id),
            "output_location_id": location_id,
            "registry_revision": revision,
            "artifact_root_path": str(artifact_root),
            "selected_base_path": str(selected_base_path),
            "is_default": is_default,
            "root_nonce": root_nonce,
            "root_identity_sha256": identity,
            "marker_sha256": marker_sha256,
            "created_at": _utc_now(),
        }

    def _resolve_record(
        self,
        project_id: UUID,
        record: Mapping[str, Any],
    ) -> ResolvedProjectArtifactRoot:
        required = {
            "schema_version",
            "project_id",
            "output_location_id",
            "registry_revision",
            "artifact_root_path",
            "selected_base_path",
            "is_default",
            "root_nonce",
            "root_identity_sha256",
            "marker_sha256",
            "created_at",
        }
        if (
            set(record) != required
            or record.get("schema_version") != LOCATION_SCHEMA
            or record.get("project_id") != str(project_id)
            or not isinstance(record.get("is_default"), bool)
            or not isinstance(record.get("registry_revision"), int)
            or record["registry_revision"] < 1
            or not isinstance(record.get("root_nonce"), str)
            or re.fullmatch(r"[a-f0-9]{32}", record["root_nonce"])
            is None
            or not isinstance(record.get("root_identity_sha256"), str)
            or _SHA256.fullmatch(record["root_identity_sha256"]) is None
            or not isinstance(record.get("marker_sha256"), str)
            or _SHA256.fullmatch(record["marker_sha256"]) is None
        ):
            raise ProjectArtifactRootConflict(
                "project artifact-root registry record is invalid"
            )
        location_id = str(record["output_location_id"])
        self._validate_location_id(location_id)
        root = Path(str(record["artifact_root_path"]))
        selected = Path(str(record["selected_base_path"]))
        if not root.is_absolute() or not selected.is_absolute():
            raise ProjectArtifactRootConflict(
                "project artifact-root registry path is invalid"
            )
        try:
            root = root.resolve(strict=True)
            selected = selected.resolve(strict=True)
        except OSError as error:
            raise ProjectArtifactRootUnavailable(
                "project artifact root is unavailable"
            ) from error
        self._validate_real_directory_chain(root, stop_at=selected)
        if record["is_default"]:
            try:
                expected = (
                    self._default_roots / str(project_id)
                ).resolve(strict=True)
            except OSError as error:
                raise ProjectArtifactRootUnavailable(
                    "default project artifact root is unavailable"
                ) from error
            if (
                location_id != DEFAULT_OUTPUT_LOCATION_ID
                or root != expected
            ):
                raise ProjectArtifactRootConflict(
                    "default project artifact root escaped its server-owned location"
                )
        else:
            try:
                expected_parent = (
                    selected
                    / ".trafficsupervision-artifacts"
                    / str(project_id)
                ).resolve(strict=True)
            except OSError as error:
                raise ProjectArtifactRootUnavailable(
                    "custom project artifact root is unavailable"
                ) from error
            if (
                location_id == DEFAULT_OUTPUT_LOCATION_ID
                or root.parent != expected_parent
                or root.name != location_id
            ):
                raise ProjectArtifactRootConflict(
                    "custom project artifact root escaped its registered base"
                )
        marker = self._read_verified(
            self._marker_path(root), "project artifact-root marker"
        )
        if (
            marker.get("schema_version") != MARKER_SCHEMA
            or marker.get("project_id") != str(project_id)
            or marker.get("output_location_id") != location_id
            or marker.get("root_nonce") != record["root_nonce"]
            or marker.get("root_identity_sha256")
            != record["root_identity_sha256"]
            or _hash_payload(marker) != record["marker_sha256"]
        ):
            raise ProjectArtifactRootConflict(
                "project artifact-root marker was changed"
            )
        try:
            identity = self._identity_hash(
                root,
                root_nonce=str(record["root_nonce"]),
                stat_result=root.stat(),
            )
        except OSError as error:
            raise ProjectArtifactRootUnavailable(
                "project artifact-root identity is unavailable"
            ) from error
        if identity != record["root_identity_sha256"]:
            raise ProjectArtifactRootConflict(
                "project artifact-root identity changed"
            )
        self._validate_writable(root)
        try:
            free = int(self._disk_usage(root).free)
        except (OSError, TypeError, ValueError) as error:
            raise ProjectArtifactRootUnavailable(
                "project artifact-root capacity is unavailable"
            ) from error
        if free < 0:
            raise ProjectArtifactRootUnavailable(
                "project artifact-root capacity is invalid"
            )
        binding = {
            "output_location_id": location_id,
            "registry_revision": int(record["registry_revision"]),
            "root_identity_sha256": str(
                record["root_identity_sha256"]
            ),
            "marker_sha256": str(record["marker_sha256"]),
        }
        summary = {
            "output_location_id": location_id,
            "registry_revision": int(record["registry_revision"]),
            "display_name": _safe_folder_name(
                selected, is_default=bool(record["is_default"])
            ),
            "volume_label": _safe_volume_label(root),
            "available_free_bytes": free,
            "is_default": bool(record["is_default"]),
            "state": "ready",
            "path_exposed": False,
        }
        return ResolvedProjectArtifactRoot(
            root=root,
            binding=binding,
            available_free_bytes=free,
            safe_summary=summary,
        )

    @staticmethod
    def _identity_hash(
        root: Path,
        *,
        root_nonce: str,
        stat_result: os.stat_result,
    ) -> str:
        identity = {
            "normalized_path": os.path.normcase(str(root.resolve())),
            "device": int(stat_result.st_dev),
            "inode": int(stat_result.st_ino),
            "root_nonce": root_nonce,
        }
        return _hash_payload(identity)

    @staticmethod
    def _validate_project_id(project_id: UUID) -> None:
        if not isinstance(project_id, UUID):
            raise ValueError("project identity is invalid")

    @staticmethod
    def _validate_location_id(location_id: str) -> None:
        if (
            not isinstance(location_id, str)
            or _LOCATION_ID.fullmatch(location_id) is None
        ):
            raise ValueError("output location identity is invalid")

    @classmethod
    def _validate_dedicated_base(cls, path: Path) -> None:
        cls._validate_real_directory(path)
        root = Path(path.anchor)
        home = Path.home().resolve()
        if path == root or path == home:
            raise ProjectArtifactRootConflict(
                "select a dedicated output directory, not a filesystem root"
            )

    @staticmethod
    def _validate_real_directory(path: Path) -> None:
        if (
            path.is_symlink()
            or _is_reparse_point(path)
            or not path.is_dir()
        ):
            raise ProjectArtifactRootConflict(
                "project artifact root must be a real directory"
            )

    @classmethod
    def _validate_no_link_chain(cls, path: Path) -> None:
        current = path
        visited = 0
        while True:
            if (
                current.is_symlink()
                or _is_reparse_point(current)
            ):
                raise ProjectArtifactRootConflict(
                    "project artifact-root path contains a linked directory"
                )
            parent = current.parent
            if parent == current:
                return
            visited += 1
            if visited > 128:
                raise ProjectArtifactRootConflict(
                    "project artifact-root path is too deep"
                )
            current = parent

    @classmethod
    def _validate_real_directory_chain(
        cls, path: Path, *, stop_at: Path
    ) -> None:
        try:
            resolved_stop = stop_at.resolve(strict=True)
        except OSError as error:
            raise ProjectArtifactRootUnavailable(
                "project artifact-root base is unavailable"
            ) from error
        current = path
        visited = 0
        while True:
            cls._validate_real_directory(current)
            if current == resolved_stop:
                return
            parent = current.parent
            visited += 1
            if parent == current or visited > 128:
                raise ProjectArtifactRootConflict(
                    "project artifact root escaped its registered base"
                )
            current = parent

    @staticmethod
    def _validate_writable(path: Path) -> None:
        try:
            descriptor, probe_name = tempfile.mkstemp(
                prefix=".trafficsupervision-write-",
                suffix=".probe",
                dir=path,
            )
            os.close(descriptor)
            Path(probe_name).unlink()
        except OSError as error:
            raise ProjectArtifactRootUnavailable(
                "project artifact root is not writable"
            ) from error

    def _next_revision(self, project_id: UUID) -> int:
        revisions = [
            int(record["registry_revision"])
            for record in self._read_project_records(project_id)
            if isinstance(record.get("registry_revision"), int)
        ]
        return max(revisions, default=0) + 1

    def _read_project_records(
        self, project_id: UUID
    ) -> list[dict[str, Any]]:
        root = self._locations_root(project_id)
        if not root.exists():
            return []
        rows: list[dict[str, Any]] = []
        for path in sorted(root.glob("*.json")):
            try:
                rows.append(
                    self._read_verified(
                        path, "project artifact-root record"
                    )
                )
            except ProjectArtifactRootError:
                continue
        return rows

    def _persist_record(
        self, project_id: UUID, record: Mapping[str, Any]
    ) -> None:
        location_id = str(record["output_location_id"])
        path = self._location_path(project_id, location_id)
        if path.exists():
            raise ProjectArtifactRootConflict(
                "project output location identity already exists"
            )
        self._persist_verified(path, record)

    @staticmethod
    def _public_location(
        resolved: ResolvedProjectArtifactRoot,
    ) -> dict[str, Any]:
        return dict(resolved.safe_summary)

    @staticmethod
    def _public_unavailable_location(
        record: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "output_location_id": str(
                record.get("output_location_id", "unavailable")
            ),
            "registry_revision": int(
                record.get("registry_revision", 0)
            ),
            "display_name": (
                "프로젝트 기본 저장소"
                if record.get("is_default")
                else "선택한 결과 폴더"
            ),
            "volume_label": "확인 필요",
            "available_free_bytes": None,
            "is_default": bool(record.get("is_default")),
            "state": "unavailable",
            "path_exposed": False,
        }

    def _persist_verified(
        self, path: Path, value: Mapping[str, Any]
    ) -> None:
        raw = _canonical_json(dict(value))
        if len(raw) > MAX_RECORD_BYTES:
            raise ProjectArtifactRootUnavailable(
                "artifact-root registry record exceeds its bound"
            )
        digest = hashlib.sha256(raw).hexdigest()
        _write_atomic(path, raw)
        _write_atomic(
            path.with_suffix(".json.sha256"),
            f"{digest}\n".encode("ascii"),
        )

    @staticmethod
    def _read_verified(path: Path, label: str) -> dict[str, Any]:
        try:
            raw = path.read_bytes()
            if not 1 <= len(raw) <= MAX_RECORD_BYTES:
                raise ValueError(f"{label} size is invalid")
            expected = (
                path.with_suffix(".json.sha256")
                .read_text(encoding="ascii")
                .strip()
            )
            actual = hashlib.sha256(raw).hexdigest()
            if _SHA256.fullmatch(expected) is None or expected != actual:
                raise ValueError(f"{label} hash does not match")
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError(f"{label} is not an object")
            return parsed
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise ProjectArtifactRootConflict(
                f"{label} could not be verified"
            ) from error

    @staticmethod
    def _marker_path(root: Path) -> Path:
        return root / ".trafficsupervision-root.json"

    def _project_registry_root(self, project_id: UUID) -> Path:
        return self._registry_root / str(project_id)

    def _locations_root(self, project_id: UUID) -> Path:
        return self._project_registry_root(project_id) / "locations"

    def _bindings_root(self, project_id: UUID) -> Path:
        return self._project_registry_root(project_id) / "profile-bindings"

    def _location_path(
        self, project_id: UUID, location_id: str
    ) -> Path:
        return self._locations_root(project_id) / f"{location_id}.json"

    def _profile_binding_path(
        self, project_id: UUID, profile_revision: int
    ) -> Path:
        return self._bindings_root(project_id) / (
            f"profile-{profile_revision:08d}.json"
        )


__all__ = [
    "DEFAULT_OUTPUT_LOCATION_ID",
    "JOB_BINDING_SCHEMA",
    "PROFILE_BINDING_SCHEMA",
    "ProjectArtifactRootConflict",
    "ProjectArtifactRootError",
    "ProjectArtifactRootNotFound",
    "ProjectArtifactRootRegistry",
    "ProjectArtifactRootUnavailable",
    "ProjectOutputLocationRegisterRequest",
    "ResolvedProjectArtifactRoot",
]
