from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from PIL import Image
import pytest

from app.provider_visual_evidence import (
    OpenCvPrivacyRedactor,
    ProviderVisualEvidenceError,
    ProviderVisualEvidencePreparationService,
)


CANDIDATE_ID = "b" * 20


def png_bytes(
    size: tuple[int, int],
    color: tuple[int, int, int],
) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def bmp_bytes(
    size: tuple[int, int],
    color: tuple[int, int, int],
) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="BMP")
    return output.getvalue()


def reference_rows(
    count: int = 3,
) -> tuple[list[dict], dict[tuple[str, str], object]]:
    cases = []
    resolved = {}
    for index in range(1, count + 1):
        body = png_bytes(
            (180 + index * 5, 120 + index * 3),
            (30 * index, 80, 140),
        )
        digest = hashlib.sha256(body).hexdigest()
        source_id = f"sample-source-{index}"
        case_id = f"sample-case-{index}"
        source_ref = f"reference:{source_id}:{case_id}"
        cases.append(
            {
                "source_id": source_id,
                "case_id": case_id,
                "source_ref": source_ref,
                "photo_sha256": digest,
                "reference_state": "approved_reference",
            }
        )
        resolved[(source_id, case_id)] = SimpleNamespace(
            body=body,
            media_type="image/png",
            size_bytes=len(body),
            sha256=digest,
        )
    return cases, resolved


def fixture(
    tmp_path: Path,
    *,
    candidate_color: tuple[int, int, int] = (180, 100, 40),
    candidate_size: tuple[int, int] = (240, 160),
):
    project_id = uuid4()
    pipeline_job_id = uuid4()
    analysis_job_id = uuid4()
    candidate_raw = png_bytes(candidate_size, candidate_color)
    candidate_path = tmp_path / "candidate.png"
    candidate_path.write_bytes(candidate_raw)
    candidate_sha256 = hashlib.sha256(candidate_raw).hexdigest()
    cases, resolved = reference_rows()

    def resolve_reference(source_id: str, case_id: str):
        return resolved[(source_id, case_id)]

    return {
        "project_id": project_id,
        "pipeline_job_id": pipeline_job_id,
        "analysis_job_id": analysis_job_id,
        "candidate_path": candidate_path.resolve(),
        "candidate_media_type": "image/png",
        "candidate_size_bytes": len(candidate_raw),
        "candidate_sha256": candidate_sha256,
        "candidate_source_ref": (
            f"deid:{CANDIDATE_ID}:representative"
        ),
        "target_box": {
            "x": 0.25,
            "y": 0.2,
            "width": 0.5,
            "height": 0.6,
        },
        "similar_cases": cases,
        "resolve_reference": resolve_reference,
    }


def injected_redactor() -> OpenCvPrivacyRedactor:
    return OpenCvPrivacyRedactor(
        detected_boxes=lambda _image: {
            "faces": [(4, 5, 18, 16)],
            "plates": [(28, 30, 24, 10)],
        }
    )


def test_preparation_is_persistent_minimized_redacted_and_awaits_review(
    tmp_path: Path,
) -> None:
    arguments = fixture(tmp_path)
    root = tmp_path / "provider-evidence"
    service = ProviderVisualEvidencePreparationService(
        root,
        redactor=injected_redactor(),
    )

    first = service.prepare(
        candidate_id=CANDIDATE_ID,
        **arguments,
    )
    second = service.prepare(
        candidate_id=CANDIDATE_ID,
        **arguments,
    )

    assert first == second
    assert first["state"] == "awaiting_deidentification_review"
    assert first["attachment_count"] == 4
    assert first["automatic_redaction_performed"] is True
    assert first["automatic_privacy_verification"] is False
    assert first["human_verified"] is False
    assert first["review_required"] is True
    assert first["path_exposed"] is False
    assert first["attachments"][0]["width"] == 240
    assert first["attachments"][0]["height"] == 160
    assert all(
        row["face_detection_count"] == 1
        and row["plate_detection_count"] == 1
        for row in first["attachments"]
    )

    preparation_root = (
        root
        / str(arguments["project_id"])
        / str(arguments["pipeline_job_id"])
        / CANDIDATE_ID
        / first["preparation_id"]
    )
    with Image.open(preparation_root / "00-candidate.png") as image:
        assert image.convert("RGB").getpixel((5, 6)) == (0, 0, 0)
    assert not (preparation_root / "privacy-review.json").exists()

    restarted = ProviderVisualEvidencePreparationService(
        root,
        redactor=injected_redactor(),
    )
    restored = restarted.get(
        project_id=arguments["project_id"],
        pipeline_job_id=arguments["pipeline_job_id"],
        candidate_id=CANDIDATE_ID,
        preparation_id=first["preparation_id"],
    )
    assert restored == first


def test_path_free_candidate_body_uses_private_ephemeral_staging(
    tmp_path: Path,
) -> None:
    arguments = fixture(tmp_path)
    candidate_body = arguments.pop("candidate_path").read_bytes()
    root = tmp_path / "provider-evidence"
    service = ProviderVisualEvidencePreparationService(
        root,
        redactor=injected_redactor(),
    )

    prepared = service.prepare_from_body(
        candidate_id=CANDIDATE_ID,
        candidate_body=candidate_body,
        **arguments,
    )

    assert (
        prepared["state"]
        == "awaiting_deidentification_review"
    )
    assert prepared["path_exposed"] is False
    staging = root / ".candidate-body-staging"
    assert staging.is_dir()
    assert list(staging.iterdir()) == []


def test_tiny_edge_target_expands_to_minimum_context_and_contains_padding(
    tmp_path: Path,
) -> None:
    arguments = fixture(
        tmp_path,
        candidate_size=(1600, 900),
    )
    arguments["target_box"] = {
        "x": 0.97,
        "y": 0.95,
        "width": 0.02,
        "height": 0.03,
    }
    root = tmp_path / "provider-evidence"
    service = ProviderVisualEvidencePreparationService(
        root,
        redactor=injected_redactor(),
    )

    prepared = service.prepare(
        candidate_id=CANDIDATE_ID,
        **arguments,
    )

    candidate = prepared["attachments"][0]
    assert (candidate["width"], candidate["height"]) == (320, 256)
    preparation_root = (
        root
        / str(arguments["project_id"])
        / str(arguments["pipeline_job_id"])
        / CANDIDATE_ID
        / prepared["preparation_id"]
    )
    manifest = json.loads(
        (preparation_root / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    policy = manifest["policy"]["candidate_context_window"]
    assert policy == {
        "schema_version": (
            "trafficvisson.candidate-context-window.v1"
        ),
        "target_padding_fraction": 0.18,
        "minimum_source_width_pixels": 320,
        "minimum_source_height_pixels": 256,
        "small_source_axis_policy": "use_full_axis",
        "boundary_policy": "center_expand_clamp_recenter",
        "padded_target_inclusion_required": True,
    }
    crop = manifest["attachments"][0]["crop_box"]
    assert crop["x"] == pytest.approx(0.8)
    assert crop["y"] == pytest.approx((900 - 256) / 900)
    assert crop["x"] <= arguments["target_box"]["x"]
    assert crop["y"] <= arguments["target_box"]["y"]
    assert crop["x"] + crop["width"] >= 0.99
    assert crop["y"] + crop["height"] >= 0.98


def test_invalid_weak_reference_is_skipped_without_discarding_candidate(
    tmp_path: Path,
) -> None:
    arguments = fixture(tmp_path)
    cases, resolved = reference_rows(5)
    invalid = b"not-a-decodable-image"
    invalid_sha256 = hashlib.sha256(invalid).hexdigest()
    cases[1]["photo_sha256"] = invalid_sha256
    resolved[
        (cases[1]["source_id"], cases[1]["case_id"])
    ] = SimpleNamespace(
        body=invalid,
        media_type="image/png",
        size_bytes=len(invalid),
        sha256=invalid_sha256,
    )
    arguments["similar_cases"] = cases
    arguments["resolve_reference"] = (
        lambda source_id, case_id: resolved[(source_id, case_id)]
    )
    service = ProviderVisualEvidencePreparationService(
        tmp_path / "provider-evidence",
        redactor=injected_redactor(),
    )

    prepared = service.prepare(
        candidate_id=CANDIDATE_ID,
        **arguments,
    )

    assert prepared["state"] == "awaiting_deidentification_review"
    assert prepared["attachment_count"] == 5
    assert [
        row["source_ref"] for row in prepared["attachments"][1:]
    ] == [
        cases[index]["source_ref"] for index in (0, 2, 3, 4)
    ]
    assert prepared["human_verified"] is False
    assert prepared["path_exposed"] is False


def test_valid_hwpx_bmp_reference_is_strictly_verified_and_prepared(
    tmp_path: Path,
) -> None:
    arguments = fixture(tmp_path)
    cases, resolved = reference_rows(5)
    body = bmp_bytes((320, 180), (64, 96, 128))
    digest = hashlib.sha256(body).hexdigest()
    cases[1]["photo_sha256"] = digest
    resolved[
        (cases[1]["source_id"], cases[1]["case_id"])
    ] = SimpleNamespace(
        body=body,
        media_type="image/bmp",
        size_bytes=len(body),
        sha256=digest,
    )
    arguments["similar_cases"] = cases
    arguments["resolve_reference"] = (
        lambda source_id, case_id: resolved[(source_id, case_id)]
    )
    root = tmp_path / "provider-evidence"
    service = ProviderVisualEvidencePreparationService(
        root,
        redactor=injected_redactor(),
    )

    prepared = service.prepare(
        candidate_id=CANDIDATE_ID,
        **arguments,
    )

    assert prepared["attachment_count"] == 6
    assert [
        row["source_ref"] for row in prepared["attachments"][1:]
    ] == [case["source_ref"] for case in cases]
    preparation_root = (
        root
        / str(arguments["project_id"])
        / str(arguments["pipeline_job_id"])
        / CANDIDATE_ID
        / prepared["preparation_id"]
    )
    with Image.open(preparation_root / "02-sample.png") as image:
        assert image.format == "PNG"


def test_too_few_valid_weak_references_fail_before_preparation(
    tmp_path: Path,
) -> None:
    arguments = fixture(tmp_path)
    cases, resolved = reference_rows(5)
    for index in (0, 1, 2):
        invalid = f"invalid-image-{index}".encode("ascii")
        invalid_sha256 = hashlib.sha256(invalid).hexdigest()
        cases[index]["photo_sha256"] = invalid_sha256
        resolved[
            (cases[index]["source_id"], cases[index]["case_id"])
        ] = SimpleNamespace(
            body=invalid,
            media_type="image/png",
            size_bytes=len(invalid),
            sha256=invalid_sha256,
        )
    arguments["similar_cases"] = cases
    arguments["resolve_reference"] = (
        lambda source_id, case_id: resolved[(source_id, case_id)]
    )
    root = tmp_path / "provider-evidence"
    service = ProviderVisualEvidencePreparationService(
        root,
        redactor=injected_redactor(),
    )

    with pytest.raises(ProviderVisualEvidenceError) as error:
        service.prepare(
            candidate_id=CANDIDATE_ID,
            **arguments,
        )

    assert (
        error.value.code
        == "similar_case_visual_evidence_insufficient"
    )
    candidate_root = (
        root
        / str(arguments["project_id"])
        / str(arguments["pipeline_job_id"])
        / CANDIDATE_ID
    )
    assert (
        not candidate_root.exists()
        or list(candidate_root.iterdir()) == []
    )


def test_invalid_candidate_source_has_distinct_error_code(
    tmp_path: Path,
) -> None:
    arguments = fixture(tmp_path)
    invalid_candidate = png_bytes((8, 8), (1, 2, 3))
    arguments["candidate_path"].write_bytes(invalid_candidate)
    arguments["candidate_size_bytes"] = len(invalid_candidate)
    arguments["candidate_sha256"] = hashlib.sha256(
        invalid_candidate
    ).hexdigest()
    service = ProviderVisualEvidencePreparationService(
        tmp_path / "provider-evidence",
        redactor=injected_redactor(),
    )

    with pytest.raises(ProviderVisualEvidenceError) as error:
        service.prepare(
            candidate_id=CANDIDATE_ID,
            **arguments,
        )

    assert (
        error.value.code
        == "candidate_visual_evidence_source_invalid"
    )


def test_verified_attachments_exist_only_after_explicit_human_review(
    tmp_path: Path,
) -> None:
    arguments = fixture(tmp_path)
    service = ProviderVisualEvidencePreparationService(
        tmp_path / "provider-evidence",
        redactor=injected_redactor(),
    )
    prepared = service.prepare(
        candidate_id=CANDIDATE_ID,
        **arguments,
    )

    with pytest.raises(
        ProviderVisualEvidenceError,
        match="Visual evidence failed",
    ) as error:
        service.resolve_verified_attachments(
            project_id=arguments["project_id"],
            pipeline_job_id=arguments["pipeline_job_id"],
            candidate_id=CANDIDATE_ID,
            preparation_id=prepared["preparation_id"],
            expected_manifest_sha256=prepared["manifest_sha256"],
        )
    assert error.value.code == "awaiting_deidentification_review"

    with pytest.raises(ProviderVisualEvidenceError) as error:
        service.record_privacy_review(
            project_id=arguments["project_id"],
            pipeline_job_id=arguments["pipeline_job_id"],
            candidate_id=CANDIDATE_ID,
            preparation_id=prepared["preparation_id"],
            expected_manifest_sha256=prepared["manifest_sha256"],
            approved=True,
            explicit_user_confirmation=False,
        )
    assert error.value.code == "privacy_review_confirmation_required"

    reviewed = service.record_privacy_review(
        project_id=arguments["project_id"],
        pipeline_job_id=arguments["pipeline_job_id"],
        candidate_id=CANDIDATE_ID,
        preparation_id=prepared["preparation_id"],
        expected_manifest_sha256=prepared["manifest_sha256"],
        approved=True,
        explicit_user_confirmation=True,
    )
    assert reviewed["state"] == "verified_for_provider_transmission"
    assert reviewed["human_verified"] is True
    assert reviewed["automatic_privacy_verification"] is False
    assert reviewed["review_record_sha256"]

    attachments = service.resolve_verified_attachments(
        project_id=arguments["project_id"],
        pipeline_job_id=arguments["pipeline_job_id"],
        candidate_id=CANDIDATE_ID,
        preparation_id=prepared["preparation_id"],
        expected_manifest_sha256=prepared["manifest_sha256"],
    )
    assert len(attachments) == 4
    assert attachments[0].role == "candidate_representative"
    assert attachments[0].verification_record[
        "artifact_scope"
    ] == "minimized_target_crop"
    assert attachments[0].verification_record[
        "candidate_context_policy"
    ]["minimum_source_width_pixels"] == 320
    assert attachments[0].verification_record[
        "candidate_context_policy"
    ]["minimum_source_height_pixels"] == 256
    assert all(
        row.verification_record["face_redaction_status"]
        == "redacted_verified"
        and row.verification_record[
            "license_plate_redaction_status"
        ]
        == "redacted_verified"
        for row in attachments
    )
    assert all(
        row.verification_record["artifact_scope"]
        == "minimized_reference_image"
        for row in attachments[1:]
    )
    assert all(
        row.verification_record["candidate_context_policy"] is None
        for row in attachments[1:]
    )


def test_rejected_review_never_resolves_provider_attachments(
    tmp_path: Path,
) -> None:
    arguments = fixture(tmp_path)
    service = ProviderVisualEvidencePreparationService(
        tmp_path / "provider-evidence",
        redactor=injected_redactor(),
    )
    prepared = service.prepare(
        candidate_id=CANDIDATE_ID,
        **arguments,
    )
    rejected = service.record_privacy_review(
        project_id=arguments["project_id"],
        pipeline_job_id=arguments["pipeline_job_id"],
        candidate_id=CANDIDATE_ID,
        preparation_id=prepared["preparation_id"],
        expected_manifest_sha256=prepared["manifest_sha256"],
        approved=False,
        explicit_user_confirmation=True,
    )
    assert rejected["state"] == "rejected_by_privacy_review"
    assert rejected["human_verified"] is False

    with pytest.raises(ProviderVisualEvidenceError) as error:
        service.resolve_verified_attachments(
            project_id=arguments["project_id"],
            pipeline_job_id=arguments["pipeline_job_id"],
            candidate_id=CANDIDATE_ID,
            preparation_id=prepared["preparation_id"],
            expected_manifest_sha256=prepared["manifest_sha256"],
        )
    assert error.value.code == "awaiting_deidentification_review"


def test_tampered_output_fails_closed_after_review(
    tmp_path: Path,
) -> None:
    arguments = fixture(tmp_path)
    root = tmp_path / "provider-evidence"
    service = ProviderVisualEvidencePreparationService(
        root,
        redactor=injected_redactor(),
    )
    prepared = service.prepare(
        candidate_id=CANDIDATE_ID,
        **arguments,
    )
    service.record_privacy_review(
        project_id=arguments["project_id"],
        pipeline_job_id=arguments["pipeline_job_id"],
        candidate_id=CANDIDATE_ID,
        preparation_id=prepared["preparation_id"],
        expected_manifest_sha256=prepared["manifest_sha256"],
        approved=True,
        explicit_user_confirmation=True,
    )
    output = (
        root
        / str(arguments["project_id"])
        / str(arguments["pipeline_job_id"])
        / CANDIDATE_ID
        / prepared["preparation_id"]
        / "00-candidate.png"
    )
    output.write_bytes(b"\x89PNG\r\n\x1a\nnot-an-image")

    with pytest.raises(ProviderVisualEvidenceError) as error:
        service.resolve_verified_attachments(
            project_id=arguments["project_id"],
            pipeline_job_id=arguments["pipeline_job_id"],
            candidate_id=CANDIDATE_ID,
            preparation_id=prepared["preparation_id"],
            expected_manifest_sha256=prepared["manifest_sha256"],
        )
    assert error.value.code == "visual_evidence_output_invalid"


def _rewrite_hashed_json(path: Path, value: dict) -> None:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    path.write_bytes(raw)
    path.with_suffix(path.suffix + ".sha256").write_text(
        f"{hashlib.sha256(raw).hexdigest()}\n",
        encoding="ascii",
    )


def test_external_job_artifact_root_keeps_bytes_out_of_metadata_ledger_and_rebinds(
    tmp_path: Path,
) -> None:
    arguments = fixture(tmp_path)
    metadata_root = (tmp_path / "provider-ledger").resolve()
    category_root = (tmp_path / "job-provider-category").resolve()
    category_root.mkdir()
    service = ProviderVisualEvidencePreparationService(
        metadata_root,
        redactor=injected_redactor(),
    )

    prepared = service.prepare(
        candidate_id=CANDIDATE_ID,
        job_artifact_root=category_root,
        **arguments,
    )

    external_root = (
        category_root
        / CANDIDATE_ID
        / prepared["preparation_id"]
    )
    ledger_candidate = (
        metadata_root
        / str(arguments["project_id"])
        / str(arguments["pipeline_job_id"])
        / CANDIDATE_ID
    )
    locator = ledger_candidate / (
        f"{prepared['preparation_id']}.artifact-location.json"
    )
    assert (external_root / "manifest.json").is_file()
    assert (external_root / "00-candidate.png").is_file()
    assert not (
        ledger_candidate / prepared["preparation_id"]
    ).exists()
    assert locator.is_file()
    assert str(category_root) not in locator.read_text(
        encoding="utf-8"
    )
    assert str(category_root) not in json.dumps(
        prepared,
        ensure_ascii=False,
    )
    assert prepared["state"] == "awaiting_deidentification_review"
    assert prepared["automatic_privacy_verification"] is False
    assert prepared["human_verified"] is False

    calls = []

    def resolver(project_id, pipeline_job_id, analysis_job_id):
        calls.append(
            (project_id, pipeline_job_id, analysis_job_id)
        )
        return category_root

    restarted = ProviderVisualEvidencePreparationService(
        metadata_root,
        redactor=injected_redactor(),
        artifact_root_resolver=resolver,
    )
    restored = restarted.get(
        project_id=arguments["project_id"],
        pipeline_job_id=arguments["pipeline_job_id"],
        candidate_id=CANDIDATE_ID,
        preparation_id=prepared["preparation_id"],
    )
    artifact = restarted.resolve_review_artifact(
        project_id=arguments["project_id"],
        pipeline_job_id=arguments["pipeline_job_id"],
        candidate_id=CANDIDATE_ID,
        preparation_id=prepared["preparation_id"],
        order=0,
    )
    assert restored == prepared
    assert artifact.path == (
        external_root / "00-candidate.png"
    ).resolve()
    assert calls == [
        (
            arguments["project_id"],
            arguments["pipeline_job_id"],
            arguments["analysis_job_id"],
        ),
        (
            arguments["project_id"],
            arguments["pipeline_job_id"],
            arguments["analysis_job_id"],
        ),
    ]
    reviewed = restarted.record_privacy_review(
        project_id=arguments["project_id"],
        pipeline_job_id=arguments["pipeline_job_id"],
        candidate_id=CANDIDATE_ID,
        preparation_id=prepared["preparation_id"],
        expected_manifest_sha256=prepared["manifest_sha256"],
        approved=True,
        explicit_user_confirmation=True,
    )
    assert reviewed["state"] == "verified_for_provider_transmission"
    assert (external_root / "privacy-review.json").is_file()
    assert not (ledger_candidate / "privacy-review.json").exists()
    assert len(
        restarted.resolve_verified_attachments(
            project_id=arguments["project_id"],
            pipeline_job_id=arguments["pipeline_job_id"],
            candidate_id=CANDIDATE_ID,
            preparation_id=prepared["preparation_id"],
            expected_manifest_sha256=prepared["manifest_sha256"],
        )
    ) == 4


def test_external_provider_root_change_scope_reuse_and_locator_escape_fail_closed(
    tmp_path: Path,
) -> None:
    arguments = fixture(tmp_path)
    ledger_a = (tmp_path / "ledger-a").resolve()
    ledger_b = (tmp_path / "ledger-b").resolve()
    category_a = (tmp_path / "category-a").resolve()
    category_b = (tmp_path / "category-b").resolve()
    category_a.mkdir()
    category_b.mkdir()
    service_a = ProviderVisualEvidencePreparationService(
        ledger_a,
        redactor=injected_redactor(),
        artifact_root_resolver=(
            lambda _project, _pipeline, _analysis: category_a
        ),
    )
    prepared_a = service_a.prepare(
        candidate_id=CANDIDATE_ID,
        **arguments,
    )
    # Initialize another authorized category root for the same scope without
    # touching the first ledger or artifact.
    service_b = ProviderVisualEvidencePreparationService(
        ledger_b,
        redactor=injected_redactor(),
        artifact_root_resolver=(
            lambda _project, _pipeline, _analysis: category_b
        ),
    )
    service_b.prepare(candidate_id=CANDIDATE_ID, **arguments)

    wrong_rebind = ProviderVisualEvidencePreparationService(
        ledger_a,
        redactor=injected_redactor(),
        artifact_root_resolver=(
            lambda _project, _pipeline, _analysis: category_b
        ),
    )
    with pytest.raises(ProviderVisualEvidenceError) as error:
        wrong_rebind.get(
            project_id=arguments["project_id"],
            pipeline_job_id=arguments["pipeline_job_id"],
            candidate_id=CANDIDATE_ID,
            preparation_id=prepared_a["preparation_id"],
        )
    assert error.value.code == "visual_evidence_artifact_root_changed"

    cross_scope = dict(arguments)
    cross_scope["pipeline_job_id"] = uuid4()
    with pytest.raises(ProviderVisualEvidenceError) as error:
        service_a.prepare(
            candidate_id=CANDIDATE_ID,
            **cross_scope,
        )
    assert error.value.code == "visual_evidence_artifact_root_conflict"

    locator = (
        ledger_a
        / str(arguments["project_id"])
        / str(arguments["pipeline_job_id"])
        / CANDIDATE_ID
        / (
            f"{prepared_a['preparation_id']}"
            ".artifact-location.json"
        )
    )
    locator_value = json.loads(locator.read_text(encoding="utf-8"))
    locator_value["relative_artifact_path"] = (
        f"../{CANDIDATE_ID}/{prepared_a['preparation_id']}"
    )
    body = {
        key: locator_value[key]
        for key in sorted(locator_value)
        if key != "record_sha256"
    }
    locator_value["record_sha256"] = hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    _rewrite_hashed_json(locator, locator_value)

    with pytest.raises(ProviderVisualEvidenceError) as error:
        service_a.get(
            project_id=arguments["project_id"],
            pipeline_job_id=arguments["pipeline_job_id"],
            candidate_id=CANDIDATE_ID,
            preparation_id=prepared_a["preparation_id"],
        )
    assert (
        error.value.code
        == "visual_evidence_artifact_location_invalid"
    )


def test_legacy_provider_record_remains_immutable_when_resolver_is_added(
    tmp_path: Path,
) -> None:
    arguments = fixture(tmp_path)
    metadata_root = (tmp_path / "provider-ledger").resolve()
    legacy = ProviderVisualEvidencePreparationService(
        metadata_root,
        redactor=injected_redactor(),
    )
    prepared = legacy.prepare(
        candidate_id=CANDIDATE_ID,
        **arguments,
    )
    legacy_root = (
        metadata_root
        / str(arguments["project_id"])
        / str(arguments["pipeline_job_id"])
        / CANDIDATE_ID
        / prepared["preparation_id"]
    )
    before = {
        child.name: child.read_bytes()
        for child in legacy_root.iterdir()
        if child.is_file()
    }
    category_root = (tmp_path / "provider-category").resolve()
    category_root.mkdir()
    restarted = ProviderVisualEvidencePreparationService(
        metadata_root,
        redactor=injected_redactor(),
        artifact_root_resolver=(
            lambda _project, _pipeline, _analysis: category_root
        ),
    )

    assert restarted.get(
        project_id=arguments["project_id"],
        pipeline_job_id=arguments["pipeline_job_id"],
        candidate_id=CANDIDATE_ID,
        preparation_id=prepared["preparation_id"],
    ) == prepared
    assert before == {
        child.name: child.read_bytes()
        for child in legacy_root.iterdir()
        if child.is_file()
    }


def test_provider_rejects_symlinked_job_artifact_root(
    tmp_path: Path,
) -> None:
    arguments = fixture(tmp_path)
    target = (tmp_path / "real-category").resolve()
    target.mkdir()
    linked = tmp_path / "linked-category"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    service = ProviderVisualEvidencePreparationService(
        tmp_path / "provider-ledger",
        redactor=injected_redactor(),
    )

    with pytest.raises(ProviderVisualEvidenceError) as error:
        service.prepare(
            candidate_id=CANDIDATE_ID,
            job_artifact_root=linked.absolute(),
            **arguments,
        )
    assert (
        error.value.code
        == "visual_evidence_artifact_root_unsafe"
    )
