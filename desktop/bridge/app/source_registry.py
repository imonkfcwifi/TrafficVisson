"""Pure validation for local model and public labeled-dataset sources.

The registry is deliberately an inactive catalogue. It does not read paths,
download URLs, unpack archives, load models, import labels, or mutate project
datasets. A trusted outer layer must hash local files and pass the observations
to this module. Only a source manifest whose canonical digest is present in a
separately pinned allowlist can become a ``verified_inactive`` registration.

Registration never turns external labels into user-approved annotations and
never turns model output into an approved road incident.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit


SOURCE_MANIFEST_SCHEMA = "trafficvisson.external-source-manifest.v1"
ALLOWLIST_SCHEMA = "trafficvisson.external-source-allowlist.v1"
REGISTRATION_SCHEMA = "trafficvisson.external-source-registration.v1"
REGISTRY_SCHEMA = "trafficvisson.external-source-registry.v1"
SOURCE_CANDIDATE_CATALOG_SCHEMA = (
    "trafficvisson.source-candidate-catalog.v1"
)

SOURCE_TYPES = ("model_pack", "labeled_dataset")
MODEL_ENGINES = (
    "onnx_yolo_v8",
    "transformers_clip_zero_shot",
    "easyocr_ocr_only",
)
CANONICAL_ISSUE_TYPES = (
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
ISSUE_TYPES = (*CANONICAL_ISSUE_TYPES, "other")
ISSUE_TYPE_ALIASES = {
    "surface_damage": "road_surface_damage",
    "debris": "road_debris",
    "sign_vegetation": "sign_vegetation_occlusion",
    "mowing": "roadside_mowing_required",
    "sign_tunnel_identity": "guide_sign_identity",
    "sign_identity": "guide_sign_identity",
    "lane_color_line_fading": "colored_guidance_faded",
    "color_guidance_line_faded": "colored_guidance_faded",
    "colored_guidance_line_faded": "colored_guidance_faded",
}
INTENDED_USES = (
    "candidate_proposals",
    "ocr_proposals",
    "training_seed",
    "evaluation_reference",
    "retrieval_reference",
)
_MODEL_USES = frozenset(("candidate_proposals", "ocr_proposals"))
_DATASET_USES = frozenset(
    ("training_seed", "evaluation_reference", "retrieval_reference")
)
_MODEL_LICENSE_SCOPES = frozenset(("runtime", "weights"))
_DATASET_LICENSE_SCOPES = frozenset(("images", "annotations"))
_MODEL_ARTIFACT_ROLES = frozenset(
    (
        "model_manifest",
        "model_weights",
        "model_config",
        "tokenizer",
        "ocr_weights",
        "label_map",
    )
)
_DATASET_ARTIFACT_ROLES = frozenset(
    (
        "dataset_manifest",
        "images_archive",
        "image",
        "annotations",
        "splits",
        "checksums",
        "documentation",
    )
)
_MEDIA_TYPES = frozenset(
    (
        "application/json",
        "application/onnx",
        "application/octet-stream",
        "application/safetensors",
        "application/zip",
        "application/x-tar",
        "application/zstd",
        "image/jpeg",
        "image/png",
        "text/csv",
        "text/plain",
    )
)
_BLOCKED_SUFFIXES = frozenset(
    (
        ".bat",
        ".cmd",
        ".com",
        ".dll",
        ".exe",
        ".jar",
        ".js",
        ".msi",
        ".ps1",
        ".py",
        ".pyd",
        ".scr",
        ".so",
        ".vbs",
    )
)
_ANNOTATION_FORMATS = frozenset(
    ("coco_json", "yolo_txt", "trafficvisson_json")
)
_SPLIT_UNITS = frozenset(
    ("source_sequence", "trip", "video", "publisher_split")
)
_PRIVACY_STATUSES = frozenset(
    ("not_present", "source_redacted", "requires_local_redaction")
)

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")
_SPDX_ID = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9.+-]{0,63}|"
    r"LicenseRef-[A-Za-z0-9][A-Za-z0-9.-]{0,52})$"
)
_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$")
_HOST = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}$"
)
_DISALLOWED_REVISIONS = frozenset(
    ("head", "latest", "main", "master", "trunk", "unknown")
)

MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_ALLOWLIST_ENTRIES = 512
MAX_REGISTRY_SOURCES = 512
MAX_MODEL_FILES = 512
MAX_DATASET_FILES = 4_096
MAX_MODEL_BYTES = 12 * 1024**3
MAX_DATASET_BYTES = 2 * 1024**4


def build_source_candidate_catalog(
    local_observations: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return bundled candidate metadata without touching external sources.

    This catalogue is intentionally separate from the verified source
    registry below. Listing a candidate never downloads or executes weights,
    creates project annotations, or grants ``verified_inactive`` status.
    """

    observed_sources: Mapping[str, Any] = {}
    if (
        isinstance(local_observations, Mapping)
        and local_observations.get("schema_version")
        == "trafficvisson.local-source-observation.v1"
        and isinstance(local_observations.get("sources"), Mapping)
    ):
        observed_sources = local_observations["sources"]

    def observation(source_id: str) -> Dict[str, Any]:
        raw = observed_sources.get(source_id)
        if not isinstance(raw, Mapping):
            return {"state": "absent", "evaluation": None}
        state = raw.get("state")
        if state not in {
            "absent",
            "unverified_local",
            "verified_snapshot",
        }:
            return {"state": "absent", "evaluation": None}
        evaluation = raw.get("evaluation")
        return {
            "state": state,
            "evaluation": (
                dict(evaluation)
                if state == "verified_snapshot"
                and isinstance(evaluation, Mapping)
                else None
            ),
        }

    grounding = observation("idea-research-grounding-dino-tiny")
    clip = observation("laion-clip-b32-road-baseline")
    pothole = observation("peterhdd-pothole-yolov8-local-cache")

    def observed_status(
        row: Mapping[str, Any],
    ) -> Tuple[str, str, str]:
        if row["state"] == "verified_snapshot":
            return (
                "evaluation_complete",
                "installed_validation_only",
                "verified_local_snapshot",
            )
        if row["state"] == "unverified_local":
            return (
                "unverified_local",
                "local_cache_quarantined",
                "unverified_local_files",
            )
        return ("review_candidate", "not_installed", "not_observed")

    grounding_status = observed_status(grounding)
    clip_status = observed_status(clip)
    pothole_installation = (
        "local_cache_quarantined"
        if pothole["state"] != "absent"
        else "not_installed"
    )
    pothole_basis = {
        "verified_snapshot": "verified_local_snapshot",
        "unverified_local": "unverified_local_files",
        "absent": "catalog_policy_only",
    }[pothole["state"]]

    candidates = [
        {
            "source_id": "idea-research-grounding-dino-tiny",
            "display_name": "Grounding DINO Tiny",
            "source_type": "model_pack",
            "publisher": "IDEA Research",
            "origin_url": (
                "https://huggingface.co/IDEA-Research/grounding-dino-tiny"
            ),
            "license_spdx_id": "Apache-2.0",
            "intended_use": "candidate_proposals",
            "capability": "natural_language_box_proposals",
            "catalog_status": grounding_status[0],
            "installation_status": grounding_status[1],
            "evaluation": grounding["evaluation"],
            "observation_basis": grounding_status[2],
            "rights_review_required": True,
            "automatic_download": False,
            "automatic_activation": False,
            "weight_execution_enabled": False,
            "project_training_linked": False,
            "human_review_required": True,
            "decision_reason": (
                (
                    "고정 해시와 일치하는 로컬 검증용 팩·평가 기록을 "
                    "확인했습니다. 결정적 30프레임 중 25프레임에서 47개 "
                    "박스를 제안했지만 자동 라벨·사건·모델 승격은 0건이며 "
                    "사용자 검토만 허용합니다."
                )
                if grounding["state"] == "verified_snapshot"
                else (
                    "현재 PC의 고정 안전 경로에서 로컬 파일을 찾았지만 "
                    "평가 증거 해시가 일치하지 않아 격리했습니다."
                    if grounding["state"] == "unverified_local"
                    else "현재 PC에서 검증된 로컬 팩·평가 기록을 확인하지 "
                    "못했습니다. 메타데이터 검토 후보일 뿐 설치하거나 "
                    "자동 활성화하지 않습니다."
                )
            ),
        },
        {
            "source_id": "laion-clip-b32-road-baseline",
            "display_name": "LAION CLIP ViT-B/32 Road Baseline",
            "source_type": "model_pack",
            "publisher": "LAION / local evaluation baseline",
            "origin_url": (
                "https://huggingface.co/"
                "laion/CLIP-ViT-B-32-laion2B-s34B-b79K"
            ),
            "license_spdx_id": "MIT",
            "intended_use": "candidate_proposals",
            "capability": "semantic_candidate_scoring",
            "catalog_status": clip_status[0],
            "installation_status": clip_status[1],
            "evaluation": clip["evaluation"],
            "observation_basis": clip_status[2],
            "rights_review_required": True,
            "automatic_download": False,
            "automatic_activation": False,
            "weight_execution_enabled": False,
            "project_training_linked": False,
            "human_review_required": True,
            "decision_reason": (
                (
                    "고정 해시와 일치하는 로컬 검증용 팩·평가 기록을 "
                    "확인했습니다. 349프레임에서 29개 검토 후보를 만들었지만 "
                    "상대 점수는 결함 확률이 아니며 자동 확정하지 않습니다."
                )
                if clip["state"] == "verified_snapshot"
                else (
                    "현재 PC의 로컬 파일과 평가 증거 해시가 일치하지 않아 "
                    "격리했습니다."
                    if clip["state"] == "unverified_local"
                    else "현재 PC에서 검증된 로컬 기준 팩을 확인하지 못했습니다. "
                    "미설치 검토 후보이며 자동 활성화하지 않습니다."
                )
            ),
        },
        {
            "source_id": "meta-sam2.1-hiera-small",
            "display_name": "SAM2.1 Hiera Small",
            "source_type": "model_pack",
            "publisher": "Meta",
            "origin_url": (
                "https://huggingface.co/facebook/sam2.1-hiera-small"
            ),
            "license_spdx_id": "Apache-2.0",
            "intended_use": "mask_refinement",
            "capability": "prompted_mask_refinement",
            "catalog_status": "review_candidate",
            "installation_status": "not_installed",
            "evaluation": None,
            "observation_basis": "catalog_policy_only",
            "rights_review_required": True,
            "automatic_download": False,
            "automatic_activation": False,
            "weight_execution_enabled": False,
            "project_training_linked": False,
            "human_review_required": True,
            "decision_reason": (
                "클릭 또는 박스로 마스크를 보정하는 후보입니다. 자동 라벨 "
                "생성기는 아니며 가중치를 설치하거나 실행하지 않습니다."
            ),
        },
        {
            "source_id": "peterhdd-pothole-yolov8-local-cache",
            "display_name": "Pothole Detection YOLOv8",
            "source_type": "model_pack",
            "publisher": "peterhdd / local evaluation cache",
            "origin_url": (
                "https://huggingface.co/peterhdd/pothole-detection-yolov8"
            ),
            "license_spdx_id": "Apache-2.0",
            "intended_use": "candidate_proposals",
            "capability": "pothole_box_proposals",
            "catalog_status": "rejected",
            "installation_status": pothole_installation,
            "evaluation": pothole["evaluation"],
            "observation_basis": pothole_basis,
            "rights_review_required": True,
            "automatic_download": False,
            "automatic_activation": False,
            "weight_execution_enabled": False,
            "project_training_linked": False,
            "human_review_required": True,
            "decision_reason": (
                (
                    "고정 해시와 일치하는 로컬 평가 기록에서 19개 제안이 "
                    "모두 오탐으로 확인되어 거부·격리했습니다. 모델 카드의 "
                    "Apache-2.0 표시와 원 데이터셋의 OpenRAIL 조건이 달라 "
                    "별도 권리 검토도 필요합니다."
                )
                if pothole["state"] == "verified_snapshot"
                else (
                    "로컬 파일이 있으나 증거 해시가 일치하지 않아 거부·"
                    "격리했습니다. 모델 카드 Apache-2.0과 원 데이터셋 "
                    "OpenRAIL 조건에 대한 별도 권리 검토가 필요합니다."
                    if pothole["state"] == "unverified_local"
                    else "TrafficVisson 배포 후보에서 거부된 모델이며 현재 PC의 "
                    "검증된 로컬 캐시는 없습니다. 모델 카드 Apache-2.0과 "
                    "원 데이터셋 OpenRAIL 조건의 별도 권리 검토가 필요합니다."
                )
            ),
        },
        {
            "source_id": "programmer-rd-ai-road-issues-detection-dataset",
            "display_name": "Road Issues Detection Dataset",
            "source_type": "labeled_dataset",
            "publisher": "Programmer-RD-AI",
            "origin_url": (
                "https://huggingface.co/datasets/"
                "Programmer-RD-AI/road-issues-detection-dataset"
            ),
            "license_spdx_id": "CC0-1.0",
            "intended_use": "retrieval_reference",
            "capability": "labeled_training_reference",
            "catalog_status": "review_candidate",
            "installation_status": "not_installed",
            "evaluation": None,
            "observation_basis": "catalog_policy_only",
            "rights_review_required": True,
            "automatic_download": False,
            "automatic_activation": False,
            "weight_execution_enabled": False,
            "project_training_linked": False,
            "human_review_required": True,
            "decision_reason": (
                "CC0-1.0으로 표시된 detection/classification 이미지 9,660장 "
                "후보입니다. 다운로드·프로젝트 혼합·자동학습은 금지하며 "
                "중복, 라벨 품질, 한국 도로 적합성, 영상 단위 분할과 권리 "
                "범위를 검증한 뒤에만 유사사례 참고를 검토할 수 있습니다."
            ),
        },
        {
            "source_id": "sekilab-rdd2022",
            "display_name": "RDD2022 공식 도로 파손 데이터셋",
            "source_type": "labeled_dataset",
            "publisher": "Sekimoto Lab / CRDDC 2022",
            "origin_url": "https://github.com/sekilab/RoadDamageDetector",
            "license_spdx_id": "CC-BY-SA-4.0",
            "intended_use": "evaluation_reference",
            "capability": "labeled_training_reference",
            "catalog_status": "review_candidate",
            "installation_status": "not_installed",
            "evaluation": None,
            "observation_basis": "catalog_policy_only",
            "rights_review_required": True,
            "automatic_download": False,
            "automatic_activation": False,
            "weight_execution_enabled": False,
            "project_training_linked": False,
            "human_review_required": True,
            "decision_reason": (
                "CC-BY-SA-4.0으로 공개된 6개국 도로 이미지 47,420장과 "
                "55,000건 이상의 Pascal VOC 주석(D00/D10/D20/D40)을 가진 "
                "공식 파손 데이터셋 후보입니다. 미설치 평가 참고 전용이며 "
                "다운로드·프로젝트 연결·자동 라벨 생성·학습은 하지 않습니다."
            ),
        },
        {
            "source_id": "daimler-lost-and-found-road-obstacles",
            "display_name": "LostAndFound Road Obstacles",
            "source_type": "labeled_dataset",
            "publisher": "Daimler AG",
            "origin_url": (
                "https://www.tensorflow.org/datasets/catalog/lost_and_found"
            ),
            "license_spdx_id": "LicenseRef-NeedsReview",
            "intended_use": "evaluation_reference",
            "capability": "labeled_training_reference",
            "catalog_status": "review_candidate",
            "installation_status": "not_installed",
            "evaluation": None,
            "observation_basis": "catalog_policy_only",
            "rights_review_required": True,
            "automatic_download": False,
            "automatic_activation": False,
            "weight_execution_enabled": False,
            "project_training_linked": False,
            "human_review_required": True,
            "decision_reason": (
                "예상 밖의 작은 도로 장애물 분할을 다루는 112개 스테레오 "
                "시퀀스·주석 프레임 2,104장 후보입니다. 카탈로그 페이지에서 "
                "원본 데이터셋 라이선스를 확정할 수 없어 권리 검토가 끝날 "
                "때까지 training blocked(학습 차단) 상태이며 다운로드·설치·"
                "프로젝트 연결·자동 라벨 생성은 하지 않습니다."
            ),
        },
    ]
    return {
        "schema_version": SOURCE_CANDIDATE_CATALOG_SCHEMA,
        "catalog_mode": "read_only_inactive",
        "network_access_performed": False,
        "project_training_linked": False,
        "automatic_labels_created": False,
        "candidates": candidates,
        "count": len(candidates),
    }


class SourceRegistryError(ValueError):
    """A source, allowlist, inventory, or registry snapshot is invalid."""


class SourceNotAllowlisted(SourceRegistryError):
    """The exact source manifest was not approved by the pinned allowlist."""


class SourceLicenseError(SourceRegistryError):
    """Required source rights were not explicitly confirmed."""


class SourceIntegrityError(SourceRegistryError):
    """Observed local artifacts do not match their declared inventory."""


class SourceRegistryConflict(SourceRegistryError):
    """A source identity is already registered with different content."""


def canonical_json_bytes(value: Any) -> bytes:
    """Encode finite JSON deterministically."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise SourceRegistryError("value must be finite JSON") from error


def canonical_sha256(value: Any) -> str:
    """Return a deterministic digest for an in-memory manifest or snapshot."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def parse_manifest_bytes(raw: bytes) -> Dict[str, Any]:
    """Parse bounded UTF-8 JSON while rejecting duplicate keys and NaN."""

    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_MANIFEST_BYTES:
        raise SourceRegistryError("manifest bytes are unavailable or too large")

    def object_pairs(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SourceRegistryError(
                    "manifest JSON contains duplicate keys"
                )
            result[key] = value
        return result

    def reject_constant(_: str) -> object:
        raise SourceRegistryError(
            "manifest JSON contains a non-finite number"
        )

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except SourceRegistryError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceRegistryError("manifest JSON is invalid") from error
    if not isinstance(value, dict):
        raise SourceRegistryError("manifest JSON must be an object")
    return value


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SourceRegistryError(f"{field_name} must be an object")
    return value


def _exact_keys(
    value: Mapping[str, Any],
    *,
    required: Sequence[str],
    optional: Sequence[str] = (),
    field_name: str,
) -> None:
    keys = set(value)
    allowed = set(required) | set(optional)
    if keys - allowed or set(required) - keys:
        raise SourceRegistryError(
            f"{field_name} has missing or unknown fields"
        )


def _text(
    value: Any,
    field_name: str,
    *,
    maximum: int,
    pattern: Optional[re.Pattern] = None,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(
            unicodedata.category(character) in {"Cc", "Cs"}
            for character in value
        )
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        raise SourceRegistryError(f"{field_name} is invalid")
    return value


def _sha256(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise SourceRegistryError(
            f"{field_name} must be a lowercase SHA-256"
        )
    return value


def _positive_integer(
    value: Any,
    field_name: str,
    *,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise SourceRegistryError(f"{field_name} is invalid")
    return value


def _https_url(value: Any, field_name: str) -> str:
    text = _text(value, field_name, maximum=2_048)
    parts = urlsplit(text)
    try:
        port = parts.port
    except ValueError as error:
        raise SourceRegistryError(
            f"{field_name} must be a public HTTPS metadata URL"
        ) from error
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or port not in {None, 443}
        or _HOST.fullmatch(parts.hostname) is None
    ):
        raise SourceRegistryError(
            f"{field_name} must be a public HTTPS metadata URL"
        )
    return text


def _safe_relative_path(value: Any, field_name: str) -> str:
    path = _text(value, field_name, maximum=1_024)
    if (
        "\\" in path
        or ":" in path
        or path.startswith("/")
        or path.endswith("/")
    ):
        raise SourceRegistryError(f"{field_name} must be a relative path")
    segments = path.split("/")
    if (
        any(segment in {"", ".", ".."} for segment in segments)
        or any(_PATH_SEGMENT.fullmatch(segment) is None for segment in segments)
    ):
        raise SourceRegistryError(f"{field_name} is unsafe")
    suffix = "." + segments[-1].rsplit(".", 1)[-1].casefold()
    if "." in segments[-1] and suffix in _BLOCKED_SUFFIXES:
        raise SourceRegistryError(
            f"{field_name} cannot reference executable content"
        )
    return path


def _string_list(
    value: Any,
    field_name: str,
    *,
    allowed: Sequence[str],
    minimum: int = 1,
    maximum: int = 32,
) -> List[str]:
    if (
        not isinstance(value, list)
        or not minimum <= len(value) <= maximum
        or any(not isinstance(item, str) or item not in allowed for item in value)
        or len(set(value)) != len(value)
    ):
        raise SourceRegistryError(f"{field_name} is invalid")
    return sorted(value)


def _normalise_issue_types(value: Any, field_name: str) -> List[str]:
    """Accept legacy source codes but persist only canonical product codes."""

    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= len(ISSUE_TYPES)
        or any(not isinstance(item, str) for item in value)
    ):
        raise SourceRegistryError(f"{field_name} is invalid")
    normalised = [
        ISSUE_TYPE_ALIASES.get(item, item)
        for item in value
    ]
    if (
        any(item not in ISSUE_TYPES for item in normalised)
        or len(set(normalised)) != len(normalised)
    ):
        raise SourceRegistryError(f"{field_name} is invalid")
    return sorted(normalised)


def _normalise_publisher(value: Any) -> Dict[str, str]:
    publisher = _mapping(value, "source.publisher")
    _exact_keys(
        publisher,
        required=("name", "homepage_url"),
        field_name="source.publisher",
    )
    return {
        "name": _text(
            publisher["name"],
            "source.publisher.name",
            maximum=256,
        ),
        "homepage_url": _https_url(
            publisher["homepage_url"],
            "source.publisher.homepage_url",
        ),
    }


def _normalise_origin(value: Any) -> Dict[str, str]:
    origin = _mapping(value, "source.origin")
    _exact_keys(
        origin,
        required=("landing_page_url", "immutable_revision"),
        field_name="source.origin",
    )
    revision = _text(
        origin["immutable_revision"],
        "source.origin.immutable_revision",
        maximum=128,
    )
    if revision.casefold() in _DISALLOWED_REVISIONS:
        raise SourceRegistryError(
            "source.origin.immutable_revision must identify a frozen revision"
        )
    return {
        "landing_page_url": _https_url(
            origin["landing_page_url"],
            "source.origin.landing_page_url",
        ),
        "immutable_revision": revision,
    }


def _normalise_licenses(
    value: Any,
    *,
    source_type: str,
    intended_uses: Sequence[str],
) -> List[Dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise SourceLicenseError("source.licenses must be a bounded array")
    licenses = []
    seen = set()
    for raw_license in value:
        license_row = _mapping(raw_license, "source.license")
        _exact_keys(
            license_row,
            required=(
                "scope",
                "spdx_id",
                "license_url",
                "commercial_use_confirmed",
                "modification_allowed",
                "redistribution_allowed",
                "notice",
            ),
            field_name="source.license",
        )
        scope = _text(
            license_row["scope"],
            "source.license.scope",
            maximum=32,
        )
        if scope in seen:
            raise SourceLicenseError("source license scopes must be unique")
        seen.add(scope)
        spdx_id = _text(
            license_row["spdx_id"],
            "source.license.spdx_id",
            maximum=64,
            pattern=_SPDX_ID,
        )
        commercial = license_row["commercial_use_confirmed"]
        modification = license_row["modification_allowed"]
        redistribution = license_row["redistribution_allowed"]
        if any(
            not isinstance(flag, bool)
            for flag in (commercial, modification, redistribution)
        ):
            raise SourceLicenseError(
                "source license rights must be explicit booleans"
            )
        licenses.append(
            {
                "scope": scope,
                "spdx_id": spdx_id,
                "license_url": _https_url(
                    license_row["license_url"],
                    "source.license.license_url",
                ),
                "commercial_use_confirmed": commercial,
                "modification_allowed": modification,
                "redistribution_allowed": redistribution,
                "notice": _text(
                    license_row["notice"],
                    "source.license.notice",
                    maximum=2_048,
                ),
            }
        )

    required_scopes = (
        _MODEL_LICENSE_SCOPES
        if source_type == "model_pack"
        else _DATASET_LICENSE_SCOPES
    )
    if not required_scopes.issubset(seen):
        raise SourceLicenseError(
            "source does not declare every required license scope"
        )
    by_scope = {row["scope"]: row for row in licenses}
    for scope in required_scopes:
        if by_scope[scope]["commercial_use_confirmed"] is not True:
            raise SourceLicenseError(
                f"commercial-use rights are not confirmed for {scope}"
            )
        if (
            source_type == "labeled_dataset"
            and "training_seed" in intended_uses
            and by_scope[scope]["modification_allowed"] is not True
        ):
            raise SourceLicenseError(
                "training_seed requires modification rights for dataset assets"
            )
    return sorted(licenses, key=lambda row: row["scope"])


def _normalise_artifacts(
    value: Any,
    *,
    source_type: str,
) -> List[Dict[str, Any]]:
    maximum_files = (
        MAX_MODEL_FILES if source_type == "model_pack" else MAX_DATASET_FILES
    )
    maximum_bytes = (
        MAX_MODEL_BYTES if source_type == "model_pack" else MAX_DATASET_BYTES
    )
    roles = (
        _MODEL_ARTIFACT_ROLES
        if source_type == "model_pack"
        else _DATASET_ARTIFACT_ROLES
    )
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= maximum_files
    ):
        raise SourceRegistryError("source.artifacts is invalid")
    result = []
    paths = set()
    total_bytes = 0
    for raw_artifact in value:
        artifact = _mapping(raw_artifact, "source.artifact")
        _exact_keys(
            artifact,
            required=(
                "path",
                "role",
                "size_bytes",
                "sha256",
                "media_type",
            ),
            field_name="source.artifact",
        )
        path = _safe_relative_path(
            artifact["path"],
            "source.artifact.path",
        )
        if path in paths:
            raise SourceRegistryError(
                "source artifact paths must be unique"
            )
        paths.add(path)
        role = artifact["role"]
        if not isinstance(role, str) or role not in roles:
            raise SourceRegistryError("source artifact role is invalid")
        media_type = artifact["media_type"]
        if not isinstance(media_type, str) or media_type not in _MEDIA_TYPES:
            raise SourceRegistryError(
                "source artifact media_type is invalid"
            )
        size_bytes = _positive_integer(
            artifact["size_bytes"],
            "source.artifact.size_bytes",
            maximum=maximum_bytes,
        )
        total_bytes += size_bytes
        if total_bytes > maximum_bytes:
            raise SourceRegistryError("source artifact inventory is too large")
        result.append(
            {
                "path": path,
                "role": role,
                "size_bytes": size_bytes,
                "sha256": _sha256(
                    artifact["sha256"],
                    "source.artifact.sha256",
                ),
                "media_type": media_type,
            }
        )
    return sorted(result, key=lambda row: row["path"])


def _normalise_model_pack(
    value: Any,
    *,
    source_id: str,
    source_version: str,
    intended_uses: Sequence[str],
    artifacts: Sequence[Mapping[str, Any]],
) -> Dict[str, str]:
    model_pack = _mapping(value, "source.model_pack")
    _exact_keys(
        model_pack,
        required=(
            "pack_id",
            "pack_version",
            "engine",
            "vision_manifest_sha256",
        ),
        field_name="source.model_pack",
    )
    pack_id = _text(
        model_pack["pack_id"],
        "source.model_pack.pack_id",
        maximum=64,
        pattern=_IDENTIFIER,
    )
    pack_version = _text(
        model_pack["pack_version"],
        "source.model_pack.pack_version",
        maximum=64,
        pattern=_VERSION,
    )
    if pack_id != source_id or pack_version != source_version:
        raise SourceRegistryError(
            "model pack identity must match source identity"
        )
    engine = model_pack["engine"]
    if engine not in MODEL_ENGINES:
        raise SourceRegistryError("source.model_pack.engine is unsupported")
    if engine == "easyocr_ocr_only" and "ocr_proposals" not in intended_uses:
        raise SourceRegistryError("OCR-only packs require ocr_proposals use")
    if engine != "easyocr_ocr_only" and "candidate_proposals" not in intended_uses:
        raise SourceRegistryError(
            "vision model packs require candidate_proposals use"
        )
    manifest_sha256 = _sha256(
        model_pack["vision_manifest_sha256"],
        "source.model_pack.vision_manifest_sha256",
    )
    manifest_artifacts = [
        row for row in artifacts if row["role"] == "model_manifest"
    ]
    weight_artifacts = [
        row
        for row in artifacts
        if row["role"] in {"model_weights", "ocr_weights"}
    ]
    if (
        len(manifest_artifacts) != 1
        or manifest_artifacts[0]["path"]
        != "trafficvisson-model-pack.json"
        or manifest_artifacts[0]["sha256"] != manifest_sha256
        or not weight_artifacts
    ):
        raise SourceRegistryError(
            "model source must inventory its vision manifest and weights"
        )
    return {
        "pack_id": pack_id,
        "pack_version": pack_version,
        "engine": engine,
        "vision_manifest_sha256": manifest_sha256,
    }


def _normalise_dataset(value: Any) -> Dict[str, Any]:
    dataset = _mapping(value, "source.dataset")
    _exact_keys(
        dataset,
        required=(
            "annotation_format",
            "split_unit",
            "external_labels_only",
            "privacy",
        ),
        field_name="source.dataset",
    )
    annotation_format = dataset["annotation_format"]
    split_unit = dataset["split_unit"]
    if annotation_format not in _ANNOTATION_FORMATS:
        raise SourceRegistryError(
            "source.dataset.annotation_format is unsupported"
        )
    if split_unit not in _SPLIT_UNITS:
        raise SourceRegistryError("source.dataset.split_unit is unsupported")
    if dataset["external_labels_only"] is not True:
        raise SourceRegistryError(
            "external dataset labels cannot claim project approval"
        )
    privacy = _mapping(dataset["privacy"], "source.dataset.privacy")
    _exact_keys(
        privacy,
        required=(
            "contains_personal_data",
            "redaction_status",
            "usage_review_required",
        ),
        field_name="source.dataset.privacy",
    )
    if not isinstance(privacy["contains_personal_data"], bool):
        raise SourceRegistryError(
            "source.dataset.privacy.contains_personal_data must be boolean"
        )
    if privacy["redaction_status"] not in _PRIVACY_STATUSES:
        raise SourceRegistryError(
            "source.dataset.privacy.redaction_status is invalid"
        )
    if privacy["usage_review_required"] is not True:
        raise SourceRegistryError(
            "external dataset privacy use always requires review"
        )
    if (
        privacy["contains_personal_data"] is False
        and privacy["redaction_status"] == "requires_local_redaction"
    ):
        raise SourceRegistryError(
            "dataset privacy declaration is internally inconsistent"
        )
    return {
        "annotation_format": annotation_format,
        "split_unit": split_unit,
        "external_labels_only": True,
        "privacy": {
            "contains_personal_data": privacy["contains_personal_data"],
            "redaction_status": privacy["redaction_status"],
            "usage_review_required": True,
        },
    }


def validate_source_manifest(
    source_manifest: Mapping[str, Any],
) -> Dict[str, Any]:
    """Strictly validate one external source manifest without activating it."""

    source = _mapping(source_manifest, "source")
    _exact_keys(
        source,
        required=(
            "schema_version",
            "source_type",
            "source_id",
            "source_version",
            "publisher",
            "origin",
            "intended_uses",
            "issue_types",
            "licenses",
            "artifacts",
            "automatic_download",
            "remote_code_allowed",
        ),
        optional=("model_pack", "dataset"),
        field_name="source",
    )
    if source["schema_version"] != SOURCE_MANIFEST_SCHEMA:
        raise SourceRegistryError("source schema is unsupported")
    source_type = source["source_type"]
    if source_type not in SOURCE_TYPES:
        raise SourceRegistryError("source.source_type is unsupported")
    if source["automatic_download"] is not False:
        raise SourceRegistryError("automatic source download is forbidden")
    if source["remote_code_allowed"] is not False:
        raise SourceRegistryError("remote model or dataset code is forbidden")
    source_id = _text(
        source["source_id"],
        "source.source_id",
        maximum=128,
        pattern=_IDENTIFIER,
    )
    source_version = _text(
        source["source_version"],
        "source.source_version",
        maximum=64,
        pattern=_VERSION,
    )
    allowed_uses = _MODEL_USES if source_type == "model_pack" else _DATASET_USES
    intended_uses = _string_list(
        source["intended_uses"],
        "source.intended_uses",
        allowed=tuple(allowed_uses),
    )
    issue_types = _normalise_issue_types(
        source["issue_types"],
        "source.issue_types",
    )
    licenses = _normalise_licenses(
        source["licenses"],
        source_type=source_type,
        intended_uses=intended_uses,
    )
    artifacts = _normalise_artifacts(
        source["artifacts"],
        source_type=source_type,
    )
    roles = {row["role"] for row in artifacts}

    model_pack = None
    dataset = None
    if source_type == "model_pack":
        if "dataset" in source or "model_pack" not in source:
            raise SourceRegistryError(
                "model source requires only model_pack configuration"
            )
        model_pack = _normalise_model_pack(
            source["model_pack"],
            source_id=source_id,
            source_version=source_version,
            intended_uses=intended_uses,
            artifacts=artifacts,
        )
    else:
        if "model_pack" in source or "dataset" not in source:
            raise SourceRegistryError(
                "dataset source requires only dataset configuration"
            )
        if (
            "annotations" not in roles
            or not roles.intersection({"images_archive", "image"})
        ):
            raise SourceRegistryError(
                "labeled dataset requires image and annotation artifacts"
            )
        dataset = _normalise_dataset(source["dataset"])

    result = {
        "schema_version": SOURCE_MANIFEST_SCHEMA,
        "source_type": source_type,
        "source_id": source_id,
        "source_version": source_version,
        "publisher": _normalise_publisher(source["publisher"]),
        "origin": _normalise_origin(source["origin"]),
        "intended_uses": intended_uses,
        "issue_types": issue_types,
        "licenses": licenses,
        "artifacts": artifacts,
        "automatic_download": False,
        "remote_code_allowed": False,
    }
    if model_pack is not None:
        result["model_pack"] = model_pack
    if dataset is not None:
        result["dataset"] = dataset
    return result


def _normalise_allowlist(
    allowlist_manifest: Mapping[str, Any],
    *,
    expected_allowlist_sha256: str,
) -> Tuple[Dict[str, Any], str]:
    expected_digest = _sha256(
        expected_allowlist_sha256,
        "expected_allowlist_sha256",
    )
    allowlist = _mapping(allowlist_manifest, "allowlist")
    _exact_keys(
        allowlist,
        required=("schema_version", "allowlist_version", "entries"),
        field_name="allowlist",
    )
    if allowlist["schema_version"] != ALLOWLIST_SCHEMA:
        raise SourceRegistryError("allowlist schema is unsupported")
    allowlist_version = _text(
        allowlist["allowlist_version"],
        "allowlist.allowlist_version",
        maximum=64,
        pattern=_VERSION,
    )
    raw_entries = allowlist["entries"]
    if (
        not isinstance(raw_entries, list)
        or not 1 <= len(raw_entries) <= MAX_ALLOWLIST_ENTRIES
    ):
        raise SourceRegistryError("allowlist entries are invalid")
    entries = []
    identities = set()
    digests = set()
    for raw_entry in raw_entries:
        entry = _mapping(raw_entry, "allowlist.entry")
        _exact_keys(
            entry,
            required=(
                "source_type",
                "source_id",
                "source_version",
                "source_manifest_sha256",
                "approved_uses",
                "license_review_sha256",
            ),
            field_name="allowlist.entry",
        )
        source_type = entry["source_type"]
        if source_type not in SOURCE_TYPES:
            raise SourceRegistryError(
                "allowlist source_type is unsupported"
            )
        source_id = _text(
            entry["source_id"],
            "allowlist.entry.source_id",
            maximum=128,
            pattern=_IDENTIFIER,
        )
        source_version = _text(
            entry["source_version"],
            "allowlist.entry.source_version",
            maximum=64,
            pattern=_VERSION,
        )
        identity = (source_type, source_id, source_version)
        if identity in identities:
            raise SourceRegistryError(
                "allowlist source identities must be unique"
            )
        identities.add(identity)
        source_digest = _sha256(
            entry["source_manifest_sha256"],
            "allowlist.entry.source_manifest_sha256",
        )
        if source_digest in digests:
            raise SourceRegistryError(
                "allowlist source manifest hashes must be unique"
            )
        digests.add(source_digest)
        allowed_uses = (
            tuple(_MODEL_USES)
            if source_type == "model_pack"
            else tuple(_DATASET_USES)
        )
        entries.append(
            {
                "source_type": source_type,
                "source_id": source_id,
                "source_version": source_version,
                "source_manifest_sha256": source_digest,
                "approved_uses": _string_list(
                    entry["approved_uses"],
                    "allowlist.entry.approved_uses",
                    allowed=allowed_uses,
                ),
                "license_review_sha256": _sha256(
                    entry["license_review_sha256"],
                    "allowlist.entry.license_review_sha256",
                ),
            }
        )
    normalised = {
        "schema_version": ALLOWLIST_SCHEMA,
        "allowlist_version": allowlist_version,
        "entries": sorted(
            entries,
            key=lambda row: (
                row["source_type"],
                row["source_id"],
                row["source_version"],
            ),
        ),
    }
    actual_digest = canonical_sha256(normalised)
    if actual_digest != expected_digest:
        raise SourceNotAllowlisted(
            "allowlist manifest does not match its pinned digest"
        )
    return normalised, actual_digest


def _normalise_observed_artifacts(
    value: Any,
) -> List[Dict[str, Any]]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= MAX_DATASET_FILES
    ):
        raise SourceIntegrityError("observed_artifacts is invalid")
    result = []
    paths = set()
    for raw_observation in value:
        observation = _mapping(
            raw_observation,
            "observed_artifact",
        )
        _exact_keys(
            observation,
            required=("path", "size_bytes", "sha256"),
            field_name="observed_artifact",
        )
        path = _safe_relative_path(
            observation["path"],
            "observed_artifact.path",
        )
        if path in paths:
            raise SourceIntegrityError(
                "observed artifact paths must be unique"
            )
        paths.add(path)
        result.append(
            {
                "path": path,
                "size_bytes": _positive_integer(
                    observation["size_bytes"],
                    "observed_artifact.size_bytes",
                    maximum=MAX_DATASET_BYTES,
                ),
                "sha256": _sha256(
                    observation["sha256"],
                    "observed_artifact.sha256",
                ),
            }
        )
    return sorted(result, key=lambda row: row["path"])


def _verify_inventory(
    artifacts: Sequence[Mapping[str, Any]],
    observed_artifacts: Sequence[Mapping[str, Any]],
) -> str:
    expected = {
        row["path"]: {
            "size_bytes": row["size_bytes"],
            "sha256": row["sha256"],
        }
        for row in artifacts
    }
    observed = {
        row["path"]: {
            "size_bytes": row["size_bytes"],
            "sha256": row["sha256"],
        }
        for row in observed_artifacts
    }
    if expected != observed:
        raise SourceIntegrityError(
            "observed artifact inventory does not match the source manifest"
        )
    return canonical_sha256(observed_artifacts)


def empty_registry() -> Dict[str, Any]:
    """Create a deterministic empty inactive registry snapshot."""

    registry = {
        "schema_version": REGISTRY_SCHEMA,
        "sources": [],
    }
    registry["registry_sha256"] = canonical_sha256(registry)
    return registry


def _validate_registration(value: Any) -> Dict[str, Any]:
    registration = _mapping(value, "registry.source")
    _exact_keys(
        registration,
        required=(
            "schema_version",
            "registration_id",
            "registration_status",
            "source_type",
            "source_id",
            "source_version",
            "source_manifest_sha256",
            "allowlist_manifest_sha256",
            "allowlist_version",
            "license_review_sha256",
            "approved_uses",
            "issue_types",
            "artifact_inventory_sha256",
            "publisher",
            "origin",
            "licenses",
            "source_contract",
            "safety",
            "registration_sha256",
        ),
        field_name="registry.source",
    )
    if registration["schema_version"] != REGISTRATION_SCHEMA:
        raise SourceRegistryError("registration schema is unsupported")
    if registration["registration_status"] != "verified_inactive":
        raise SourceRegistryError("registration must remain inactive")
    if registration["source_type"] not in SOURCE_TYPES:
        raise SourceRegistryError("registration source_type is unsupported")
    for field_name in (
        "registration_id",
        "source_manifest_sha256",
        "allowlist_manifest_sha256",
        "license_review_sha256",
        "artifact_inventory_sha256",
        "registration_sha256",
    ):
        _sha256(registration[field_name], f"registration.{field_name}")
    without_hash = {
        key: item
        for key, item in registration.items()
        if key != "registration_sha256"
    }
    if canonical_sha256(without_hash) != registration["registration_sha256"]:
        raise SourceRegistryError("registration hash is invalid")
    issue_types = _normalise_issue_types(
        registration["issue_types"],
        "registration.issue_types",
    )
    safety = _mapping(registration["safety"], "registration.safety")
    required_false = (
        "automatic_download",
        "automatic_activation",
        "remote_code_allowed",
        "external_labels_are_project_approved",
        "include_in_project_training",
        "model_outputs_are_human_approved",
    )
    if any(safety.get(field_name) is not False for field_name in required_false):
        raise SourceRegistryError("registration safety flags are invalid")
    if safety.get("requires_explicit_project_import") is not True:
        raise SourceRegistryError("registration project gate is invalid")
    normalised = dict(registration)
    normalised["issue_types"] = issue_types
    if issue_types != registration["issue_types"]:
        normalised_without_hash = {
            key: item
            for key, item in normalised.items()
            if key != "registration_sha256"
        }
        normalised["registration_sha256"] = canonical_sha256(
            normalised_without_hash
        )
    return normalised


def _normalise_registry(value: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if value is None:
        return empty_registry()
    registry = _mapping(value, "registry")
    _exact_keys(
        registry,
        required=("schema_version", "sources", "registry_sha256"),
        field_name="registry",
    )
    if registry["schema_version"] != REGISTRY_SCHEMA:
        raise SourceRegistryError("registry schema is unsupported")
    raw_sources = registry["sources"]
    if (
        not isinstance(raw_sources, list)
        or len(raw_sources) > MAX_REGISTRY_SOURCES
    ):
        raise SourceRegistryError("registry sources are invalid")
    sources = [_validate_registration(row) for row in raw_sources]
    identities = [
        (row["source_type"], row["source_id"], row["source_version"])
        for row in sources
    ]
    if len(set(identities)) != len(identities):
        raise SourceRegistryError("registry source identities must be unique")
    normalised = {
        "schema_version": REGISTRY_SCHEMA,
        "sources": sorted(
            sources,
            key=lambda row: (
                row["source_type"],
                row["source_id"],
                row["source_version"],
            ),
        ),
    }
    original_sources = sorted(
        [dict(_mapping(row, "registry.source")) for row in raw_sources],
        key=lambda row: (
            row["source_type"],
            row["source_id"],
            row["source_version"],
        ),
    )
    original = {
        "schema_version": REGISTRY_SCHEMA,
        "sources": original_sources,
    }
    if canonical_sha256(original) != registry["registry_sha256"]:
        raise SourceRegistryError("registry hash is invalid")
    normalised["registry_sha256"] = canonical_sha256(normalised)
    return normalised


def register_verified_source(
    *,
    registry: Optional[Mapping[str, Any]],
    source_manifest: Mapping[str, Any],
    allowlist_manifest: Mapping[str, Any],
    expected_allowlist_sha256: str,
    observed_artifacts: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Return a new deterministic registry containing one inactive source.

    ``observed_artifacts`` must come from a trusted read-only hashing boundary.
    This function merely compares those observations; it performs no I/O.
    """

    current = _normalise_registry(registry)
    source = validate_source_manifest(source_manifest)
    source_manifest_sha256 = canonical_sha256(source)
    allowlist, allowlist_sha256 = _normalise_allowlist(
        allowlist_manifest,
        expected_allowlist_sha256=expected_allowlist_sha256,
    )
    matching_entries = [
        entry
        for entry in allowlist["entries"]
        if entry["source_manifest_sha256"] == source_manifest_sha256
    ]
    if len(matching_entries) != 1:
        raise SourceNotAllowlisted(
            "the exact source manifest is not in the pinned allowlist"
        )
    allowlist_entry = matching_entries[0]
    identity = (
        source["source_type"],
        source["source_id"],
        source["source_version"],
    )
    if identity != (
        allowlist_entry["source_type"],
        allowlist_entry["source_id"],
        allowlist_entry["source_version"],
    ):
        raise SourceNotAllowlisted(
            "allowlist identity does not match the source manifest"
        )
    if not set(source["intended_uses"]).issubset(
        allowlist_entry["approved_uses"]
    ):
        raise SourceNotAllowlisted(
            "source requests a use that was not approved"
        )
    observations = _normalise_observed_artifacts(observed_artifacts)
    inventory_sha256 = _verify_inventory(
        source["artifacts"],
        observations,
    )
    privacy_status = (
        source["dataset"]["privacy"]["redaction_status"]
        if source["source_type"] == "labeled_dataset"
        else "not_applicable"
    )
    registration_identity = {
        "source_type": source["source_type"],
        "source_id": source["source_id"],
        "source_version": source["source_version"],
        "source_manifest_sha256": source_manifest_sha256,
        "allowlist_manifest_sha256": allowlist_sha256,
        "artifact_inventory_sha256": inventory_sha256,
    }
    source_contract = (
        source["model_pack"]
        if source["source_type"] == "model_pack"
        else source["dataset"]
    )
    registration: Dict[str, Any] = {
        "schema_version": REGISTRATION_SCHEMA,
        "registration_id": canonical_sha256(registration_identity),
        "registration_status": "verified_inactive",
        "source_type": source["source_type"],
        "source_id": source["source_id"],
        "source_version": source["source_version"],
        "source_manifest_sha256": source_manifest_sha256,
        "allowlist_manifest_sha256": allowlist_sha256,
        "allowlist_version": allowlist["allowlist_version"],
        "license_review_sha256": allowlist_entry[
            "license_review_sha256"
        ],
        "approved_uses": source["intended_uses"],
        "issue_types": source["issue_types"],
        "artifact_inventory_sha256": inventory_sha256,
        "publisher": source["publisher"],
        "origin": source["origin"],
        "licenses": source["licenses"],
        "source_contract": source_contract,
        "safety": {
            "automatic_download": False,
            "automatic_activation": False,
            "remote_code_allowed": False,
            "external_labels_are_project_approved": False,
            "include_in_project_training": False,
            "model_outputs_are_human_approved": False,
            "requires_explicit_project_import": True,
            "privacy_gate": privacy_status,
        },
    }
    registration["registration_sha256"] = canonical_sha256(registration)

    new_sources = list(current["sources"])
    for existing in new_sources:
        existing_identity = (
            existing["source_type"],
            existing["source_id"],
            existing["source_version"],
        )
        if existing_identity != identity:
            continue
        if existing == registration:
            return current
        raise SourceRegistryConflict(
            "source identity is registered with different content"
        )
    if len(new_sources) >= MAX_REGISTRY_SOURCES:
        raise SourceRegistryError("source registry is full")
    new_sources.append(registration)
    result = {
        "schema_version": REGISTRY_SCHEMA,
        "sources": sorted(
            new_sources,
            key=lambda row: (
                row["source_type"],
                row["source_id"],
                row["source_version"],
            ),
        ),
    }
    result["registry_sha256"] = canonical_sha256(result)
    return result
