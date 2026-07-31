from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID

from PIL import Image
import pytest

from app.report_deidentified_evidence import (
    OpenCvReportPrivacyDetector,
    ReportDeidentifiedEvidenceError,
    ReportDeidentifiedEvidenceService,
    ReportEvidenceSource,
)


PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
PIPELINE_JOB_ID = UUID("22222222-2222-4222-8222-222222222222")
ANALYSIS_JOB_ID = UUID("33333333-3333-4333-8333-333333333333")
INCIDENT_ID = "44444444-4444-4444-8444-444444444444"
CANDIDATE_ID = "a" * 20
SOURCE_SHA256 = "b" * 64


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _detector() -> OpenCvReportPrivacyDetector:
    return OpenCvReportPrivacyDetector(
        injected_regions=lambda _image: (
            {
                "kind": "face",
                "x": 20,
                "y": 15,
                "width": 30,
                "height": 35,
            },
            {
                "kind": "license_plate",
                "x": 70,
                "y": 60,
                "width": 42,
                "height": 16,
            },
        )
    )


def _service(
    tmp_path: Path,
    *,
    artifact_root_resolver=None,
) -> ReportDeidentifiedEvidenceService:
    return ReportDeidentifiedEvidenceService(
        (tmp_path / "registry").resolve(),
        detector=_detector(),
        artifact_root_resolver=artifact_root_resolver,
    )


def _source(tmp_path: Path) -> ReportEvidenceSource:
    path = (tmp_path / "representative.jpg").resolve()
    Image.new("RGB", (160, 100), color=(220, 210, 190)).save(
        path,
        format="JPEG",
        quality=84,
    )
    return ReportEvidenceSource(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        incident_id=INCIDENT_ID,
        candidate_id=CANDIDATE_ID,
        source_sha256=SOURCE_SHA256,
        original_ref=f"analysis:{CANDIDATE_ID}:representative",
        path=path,
        original_sha256=_sha(path),
        size_bytes=path.stat().st_size,
        media_type="image/jpeg",
    )


def test_report_derivative_requires_human_review_and_resolves_after_approval(
    tmp_path,
):
    service = _service(tmp_path)
    source = _source(tmp_path)

    prepared = service.prepare(source)

    assert prepared["state"] == "awaiting_deidentification_review"
    assert prepared["automatic_privacy_verification"] is False
    assert prepared["human_verified"] is False
    assert prepared["review_required"] is True
    assert prepared["redaction_region_count"] == 2
    assert prepared["path_exposed"] is False
    assert all(
        "path" not in key
        for key in prepared
        if key != "path_exposed"
    )
    assert (
        service.resolve_verified_descriptor(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            analysis_job_id=ANALYSIS_JOB_ID,
            source_sha256=SOURCE_SHA256,
            incident_id=INCIDENT_ID,
            original_ref=source.original_ref,
            original_sha256=source.original_sha256,
        )
        is None
    )

    approved = service.record_review(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        incident_id=INCIDENT_ID,
        derivative_id=prepared["derivative_id"],
        expected_manifest_sha256=prepared["manifest_sha256"],
        approved=True,
        explicit_user_confirmation=True,
    )

    assert approved["state"] == "verified_for_shared_report"
    assert approved["reviewer_decision"] == "approved"
    assert approved["reviewer_actor"] == "local_user"
    assert approved["review_revision"] == 1
    descriptor = service.resolve_verified_descriptor(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        source_sha256=SOURCE_SHA256,
        incident_id=INCIDENT_ID,
        original_ref=source.original_ref,
        original_sha256=source.original_sha256,
    )
    assert descriptor == {
        "deidentified_ref": approved["deidentified_ref"],
        "deidentified_sha256": approved["deidentified_sha256"],
        "deidentification_record_sha256": approved[
            "deidentification_record_sha256"
        ],
        "deidentified_verified": True,
    }
    artifact = service.resolve_verified_file(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        deidentified_ref=approved["deidentified_ref"],
        expected_sha256=approved["deidentified_sha256"],
    )
    assert artifact.media_type == "image/jpeg"
    assert artifact.sha256 != source.original_sha256


def test_report_derivative_rejects_cross_job_and_tamper(tmp_path):
    service = _service(tmp_path)
    source = _source(tmp_path)
    prepared = service.prepare(source)
    service.record_review(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        incident_id=INCIDENT_ID,
        derivative_id=prepared["derivative_id"],
        expected_manifest_sha256=prepared["manifest_sha256"],
        approved=True,
        explicit_user_confirmation=True,
    )

    with pytest.raises(ReportDeidentifiedEvidenceError):
        service.resolve_verified_file(
            project_id=PROJECT_ID,
            pipeline_job_id=UUID(
                "55555555-5555-4555-8555-555555555555"
            ),
            analysis_job_id=ANALYSIS_JOB_ID,
            deidentified_ref=prepared["deidentified_ref"],
            expected_sha256=prepared["deidentified_sha256"],
        )

    source.path.write_bytes(source.path.read_bytes() + b"tampered")
    with pytest.raises(
        ReportDeidentifiedEvidenceError,
        match="Report deidentification failed",
    ):
        service.resolve_verified_descriptor(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            analysis_job_id=ANALYSIS_JOB_ID,
            source_sha256=SOURCE_SHA256,
            incident_id=INCIDENT_ID,
            original_ref=source.original_ref,
            original_sha256=source.original_sha256,
        )


def test_rejected_derivative_never_resolves(tmp_path):
    service = _service(tmp_path)
    source = _source(tmp_path)
    prepared = service.prepare(source)
    rejected = service.record_review(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        incident_id=INCIDENT_ID,
        derivative_id=prepared["derivative_id"],
        expected_manifest_sha256=prepared["manifest_sha256"],
        approved=False,
        explicit_user_confirmation=True,
    )
    assert rejected["state"] == "rejected_by_deidentification_review"
    assert rejected["deidentification_record_sha256"] is None
    assert (
        service.resolve_verified_descriptor(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            analysis_job_id=ANALYSIS_JOB_ID,
            source_sha256=SOURCE_SHA256,
            incident_id=INCIDENT_ID,
            original_ref=source.original_ref,
            original_sha256=source.original_sha256,
        )
        is None
    )


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


def test_external_report_category_root_separates_heavy_bytes_and_rebinds(
    tmp_path: Path,
) -> None:
    metadata_root = (tmp_path / "report-ledger").resolve()
    category_root = (tmp_path / "job-report-category").resolve()
    category_root.mkdir()
    source = _source(tmp_path)
    service = ReportDeidentifiedEvidenceService(
        metadata_root,
        detector=_detector(),
    )

    prepared = service.prepare(
        source,
        job_artifact_root=category_root,
    )

    external_root = (
        category_root
        / INCIDENT_ID
        / prepared["derivative_id"]
    )
    ledger_incident = (
        metadata_root
        / str(PROJECT_ID)
        / str(PIPELINE_JOB_ID)
        / str(ANALYSIS_JOB_ID)
        / INCIDENT_ID
    )
    locator = ledger_incident / (
        f"{prepared['derivative_id']}.artifact-location.json"
    )
    assert (external_root / "manifest.json").is_file()
    assert (external_root / "deidentified.jpg").is_file()
    assert not (
        ledger_incident / prepared["derivative_id"]
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

    restarted = ReportDeidentifiedEvidenceService(
        metadata_root,
        detector=_detector(),
        artifact_root_resolver=resolver,
    )
    restored = restarted.get(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        incident_id=INCIDENT_ID,
        derivative_id=prepared["derivative_id"],
    )
    listed = restarted.list(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
    )
    artifact = restarted.resolve_review_artifact(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        incident_id=INCIDENT_ID,
        derivative_id=prepared["derivative_id"],
    )
    assert restored == prepared
    assert listed == (prepared,)
    assert artifact.path == (
        external_root / "deidentified.jpg"
    ).resolve()
    assert calls
    assert all(
        row == (PROJECT_ID, PIPELINE_JOB_ID, ANALYSIS_JOB_ID)
        for row in calls
    )
    reviewed = restarted.record_review(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        incident_id=INCIDENT_ID,
        derivative_id=prepared["derivative_id"],
        expected_manifest_sha256=prepared["manifest_sha256"],
        approved=True,
        explicit_user_confirmation=True,
    )
    assert reviewed["state"] == "verified_for_shared_report"
    assert (external_root / "review.json").is_file()
    assert not (ledger_incident / "review.json").exists()
    verified = restarted.resolve_verified_file(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        deidentified_ref=prepared["deidentified_ref"],
        expected_sha256=prepared["deidentified_sha256"],
    )
    assert verified.path == (
        external_root / "deidentified.jpg"
    ).resolve()


def test_external_report_root_change_scope_reuse_and_locator_escape_fail_closed(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    ledger_a = (tmp_path / "report-ledger-a").resolve()
    ledger_b = (tmp_path / "report-ledger-b").resolve()
    category_a = (tmp_path / "report-category-a").resolve()
    category_b = (tmp_path / "report-category-b").resolve()
    category_a.mkdir()
    category_b.mkdir()
    service_a = ReportDeidentifiedEvidenceService(
        ledger_a,
        detector=_detector(),
        artifact_root_resolver=(
            lambda _project, _pipeline, _analysis: category_a
        ),
    )
    prepared_a = service_a.prepare(source)
    service_b = ReportDeidentifiedEvidenceService(
        ledger_b,
        detector=_detector(),
        artifact_root_resolver=(
            lambda _project, _pipeline, _analysis: category_b
        ),
    )
    service_b.prepare(source)

    wrong_rebind = ReportDeidentifiedEvidenceService(
        ledger_a,
        detector=_detector(),
        artifact_root_resolver=(
            lambda _project, _pipeline, _analysis: category_b
        ),
    )
    with pytest.raises(ReportDeidentifiedEvidenceError) as error:
        wrong_rebind.get(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            analysis_job_id=ANALYSIS_JOB_ID,
            incident_id=INCIDENT_ID,
            derivative_id=prepared_a["derivative_id"],
        )
    assert (
        error.value.code
        == "report_deidentification_artifact_root_changed"
    )

    cross_scope = ReportEvidenceSource(
        project_id=PROJECT_ID,
        pipeline_job_id=UUID(
            "55555555-5555-4555-8555-555555555555"
        ),
        analysis_job_id=ANALYSIS_JOB_ID,
        incident_id=INCIDENT_ID,
        candidate_id=CANDIDATE_ID,
        source_sha256=SOURCE_SHA256,
        original_ref=source.original_ref,
        path=source.path,
        original_sha256=source.original_sha256,
        size_bytes=source.size_bytes,
        media_type=source.media_type,
        time_ms=source.time_ms,
    )
    with pytest.raises(ReportDeidentifiedEvidenceError) as error:
        service_a.prepare(cross_scope)
    assert (
        error.value.code
        == "report_deidentification_artifact_root_conflict"
    )

    locator = (
        ledger_a
        / str(PROJECT_ID)
        / str(PIPELINE_JOB_ID)
        / str(ANALYSIS_JOB_ID)
        / INCIDENT_ID
        / (
            f"{prepared_a['derivative_id']}"
            ".artifact-location.json"
        )
    )
    locator_value = json.loads(locator.read_text(encoding="utf-8"))
    locator_value["relative_artifact_path"] = (
        f"../{INCIDENT_ID}/{prepared_a['derivative_id']}"
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

    with pytest.raises(ReportDeidentifiedEvidenceError) as error:
        service_a.get(
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            analysis_job_id=ANALYSIS_JOB_ID,
            incident_id=INCIDENT_ID,
            derivative_id=prepared_a["derivative_id"],
        )
    assert (
        error.value.code
        == "report_deidentification_artifact_location_invalid"
    )


def test_legacy_report_record_stays_immutable_after_resolver_rebind(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    metadata_root = (tmp_path / "report-ledger").resolve()
    legacy = ReportDeidentifiedEvidenceService(
        metadata_root,
        detector=_detector(),
    )
    prepared = legacy.prepare(source)
    legacy_root = (
        metadata_root
        / str(PROJECT_ID)
        / str(PIPELINE_JOB_ID)
        / str(ANALYSIS_JOB_ID)
        / INCIDENT_ID
        / prepared["derivative_id"]
    )
    before = {
        child.name: child.read_bytes()
        for child in legacy_root.iterdir()
        if child.is_file()
    }
    category_root = (tmp_path / "report-category").resolve()
    category_root.mkdir()
    restarted = ReportDeidentifiedEvidenceService(
        metadata_root,
        detector=_detector(),
        artifact_root_resolver=(
            lambda _project, _pipeline, _analysis: category_root
        ),
    )

    assert restarted.get(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        incident_id=INCIDENT_ID,
        derivative_id=prepared["derivative_id"],
    ) == prepared
    assert before == {
        child.name: child.read_bytes()
        for child in legacy_root.iterdir()
        if child.is_file()
    }


def test_report_rejects_symlinked_job_artifact_root(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    target = (tmp_path / "real-report-category").resolve()
    target.mkdir()
    linked = tmp_path / "linked-report-category"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    service = ReportDeidentifiedEvidenceService(
        (tmp_path / "report-ledger").resolve(),
        detector=_detector(),
    )

    with pytest.raises(ReportDeidentifiedEvidenceError) as error:
        service.prepare(
            source,
            job_artifact_root=linked.absolute(),
        )
    assert (
        error.value.code
        == "report_deidentification_artifact_root_unsafe"
    )
