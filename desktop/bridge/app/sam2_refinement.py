"""Capability-gated, review-only SAM2 point-prompt refinement.

The renderer supplies only project/job/question/candidate identities and a
normalised click.  This service resolves and integrity-checks the candidate
image server-side, invokes an injected local adapter, and returns bounded
geometry plus path-free provenance.  A refinement never creates an
Annotation, Incident, training member, or approved answer.

No production adapter is enabled by default.  Large SAM2 weights remain a
separately verified Model Pack concern; Codex and Claude are never used as
frame engines.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from uuid import UUID, uuid4


MASK_REFINEMENT_SCHEMA = "trafficvisson.sam2-mask-refinement.v1"
MASK_REFINEMENT_CAPABILITY_SCHEMA = (
    "trafficvisson.sam2-mask-refinement-capability.v1"
)
_REFINEMENT_ID = re.compile(r"^smr_[a-f0-9]{24}$")
_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_OPAQUE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PACK_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PACK_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,31}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
MAX_POLYGON_POINTS = 256
MAX_TRACKED_FRAME_REFS = 32


class Sam2RefinementError(ValueError):
    """Base class for safe SAM2 refinement failures."""


class Sam2RefinementUnavailable(Sam2RefinementError):
    """The local SAM2 runtime/model pack is not ready."""


class Sam2RefinementNotFound(Sam2RefinementError):
    """A refinement identity is absent or belongs to another scope."""


class Sam2RefinementAdapter(Protocol):
    """Small injection surface for a verified local SAM2 implementation."""

    def status(self) -> Mapping[str, Any]:
        """Return state and immutable model-manifest identity."""

    def refine(
        self,
        *,
        image_path: Path,
        point: tuple[float, float],
    ) -> Mapping[str, Any]:
        """Return a bounded normalised polygon and optional opaque track refs."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _strict_unit(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        raise Sam2RefinementError(f"{label} must be a finite number in [0, 1]")
    return float(value)


def _uuid(value: object, label: str) -> UUID:
    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise Sam2RefinementError(f"{label} must be a UUID") from error
    if str(parsed) != str(value).lower():
        raise Sam2RefinementError(f"{label} must be a canonical UUID")
    return parsed


def _candidate_id(value: object) -> str:
    if not isinstance(value, str) or _CANDIDATE_ID.fullmatch(value) is None:
        raise Sam2RefinementError("candidate_id is invalid")
    return value


def _manifest(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {
        "pack_id",
        "pack_version",
        "manifest_sha256",
    }:
        raise Sam2RefinementUnavailable(
            "SAM2 model manifest identity is unavailable"
        )
    pack_id = value.get("pack_id")
    pack_version = value.get("pack_version")
    manifest_sha256 = value.get("manifest_sha256")
    if not isinstance(pack_id, str) or _PACK_ID.fullmatch(pack_id) is None:
        raise Sam2RefinementUnavailable("SAM2 pack_id is invalid")
    if (
        not isinstance(pack_version, str)
        or _PACK_VERSION.fullmatch(pack_version) is None
    ):
        raise Sam2RefinementUnavailable("SAM2 pack_version is invalid")
    if (
        not isinstance(manifest_sha256, str)
        or _SHA256.fullmatch(manifest_sha256) is None
    ):
        raise Sam2RefinementUnavailable(
            "SAM2 manifest_sha256 is invalid"
        )
    return {
        "pack_id": pack_id,
        "pack_version": pack_version,
        "manifest_sha256": manifest_sha256,
    }


def _polygon(value: object) -> list[list[float]]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or not 3 <= len(value) <= MAX_POLYGON_POINTS
    ):
        raise Sam2RefinementError(
            "SAM2 polygon must contain 3 to 256 points"
        )
    points: list[list[float]] = []
    for index, raw in enumerate(value):
        if (
            not isinstance(raw, Sequence)
            or isinstance(raw, (str, bytes, bytearray))
            or len(raw) != 2
        ):
            raise Sam2RefinementError(
                f"SAM2 polygon point {index} is invalid"
            )
        points.append(
            [
                _strict_unit(raw[0], f"polygon[{index}].x"),
                _strict_unit(raw[1], f"polygon[{index}].y"),
            ]
        )
    if len({(point[0], point[1]) for point in points}) < 3:
        raise Sam2RefinementError("SAM2 polygon is degenerate")
    return points


def _box_for_polygon(points: Sequence[Sequence[float]]) -> dict[str, float]:
    minimum_x = min(point[0] for point in points)
    minimum_y = min(point[1] for point in points)
    maximum_x = max(point[0] for point in points)
    maximum_y = max(point[1] for point in points)
    if maximum_x <= minimum_x or maximum_y <= minimum_y:
        raise Sam2RefinementError("SAM2 polygon has no bounded area")
    return {
        "x": minimum_x,
        "y": minimum_y,
        "width": maximum_x - minimum_x,
        "height": maximum_y - minimum_y,
    }


def _tracked_refs(value: object) -> list[str]:
    if value is None:
        return []
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) > MAX_TRACKED_FRAME_REFS
    ):
        raise Sam2RefinementError("tracked_frame_refs is invalid")
    result: list[str] = []
    for raw in value:
        if not isinstance(raw, str) or _OPAQUE_REF.fullmatch(raw) is None:
            raise Sam2RefinementError("tracked frame reference is invalid")
        result.append(raw)
    if len(result) != len(set(result)):
        raise Sam2RefinementError("tracked frame references are duplicated")
    return result


class Sam2MaskRefinementService:
    """Resolve evidence, run a local adapter, and retain review-only grants."""

    def __init__(
        self,
        *,
        adapter: Optional[Sam2RefinementAdapter],
        artifact_resolver: Callable[[UUID, UUID, str], object],
        question_resolver: Callable[[UUID, UUID, str], Mapping[str, Any]],
    ) -> None:
        if not callable(artifact_resolver) or not callable(question_resolver):
            raise Sam2RefinementError("refinement resolvers must be callable")
        self._adapter = adapter
        self._artifact_resolver = artifact_resolver
        self._question_resolver = question_resolver
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}

    def capability(
        self,
        *,
        project_id: UUID | str,
        analysis_job_id: UUID | str,
        question_id: UUID | str,
        candidate_id: str,
    ) -> dict[str, Any]:
        identity = self._resolve_identity(
            project_id=project_id,
            analysis_job_id=analysis_job_id,
            question_id=question_id,
            candidate_id=candidate_id,
        )
        try:
            manifest = self._ready_manifest()
        except Sam2RefinementUnavailable as error:
            return {
                "schema_version": MASK_REFINEMENT_CAPABILITY_SCHEMA,
                **identity,
                "state": "model_unavailable",
                "error_code": "model_pack_not_configured",
                "message": str(error),
                "model_manifest": None,
                "click_prompt_enabled": False,
            }
        return {
            "schema_version": MASK_REFINEMENT_CAPABILITY_SCHEMA,
            **identity,
            "state": "ready",
            "error_code": None,
            "message": (
                "검증된 로컬 SAM2 모델팩으로 한 점 마스크 수정을 "
                "요청할 수 있습니다."
            ),
            "model_manifest": manifest,
            "click_prompt_enabled": True,
        }

    def refine(
        self,
        *,
        project_id: UUID | str,
        analysis_job_id: UUID | str,
        question_id: UUID | str,
        candidate_id: str,
        click_x: object,
        click_y: object,
    ) -> dict[str, Any]:
        identity = self._resolve_identity(
            project_id=project_id,
            analysis_job_id=analysis_job_id,
            question_id=question_id,
            candidate_id=candidate_id,
        )
        point = (
            _strict_unit(click_x, "click.x"),
            _strict_unit(click_y, "click.y"),
        )
        try:
            manifest = self._ready_manifest()
        except Sam2RefinementUnavailable as error:
            return {
                "schema_version": MASK_REFINEMENT_SCHEMA,
                **identity,
                "status": "model_unavailable",
                "error_code": "model_pack_not_configured",
                "message": str(error),
                "refinement_id": None,
                "click": {"x": point[0], "y": point[1]},
                "polygon": None,
                "normalized_box": None,
                "mask_artifact_ref": None,
                "tracked_frame_refs": [],
                "input_sha256": None,
                "output_sha256": None,
                "model_manifest": None,
                "requires_user_confirmation": True,
                "automatic_annotation_allowed": False,
                "automatic_incident_creation_allowed": False,
                "training_membership_allowed": False,
            }

        artifact = self._artifact_resolver(
            UUID(identity["project_id"]),
            UUID(identity["analysis_job_id"]),
            identity["candidate_id"],
        )
        image_path = getattr(artifact, "path", None)
        input_sha256 = getattr(artifact, "sha256", None)
        if (
            not isinstance(image_path, Path)
            or not image_path.is_absolute()
            or not isinstance(input_sha256, str)
            or _SHA256.fullmatch(input_sha256) is None
        ):
            raise Sam2RefinementError(
                "resolved candidate image provenance is invalid"
            )
        try:
            raw = self._adapter.refine(
                image_path=image_path,
                point=point,
            )
        except Sam2RefinementError:
            raise
        except Exception as error:
            raise Sam2RefinementError("SAM2 mask refinement failed") from error
        if not isinstance(raw, Mapping):
            raise Sam2RefinementError("SAM2 adapter output is invalid")
        if not set(raw).issubset({"polygon", "tracked_frame_refs"}):
            raise Sam2RefinementError(
                "SAM2 adapter output contains an unsupported field"
            )
        points = _polygon(raw.get("polygon"))
        normalized_box = _box_for_polygon(points)
        tracked_frame_refs = _tracked_refs(raw.get("tracked_frame_refs"))
        semantic_output = {
            "polygon": points,
            "normalized_box": normalized_box,
            "tracked_frame_refs": tracked_frame_refs,
        }
        output_sha256 = hashlib.sha256(
            _canonical_bytes(semantic_output)
        ).hexdigest()
        refinement_id = f"smr_{uuid4().hex[:24]}"
        mask_artifact_ref = f"sam2mask_{output_sha256[:24]}"
        row = {
            "schema_version": MASK_REFINEMENT_SCHEMA,
            **identity,
            "status": "refined",
            "error_code": None,
            "message": (
                "SAM2가 새 마스크를 제안했습니다. 사용자가 확인하기 "
                "전에는 라벨이나 사건으로 저장되지 않습니다."
            ),
            "refinement_id": refinement_id,
            "click": {"x": point[0], "y": point[1]},
            "polygon": points,
            "normalized_box": normalized_box,
            "mask_artifact_ref": mask_artifact_ref,
            "tracked_frame_refs": tracked_frame_refs,
            "input_sha256": input_sha256,
            "output_sha256": output_sha256,
            "model_manifest": manifest,
            "requires_user_confirmation": True,
            "automatic_annotation_allowed": False,
            "automatic_incident_creation_allowed": False,
            "training_membership_allowed": False,
        }
        with self._lock:
            self._records[refinement_id] = json.loads(
                _canonical_bytes(row).decode("utf-8")
            )
            # A bounded UI session cache prevents unbounded memory growth.
            while len(self._records) > 512:
                self._records.pop(next(iter(self._records)))
        return row

    def resolve_for_answer(
        self,
        *,
        project_id: UUID | str,
        analysis_job_id: UUID | str,
        question_id: UUID | str,
        candidate_id: str,
        refinement_id: str,
    ) -> dict[str, Any]:
        identity = self._resolve_identity(
            project_id=project_id,
            analysis_job_id=analysis_job_id,
            question_id=question_id,
            candidate_id=candidate_id,
        )
        if (
            not isinstance(refinement_id, str)
            or _REFINEMENT_ID.fullmatch(refinement_id) is None
        ):
            raise Sam2RefinementNotFound("mask refinement was not found")
        with self._lock:
            row = self._records.get(refinement_id)
            detached = (
                json.loads(_canonical_bytes(row).decode("utf-8"))
                if row is not None
                else None
            )
        if detached is None or any(
            detached.get(key) != identity[key]
            for key in (
                "project_id",
                "analysis_job_id",
                "question_id",
                "candidate_id",
            )
        ):
            raise Sam2RefinementNotFound(
                "mask refinement does not belong to this question"
            )
        return detached

    def record_count(self) -> int:
        """Test/diagnostic count; records remain review-only."""

        with self._lock:
            return len(self._records)

    def _resolve_identity(
        self,
        *,
        project_id: UUID | str,
        analysis_job_id: UUID | str,
        question_id: UUID | str,
        candidate_id: str,
    ) -> dict[str, str]:
        project_uuid = _uuid(project_id, "project_id")
        job_uuid = _uuid(analysis_job_id, "analysis_job_id")
        question_uuid = _uuid(question_id, "question_id")
        candidate = _candidate_id(candidate_id)
        try:
            resolved = self._question_resolver(
                project_uuid,
                job_uuid,
                candidate,
            )
        except Exception as error:
            raise Sam2RefinementNotFound(
                "completed candidate question could not be resolved"
            ) from error
        if (
            not isinstance(resolved, Mapping)
            or resolved.get("project_id") != str(project_uuid)
            or resolved.get("analysis_job_id") != str(job_uuid)
            or resolved.get("candidate_id") != candidate
            or resolved.get("question_id") != str(question_uuid)
        ):
            raise Sam2RefinementNotFound(
                "question identity does not match the completed candidate"
            )
        return {
            "project_id": str(project_uuid),
            "analysis_job_id": str(job_uuid),
            "question_id": str(question_uuid),
            "candidate_id": candidate,
        }

    def _ready_manifest(self) -> dict[str, str]:
        if self._adapter is None:
            raise Sam2RefinementUnavailable(
                "검증된 SAM2 모델팩이 설치되지 않았습니다."
            )
        try:
            status = self._adapter.status()
        except Exception as error:
            raise Sam2RefinementUnavailable(
                "SAM2 런타임 상태를 확인하지 못했습니다."
            ) from error
        if not isinstance(status, Mapping) or status.get("state") != "ready":
            raise Sam2RefinementUnavailable(
                "검증된 SAM2 런타임과 모델팩이 준비되지 않았습니다."
            )
        return _manifest(status.get("model_manifest"))
