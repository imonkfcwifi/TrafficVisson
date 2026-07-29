from __future__ import annotations

import json
from copy import deepcopy

import pytest

from app.source_registry import (
    ALLOWLIST_SCHEMA,
    CANONICAL_ISSUE_TYPES,
    SOURCE_MANIFEST_SCHEMA,
    SourceIntegrityError,
    SourceLicenseError,
    SourceNotAllowlisted,
    SourceRegistryConflict,
    SourceRegistryError,
    build_source_candidate_catalog,
    canonical_sha256,
    empty_registry,
    parse_manifest_bytes,
    register_verified_source,
    validate_source_manifest,
)


MODEL_MANIFEST_SHA256 = "a" * 64
MODEL_WEIGHTS_SHA256 = "b" * 64
DATASET_IMAGES_SHA256 = "c" * 64
DATASET_LABELS_SHA256 = "d" * 64
LICENSE_REVIEW_SHA256 = "e" * 64


def _verified_source_observations() -> dict:
    return {
        "schema_version": "trafficvisson.local-source-observation.v1",
        "sources": {
            "idea-research-grounding-dino-tiny": {
                "state": "verified_snapshot",
                "evaluation": {
                    "evaluated_frame_count": 30,
                    "proposal_frame_count": 25,
                    "proposal_count": 47,
                    "false_positive_count": None,
                    "verdict": "review_only",
                },
            },
            "laion-clip-b32-road-baseline": {
                "state": "verified_snapshot",
                "evaluation": {
                    "evaluated_frame_count": 349,
                    "proposal_frame_count": 29,
                    "proposal_count": 29,
                    "false_positive_count": None,
                    "verdict": "review_only",
                },
            },
            "peterhdd-pothole-yolov8-local-cache": {
                "state": "verified_snapshot",
                "evaluation": {
                    "evaluated_frame_count": 349,
                    "proposal_frame_count": 19,
                    "proposal_count": 19,
                    "false_positive_count": 19,
                    "verdict": "rejected",
                },
            },
        },
    }


def test_candidate_catalog_is_read_only_separate_and_deterministic():
    catalog = build_source_candidate_catalog(
        _verified_source_observations()
    )

    assert catalog["schema_version"] == (
        "trafficvisson.source-candidate-catalog.v1"
    )
    assert catalog["catalog_mode"] == "read_only_inactive"
    assert catalog["network_access_performed"] is False
    assert catalog["project_training_linked"] is False
    assert catalog["automatic_labels_created"] is False
    assert catalog["count"] == 7
    assert [
        candidate["display_name"] for candidate in catalog["candidates"]
    ] == [
        "Grounding DINO Tiny",
        "LAION CLIP ViT-B/32 Road Baseline",
        "SAM2.1 Hiera Small",
        "Pothole Detection YOLOv8",
        "Road Issues Detection Dataset",
        "RDD2022 공식 도로 파손 데이터셋",
        "LostAndFound Road Obstacles",
    ]

    grounding, clip, sam, pothole, dataset, rdd2022, lost_and_found = (
        catalog["candidates"]
    )
    assert grounding["license_spdx_id"] == "Apache-2.0"
    assert grounding["capability"] == "natural_language_box_proposals"
    assert grounding["catalog_status"] == "evaluation_complete"
    assert grounding["installation_status"] == "installed_validation_only"
    assert grounding["evaluation"] == {
        "evaluated_frame_count": 30,
        "proposal_frame_count": 25,
        "proposal_count": 47,
        "false_positive_count": None,
        "verdict": "review_only",
    }
    assert clip["license_spdx_id"] == "MIT"
    assert clip["capability"] == "semantic_candidate_scoring"
    assert clip["catalog_status"] == "evaluation_complete"
    assert clip["installation_status"] == "installed_validation_only"
    assert clip["evaluation"]["evaluated_frame_count"] == 349
    assert clip["evaluation"]["proposal_count"] == 29
    assert sam["license_spdx_id"] == "Apache-2.0"
    assert sam["capability"] == "prompted_mask_refinement"
    assert sam["catalog_status"] == "review_candidate"
    assert sam["installation_status"] == "not_installed"
    assert pothole["catalog_status"] == "rejected"
    assert pothole["installation_status"] == "local_cache_quarantined"
    assert pothole["evaluation"] == {
        "evaluated_frame_count": 349,
        "proposal_frame_count": 19,
        "proposal_count": 19,
        "false_positive_count": 19,
        "verdict": "rejected",
    }
    assert "OpenRAIL" in pothole["decision_reason"]
    assert dataset["source_type"] == "labeled_dataset"
    assert dataset["license_spdx_id"] == "CC0-1.0"
    assert dataset["installation_status"] == "not_installed"
    assert dataset["intended_use"] == "retrieval_reference"
    assert "9,660" in dataset["decision_reason"]
    assert rdd2022["source_id"] == "sekilab-rdd2022"
    assert rdd2022["license_spdx_id"] == "CC-BY-SA-4.0"
    assert rdd2022["intended_use"] == "evaluation_reference"
    assert rdd2022["capability"] == "labeled_training_reference"
    assert "47,420" in rdd2022["decision_reason"]
    assert "55,000" in rdd2022["decision_reason"]
    assert "D00/D10/D20/D40" in rdd2022["decision_reason"]
    assert lost_and_found["source_id"] == (
        "daimler-lost-and-found-road-obstacles"
    )
    assert lost_and_found["license_spdx_id"] == "LicenseRef-NeedsReview"
    assert lost_and_found["catalog_status"] == "review_candidate"
    assert lost_and_found["installation_status"] == "not_installed"
    assert lost_and_found["rights_review_required"] is True
    assert "112" in lost_and_found["decision_reason"]
    assert "2,104" in lost_and_found["decision_reason"]
    assert "training blocked" in lost_and_found["decision_reason"]

    assert "verified_inactive" not in json.dumps(catalog)
    for candidate in catalog["candidates"]:
        assert candidate["automatic_download"] is False
        assert candidate["automatic_activation"] is False
        assert candidate["weight_execution_enabled"] is False
        assert candidate["project_training_linked"] is False
        assert candidate["human_review_required"] is True
        assert candidate["rights_review_required"] is True

    catalog["candidates"][0]["display_name"] = "mutated"
    assert (
        build_source_candidate_catalog()["candidates"][0]["display_name"]
        == "Grounding DINO Tiny"
    )


def test_candidate_catalog_never_claims_installation_without_observation():
    catalog = build_source_candidate_catalog()
    grounding, clip, _, pothole, _, rdd2022, lost_and_found = (
        catalog["candidates"]
    )

    assert grounding["installation_status"] == "not_installed"
    assert grounding["catalog_status"] == "review_candidate"
    assert grounding["evaluation"] is None
    assert clip["installation_status"] == "not_installed"
    assert clip["catalog_status"] == "review_candidate"
    assert clip["evaluation"] is None
    assert pothole["catalog_status"] == "rejected"
    assert pothole["installation_status"] == "not_installed"
    assert pothole["evaluation"] is None
    assert rdd2022["catalog_status"] == "review_candidate"
    assert rdd2022["installation_status"] == "not_installed"
    assert rdd2022["evaluation"] is None
    assert lost_and_found["catalog_status"] == "review_candidate"
    assert lost_and_found["installation_status"] == "not_installed"
    assert lost_and_found["evaluation"] is None


def _license(scope: str, spdx_id: str = "Apache-2.0") -> dict:
    return {
        "scope": scope,
        "spdx_id": spdx_id,
        "license_url": f"https://licenses.example.org/{spdx_id}",
        "commercial_use_confirmed": True,
        "modification_allowed": True,
        "redistribution_allowed": False,
        "notice": f"Verified {scope} license facts for this frozen source.",
    }


def _model_source() -> dict:
    return {
        "schema_version": SOURCE_MANIFEST_SCHEMA,
        "source_type": "model_pack",
        "source_id": "trafficvisson-road-detector-reviewed",
        "source_version": "1.0.0",
        "publisher": {
            "name": "Example Model Publisher",
            "homepage_url": "https://models.example.org",
        },
        "origin": {
            "landing_page_url": (
                "https://models.example.org/trafficvisson-road-detector"
            ),
            "immutable_revision": "commit-0123456789abcdef",
        },
        "intended_uses": ["candidate_proposals"],
        "issue_types": ["pothole", "road_surface_damage"],
        "licenses": [
            _license("runtime", "MIT"),
            _license("weights", "Apache-2.0"),
        ],
        "artifacts": [
            {
                "path": "trafficvisson-model-pack.json",
                "role": "model_manifest",
                "size_bytes": 1200,
                "sha256": MODEL_MANIFEST_SHA256,
                "media_type": "application/json",
            },
            {
                "path": "road-detector.onnx",
                "role": "model_weights",
                "size_bytes": 5000,
                "sha256": MODEL_WEIGHTS_SHA256,
                "media_type": "application/onnx",
            },
        ],
        "automatic_download": False,
        "remote_code_allowed": False,
        "model_pack": {
            "pack_id": "trafficvisson-road-detector-reviewed",
            "pack_version": "1.0.0",
            "engine": "onnx_yolo_v8",
            "vision_manifest_sha256": MODEL_MANIFEST_SHA256,
        },
    }


def _dataset_source() -> dict:
    return {
        "schema_version": SOURCE_MANIFEST_SCHEMA,
        "source_type": "labeled_dataset",
        "source_id": "public-road-scenes-reviewed",
        "source_version": "2026.07.1",
        "publisher": {
            "name": "Example Dataset Publisher",
            "homepage_url": "https://data.example.org",
        },
        "origin": {
            "landing_page_url": (
                "https://data.example.org/public-road-scenes"
            ),
            "immutable_revision": "release-2026-07-1-sha256",
        },
        "intended_uses": [
            "training_seed",
            "retrieval_reference",
        ],
        "issue_types": ["pothole", "road_debris"],
        "licenses": [
            _license("images", "CC-BY-4.0"),
            _license("annotations", "CC-BY-4.0"),
        ],
        "artifacts": [
            {
                "path": "road-images.zip",
                "role": "images_archive",
                "size_bytes": 9000,
                "sha256": DATASET_IMAGES_SHA256,
                "media_type": "application/zip",
            },
            {
                "path": "annotations.json",
                "role": "annotations",
                "size_bytes": 2100,
                "sha256": DATASET_LABELS_SHA256,
                "media_type": "application/json",
            },
        ],
        "automatic_download": False,
        "remote_code_allowed": False,
        "dataset": {
            "annotation_format": "coco_json",
            "split_unit": "source_sequence",
            "external_labels_only": True,
            "privacy": {
                "contains_personal_data": True,
                "redaction_status": "requires_local_redaction",
                "usage_review_required": True,
            },
        },
    }


def _observations(source: dict) -> list:
    return [
        {
            "path": row["path"],
            "size_bytes": row["size_bytes"],
            "sha256": row["sha256"],
        }
        for row in source["artifacts"]
    ]


def _allowlist(*sources: dict) -> tuple:
    entries = []
    for source in sources:
        normalised = validate_source_manifest(source)
        entries.append(
            {
                "source_type": normalised["source_type"],
                "source_id": normalised["source_id"],
                "source_version": normalised["source_version"],
                "source_manifest_sha256": canonical_sha256(normalised),
                "approved_uses": normalised["intended_uses"],
                "license_review_sha256": LICENSE_REVIEW_SHA256,
            }
        )
    allowlist = {
        "schema_version": ALLOWLIST_SCHEMA,
        "allowlist_version": "2026.07.28",
        "entries": sorted(
            entries,
            key=lambda row: (
                row["source_type"],
                row["source_id"],
                row["source_version"],
            ),
        ),
    }
    return allowlist, canonical_sha256(allowlist)


def _register(source: dict, registry=None, allowlist_sources=None) -> dict:
    sources = allowlist_sources or [source]
    allowlist, allowlist_sha256 = _allowlist(*sources)
    return register_verified_source(
        registry=registry,
        source_manifest=source,
        allowlist_manifest=allowlist,
        expected_allowlist_sha256=allowlist_sha256,
        observed_artifacts=_observations(source),
    )


def test_model_pack_registers_as_verified_but_inactive():
    source = _model_source()
    registry = _register(source)
    registration = registry["sources"][0]

    assert registration["source_type"] == "model_pack"
    assert registration["registration_status"] == "verified_inactive"
    assert registration["source_contract"]["engine"] == "onnx_yolo_v8"
    assert registration["source_contract"]["vision_manifest_sha256"] == (
        MODEL_MANIFEST_SHA256
    )
    assert registration["safety"] == {
        "automatic_download": False,
        "automatic_activation": False,
        "remote_code_allowed": False,
        "external_labels_are_project_approved": False,
        "include_in_project_training": False,
        "model_outputs_are_human_approved": False,
        "requires_explicit_project_import": True,
        "privacy_gate": "not_applicable",
    }
    assert len(registration["registration_id"]) == 64
    assert len(registration["registration_sha256"]) == 64
    assert len(registry["registry_sha256"]) == 64


def test_public_dataset_registration_preserves_external_and_privacy_gates():
    source = _dataset_source()
    registry = _register(source)
    registration = registry["sources"][0]

    assert registration["source_type"] == "labeled_dataset"
    assert registration["source_contract"]["external_labels_only"] is True
    assert registration["source_contract"]["split_unit"] == "source_sequence"
    assert registration["safety"]["external_labels_are_project_approved"] is False
    assert registration["safety"]["include_in_project_training"] is False
    assert (
        registration["safety"]["privacy_gate"]
        == "requires_local_redaction"
    )


def test_exact_source_manifest_must_be_in_pinned_allowlist():
    approved = _model_source()
    changed = deepcopy(approved)
    changed["issue_types"].append("road_debris")
    allowlist, allowlist_sha256 = _allowlist(approved)

    with pytest.raises(SourceNotAllowlisted, match="exact source"):
        register_verified_source(
            registry=None,
            source_manifest=changed,
            allowlist_manifest=allowlist,
            expected_allowlist_sha256=allowlist_sha256,
            observed_artifacts=_observations(changed),
        )

    with pytest.raises(SourceNotAllowlisted, match="pinned digest"):
        register_verified_source(
            registry=None,
            source_manifest=approved,
            allowlist_manifest=allowlist,
            expected_allowlist_sha256="f" * 64,
            observed_artifacts=_observations(approved),
        )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("automatic_download", True, "download"),
        ("remote_code_allowed", True, "remote"),
    ],
)
def test_source_cannot_request_download_or_remote_code(field, value, match):
    source = _model_source()
    source[field] = value
    with pytest.raises(SourceRegistryError, match=match):
        validate_source_manifest(source)


def test_license_scope_rights_and_training_modification_are_required():
    missing_scope = _model_source()
    missing_scope["licenses"] = [_license("weights")]
    with pytest.raises(SourceLicenseError, match="every required"):
        validate_source_manifest(missing_scope)

    unconfirmed = _dataset_source()
    unconfirmed["licenses"][0]["commercial_use_confirmed"] = False
    with pytest.raises(SourceLicenseError, match="commercial-use"):
        validate_source_manifest(unconfirmed)

    no_derivatives = _dataset_source()
    no_derivatives["licenses"][1]["modification_allowed"] = False
    with pytest.raises(SourceLicenseError, match="modification rights"):
        validate_source_manifest(no_derivatives)


def test_origin_and_license_urls_are_metadata_only_public_https():
    source = _model_source()
    source["origin"]["landing_page_url"] = "http://models.example.org/model"
    with pytest.raises(SourceRegistryError, match="HTTPS"):
        validate_source_manifest(source)

    source = _model_source()
    source["licenses"][0]["license_url"] = (
        "https://user:secret@licenses.example.org/MIT"
    )
    with pytest.raises(SourceRegistryError, match="HTTPS"):
        validate_source_manifest(source)

    source = _model_source()
    source["origin"]["immutable_revision"] = "latest"
    with pytest.raises(SourceRegistryError, match="frozen revision"):
        validate_source_manifest(source)

    source = _model_source()
    source["origin"]["landing_page_url"] = (
        "https://models.example.org:not-a-port/model"
    )
    with pytest.raises(SourceRegistryError, match="HTTPS"):
        validate_source_manifest(source)


@pytest.mark.parametrize("mutation", ["hash", "size", "missing", "extra"])
def test_observed_local_inventory_must_match_exactly(mutation):
    source = _model_source()
    allowlist, allowlist_sha256 = _allowlist(source)
    observed = _observations(source)
    if mutation == "hash":
        observed[0]["sha256"] = "f" * 64
    elif mutation == "size":
        observed[0]["size_bytes"] += 1
    elif mutation == "missing":
        observed.pop()
    else:
        observed.append(
            {
                "path": "unexpected.bin",
                "size_bytes": 1,
                "sha256": "f" * 64,
            }
        )

    with pytest.raises(SourceIntegrityError, match="does not match"):
        register_verified_source(
            registry=None,
            source_manifest=source,
            allowlist_manifest=allowlist,
            expected_allowlist_sha256=allowlist_sha256,
            observed_artifacts=observed,
        )


def test_executable_or_traversal_artifacts_are_rejected():
    executable = _model_source()
    executable["artifacts"][1]["path"] = "plugins/model.py"
    with pytest.raises(SourceRegistryError, match="executable"):
        validate_source_manifest(executable)

    traversal = _dataset_source()
    traversal["artifacts"][0]["path"] = "../road-images.zip"
    with pytest.raises(SourceRegistryError, match="unsafe"):
        validate_source_manifest(traversal)


def test_unknown_issue_type_and_false_external_label_claim_are_rejected():
    source = _dataset_source()
    source["issue_types"] = ["magic_road_damage"]
    with pytest.raises(SourceRegistryError, match="issue_types"):
        validate_source_manifest(source)

    source = _dataset_source()
    source["dataset"]["external_labels_only"] = False
    with pytest.raises(SourceRegistryError, match="project approval"):
        validate_source_manifest(source)


def test_legacy_source_issue_types_are_stored_as_canonical_twelve_types():
    source = _dataset_source()
    source["issue_types"] = [
        "sign_damage",
        "sign_identity",
        "tunnel_name_occlusion",
        "color_guidance_line_faded",
    ]

    normalised = validate_source_manifest(source)
    assert len(CANONICAL_ISSUE_TYPES) == 12
    assert normalised["issue_types"] == [
        "colored_guidance_faded",
        "guide_sign_identity",
        "sign_damage",
        "tunnel_name_occlusion",
    ]

    registration = _register(source)["sources"][0]
    assert registration["issue_types"] == normalised["issue_types"]
    assert registration["registration_status"] == "verified_inactive"
    assert registration["safety"]["automatic_activation"] is False
    assert registration["safety"]["model_outputs_are_human_approved"] is False


def test_alias_and_canonical_duplicate_issue_types_are_rejected():
    source = _dataset_source()
    source["issue_types"] = ["guide_sign_identity", "sign_identity"]

    with pytest.raises(SourceRegistryError, match="issue_types"):
        validate_source_manifest(source)


def test_legacy_registry_storage_is_integrity_checked_then_migrated():
    model = _model_source()
    model["issue_types"] = [
        "guide_sign_identity",
        "colored_guidance_faded",
    ]
    registry = _register(model)
    legacy = deepcopy(registry)
    registration = legacy["sources"][0]
    original_registration_id = registration["registration_id"]
    original_source_digest = registration["source_manifest_sha256"]
    registration["issue_types"] = [
        "sign_identity",
        "color_guidance_line_faded",
    ]
    registration["registration_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in registration.items()
            if key != "registration_sha256"
        }
    )
    legacy["registry_sha256"] = canonical_sha256(
        {
            "schema_version": legacy["schema_version"],
            "sources": legacy["sources"],
        }
    )

    dataset = _dataset_source()
    allowlist, allowlist_sha256 = _allowlist(model, dataset)
    migrated = register_verified_source(
        registry=legacy,
        source_manifest=dataset,
        allowlist_manifest=allowlist,
        expected_allowlist_sha256=allowlist_sha256,
        observed_artifacts=_observations(dataset),
    )
    migrated_model = next(
        row
        for row in migrated["sources"]
        if row["source_type"] == "model_pack"
    )

    assert migrated_model["issue_types"] == [
        "colored_guidance_faded",
        "guide_sign_identity",
    ]
    assert migrated_model["registration_id"] == original_registration_id
    assert migrated_model["source_manifest_sha256"] == original_source_digest
    assert migrated_model["safety"]["automatic_activation"] is False
    assert migrated_model["safety"]["include_in_project_training"] is False
    assert migrated["registry_sha256"] != legacy["registry_sha256"]


def test_manifest_byte_parser_rejects_duplicate_keys_nan_and_non_object():
    with pytest.raises(SourceRegistryError, match="duplicate"):
        parse_manifest_bytes(b'{"schema_version":"a","schema_version":"b"}')
    with pytest.raises(SourceRegistryError, match="non-finite"):
        parse_manifest_bytes(b'{"value":NaN}')
    with pytest.raises(SourceRegistryError, match="object"):
        parse_manifest_bytes(b"[]")

    parsed = parse_manifest_bytes(
        json.dumps(_model_source(), ensure_ascii=False).encode("utf-8")
    )
    assert parsed["source_type"] == "model_pack"


def test_registration_is_deterministic_idempotent_and_sorted():
    model = _model_source()
    dataset = _dataset_source()
    allowlist, allowlist_sha256 = _allowlist(model, dataset)

    model_registry = register_verified_source(
        registry=empty_registry(),
        source_manifest=model,
        allowlist_manifest=allowlist,
        expected_allowlist_sha256=allowlist_sha256,
        observed_artifacts=_observations(model),
    )
    repeated = register_verified_source(
        registry=deepcopy(model_registry),
        source_manifest=deepcopy(model),
        allowlist_manifest=deepcopy(allowlist),
        expected_allowlist_sha256=allowlist_sha256,
        observed_artifacts=deepcopy(_observations(model)),
    )
    assert repeated == model_registry

    combined = register_verified_source(
        registry=model_registry,
        source_manifest=dataset,
        allowlist_manifest=allowlist,
        expected_allowlist_sha256=allowlist_sha256,
        observed_artifacts=_observations(dataset),
    )
    assert [row["source_type"] for row in combined["sources"]] == [
        "labeled_dataset",
        "model_pack",
    ]
    assert combined == register_verified_source(
        registry=model_registry,
        source_manifest=deepcopy(dataset),
        allowlist_manifest=deepcopy(allowlist),
        expected_allowlist_sha256=allowlist_sha256,
        observed_artifacts=deepcopy(_observations(dataset)),
    )


def test_same_source_identity_with_different_allowlisted_content_conflicts():
    first = _model_source()
    changed = deepcopy(first)
    changed["origin"]["immutable_revision"] = "commit-fedcba9876543210"
    allowlist, allowlist_sha256 = _allowlist(first)
    registry = register_verified_source(
        registry=None,
        source_manifest=first,
        allowlist_manifest=allowlist,
        expected_allowlist_sha256=allowlist_sha256,
        observed_artifacts=_observations(first),
    )
    changed_allowlist, changed_allowlist_sha256 = _allowlist(changed)

    with pytest.raises(SourceRegistryConflict, match="different content"):
        register_verified_source(
            registry=registry,
            source_manifest=changed,
            allowlist_manifest=changed_allowlist,
            expected_allowlist_sha256=changed_allowlist_sha256,
            observed_artifacts=_observations(changed),
        )


def test_registry_hash_detects_tampering():
    source = _model_source()
    registry = _register(source)
    registry["sources"][0]["safety"]["automatic_activation"] = True

    with pytest.raises(SourceRegistryError, match="hash"):
        _register(source, registry=registry)
