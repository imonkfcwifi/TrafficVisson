"""Pure policy helpers for provisional, command-driven AI labels.

This module intentionally performs no file, network, model, or database work.
It compiles an operator command to a fixed allowlisted prompt set, validates an
existing experimental proposal, and produces a separate provisional decision.
Neither a proposal nor an auto-accepted decision is a human approval or a
training annotation.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple
from uuid import UUID

from .candidate_labels import (
    CANONICAL_ISSUE_TYPES,
    ISSUE_TYPE_ALIASES,
    CandidateLabelError,
    canonical_issue_type,
)


COMMAND_SCHEMA = "trafficvisson.auto-label-command.v1"
DECISION_SCHEMA = "trafficvisson.provisional-auto-label-decision.v1"
PROPOSAL_SCHEMA = "trafficvisson.experimental-candidate-proposal.v1"
MAPPING_VERSION = "trafficvisson.command-map.2026-07-28.v1"
PROMPT_SET_VERSION = "trafficvisson.road-prompts.2026-07-28.v1"
QC_SAMPLE_VERSION = "trafficvisson.auto-label-qc-10pct.v1"

ISSUE_TYPES: Tuple[str, ...] = (*CANONICAL_ISSUE_TYPES, "other")

PROPOSAL_ONLY = "proposal_only"
HIGH_CONFIDENCE_AUTO_ACCEPT = "high_confidence_auto_accept"

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_PACK_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PACK_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,31}$")
_DATASET_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_DATASET_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")
_EVALUATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PRODUCER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_PROVIDERS = {
    "onnx_yolo_v8": (
        "model_score",
        "model_score_not_probability",
    ),
    "transformers_clip_zero_shot": (
        "zero_shot_relative",
        "zero_shot_relative_not_probability",
    ),
    "transformers_grounding_dino": (
        "model_score",
        "model_score_not_probability",
    ),
}

_NORMAL_ROAD_NEGATIVE_PROMPTS: Tuple[str, ...] = (
    "normal intact road surface with no maintenance defect",
    "clear unobstructed road signs and fully visible road markings",
    "normal roadway without debris or damaged roadside facilities",
)

_PROMPT_PROFILES: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "pothole": {
        "grounding_dino": ("pothole in the road surface",),
        "clip": (
            "a road scene containing a visible pothole",
            "damaged asphalt with a pothole",
        ),
    },
    "road_surface_damage": {
        "grounding_dino": ("cracked or broken road surface",),
        "clip": (
            "a road surface with cracks or structural damage",
            "damaged pavement requiring road repair",
        ),
    },
    "road_debris": {
        "grounding_dino": ("foreign object or debris on the roadway",),
        "clip": (
            "a roadway containing fallen debris",
            "an unexpected object obstructing a traffic lane",
        ),
    },
    "median_barrier_damage": {
        "grounding_dino": ("damaged median barrier",),
        "clip": (
            "a road median barrier with visible damage",
            "a broken central divider beside traffic lanes",
        ),
    },
    "sign_damage": {
        "grounding_dino": ("damaged or fallen road sign",),
        "clip": (
            "a bent broken or fallen road sign requiring repair",
            "a visibly damaged road sign or support structure",
        ),
    },
    "sign_vegetation_occlusion": {
        "grounding_dino": ("road sign obscured by vegetation",),
        "clip": (
            "vegetation blocking the readable area of a road sign",
            "a road direction sign hidden by branches or leaves",
        ),
    },
    "roadside_mowing_required": {
        "grounding_dino": ("overgrown roadside grass or vegetation",),
        "clip": (
            "overgrown roadside vegetation requiring mowing",
            "tall grass encroaching on a maintained road area",
        ),
    },
    "lane_marking_faded": {
        "grounding_dino": ("faded lane marking on the road",),
        "clip": (
            "a worn lane marking requiring repainting",
            "a road lane line with substantially faded paint",
        ),
    },
    "colored_guidance_faded": {
        "grounding_dino": ("faded colored guidance line on the road",),
        "clip": (
            "a worn colored road guidance line requiring repainting",
            "a roadway color guidance marking with faded paint",
        ),
    },
    "guide_sign_identity": {
        "grounding_dino": ("road direction sign with location text",),
        "clip": (
            "a readable road direction sign containing place names",
            "a road sign whose printed identity must be verified",
        ),
    },
    "tunnel_identity": {
        "grounding_dino": ("tunnel entrance sign with tunnel name",),
        "clip": (
            "a tunnel entrance displaying its name",
            "a tunnel name sign requiring text verification",
        ),
    },
    "tunnel_name_occlusion": {
        "grounding_dino": ("tunnel name sign obscured by vegetation",),
        "clip": (
            "branches or vegetation blocking a tunnel name sign",
            "an obscured tunnel entrance name requiring vegetation removal",
        ),
    },
    "other": {
        "grounding_dino": ("visible road maintenance anomaly",),
        "clip": (
            "a road scene containing an operator-specified anomaly",
            "a visible road maintenance issue outside named classes",
        ),
    },
}

_FLAGS: Dict[str, Tuple[bool, bool, bool]] = {
    # requires_ocr, requires_relationship, requires_segmentation
    "pothole": (False, False, True),
    "road_surface_damage": (False, False, True),
    "road_debris": (False, False, True),
    "median_barrier_damage": (False, False, True),
    "sign_damage": (False, False, True),
    "sign_vegetation_occlusion": (False, True, True),
    "roadside_mowing_required": (False, True, True),
    "lane_marking_faded": (False, False, True),
    "colored_guidance_faded": (False, False, True),
    "guide_sign_identity": (True, False, False),
    "tunnel_identity": (True, False, False),
    "tunnel_name_occlusion": (True, True, True),
    "other": (False, False, False),
}

# A rule is satisfied when every term in one of its term groups is present.
# Raw command text is never copied into a model prompt.
_COMMAND_RULES: Dict[str, Tuple[Tuple[str, ...], ...]] = {
    "pothole": (
        ("포트홀",),
        ("pothole",),
        ("pot", "hole"),
    ),
    "road_surface_damage": (
        ("도로", "파손"),
        ("노면", "파손"),
        ("노면", "균열"),
        ("도로", "깨"),
        ("road", "surface", "damage"),
        ("pavement", "damage"),
        ("road", "crack"),
        ("cracked", "road"),
    ),
    "road_debris": (
        ("낙하물",),
        ("도로", "물체"),
        ("road", "debris"),
        ("fallen", "object"),
        ("object", "roadway"),
    ),
    "median_barrier_damage": (
        ("중앙분리대", "파손"),
        ("중앙", "분리대", "파손"),
        ("median", "barrier", "damage"),
        ("damaged", "median", "barrier"),
    ),
    "sign_damage": (
        ("표지판", "파손"),
        ("이정표", "파손"),
        ("표지판", "전도"),
        ("표지판", "찌그러"),
        ("damaged", "road", "sign"),
        ("fallen", "road", "sign"),
    ),
    "sign_vegetation_occlusion": (
        ("표지판", "가려"),
        ("표지판", "가린"),
        ("이정표", "가려"),
        ("이정표", "가린"),
        ("표지판", "식생"),
        ("이정표", "수풀"),
        ("표지판", "수풀"),
        ("표지판", "나뭇가지"),
        ("sign", "vegetation"),
        ("obscured", "sign"),
        ("sign", "branches"),
    ),
    "roadside_mowing_required": (
        ("삭초",),
        ("예초",),
        ("풀", "베"),
        ("풀", "제거"),
        ("mowing",),
        ("overgrown", "roadside"),
        ("cut", "roadside", "grass"),
    ),
    "lane_marking_faded": (
        ("차선", "지워"),
        ("차선", "도색"),
        ("차선", "재도색"),
        ("lane", "marking", "faded"),
        ("faded", "lane", "line"),
        ("repaint", "lane"),
    ),
    "colored_guidance_faded": (
        ("색깔유도선",),
        ("색깔", "유도선"),
        ("color", "guidance", "line"),
        ("colored", "guidance", "line"),
        ("coloured", "guidance", "line"),
    ),
    "guide_sign_identity": (
        ("이정표", "읽"),
        ("이정표", "검증"),
        ("표지판", "읽"),
        ("sign", "identity"),
        ("read", "road", "sign"),
        ("verify", "sign", "text"),
    ),
    "tunnel_identity": (
        ("터널명", "읽"),
        ("터널명", "검증"),
        ("터널", "이름", "읽"),
        ("터널", "이름", "검증"),
        ("read", "tunnel", "name"),
        ("verify", "tunnel", "name"),
        ("tunnel", "identity"),
    ),
    "tunnel_name_occlusion": (
        ("터널명", "가려"),
        ("터널명", "가린"),
        ("터널", "이름", "수풀"),
        ("터널", "이름", "식생"),
        ("터널", "명판", "나뭇가지"),
        ("tunnel", "name", "vegetation"),
        ("obscured", "tunnel", "name"),
    ),
}


class AutoLabelingError(ValueError):
    """Base error for a rejected command, proposal, or policy input."""


class UnknownLabelingCommand(AutoLabelingError):
    """A command could not be resolved to exactly one allowlisted class."""


class InvalidExperimentalProposal(AutoLabelingError):
    """An experimental proposal does not satisfy its provenance contract."""


class ProposalEvidenceMismatch(InvalidExperimentalProposal):
    """A proposal points at evidence other than the expected candidate."""


class InvalidAutoLabelPolicy(AutoLabelingError):
    """An auto-label policy snapshot is malformed."""


def _canonical_issue(
    value: object,
    field_name: str,
    error_type: type[AutoLabelingError],
) -> str:
    try:
        canonical = canonical_issue_type(value)
    except CandidateLabelError as error:
        raise error_type(f"{field_name} is not allowlisted") from error
    if canonical not in ISSUE_TYPES:
        raise error_type(f"{field_name} is not allowlisted")
    return canonical


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise AutoLabelingError("data must be finite JSON") from error


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _normalise_command(command: str) -> str:
    if not isinstance(command, str):
        raise UnknownLabelingCommand("command must be text")
    value = unicodedata.normalize("NFKC", command).strip().casefold()
    if not value or len(value) > 500:
        raise UnknownLabelingCommand(
            "command must contain between 1 and 500 characters"
        )
    if any(
        unicodedata.category(character) in {"Cc", "Cs"}
        and not character.isspace()
        for character in value
    ):
        raise UnknownLabelingCommand("command contains unsupported controls")
    value = re.sub(r"[\W_]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def _term_present(command: str, term: str) -> bool:
    normalised_term = _normalise_command(term)
    if any("가" <= character <= "힣" for character in normalised_term):
        return normalised_term.replace(" ", "") in command.replace(" ", "")
    return f" {normalised_term} " in f" {command} "


def _matches_rule(
    command: str,
    rule: Sequence[Sequence[str]],
) -> bool:
    return any(
        all(_term_present(command, term) for term in term_group)
        for term_group in rule
    )


def _prompt_payload(issue_type: str) -> Dict[str, Any]:
    profile = _PROMPT_PROFILES[issue_type]
    return {
        "issue_type": issue_type,
        "positive_prompts": {
            "grounding_dino": list(profile["grounding_dino"]),
            "clip": list(profile["clip"]),
        },
        "negative_prompts": list(_NORMAL_ROAD_NEGATIVE_PROMPTS),
    }


def compile_labeling_command(
    command: str,
    *,
    explicit_issue_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Compile operator text to fixed, bounded prompts for one issue class.

    Unmapped or multi-class text is rejected unless the caller supplies an
    explicit allowlisted issue type. The raw command is hashed for provenance
    but is never interpolated into a model prompt.
    """

    normalised_command = _normalise_command(command)
    if explicit_issue_type is not None:
        issue_type = _canonical_issue(
            explicit_issue_type,
            "explicit_issue_type",
            UnknownLabelingCommand,
        )
        resolution = "explicit_issue_type"
    else:
        matches = [
            issue_type
            for issue_type, rules in _COMMAND_RULES.items()
            if _matches_rule(normalised_command, rules)
        ]
        if len(matches) != 1:
            detail = "unmapped" if not matches else "ambiguous"
            raise UnknownLabelingCommand(
                f"command is {detail}; provide explicit_issue_type"
            )
        issue_type = matches[0]
        resolution = "natural_language"

    prompt_payload = _prompt_payload(issue_type)
    requires_ocr, requires_relationship, requires_segmentation = _FLAGS[
        issue_type
    ]
    command_identity = {
        "normalised_command": normalised_command,
        "explicit_issue_type": (
            issue_type if explicit_issue_type is not None else None
        ),
    }
    return {
        "schema_version": COMMAND_SCHEMA,
        "issue_type": issue_type,
        "resolution": resolution,
        "requires_ocr": requires_ocr,
        "requires_relationship": requires_relationship,
        "requires_segmentation": requires_segmentation,
        "positive_prompts": prompt_payload["positive_prompts"],
        "negative_prompts": prompt_payload["negative_prompts"],
        "mapping_version": MAPPING_VERSION,
        "prompt_set_version": PROMPT_SET_VERSION,
        "command_sha256": _sha256_json(command_identity),
        "prompt_sha256": _sha256_json(prompt_payload),
    }


def _expect_mapping(value: Any, field_name: str, error_type: type) -> Mapping:
    if not isinstance(value, Mapping):
        raise error_type(f"{field_name} must be an object")
    return value


def _expect_keys(
    value: Mapping,
    *,
    required: Sequence[str],
    optional: Sequence[str] = (),
    field_name: str,
    error_type: type,
) -> None:
    keys = set(value.keys())
    allowed = set(required) | set(optional)
    missing = set(required) - keys
    unknown = keys - allowed
    if missing or unknown:
        raise error_type(
            f"{field_name} has missing or unknown fields"
        )


def _bounded_text(
    value: Any,
    field_name: str,
    *,
    minimum: int,
    maximum: int,
    pattern: Optional[re.Pattern] = None,
    error_type: type = InvalidExperimentalProposal,
) -> str:
    if (
        not isinstance(value, str)
        or len(value) < minimum
        or len(value) > maximum
        or any(
            unicodedata.category(character) in {"Cc", "Cs"}
            for character in value
        )
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        raise error_type(f"{field_name} is invalid")
    return value


def _sha256(
    value: Any,
    field_name: str,
    error_type: type = InvalidExperimentalProposal,
) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise error_type(f"{field_name} must be a lowercase SHA-256")
    return value


def _uuid(
    value: Any,
    field_name: str,
    error_type: type = InvalidExperimentalProposal,
) -> str:
    if isinstance(value, UUID):
        return str(value)
    if not isinstance(value, str):
        raise error_type(f"{field_name} must be a UUID")
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise error_type(f"{field_name} must be a UUID") from error


def _finite_number(
    value: Any,
    field_name: str,
    *,
    minimum: float = 0.0,
    maximum: float = 1.0,
    error_type: type = InvalidExperimentalProposal,
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise error_type(f"{field_name} must be a finite JSON number")
    result = float(value)
    if result < minimum or result > maximum:
        raise error_type(f"{field_name} is outside its allowed range")
    return result


def _timestamp(value: Any) -> str:
    if isinstance(value, str):
        source = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(source)
        except ValueError as error:
            raise InvalidExperimentalProposal(
                "created_at must be an ISO-8601 timestamp"
            ) from error
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise InvalidExperimentalProposal(
            "created_at must be an ISO-8601 timestamp"
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidExperimentalProposal("created_at must include a timezone")
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _normalise_box(value: Any) -> Optional[Dict[str, float]]:
    if value is None:
        return None
    box = _expect_mapping(
        value,
        "prediction.box",
        InvalidExperimentalProposal,
    )
    _expect_keys(
        box,
        required=("x", "y", "width", "height"),
        field_name="prediction.box",
        error_type=InvalidExperimentalProposal,
    )
    result = {
        "x": _finite_number(box["x"], "prediction.box.x"),
        "y": _finite_number(box["y"], "prediction.box.y"),
        "width": _finite_number(
            box["width"],
            "prediction.box.width",
            minimum=0.0000000001,
        ),
        "height": _finite_number(
            box["height"],
            "prediction.box.height",
            minimum=0.0000000001,
        ),
    }
    if (
        result["x"] + result["width"] > 1.00000001
        or result["y"] + result["height"] > 1.00000001
    ):
        raise InvalidExperimentalProposal(
            "prediction.box must remain inside the image"
        )
    return result


def _normalise_model(value: Any) -> Dict[str, str]:
    model = _expect_mapping(
        value,
        "proposal.model",
        InvalidExperimentalProposal,
    )
    _expect_keys(
        model,
        required=("provider", "pack_id", "pack_version", "manifest_sha256"),
        field_name="proposal.model",
        error_type=InvalidExperimentalProposal,
    )
    provider = model["provider"]
    if provider not in _PROVIDERS:
        raise InvalidExperimentalProposal("proposal.model.provider is invalid")
    return {
        "provider": provider,
        "pack_id": _bounded_text(
            model["pack_id"],
            "proposal.model.pack_id",
            minimum=1,
            maximum=64,
            pattern=_PACK_ID,
        ),
        "pack_version": _bounded_text(
            model["pack_version"],
            "proposal.model.pack_version",
            minimum=1,
            maximum=32,
            pattern=_PACK_VERSION,
        ),
        "manifest_sha256": _sha256(
            model["manifest_sha256"],
            "proposal.model.manifest_sha256",
        ),
    }


def _normalise_dataset(value: Any) -> Optional[Dict[str, str]]:
    if value is None:
        return None
    dataset = _expect_mapping(
        value,
        "proposal.dataset",
        InvalidExperimentalProposal,
    )
    _expect_keys(
        dataset,
        required=("dataset_id", "dataset_version", "dataset_sha256"),
        field_name="proposal.dataset",
        error_type=InvalidExperimentalProposal,
    )
    return {
        "dataset_id": _bounded_text(
            dataset["dataset_id"],
            "proposal.dataset.dataset_id",
            minimum=1,
            maximum=128,
            pattern=_DATASET_ID,
        ),
        "dataset_version": _bounded_text(
            dataset["dataset_version"],
            "proposal.dataset.dataset_version",
            minimum=1,
            maximum=64,
            pattern=_DATASET_VERSION,
        ),
        "dataset_sha256": _sha256(
            dataset["dataset_sha256"],
            "proposal.dataset.dataset_sha256",
        ),
    }


def _normalise_evaluation(value: Any) -> Dict[str, str]:
    evaluation = _expect_mapping(
        value,
        "proposal.evaluation",
        InvalidExperimentalProposal,
    )
    _expect_keys(
        evaluation,
        required=("evaluation_id", "producer", "producer_version"),
        field_name="proposal.evaluation",
        error_type=InvalidExperimentalProposal,
    )
    return {
        "evaluation_id": _bounded_text(
            evaluation["evaluation_id"],
            "proposal.evaluation.evaluation_id",
            minimum=1,
            maximum=128,
            pattern=_EVALUATION_ID,
        ),
        "producer": _bounded_text(
            evaluation["producer"],
            "proposal.evaluation.producer",
            minimum=1,
            maximum=64,
            pattern=_PRODUCER,
        ),
        "producer_version": _bounded_text(
            evaluation["producer_version"],
            "proposal.evaluation.producer_version",
            minimum=1,
            maximum=32,
            pattern=_PACK_VERSION,
        ),
    }


def _normalise_source(value: Any) -> Dict[str, str]:
    source = _expect_mapping(
        value,
        "proposal.source",
        InvalidExperimentalProposal,
    )
    _expect_keys(
        source,
        required=(
            "representative_image_sha256",
            "analysis_candidate_sha256",
        ),
        field_name="proposal.source",
        error_type=InvalidExperimentalProposal,
    )
    return {
        "representative_image_sha256": _sha256(
            source["representative_image_sha256"],
            "proposal.source.representative_image_sha256",
        ),
        "analysis_candidate_sha256": _sha256(
            source["analysis_candidate_sha256"],
            "proposal.source.analysis_candidate_sha256",
        ),
    }


def _normalise_prediction(
    value: Any,
    *,
    expected_calibration: str,
) -> Dict[str, Any]:
    prediction = _expect_mapping(
        value,
        "proposal.prediction",
        InvalidExperimentalProposal,
    )
    _expect_keys(
        prediction,
        required=(
            "issue_type",
            "source_label",
            "calibration",
        ),
        optional=("relative_score", "model_score", "box"),
        field_name="proposal.prediction",
        error_type=InvalidExperimentalProposal,
    )
    issue_type = _canonical_issue(
        prediction["issue_type"],
        "proposal.prediction.issue_type",
        InvalidExperimentalProposal,
    )
    calibration = prediction["calibration"]
    if calibration != expected_calibration:
        raise InvalidExperimentalProposal(
            "proposal prediction calibration does not match its provider"
        )
    relative_score = prediction.get("relative_score")
    model_score = prediction.get("model_score")
    if calibration == "zero_shot_relative":
        if relative_score is None or model_score is not None:
            raise InvalidExperimentalProposal(
                "zero-shot prediction requires only relative_score"
            )
        score = _finite_number(
            relative_score,
            "proposal.prediction.relative_score",
        )
    else:
        if model_score is None or relative_score is not None:
            raise InvalidExperimentalProposal(
                "detector prediction requires only model_score"
            )
        score = _finite_number(
            model_score,
            "proposal.prediction.model_score",
        )

    box = _normalise_box(prediction.get("box"))
    if calibration == "zero_shot_relative" and box is not None:
        raise InvalidExperimentalProposal(
            "zero-shot prediction cannot claim a box"
        )
    if calibration == "model_score" and box is None:
        raise InvalidExperimentalProposal(
            "detector prediction requires a box"
        )
    return {
        "issue_type": issue_type,
        "relative_score": (
            score if calibration == "zero_shot_relative" else None
        ),
        "model_score": score if calibration == "model_score" else None,
        "box": box,
        "source_label": _bounded_text(
            prediction["source_label"],
            "proposal.prediction.source_label",
            minimum=1,
            maximum=128,
        ),
        "calibration": calibration,
    }


def _prediction_score(prediction: Mapping[str, Any]) -> float:
    if prediction["calibration"] == "zero_shot_relative":
        return prediction["relative_score"]
    return prediction["model_score"]


def _normalise_expected_evidence(value: Any) -> Dict[str, str]:
    evidence = _expect_mapping(
        value,
        "expected_evidence",
        ProposalEvidenceMismatch,
    )
    _expect_keys(
        evidence,
        required=(
            "project_id",
            "analysis_job_id",
            "candidate_id",
            "representative_image_sha256",
            "analysis_candidate_sha256",
        ),
        field_name="expected_evidence",
        error_type=ProposalEvidenceMismatch,
    )
    candidate_id = evidence["candidate_id"]
    if (
        not isinstance(candidate_id, str)
        or _CANDIDATE_ID.fullmatch(candidate_id) is None
    ):
        raise ProposalEvidenceMismatch(
            "expected_evidence.candidate_id is invalid"
        )
    return {
        "project_id": _uuid(
            evidence["project_id"],
            "expected_evidence.project_id",
            ProposalEvidenceMismatch,
        ),
        "analysis_job_id": _uuid(
            evidence["analysis_job_id"],
            "expected_evidence.analysis_job_id",
            ProposalEvidenceMismatch,
        ),
        "candidate_id": candidate_id,
        "representative_image_sha256": _sha256(
            evidence["representative_image_sha256"],
            "expected_evidence.representative_image_sha256",
            ProposalEvidenceMismatch,
        ),
        "analysis_candidate_sha256": _sha256(
            evidence["analysis_candidate_sha256"],
            "expected_evidence.analysis_candidate_sha256",
            ProposalEvidenceMismatch,
        ),
    }


def validate_experimental_proposal(
    proposal: Mapping[str, Any],
    *,
    expected_evidence: Mapping[str, Any],
) -> Dict[str, Any]:
    """Validate and normalise an immutable experimental proposal snapshot."""

    value = _expect_mapping(
        proposal,
        "proposal",
        InvalidExperimentalProposal,
    )
    _expect_keys(
        value,
        required=(
            "schema_version",
            "run_id",
            "project_id",
            "analysis_job_id",
            "candidate_id",
            "created_at",
            "status",
            "review_only",
            "automatic_labeling_allowed",
            "score_semantics",
            "model",
            "evaluation",
            "source",
            "predictions",
        ),
        optional=("dataset",),
        field_name="proposal",
        error_type=InvalidExperimentalProposal,
    )
    if value["schema_version"] != PROPOSAL_SCHEMA:
        raise InvalidExperimentalProposal("proposal schema is unsupported")
    if value["review_only"] is not True:
        raise InvalidExperimentalProposal("proposal must remain review_only")
    if value["automatic_labeling_allowed"] is not False:
        raise InvalidExperimentalProposal(
            "proposal cannot grant automatic labeling authority"
        )

    candidate_id = value["candidate_id"]
    if (
        not isinstance(candidate_id, str)
        or _CANDIDATE_ID.fullmatch(candidate_id) is None
    ):
        raise InvalidExperimentalProposal("proposal.candidate_id is invalid")
    status = value["status"]
    if status not in {"classified", "no_suggestion"}:
        raise InvalidExperimentalProposal("proposal.status is invalid")

    model = _normalise_model(value["model"])
    expected_calibration, expected_semantics = _PROVIDERS[
        model["provider"]
    ]
    if value["score_semantics"] != expected_semantics:
        raise InvalidExperimentalProposal(
            "proposal score semantics do not match its provider"
        )
    raw_predictions = value["predictions"]
    if (
        not isinstance(raw_predictions, list)
        or len(raw_predictions) > 32
    ):
        raise InvalidExperimentalProposal(
            "proposal.predictions must be a bounded array"
        )
    if status == "classified" and not raw_predictions:
        raise InvalidExperimentalProposal(
            "classified proposal requires a prediction"
        )
    if status == "no_suggestion" and raw_predictions:
        raise InvalidExperimentalProposal(
            "no_suggestion proposal cannot contain predictions"
        )
    predictions = [
        _normalise_prediction(
            prediction,
            expected_calibration=expected_calibration,
        )
        for prediction in raw_predictions
    ]
    if any(
        _prediction_score(predictions[index])
        < _prediction_score(predictions[index + 1])
        for index in range(len(predictions) - 1)
    ):
        raise InvalidExperimentalProposal(
            "proposal predictions must be score-sorted"
        )

    normalised = {
        "schema_version": PROPOSAL_SCHEMA,
        "run_id": _uuid(value["run_id"], "proposal.run_id"),
        "project_id": _uuid(value["project_id"], "proposal.project_id"),
        "analysis_job_id": _uuid(
            value["analysis_job_id"],
            "proposal.analysis_job_id",
        ),
        "candidate_id": candidate_id,
        "created_at": _timestamp(value["created_at"]),
        "status": status,
        "review_only": True,
        "automatic_labeling_allowed": False,
        "score_semantics": expected_semantics,
        "model": model,
        "dataset": _normalise_dataset(value.get("dataset")),
        "evaluation": _normalise_evaluation(value["evaluation"]),
        "source": _normalise_source(value["source"]),
        "predictions": predictions,
    }
    expected = _normalise_expected_evidence(expected_evidence)
    actual_identity = {
        "project_id": normalised["project_id"],
        "analysis_job_id": normalised["analysis_job_id"],
        "candidate_id": normalised["candidate_id"],
        **normalised["source"],
    }
    if actual_identity != expected:
        raise ProposalEvidenceMismatch(
            "proposal identity or source hash does not match expected evidence"
        )
    return normalised


def _validate_command_plan(value: Any) -> Dict[str, Any]:
    plan = _expect_mapping(
        value,
        "command_plan",
        AutoLabelingError,
    )
    _expect_keys(
        plan,
        required=(
            "schema_version",
            "issue_type",
            "resolution",
            "requires_ocr",
            "requires_relationship",
            "requires_segmentation",
            "positive_prompts",
            "negative_prompts",
            "mapping_version",
            "prompt_set_version",
            "command_sha256",
            "prompt_sha256",
        ),
        field_name="command_plan",
        error_type=AutoLabelingError,
    )
    raw_issue_type = plan["issue_type"]
    issue_type = _canonical_issue(
        raw_issue_type,
        "command_plan.issue_type",
        AutoLabelingError,
    )
    if plan["schema_version"] != COMMAND_SCHEMA:
        raise AutoLabelingError("command plan schema is unsupported")
    if plan["resolution"] not in {
        "natural_language",
        "explicit_issue_type",
    }:
        raise AutoLabelingError("command plan resolution is invalid")
    if (
        plan["mapping_version"] != MAPPING_VERSION
        or plan["prompt_set_version"] != PROMPT_SET_VERSION
    ):
        raise AutoLabelingError("command plan version is unsupported")
    expected_flags = _FLAGS[issue_type]
    supplied_flags = (
        plan["requires_ocr"],
        plan["requires_relationship"],
        plan["requires_segmentation"],
    )
    if (
        any(not isinstance(flag, bool) for flag in supplied_flags)
        or supplied_flags != expected_flags
    ):
        raise AutoLabelingError("command plan flags were modified")
    expected_prompts = _prompt_payload(issue_type)
    if (
        plan["positive_prompts"]
        != expected_prompts["positive_prompts"]
        or plan["negative_prompts"]
        != expected_prompts["negative_prompts"]
    ):
        raise AutoLabelingError("command plan prompts were modified")
    command_sha256 = _sha256(
        plan["command_sha256"],
        "command_plan.command_sha256",
        AutoLabelingError,
    )
    prompt_sha256 = _sha256(
        plan["prompt_sha256"],
        "command_plan.prompt_sha256",
        AutoLabelingError,
    )
    accepted_prompt_hashes = {_sha256_json(expected_prompts)}
    if raw_issue_type in ISSUE_TYPE_ALIASES:
        accepted_prompt_hashes.add(
            _sha256_json(
                {
                    **expected_prompts,
                    "issue_type": raw_issue_type,
                }
            )
        )
    if prompt_sha256 not in accepted_prompt_hashes:
        raise AutoLabelingError("command plan prompt hash is invalid")
    return {
        "schema_version": COMMAND_SCHEMA,
        "issue_type": issue_type,
        "resolution": plan["resolution"],
        "requires_ocr": expected_flags[0],
        "requires_relationship": expected_flags[1],
        "requires_segmentation": expected_flags[2],
        "positive_prompts": expected_prompts["positive_prompts"],
        "negative_prompts": expected_prompts["negative_prompts"],
        "mapping_version": MAPPING_VERSION,
        "prompt_set_version": PROMPT_SET_VERSION,
        "command_sha256": command_sha256,
        "prompt_sha256": _sha256_json(expected_prompts),
    }


def validate_command_plan(value: Any) -> Dict[str, Any]:
    """Validate an already compiled plan before optional model execution."""

    return _validate_command_plan(value)


def _normalise_policy(
    policy: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    if policy is None:
        return {
            "mode": PROPOSAL_ONLY,
            "per_class_reviews": {},
            "validated_thresholds": {},
        }
    value = _expect_mapping(
        policy,
        "policy",
        InvalidAutoLabelPolicy,
    )
    _expect_keys(
        value,
        required=("mode",),
        optional=("per_class_reviews", "validated_thresholds"),
        field_name="policy",
        error_type=InvalidAutoLabelPolicy,
    )
    mode = value["mode"]
    if mode not in {PROPOSAL_ONLY, HIGH_CONFIDENCE_AUTO_ACCEPT}:
        raise InvalidAutoLabelPolicy("policy.mode is invalid")

    raw_reviews = _expect_mapping(
        value.get("per_class_reviews", {}),
        "policy.per_class_reviews",
        InvalidAutoLabelPolicy,
    )
    reviews: Dict[str, Dict[str, Any]] = {}
    for raw_issue_type, raw_record in raw_reviews.items():
        issue_type = _canonical_issue(
            raw_issue_type,
            "policy review class",
            InvalidAutoLabelPolicy,
        )
        if issue_type in reviews:
            raise InvalidAutoLabelPolicy(
                "policy review classes contain duplicate aliases"
            )
        record = _expect_mapping(
            raw_record,
            f"policy.per_class_reviews.{raw_issue_type}",
            InvalidAutoLabelPolicy,
        )
        _expect_keys(
            record,
            required=("last_review_count", "agreement_rate"),
            field_name=f"policy.per_class_reviews.{raw_issue_type}",
            error_type=InvalidAutoLabelPolicy,
        )
        count = record["last_review_count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise InvalidAutoLabelPolicy(
                "policy last_review_count must be a non-negative integer"
            )
        reviews[issue_type] = {
            "last_review_count": count,
            "agreement_rate": _finite_number(
                record["agreement_rate"],
                "policy agreement_rate",
                error_type=InvalidAutoLabelPolicy,
            ),
        }

    raw_thresholds = _expect_mapping(
        value.get("validated_thresholds", {}),
        "policy.validated_thresholds",
        InvalidAutoLabelPolicy,
    )
    thresholds: Dict[str, Dict[str, Any]] = {}
    for raw_issue_type, raw_record in raw_thresholds.items():
        issue_type = _canonical_issue(
            raw_issue_type,
            "policy threshold class",
            InvalidAutoLabelPolicy,
        )
        if issue_type in thresholds:
            raise InvalidAutoLabelPolicy(
                "policy threshold classes contain duplicate aliases"
            )
        record = _expect_mapping(
            raw_record,
            f"policy.validated_thresholds.{raw_issue_type}",
            InvalidAutoLabelPolicy,
        )
        _expect_keys(
            record,
            required=(
                "project_id",
                "provider",
                "calibration",
                "threshold",
                "validated",
                "validation_evidence_sha256",
            ),
            field_name=f"policy.validated_thresholds.{raw_issue_type}",
            error_type=InvalidAutoLabelPolicy,
        )
        provider = record["provider"]
        if provider not in _PROVIDERS:
            raise InvalidAutoLabelPolicy(
                "policy threshold provider is invalid"
            )
        calibration = record["calibration"]
        if calibration not in {"model_score", "zero_shot_relative"}:
            raise InvalidAutoLabelPolicy(
                "policy threshold calibration is invalid"
            )
        if not isinstance(record["validated"], bool):
            raise InvalidAutoLabelPolicy(
                "policy threshold validated must be boolean"
            )
        thresholds[issue_type] = {
            "project_id": _uuid(
                record["project_id"],
                "policy threshold project_id",
                InvalidAutoLabelPolicy,
            ),
            "provider": provider,
            "calibration": calibration,
            "threshold": _finite_number(
                record["threshold"],
                "policy threshold",
                error_type=InvalidAutoLabelPolicy,
            ),
            "validated": record["validated"],
            "validation_evidence_sha256": _sha256(
                record["validation_evidence_sha256"],
                "policy threshold validation_evidence_sha256",
                InvalidAutoLabelPolicy,
            ),
        }
    return {
        "mode": mode,
        "per_class_reviews": reviews,
        "validated_thresholds": thresholds,
    }


def deterministic_qc_bucket(
    project_id: str,
    candidate_id: str,
    issue_type: str,
) -> int:
    """Return the stable 0..99 QC bucket for one project candidate class."""

    normalised_project = _uuid(
        project_id,
        "project_id",
        InvalidAutoLabelPolicy,
    )
    if (
        not isinstance(candidate_id, str)
        or _CANDIDATE_ID.fullmatch(candidate_id) is None
    ):
        raise InvalidAutoLabelPolicy("candidate_id is invalid")
    canonical = _canonical_issue(
        issue_type,
        "issue_type",
        InvalidAutoLabelPolicy,
    )
    identity = (
        f"{QC_SAMPLE_VERSION}|{normalised_project}|"
        f"{candidate_id}|{canonical}"
    )
    digest = hashlib.sha256(identity.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 100


def is_deterministic_qc_sample(
    project_id: str,
    candidate_id: str,
    issue_type: str,
) -> bool:
    """Select the fixed ten hash buckets reserved for human QC."""

    return deterministic_qc_bucket(
        project_id,
        candidate_id,
        issue_type,
    ) < 10


def create_provisional_auto_label_decision(
    *,
    command_plan: Mapping[str, Any],
    proposal: Mapping[str, Any],
    expected_evidence: Mapping[str, Any],
    policy: Optional[Mapping[str, Any]] = None,
    urgent: bool = False,
    maintenance_relation_decision: bool = False,
) -> Dict[str, Any]:
    """Create a deterministic provisional decision without human approval."""

    if not isinstance(urgent, bool):
        raise InvalidAutoLabelPolicy("urgent must be boolean")
    if not isinstance(maintenance_relation_decision, bool):
        raise InvalidAutoLabelPolicy(
            "maintenance_relation_decision must be boolean"
        )
    plan = _validate_command_plan(command_plan)
    validated_proposal = validate_experimental_proposal(
        proposal,
        expected_evidence=expected_evidence,
    )
    if validated_proposal["status"] != "classified":
        raise InvalidExperimentalProposal(
            "no_suggestion proposal cannot produce a label decision"
        )
    matching_predictions = [
        prediction
        for prediction in validated_proposal["predictions"]
        if prediction["issue_type"] == plan["issue_type"]
    ]
    if not matching_predictions:
        raise InvalidExperimentalProposal(
            "proposal does not predict the compiled issue type"
        )
    prediction = matching_predictions[0]
    score_value = _prediction_score(prediction)
    policy_snapshot = _normalise_policy(policy)
    issue_type = plan["issue_type"]

    qc_sample = is_deterministic_qc_sample(
        validated_proposal["project_id"],
        validated_proposal["candidate_id"],
        issue_type,
    )
    mandatory_review_reasons = []
    if issue_type == "road_debris":
        mandatory_review_reasons.append("road_debris")
    if urgent:
        mandatory_review_reasons.append("urgent")
    if plan["requires_ocr"]:
        mandatory_review_reasons.append("ocr_identity")
    if (
        plan["requires_relationship"]
        or maintenance_relation_decision
    ):
        mandatory_review_reasons.append("maintenance_relation")
    if qc_sample:
        mandatory_review_reasons.append("deterministic_qc_sample")

    gate_reasons = []
    if policy_snapshot["mode"] != HIGH_CONFIDENCE_AUTO_ACCEPT:
        gate_reasons.append("proposal_only_policy")
    review_stats = policy_snapshot["per_class_reviews"].get(issue_type)
    if review_stats is None:
        gate_reasons.append("missing_per_class_review_history")
    else:
        if review_stats["last_review_count"] < 30:
            gate_reasons.append("fewer_than_30_last_reviews")
        if review_stats["agreement_rate"] < 0.90:
            gate_reasons.append("agreement_below_0_90")

    threshold = policy_snapshot["validated_thresholds"].get(issue_type)
    if threshold is None:
        gate_reasons.append("missing_validated_score_threshold")
    else:
        if not threshold["validated"]:
            gate_reasons.append("score_threshold_not_validated")
        if threshold["project_id"] != validated_proposal["project_id"]:
            gate_reasons.append("score_threshold_project_mismatch")
        if threshold["provider"] != validated_proposal["model"]["provider"]:
            gate_reasons.append("score_threshold_provider_mismatch")
        if threshold["calibration"] != prediction["calibration"]:
            gate_reasons.append("score_threshold_calibration_mismatch")
        if score_value < threshold["threshold"]:
            gate_reasons.append("score_below_validated_threshold")

    can_auto_accept = not mandatory_review_reasons and not gate_reasons
    label_source = (
        "ai_auto_accepted" if can_auto_accept else "ai_proposed"
    )
    review_status = "auto_accepted" if can_auto_accept else "pending"

    decision: Dict[str, Any] = {
        "schema_version": DECISION_SCHEMA,
        "decision_kind": "provisional_auto_label",
        "project_id": validated_proposal["project_id"],
        "analysis_job_id": validated_proposal["analysis_job_id"],
        "candidate_id": validated_proposal["candidate_id"],
        "issue_type": issue_type,
        "label_source": label_source,
        "review_status": review_status,
        "human_approved": False,
        "include_in_training": False,
        "human_review_required": not can_auto_accept,
        "urgent": urgent,
        "maintenance_relation_decision": (
            plan["requires_relationship"]
            or maintenance_relation_decision
        ),
        "prediction": {
            "score_value": score_value,
            "score_semantics": validated_proposal["score_semantics"],
            "score_is_probability": False,
            "calibration": prediction["calibration"],
            "box": prediction["box"],
        },
        "policy": {
            "mode": policy_snapshot["mode"],
            "auto_accept_eligible": can_auto_accept,
            "qc_sample": qc_sample,
            "qc_bucket": deterministic_qc_bucket(
                validated_proposal["project_id"],
                validated_proposal["candidate_id"],
                issue_type,
            ),
            "qc_sample_version": QC_SAMPLE_VERSION,
            "mandatory_review_reasons": mandatory_review_reasons,
            "gate_reasons": gate_reasons,
            "per_class_review_snapshot": review_stats,
            "score_threshold_snapshot": threshold,
        },
        "provenance": {
            "source": validated_proposal["source"],
            "evidence": {
                "proposal_schema_version": PROPOSAL_SCHEMA,
                "run_id": validated_proposal["run_id"],
                "project_id": validated_proposal["project_id"],
                "analysis_job_id": validated_proposal["analysis_job_id"],
                "candidate_id": validated_proposal["candidate_id"],
                "proposal_created_at": validated_proposal["created_at"],
                "evaluation": validated_proposal["evaluation"],
                "dataset": validated_proposal["dataset"],
            },
            "model": {
                **validated_proposal["model"],
                "score_semantics": validated_proposal["score_semantics"],
                "prediction_calibration": prediction["calibration"],
                "source_label": prediction["source_label"],
            },
            "prompt": {
                "command_sha256": plan["command_sha256"],
                "mapping_version": plan["mapping_version"],
                "prompt_set_version": plan["prompt_set_version"],
                "prompt_sha256": plan["prompt_sha256"],
                "positive_prompts": plan["positive_prompts"],
                "negative_prompts": plan["negative_prompts"],
                "resolution": plan["resolution"],
            },
        },
    }
    decision["decision_sha256"] = _sha256_json(decision)
    return decision


# Descriptive aliases for callers that prefer shorter service names.
compile_auto_label_command = compile_labeling_command
build_provisional_auto_label_decision = (
    create_provisional_auto_label_decision
)
