"""Resolve immutable SAM2 track evidence into one Provider candidate montage.

This adapter deliberately has no PipelineJob hook and performs no Provider
call.  A later server-owned hook may pass one exact project/job/candidate
binding plus the SAM2 evidence fields frozen in its checkpoint.  The adapter
then:

* selects the nearest tracked frame before and after the representative frame;
* re-resolves and re-hashes all three source images through a callback;
* renders the representative SAM2 polygon as an actual translucent mask PNG;
* passes before/representative/mask/after pixels to the deterministic montage
  builder; and
* returns only path-free, self-digested lineage marked unreviewed and
  non-transmittable.

The resulting montage still must pass through ``provider_visual_evidence`` and
its explicit local privacy review.  This module never grants transmission,
Annotation, Incident, report, training, promotion, or HWP authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import math
import re
from typing import Any, Callable, Mapping, Protocol, Sequence, Tuple
from uuid import UUID

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

from .provider_candidate_evidence_montage import (
    CandidateEvidenceMontage,
    CandidateEvidenceMontageError,
    build_candidate_evidence_montage,
)


INPUT_SCHEMA_VERSION = (
    "trafficvisson.provider-candidate-sam2-evidence.v1"
)
OUTPUT_SCHEMA_VERSION = (
    "trafficvisson.provider-candidate-evidence-resolution.v1"
)
ALIAS_MAPPING_SCHEMA_VERSION = (
    "trafficvisson.temporal-sam2-candidate-alias.v1"
)
MAX_TRACKED_FRAMES = 2_000
MAX_POLYGON_POINTS = 4_096
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_ARTIFACT_PIXELS = 32_000_000
_TIMESTAMP_TOLERANCE = 0.000001

_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_DINO_PROPOSAL_ID = re.compile(r"^dino_[a-f0-9]{24}$")
_EVIDENCE_REF = re.compile(
    r"^(?:candidate|rescan|dense|analysis|evidence):"
    r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$"
)
_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


class ProviderCandidateEvidenceResolutionError(ValueError):
    """The SAM2/frame evidence could not be safely resolved."""

    def __init__(self, code: str, message: str) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{2,63}", code) is None:
            raise ValueError("candidate evidence error code is invalid")
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResolvedCandidateFrameArtifact:
    """One server-resolved, path-free frame artifact."""

    project_id: UUID
    analysis_job_id: UUID
    pipeline_job_id: UUID
    candidate_id: str
    evidence_ref: str
    timestamp_seconds: float
    body: bytes
    media_type: str
    size_bytes: int
    sha256: str


class CandidateFrameArtifactResolver(Protocol):
    def __call__(
        self,
        *,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        candidate_id: str,
        evidence_ref: str,
    ) -> ResolvedCandidateFrameArtifact: ...


@dataclass(frozen=True)
class ResolvedAliasedCandidateFrameArtifact:
    """One frame explicitly scoped to both review and SAM2 identities."""

    project_id: UUID
    analysis_job_id: UUID
    pipeline_job_id: UUID
    review_candidate_id: str
    sam2_proposal_candidate_id: str
    evidence_ref: str
    timestamp_seconds: float
    body: bytes
    media_type: str
    size_bytes: int
    sha256: str


class AliasedCandidateFrameArtifactResolver(Protocol):
    def __call__(
        self,
        *,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        review_candidate_id: str,
        sam2_proposal_candidate_id: str,
        evidence_ref: str,
    ) -> ResolvedAliasedCandidateFrameArtifact: ...


@dataclass(frozen=True)
class ResolvedCandidateAliasBinding:
    """Hash-bound temporal-review to SAM2-proposal identity mapping."""

    review_candidate_id: str
    sam2_proposal_candidate_id: str
    manifest: Mapping[str, Any]


@dataclass(frozen=True)
class ResolvedProviderCandidateEvidence:
    """One unreviewed montage plus its path-free immutable lineage."""

    montage: CandidateEvidenceMontage
    manifest: Mapping[str, Any]


@dataclass(frozen=True)
class _TrackedFrame:
    evidence_ref: str
    timestamp_seconds: float
    polygon: Tuple[Tuple[float, float], ...]
    polygon_sha256: str


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _timestamp(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 7 * 24 * 60 * 60
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_timestamp_invalid",
            f"{field} is invalid",
        )
    return round(float(value), 6)


def _polygon(
    value: object,
    field: str,
) -> Tuple[Tuple[float, float], ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or not 3 <= len(value) <= MAX_POLYGON_POINTS
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_polygon_invalid",
            f"{field} is invalid",
        )
    points = []
    for index, point in enumerate(value):
        if (
            not isinstance(point, Sequence)
            or isinstance(point, (str, bytes, bytearray))
            or len(point) != 2
        ):
            raise ProviderCandidateEvidenceResolutionError(
                "candidate_evidence_polygon_invalid",
                f"{field}[{index}] is invalid",
            )
        x, y = point
        if any(
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(float(number))
            or not 0 <= float(number) <= 1
            for number in (x, y)
        ):
            raise ProviderCandidateEvidenceResolutionError(
                "candidate_evidence_polygon_invalid",
                f"{field}[{index}] is outside normalized bounds",
            )
        points.append((round(float(x), 8), round(float(y), 8)))
    if len(set(points)) < 3:
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_polygon_invalid",
            f"{field} has fewer than three distinct points",
        )
    twice_area = abs(
        sum(
            points[index][0] * points[(index + 1) % len(points)][1]
            - points[(index + 1) % len(points)][0] * points[index][1]
            for index in range(len(points))
        )
    )
    if twice_area <= 0.00000001:
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_polygon_invalid",
            f"{field} has no usable area",
        )
    return tuple(points)


def _polygon_sha256(points: Sequence[Sequence[float]]) -> str:
    return _sha256(
        _canonical_json([[point[0], point[1]] for point in points])
    )


def _tracked_frames(
    value: object,
) -> Tuple[_TrackedFrame, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or not 3 <= len(value) <= MAX_TRACKED_FRAMES
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_track_invalid",
            "SAM2 tracked_frames are invalid",
        )
    result = []
    seen_refs = set()
    seen_timestamps = set()
    for index, row in enumerate(value):
        if not isinstance(row, Mapping) or set(row) != {
            "evidence_ref",
            "timestamp_seconds",
            "polygon",
        }:
            raise ProviderCandidateEvidenceResolutionError(
                "candidate_evidence_track_invalid",
                f"tracked_frames[{index}] fields are invalid",
            )
        evidence_ref = row["evidence_ref"]
        timestamp = _timestamp(
            row["timestamp_seconds"],
            f"tracked_frames[{index}].timestamp_seconds",
        )
        polygon = _polygon(
            row["polygon"],
            f"tracked_frames[{index}].polygon",
        )
        if (
            not isinstance(evidence_ref, str)
            or _EVIDENCE_REF.fullmatch(evidence_ref) is None
            or evidence_ref in seen_refs
            or timestamp in seen_timestamps
        ):
            raise ProviderCandidateEvidenceResolutionError(
                "candidate_evidence_track_invalid",
                "SAM2 evidence refs and timestamps must be unique",
            )
        seen_refs.add(evidence_ref)
        seen_timestamps.add(timestamp)
        result.append(
            _TrackedFrame(
                evidence_ref=evidence_ref,
                timestamp_seconds=timestamp,
                polygon=polygon,
                polygon_sha256=_polygon_sha256(polygon),
            )
        )
    return tuple(
        sorted(
            result,
            key=lambda item: (
                item.timestamp_seconds,
                item.evidence_ref,
            ),
        )
    )


def _sam2_evidence(
    value: object,
    *,
    candidate_id: str,
) -> tuple[
    dict[str, Any],
    _TrackedFrame,
    _TrackedFrame,
    _TrackedFrame,
]:
    expected = {
        "schema_version",
        "candidate_id",
        "sam2_revision",
        "proposal_sha256",
        "representative_evidence_ref",
        "representative_polygon",
        "tracked_frames",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_sam2_binding_invalid",
            "SAM2 evidence binding fields are invalid",
        )
    representative_ref = value["representative_evidence_ref"]
    if (
        value["schema_version"] != INPUT_SCHEMA_VERSION
        or value["candidate_id"] != candidate_id
        or not isinstance(value["sam2_revision"], str)
        or _SHA256.fullmatch(value["sam2_revision"]) is None
        or not isinstance(value["proposal_sha256"], str)
        or _SHA256.fullmatch(value["proposal_sha256"]) is None
        or not isinstance(representative_ref, str)
        or _EVIDENCE_REF.fullmatch(representative_ref) is None
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_sam2_binding_invalid",
            "SAM2 evidence binding identity is invalid",
        )
    representative_polygon = _polygon(
        value["representative_polygon"],
        "representative_polygon",
    )
    tracked = _tracked_frames(value["tracked_frames"])
    matches = [
        item for item in tracked
        if item.evidence_ref == representative_ref
    ]
    if (
        len(matches) != 1
        or matches[0].polygon != representative_polygon
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_representative_invalid",
            "representative evidence does not match its tracked polygon",
        )
    representative = matches[0]
    before_rows = [
        item
        for item in tracked
        if item.timestamp_seconds
        < representative.timestamp_seconds
    ]
    after_rows = [
        item
        for item in tracked
        if item.timestamp_seconds
        > representative.timestamp_seconds
    ]
    if not before_rows or not after_rows:
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_temporal_context_missing",
            "nearest before and after SAM2 frames are required",
        )
    before = max(
        before_rows, key=lambda item: item.timestamp_seconds
    )
    after = min(
        after_rows, key=lambda item: item.timestamp_seconds
    )
    canonical = {
        "schema_version": INPUT_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "sam2_revision": value["sam2_revision"],
        "proposal_sha256": value["proposal_sha256"],
        "representative_evidence_ref": representative_ref,
        "representative_polygon": [
            [point[0], point[1]]
            for point in representative_polygon
        ],
        "tracked_frames": [
            {
                "evidence_ref": item.evidence_ref,
                "timestamp_seconds": item.timestamp_seconds,
                "polygon": [
                    [point[0], point[1]]
                    for point in item.polygon
                ],
                "polygon_sha256": item.polygon_sha256,
            }
            for item in tracked
        ],
    }
    canonical["sam2_evidence_sha256"] = _sha256(
        _canonical_json(canonical)
    )
    return canonical, before, representative, after


def _verified_sam2_proposal_from_checkpoint(
    *,
    candidate_id: str,
    checkpoint: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    """Return one proposal after verifying checkpoint and proposal digests."""

    if (
        not isinstance(candidate_id, str)
        or _CANDIDATE_ID.fullmatch(candidate_id) is None
        or not isinstance(checkpoint, Mapping)
        or checkpoint.get("schema_version")
        != "trafficvisson.pipeline-sam2-review.v1"
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_checkpoint_invalid",
            "SAM2 checkpoint identity is invalid",
        )
    revision = checkpoint.get("sam2_revision")
    if (
        not isinstance(revision, str)
        or _SHA256.fullmatch(revision) is None
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_checkpoint_invalid",
            "SAM2 checkpoint revision is invalid",
        )
    # ``artifact_root_binding`` is appended by the generic PipelineJob
    # persistence wrapper after the SAM2 stage seals ``sam2_revision``.  It is
    # independently verified by the PipelineJob artifact-root contract and is
    # therefore deliberately outside the SAM2 stage digest.
    checkpoint_body = {
        key: checkpoint[key]
        for key in checkpoint
        if key not in {"sam2_revision", "artifact_root_binding"}
    }
    try:
        expected_revision = _sha256(
            _canonical_json(checkpoint_body)
        )
    except (TypeError, ValueError) as error:
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_checkpoint_invalid",
            "SAM2 checkpoint is not canonical JSON",
        ) from error
    if revision != expected_revision:
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_checkpoint_changed",
            "SAM2 checkpoint digest does not match",
        )
    proposals = checkpoint.get("proposals")
    if (
        not isinstance(proposals, Sequence)
        or isinstance(proposals, (str, bytes, bytearray))
        or len(proposals) > 512
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_checkpoint_invalid",
            "SAM2 checkpoint proposals are invalid",
        )
    matches = [
        proposal
        for proposal in proposals
        if isinstance(proposal, Mapping)
        and proposal.get("candidate_id") == candidate_id
    ]
    if len(matches) != 1:
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_checkpoint_scope_mismatch",
            "SAM2 candidate proposal is missing or ambiguous",
        )
    proposal = matches[0]
    expected_proposal_fields = {
        "candidate_id",
        "prompt",
        "representative_polygon",
        "normalized_box",
        "tracked_frames",
        "tracking_performed",
        "review_only",
        "requires_user_confirmation",
        "automatic_annotation_allowed",
        "automatic_incident_creation_allowed",
        "training_membership_allowed",
        "proposal_sha256",
    }
    if (
        set(proposal) != expected_proposal_fields
        or proposal.get("review_only") is not True
        or proposal.get("requires_user_confirmation") is not True
        or proposal.get("automatic_annotation_allowed") is not False
        or proposal.get(
            "automatic_incident_creation_allowed"
        )
        is not False
        or proposal.get("training_membership_allowed") is not False
        or not isinstance(proposal.get("tracking_performed"), bool)
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_proposal_invalid",
            "SAM2 proposal is not a review-only proposal",
        )
    proposal_sha256 = proposal.get("proposal_sha256")
    proposal_body = {
        key: proposal[key]
        for key in proposal
        if key != "proposal_sha256"
    }
    try:
        computed_proposal_sha256 = _sha256(
            _canonical_json(proposal_body)
        )
    except (TypeError, ValueError) as error:
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_proposal_invalid",
            "SAM2 proposal is not canonical JSON",
        ) from error
    if (
        not isinstance(proposal_sha256, str)
        or _SHA256.fullmatch(proposal_sha256) is None
        or computed_proposal_sha256 != proposal_sha256
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_proposal_changed",
            "SAM2 proposal digest does not match",
        )
    prompt = proposal.get("prompt")
    if (
        not isinstance(prompt, Mapping)
        or set(prompt)
        != {"kind", "x", "y", "prompt_frame_ref"}
        or prompt.get("kind") != "detector_box_center_point"
        or not isinstance(prompt.get("prompt_frame_ref"), str)
        or _EVIDENCE_REF.fullmatch(
            prompt["prompt_frame_ref"]
        )
        is None
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_proposal_invalid",
            "SAM2 representative frame binding is invalid",
        )
    return (
        json.loads(_canonical_json(proposal).decode("utf-8")),
        revision,
    )


def sam2_evidence_from_checkpoint(
    *,
    candidate_id: str,
    checkpoint: Mapping[str, Any],
) -> dict[str, Any]:
    """Adapt one hash-verified production SAM2 proposal to this resolver.

    The production checkpoint may contain additional stage-owned fields such
    as DINO prompt bindings.  They remain covered by ``sam2_revision``; this
    adapter neither drops them before digest verification nor trusts a
    renderer-selected proposal.
    """

    proposal, revision = _verified_sam2_proposal_from_checkpoint(
        candidate_id=candidate_id,
        checkpoint=checkpoint,
    )
    if proposal["tracking_performed"] is not True:
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_proposal_invalid",
            "SAM2 proposal is not a tracked review-only proposal",
        )
    prompt = proposal["prompt"]
    proposal_sha256 = proposal["proposal_sha256"]
    adapted = {
        "schema_version": INPUT_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "sam2_revision": revision,
        "proposal_sha256": proposal_sha256,
        "representative_evidence_ref": prompt[
            "prompt_frame_ref"
        ],
        "representative_polygon": proposal[
            "representative_polygon"
        ],
        "tracked_frames": proposal["tracked_frames"],
    }
    # Run the canonical adapter validation now so a caller cannot persist a
    # superficially adapted but temporally unusable record.
    _sam2_evidence(adapted, candidate_id=candidate_id)
    return json.loads(_canonical_json(adapted).decode("utf-8"))


def _alias_normalized_box(
    value: object,
    *,
    field: str,
) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != {
        "x",
        "y",
        "width",
        "height",
    }:
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_scope_mismatch",
            f"{field} is invalid",
        )
    result: dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        number = value[key]
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(float(number))
        ):
            raise ProviderCandidateEvidenceResolutionError(
                "candidate_alias_scope_mismatch",
                f"{field}.{key} is invalid",
            )
        result[key] = round(float(number), 8)
    if (
        not 0 <= result["x"] < 1
        or not 0 <= result["y"] < 1
        or not 0 < result["width"] <= 1
        or not 0 < result["height"] <= 1
        or result["x"] + result["width"] > 1.00000001
        or result["y"] + result["height"] > 1.00000001
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_scope_mismatch",
            f"{field} is outside normalized bounds",
        )
    return result


def _verified_temporal_merge_revision(
    checkpoint: Mapping[str, Any],
) -> str:
    if not isinstance(checkpoint, Mapping):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_temporal_checkpoint_invalid",
            "temporal merge checkpoint is invalid",
        )
    revision = checkpoint.get("temporal_merge_revision")
    if (
        not isinstance(revision, str)
        or _SHA256.fullmatch(revision) is None
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_temporal_checkpoint_invalid",
            "temporal merge revision is invalid",
        )
    required = (
        "analysis_job_id",
        "evaluation_id",
        "detection_revision",
        "question_candidate_ids",
        "groups",
    )
    if any(key not in checkpoint for key in required):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_temporal_checkpoint_invalid",
            "temporal merge revision inputs are missing",
        )
    revision_payload = {
        key: checkpoint[key]
        for key in required
    }
    dino_groups = checkpoint.get(
        "dino_temporal_groups_by_candidate"
    )
    if dino_groups:
        if "dino_candidate_evidence_overlay" not in checkpoint:
            raise ProviderCandidateEvidenceResolutionError(
                "candidate_alias_temporal_checkpoint_invalid",
                "DINO temporal evidence overlay is missing",
            )
        revision_payload[
            "dino_temporal_groups_by_candidate"
        ] = dino_groups
        revision_payload["dino_candidate_evidence_overlay"] = (
            checkpoint["dino_candidate_evidence_overlay"]
        )
    try:
        expected_revision = _sha256(
            _canonical_json(revision_payload)
        )
    except (TypeError, ValueError) as error:
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_temporal_checkpoint_invalid",
            "temporal merge checkpoint is not canonical JSON",
        ) from error
    if revision != expected_revision:
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_temporal_checkpoint_changed",
            "temporal merge checkpoint digest does not match",
        )
    return revision


def bind_review_candidate_to_sam2_proposal(
    *,
    project_id: UUID,
    analysis_job_id: UUID,
    pipeline_job_id: UUID,
    review_candidate_id: str,
    sam2_proposal_candidate_id: str,
    temporal_checkpoint: Mapping[str, Any],
    sam2_checkpoint: Mapping[str, Any],
) -> ResolvedCandidateAliasBinding:
    """Bind one temporal review candidate to exactly one peak SAM2 prompt.

    Temporal DINO groups use a synthetic question candidate id.  The SAM2
    checkpoint instead uses a prompt candidate id derived from the underlying
    DINO proposal.  This adapter verifies both stage revisions, the exact
    temporal peak, the DINO prompt binding, and the SAM2 proposal digest before
    recording that alias.  It never grants Provider or truth authority.
    """

    if (
        not isinstance(project_id, UUID)
        or not isinstance(analysis_job_id, UUID)
        or not isinstance(pipeline_job_id, UUID)
        or not isinstance(review_candidate_id, str)
        or _CANDIDATE_ID.fullmatch(review_candidate_id) is None
        or not isinstance(sam2_proposal_candidate_id, str)
        or _CANDIDATE_ID.fullmatch(
            sam2_proposal_candidate_id
        )
        is None
        or review_candidate_id == sam2_proposal_candidate_id
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_binding_invalid",
            "temporal-to-SAM2 candidate identities are invalid",
        )
    temporal_revision = _verified_temporal_merge_revision(
        temporal_checkpoint
    )
    if temporal_checkpoint.get("analysis_job_id") != str(
        analysis_job_id
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_scope_mismatch",
            "temporal merge analysis job does not match",
        )
    overlay = temporal_checkpoint.get(
        "dino_candidate_evidence_overlay"
    )
    if (
        not isinstance(overlay, Mapping)
        or overlay.get("project_id") != str(project_id)
        or overlay.get("analysis_job_id") != str(analysis_job_id)
        or overlay.get("pipeline_job_id") != str(pipeline_job_id)
        or overlay.get("review_only") is not True
        or overlay.get("annotations_created") != 0
        or overlay.get("incidents_created") != 0
        or overlay.get("training_members_created") != 0
        or overlay.get("report_truth_records_created") != 0
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_scope_mismatch",
            "temporal DINO evidence overlay scope does not match",
        )
    question_ids = temporal_checkpoint.get(
        "question_candidate_ids"
    )
    if (
        not isinstance(question_ids, Sequence)
        or isinstance(question_ids, (str, bytes, bytearray))
        or len(question_ids) > 10_000
        or any(
            not isinstance(item, str)
            or _CANDIDATE_ID.fullmatch(item) is None
            for item in question_ids
        )
        or len(question_ids) != len(set(question_ids))
        or question_ids.count(review_candidate_id) != 1
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_group_missing",
            "review candidate is not uniquely present in the question queue",
        )
    group_map = temporal_checkpoint.get(
        "dino_temporal_groups_by_candidate"
    )
    if (
        not isinstance(group_map, Mapping)
        or len(group_map) > 10_000
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_group_missing",
            "DINO temporal group mapping is invalid",
        )
    group = group_map.get(review_candidate_id)
    if (
        not isinstance(group, Mapping)
        or group.get("synthetic_candidate_id")
        != review_candidate_id
        or group.get("review_only") is not True
        or group.get("automatic_annotation_allowed") is not False
        or group.get("automatic_incident_creation_allowed") is not False
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_group_missing",
            "review candidate DINO temporal group is missing",
        )
    summaries = temporal_checkpoint.get("groups")
    if (
        not isinstance(summaries, Sequence)
        or isinstance(summaries, (str, bytes, bytearray))
        or len(summaries) > 10_000
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_temporal_checkpoint_invalid",
            "temporal group summaries are invalid",
        )
    summary_matches = [
        item
        for item in summaries
        if isinstance(item, Mapping)
        and item.get("representative_candidate_id")
        == review_candidate_id
    ]
    if len(summary_matches) != 1:
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_group_ambiguous",
            "review candidate temporal summary is missing or ambiguous",
        )
    summary = summary_matches[0]
    summary_evidence = summary.get("evidence_candidate_ids")
    if (
        summary.get("proposal_id")
        != group.get("temporal_proposal_id")
        or summary.get("issue_type") != group.get("issue_type")
        or not isinstance(summary_evidence, Sequence)
        or isinstance(
            summary_evidence, (str, bytes, bytearray)
        )
        or summary_evidence.count(review_candidate_id) != 1
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_scope_mismatch",
            "temporal group summary does not bind the review candidate",
        )
    evidence_proposal_ids = group.get(
        "evidence_proposal_ids"
    )
    evidence_frame_ids = group.get("evidence_frame_ids")
    declared_prompt_ids = group.get(
        "sam2_prompt_candidate_ids"
    )
    if (
        not isinstance(evidence_proposal_ids, Sequence)
        or isinstance(
            evidence_proposal_ids, (str, bytes, bytearray)
        )
        or not evidence_proposal_ids
        or len(evidence_proposal_ids)
        != len(set(evidence_proposal_ids))
        or any(
            not isinstance(item, str)
            or _DINO_PROPOSAL_ID.fullmatch(item) is None
            for item in evidence_proposal_ids
        )
        or not isinstance(evidence_frame_ids, Sequence)
        or isinstance(
            evidence_frame_ids, (str, bytes, bytearray)
        )
        or not evidence_frame_ids
        or len(evidence_frame_ids) != len(set(evidence_frame_ids))
        or any(
            not isinstance(item, str)
            or _CANDIDATE_ID.fullmatch(item) is None
            for item in evidence_frame_ids
        )
        or not isinstance(declared_prompt_ids, Sequence)
        or isinstance(
            declared_prompt_ids, (str, bytes, bytearray)
        )
        or not declared_prompt_ids
        or len(declared_prompt_ids)
        != len(set(declared_prompt_ids))
        or any(
            not isinstance(item, str)
            or _CANDIDATE_ID.fullmatch(item) is None
            for item in declared_prompt_ids
        )
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_prompt_missing",
            "temporal group prompt identity set is invalid",
        )
    peak = group.get("peak_evidence")
    if not isinstance(peak, Mapping):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_scope_mismatch",
            "temporal group peak evidence is invalid",
        )
    peak_dino_id = peak.get("dino_proposal_id")
    peak_parent_id = peak.get("parent_candidate_id")
    peak_evidence_id = peak.get("evidence_candidate_id")
    peak_image_sha256 = peak.get("image_sha256")
    if (
        not isinstance(peak_dino_id, str)
        or _DINO_PROPOSAL_ID.fullmatch(peak_dino_id) is None
        or peak_dino_id not in evidence_proposal_ids
        or not isinstance(peak_parent_id, str)
        or _CANDIDATE_ID.fullmatch(peak_parent_id) is None
        or not isinstance(peak_evidence_id, str)
        or _CANDIDATE_ID.fullmatch(peak_evidence_id) is None
        or peak_evidence_id not in evidence_frame_ids
        or not isinstance(peak_image_sha256, str)
        or _SHA256.fullmatch(peak_image_sha256) is None
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_scope_mismatch",
            "temporal group peak identity is invalid",
        )
    peak_timestamp = _timestamp(
        peak.get("timestamp_seconds"),
        "peak_evidence.timestamp_seconds",
    )
    peak_box = _alias_normalized_box(
        peak.get("normalized_box"),
        field="peak_evidence.normalized_box",
    )
    bindings = sam2_checkpoint.get("dino_prompt_bindings")
    if (
        not isinstance(bindings, Sequence)
        or isinstance(bindings, (str, bytes, bytearray))
        or len(bindings) > 512
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_prompt_missing",
            "SAM2 DINO prompt bindings are invalid",
        )
    peak_bindings = [
        item
        for item in bindings
        if isinstance(item, Mapping)
        and item.get("prompt_candidate_id")
        in declared_prompt_ids
        and item.get("dino_proposal_id") == peak_dino_id
    ]
    if not peak_bindings:
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_prompt_missing",
            "temporal peak has no SAM2 prompt binding",
        )
    if len(peak_bindings) != 1:
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_prompt_ambiguous",
            "temporal peak has multiple SAM2 prompt bindings",
        )
    binding = peak_bindings[0]
    if (
        binding.get("prompt_candidate_id")
        != sam2_proposal_candidate_id
        or sam2_proposal_candidate_id not in declared_prompt_ids
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_scope_mismatch",
            "requested SAM2 proposal does not match the temporal peak",
        )
    binding_box = _alias_normalized_box(
        binding.get("normalized_box"),
        field="dino_prompt_binding.normalized_box",
    )
    prompt_evidence_ref = binding.get("prompt_evidence_ref")
    prompt_ref_matches_evidence = (
        prompt_evidence_ref == f"rescan:{peak_evidence_id}"
        or prompt_evidence_ref == f"candidate:{peak_parent_id}"
    )
    if (
        binding.get("parent_candidate_id") != peak_parent_id
        or binding.get("evidence_candidate_id")
        != peak_evidence_id
        or binding.get("evidence_image_sha256")
        != peak_image_sha256
        or binding.get("issue_type") != group.get("issue_type")
        or _timestamp(
            binding.get("evidence_timestamp_seconds"),
            "dino_prompt_binding.evidence_timestamp_seconds",
        )
        != peak_timestamp
        or binding_box != peak_box
        or not isinstance(prompt_evidence_ref, str)
        or not prompt_ref_matches_evidence
        or binding.get("sam2_status") != "available"
        or binding.get("sam2_error_code") is not None
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_scope_mismatch",
            "SAM2 prompt crosses temporal parent or evidence scope",
        )
    proposal, sam2_revision = (
        _verified_sam2_proposal_from_checkpoint(
            candidate_id=sam2_proposal_candidate_id,
            checkpoint=sam2_checkpoint,
        )
    )
    if (
        binding.get("sam2_proposal_sha256")
        != proposal["proposal_sha256"]
        or proposal["prompt"]["prompt_frame_ref"]
        != prompt_evidence_ref
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_scope_mismatch",
            "SAM2 proposal does not match its DINO prompt binding",
        )
    mapping_body = {
        "schema_version": ALIAS_MAPPING_SCHEMA_VERSION,
        "project_id": str(project_id),
        "analysis_job_id": str(analysis_job_id),
        "pipeline_job_id": str(pipeline_job_id),
        "review_candidate_id": review_candidate_id,
        "sam2_proposal_candidate_id": (
            sam2_proposal_candidate_id
        ),
        "temporal_merge_revision": temporal_revision,
        "sam2_revision": sam2_revision,
        "temporal_proposal_id": group[
            "temporal_proposal_id"
        ],
        "peak_dino_proposal_id": peak_dino_id,
        "parent_candidate_id": peak_parent_id,
        "evidence_candidate_id": peak_evidence_id,
        "evidence_image_sha256": peak_image_sha256,
        "prompt_evidence_ref": prompt_evidence_ref,
        "temporal_group_sha256": _sha256(
            _canonical_json(group)
        ),
        "temporal_summary_sha256": _sha256(
            _canonical_json(summary)
        ),
        "dino_prompt_binding_sha256": _sha256(
            _canonical_json(binding)
        ),
        "sam2_proposal_sha256": proposal["proposal_sha256"],
        "review_only": True,
        "provider_transmission_allowed": False,
        "provider_command_invocation_count": 0,
        "annotation_allowed": False,
        "incident_allowed": False,
        "training_start_allowed": False,
        "model_promotion_allowed": False,
        "report_truth_allowed": False,
        "hwp_apply_allowed": False,
    }
    manifest = {
        **mapping_body,
        "mapping_sha256": _sha256(
            _canonical_json(mapping_body)
        ),
    }
    return ResolvedCandidateAliasBinding(
        review_candidate_id=review_candidate_id,
        sam2_proposal_candidate_id=(
            sam2_proposal_candidate_id
        ),
        manifest=json.loads(
            _canonical_json(manifest).decode("utf-8")
        ),
    )


def _detected_media_type(raw: bytes) -> str:
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if (
        len(raw) >= 12
        and raw[:4] == b"RIFF"
        and raw[8:12] == b"WEBP"
    ):
        return "image/webp"
    return ""


def _resolve_frame(
    *,
    resolver: CandidateFrameArtifactResolver,
    project_id: UUID,
    analysis_job_id: UUID,
    pipeline_job_id: UUID,
    candidate_id: str,
    frame: _TrackedFrame,
) -> ResolvedCandidateFrameArtifact:
    try:
        artifact = resolver(
            project_id=project_id,
            analysis_job_id=analysis_job_id,
            pipeline_job_id=pipeline_job_id,
            candidate_id=candidate_id,
            evidence_ref=frame.evidence_ref,
        )
    except ProviderCandidateEvidenceResolutionError:
        raise
    except Exception as error:
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_artifact_unavailable",
            "candidate frame artifact could not be resolved",
        ) from error
    if not isinstance(artifact, ResolvedCandidateFrameArtifact):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_artifact_invalid",
            "candidate frame resolver returned an unsafe object",
        )
    if (
        artifact.project_id != project_id
        or artifact.analysis_job_id != analysis_job_id
        or artifact.pipeline_job_id != pipeline_job_id
        or artifact.candidate_id != candidate_id
        or artifact.evidence_ref != frame.evidence_ref
        or _timestamp(
            artifact.timestamp_seconds,
            "artifact.timestamp_seconds",
        )
        != frame.timestamp_seconds
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_artifact_scope_mismatch",
            "candidate frame artifact crossed its immutable scope",
        )
    if (
        not isinstance(artifact.body, bytes)
        or not isinstance(artifact.media_type, str)
        or artifact.media_type not in _MEDIA_TYPES
        or isinstance(artifact.size_bytes, bool)
        or not isinstance(artifact.size_bytes, int)
        or not 1 <= artifact.size_bytes <= MAX_ARTIFACT_BYTES
        or len(artifact.body) != artifact.size_bytes
        or not isinstance(artifact.sha256, str)
        or _SHA256.fullmatch(artifact.sha256) is None
        or _sha256(artifact.body) != artifact.sha256
        or _detected_media_type(artifact.body)
        != artifact.media_type
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_artifact_changed",
            "candidate frame bytes, MIME, size, or SHA-256 changed",
        )
    return artifact


def _decode_frame(
    artifact: ResolvedCandidateFrameArtifact,
) -> Image.Image:
    try:
        with Image.open(io.BytesIO(artifact.body)) as source:
            source.load()
            image = ImageOps.exif_transpose(source).convert("RGB")
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as error:
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_artifact_invalid",
            "candidate frame image cannot be decoded",
        ) from error
    if (
        image.width < 16
        or image.height < 16
        or image.width * image.height > MAX_ARTIFACT_PIXELS
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_artifact_invalid",
            "candidate frame dimensions are invalid",
        )
    return image


def _render_mask_overlay(
    artifact: ResolvedCandidateFrameArtifact,
    polygon: Sequence[Sequence[float]],
) -> bytes:
    image = _decode_frame(artifact).convert("RGBA")
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    pixels = [
        (
            int(round(point[0] * (image.width - 1))),
            int(round(point[1] * (image.height - 1))),
        )
        for point in polygon
    ]
    draw.polygon(
        pixels,
        fill=(41, 182, 246, 104),
        outline=(34, 211, 238, 255),
        width=3,
    )
    rendered = Image.alpha_composite(image, overlay).convert("RGB")
    output = io.BytesIO()
    rendered.save(
        output,
        format="PNG",
        optimize=False,
        compress_level=6,
    )
    return output.getvalue()


def _target_box(
    polygon: Sequence[Sequence[float]],
) -> dict[str, float]:
    xs = [float(point[0]) for point in polygon]
    ys = [float(point[1]) for point in polygon]
    return {
        "x": min(xs),
        "y": min(ys),
        "width": max(xs) - min(xs),
        "height": max(ys) - min(ys),
    }


def _resolve_provider_candidate_evidence_bound(
    *,
    project_id: UUID,
    analysis_job_id: UUID,
    pipeline_job_id: UUID,
    candidate_id: str,
    sam2_proposal_candidate_id: str,
    sam2_checkpoint: Mapping[str, Any],
    artifact_resolver: CandidateFrameArtifactResolver,
    alias_binding: ResolvedCandidateAliasBinding | None,
) -> ResolvedProviderCandidateEvidence:
    """Resolve and re-hash exact frames without granting transmission."""

    if (
        not isinstance(project_id, UUID)
        or not isinstance(analysis_job_id, UUID)
        or not isinstance(pipeline_job_id, UUID)
        or not isinstance(candidate_id, str)
        or _CANDIDATE_ID.fullmatch(candidate_id) is None
        or not isinstance(sam2_proposal_candidate_id, str)
        or _CANDIDATE_ID.fullmatch(
            sam2_proposal_candidate_id
        )
        is None
        or not callable(artifact_resolver)
    ):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_binding_invalid",
            "candidate evidence identity binding is invalid",
        )
    identities_are_aliased = (
        candidate_id != sam2_proposal_candidate_id
    )
    if identities_are_aliased != (alias_binding is not None):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_binding_invalid",
            "candidate alias binding presence does not match identities",
        )
    if alias_binding is not None:
        alias_manifest = alias_binding.manifest
        if (
            alias_binding.review_candidate_id != candidate_id
            or alias_binding.sam2_proposal_candidate_id
            != sam2_proposal_candidate_id
            or not isinstance(alias_manifest, Mapping)
            or alias_manifest.get("review_candidate_id")
            != candidate_id
            or alias_manifest.get(
                "sam2_proposal_candidate_id"
            )
            != sam2_proposal_candidate_id
            or not isinstance(
                alias_manifest.get("mapping_sha256"), str
            )
            or _SHA256.fullmatch(
                alias_manifest["mapping_sha256"]
            )
            is None
        ):
            raise ProviderCandidateEvidenceResolutionError(
                "candidate_alias_binding_invalid",
                "candidate alias manifest identity is invalid",
            )
        alias_body = {
            key: alias_manifest[key]
            for key in alias_manifest
            if key != "mapping_sha256"
        }
        if _sha256(_canonical_json(alias_body)) != alias_manifest[
            "mapping_sha256"
        ]:
            raise ProviderCandidateEvidenceResolutionError(
                "candidate_alias_binding_changed",
                "candidate alias manifest digest does not match",
            )
    verified_sam2_evidence = sam2_evidence_from_checkpoint(
        candidate_id=sam2_proposal_candidate_id,
        checkpoint=sam2_checkpoint,
    )
    (
        canonical_sam2,
        before_frame,
        representative_frame,
        after_frame,
    ) = _sam2_evidence(
        verified_sam2_evidence,
        candidate_id=sam2_proposal_candidate_id,
    )
    selected = (
        ("before_frame", before_frame),
        ("representative_frame", representative_frame),
        ("after_frame", after_frame),
    )
    artifacts = {
        role: _resolve_frame(
            resolver=artifact_resolver,
            project_id=project_id,
            analysis_job_id=analysis_job_id,
            pipeline_job_id=pipeline_job_id,
            candidate_id=candidate_id,
            frame=frame,
        )
        for role, frame in selected
    }
    representative = artifacts["representative_frame"]
    mask_body = _render_mask_overlay(
        representative,
        representative_frame.polygon,
    )
    mask_sha256 = _sha256(mask_body)
    source_components = []
    for role, frame in selected:
        artifact = artifacts[role]
        source_components.append(
            {
                "role": role,
                "source_ref": (
                    f"evidence:{candidate_id}:provider-{role}-"
                    f"{artifact.sha256[:16]}"
                ),
                "source_sha256": artifact.sha256,
                "media_type": artifact.media_type,
                "size_bytes": artifact.size_bytes,
                "body": artifact.body,
            }
        )
    source_components.insert(
        2,
        {
            "role": "mask_overlay",
            "source_ref": (
                f"evidence:{candidate_id}:sam2-mask-overlay-"
                f"{mask_sha256[:16]}"
            ),
            "source_sha256": mask_sha256,
            "media_type": "image/png",
            "size_bytes": len(mask_body),
            "body": mask_body,
        },
    )
    try:
        montage = build_candidate_evidence_montage(
            candidate_id=candidate_id,
            components=source_components,
            target_box=_target_box(
                representative_frame.polygon
            ),
        )
    except CandidateEvidenceMontageError as error:
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_evidence_montage_failed",
            "candidate evidence montage could not be built",
        ) from error

    selected_manifest = []
    for role, frame in selected:
        artifact = artifacts[role]
        selected_manifest.append(
            {
                "role": role,
                "evidence_ref": frame.evidence_ref,
                "timestamp_seconds": frame.timestamp_seconds,
                "polygon_sha256": frame.polygon_sha256,
                "source_media_type": artifact.media_type,
                "source_size_bytes": artifact.size_bytes,
                "source_sha256": artifact.sha256,
            }
        )
    mask_manifest = {
        "role": "mask_overlay",
        "source_evidence_ref": (
            representative_frame.evidence_ref
        ),
        "source_timestamp_seconds": (
            representative_frame.timestamp_seconds
        ),
        "source_sha256": representative.sha256,
        "polygon_sha256": (
            representative_frame.polygon_sha256
        ),
        "media_type": "image/png",
        "size_bytes": len(mask_body),
        "sha256": mask_sha256,
    }
    lineage = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "project_id": str(project_id),
        "analysis_job_id": str(analysis_job_id),
        "pipeline_job_id": str(pipeline_job_id),
        "candidate_id": candidate_id,
        **(
            {
                "review_candidate_id": candidate_id,
                "sam2_proposal_candidate_id": (
                    sam2_proposal_candidate_id
                ),
                "mapping_sha256": alias_binding.manifest[
                    "mapping_sha256"
                ],
                "candidate_alias_schema_version": (
                    alias_binding.manifest["schema_version"]
                ),
                "temporal_merge_revision": (
                    alias_binding.manifest[
                        "temporal_merge_revision"
                    ]
                ),
            }
            if alias_binding is not None
            else {}
        ),
        "sam2_revision": canonical_sam2["sam2_revision"],
        "sam2_proposal_sha256": canonical_sam2[
            "proposal_sha256"
        ],
        "sam2_evidence_sha256": canonical_sam2[
            "sam2_evidence_sha256"
        ],
        "selected_frames": selected_manifest,
        "mask_overlay": mask_manifest,
        "montage_manifest_sha256": montage.manifest[
            "manifest_sha256"
        ],
        "montage_sha256": montage.sha256,
        "montage_size_bytes": montage.size_bytes,
        "privacy_state": "unreviewed",
        "provider_transmission_allowed": False,
        "provider_command_invocation_count": 0,
        "provider_visual_evidence_review_required": True,
        "annotation_allowed": False,
        "incident_allowed": False,
        "training_start_allowed": False,
        "model_promotion_allowed": False,
        "report_truth_allowed": False,
        "hwp_apply_allowed": False,
        "path_exposed": False,
    }
    manifest = {
        **lineage,
        "lineage_sha256": _sha256(_canonical_json(lineage)),
    }
    return ResolvedProviderCandidateEvidence(
        montage=montage,
        manifest=manifest,
    )


def resolve_provider_candidate_evidence(
    *,
    project_id: UUID,
    analysis_job_id: UUID,
    pipeline_job_id: UUID,
    candidate_id: str,
    sam2_checkpoint: Mapping[str, Any],
    artifact_resolver: CandidateFrameArtifactResolver,
) -> ResolvedProviderCandidateEvidence:
    """Preserve the legacy same-candidate resolver path."""

    return _resolve_provider_candidate_evidence_bound(
        project_id=project_id,
        analysis_job_id=analysis_job_id,
        pipeline_job_id=pipeline_job_id,
        candidate_id=candidate_id,
        sam2_proposal_candidate_id=candidate_id,
        sam2_checkpoint=sam2_checkpoint,
        artifact_resolver=artifact_resolver,
        alias_binding=None,
    )


def resolve_aliased_provider_candidate_evidence(
    *,
    project_id: UUID,
    analysis_job_id: UUID,
    pipeline_job_id: UUID,
    review_candidate_id: str,
    sam2_proposal_candidate_id: str,
    temporal_checkpoint: Mapping[str, Any],
    sam2_checkpoint: Mapping[str, Any],
    artifact_resolver: AliasedCandidateFrameArtifactResolver,
) -> ResolvedProviderCandidateEvidence:
    """Resolve an explicitly bound temporal-review/SAM2 candidate pair."""

    if not callable(artifact_resolver):
        raise ProviderCandidateEvidenceResolutionError(
            "candidate_alias_binding_invalid",
            "aliased candidate artifact resolver is invalid",
        )
    alias_binding = bind_review_candidate_to_sam2_proposal(
        project_id=project_id,
        analysis_job_id=analysis_job_id,
        pipeline_job_id=pipeline_job_id,
        review_candidate_id=review_candidate_id,
        sam2_proposal_candidate_id=(
            sam2_proposal_candidate_id
        ),
        temporal_checkpoint=temporal_checkpoint,
        sam2_checkpoint=sam2_checkpoint,
    )

    def resolve_frame(
        *,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        candidate_id: str,
        evidence_ref: str,
    ) -> ResolvedCandidateFrameArtifact:
        if candidate_id != review_candidate_id:
            raise ProviderCandidateEvidenceResolutionError(
                "candidate_alias_artifact_scope_mismatch",
                "aliased artifact request changed review identity",
            )
        artifact = artifact_resolver(
            project_id=project_id,
            analysis_job_id=analysis_job_id,
            pipeline_job_id=pipeline_job_id,
            review_candidate_id=review_candidate_id,
            sam2_proposal_candidate_id=(
                sam2_proposal_candidate_id
            ),
            evidence_ref=evidence_ref,
        )
        if not isinstance(
            artifact, ResolvedAliasedCandidateFrameArtifact
        ):
            raise ProviderCandidateEvidenceResolutionError(
                "candidate_evidence_artifact_invalid",
                "aliased artifact resolver returned an invalid type",
            )
        if (
            artifact.project_id != project_id
            or artifact.analysis_job_id != analysis_job_id
            or artifact.pipeline_job_id != pipeline_job_id
            or artifact.review_candidate_id
            != review_candidate_id
            or artifact.sam2_proposal_candidate_id
            != sam2_proposal_candidate_id
            or artifact.evidence_ref != evidence_ref
        ):
            raise ProviderCandidateEvidenceResolutionError(
                "candidate_alias_artifact_scope_mismatch",
                "aliased artifact crosses candidate or job scope",
            )
        return ResolvedCandidateFrameArtifact(
            project_id=artifact.project_id,
            analysis_job_id=artifact.analysis_job_id,
            pipeline_job_id=artifact.pipeline_job_id,
            candidate_id=review_candidate_id,
            evidence_ref=artifact.evidence_ref,
            timestamp_seconds=artifact.timestamp_seconds,
            body=artifact.body,
            media_type=artifact.media_type,
            size_bytes=artifact.size_bytes,
            sha256=artifact.sha256,
        )

    return _resolve_provider_candidate_evidence_bound(
        project_id=project_id,
        analysis_job_id=analysis_job_id,
        pipeline_job_id=pipeline_job_id,
        candidate_id=review_candidate_id,
        sam2_proposal_candidate_id=(
            sam2_proposal_candidate_id
        ),
        sam2_checkpoint=sam2_checkpoint,
        artifact_resolver=resolve_frame,
        alias_binding=alias_binding,
    )


__all__ = [
    "ALIAS_MAPPING_SCHEMA_VERSION",
    "AliasedCandidateFrameArtifactResolver",
    "CandidateFrameArtifactResolver",
    "INPUT_SCHEMA_VERSION",
    "OUTPUT_SCHEMA_VERSION",
    "ProviderCandidateEvidenceResolutionError",
    "ResolvedAliasedCandidateFrameArtifact",
    "ResolvedCandidateAliasBinding",
    "ResolvedCandidateFrameArtifact",
    "ResolvedProviderCandidateEvidence",
    "bind_review_candidate_to_sam2_proposal",
    "resolve_aliased_provider_candidate_evidence",
    "resolve_provider_candidate_evidence",
    "sam2_evidence_from_checkpoint",
]
