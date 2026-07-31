from __future__ import annotations

import hashlib
import json
from collections import namedtuple
from pathlib import Path
from uuid import UUID, uuid4

import pytest

import app.current_report_publication as publication_module
from app.current_report_application import (
    AcceptedCurrentReportPublicationSource,
    CurrentReportApplicationError,
)
from app.current_report_publication import (
    CurrentReportPublicationError,
    CurrentReportPublicationService,
)
from app.project_artifact_roots import ProjectArtifactRootRegistry


APPLICATION_ID = "current-report-application-" + ("a" * 32)
DRAFT_ID = "current-report-" + ("b" * 32)
DiskUsage = namedtuple("DiskUsage", "total used free")


class AcceptedSourceResolver:
    def __init__(
        self,
        source: AcceptedCurrentReportPublicationSource | None,
    ) -> None:
        self.source = source
        self.calls: list[dict[str, object]] = []
        self.error: CurrentReportApplicationError | None = None

    def resolve_accepted_publication_source(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        assert self.source is not None
        return self.source


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _setup(tmp_path: Path, *, disk_usage=None):
    project_id = uuid4()
    pipeline_job_id = uuid4()
    analysis_job_id = uuid4()
    bridge_root = (tmp_path / "bridge").resolve()
    selected_root = (tmp_path / "selected-output").resolve()
    source_root = (tmp_path / "accepted-source").resolve()
    selected_root.mkdir()
    source_root.mkdir()
    hwpx = source_root / "validated.hwpx"
    pdf = source_root / "validated.pdf"
    hwpx.write_bytes(b"accepted-hwpx-package")
    pdf.write_bytes(b"accepted-pdf-companion")
    source = AcceptedCurrentReportPublicationSource(
        application_id=APPLICATION_ID,
        project_id=project_id,
        pipeline_job_id=pipeline_job_id,
        analysis_job_id=analysis_job_id,
        draft_id=DRAFT_ID,
        output_path=hwpx,
        output_file_name=hwpx.name,
        output_sha256=_sha256(hwpx),
        output_size_bytes=hwpx.stat().st_size,
        pdf_path=pdf,
        pdf_file_name=pdf.name,
        pdf_sha256=_sha256(pdf),
        pdf_size_bytes=pdf.stat().st_size,
        binding_manifest_sha256="1" * 64,
        exact_plan_sha256="2" * 64,
        document_edit_plan_sha256="3" * 64,
        accept_reject_rollback_binding_sha256="4" * 64,
        output_reopen_structure_sha256="5" * 64,
        raster_artifact_manifest_sha256="6" * 64,
        rendered_page_count=48,
    )
    registry = ProjectArtifactRootRegistry(data_root=bridge_root)
    location = registry.register(
        project_id,
        selected_directory=selected_root,
    )
    registry.capture_profile_binding(
        project_id,
        profile_revision=1,
        output_location_id=location["output_location_id"],
    )
    resolved = registry.capture_job_binding(
        project_id,
        profile_revision=1,
        output_location_id=location["output_location_id"],
    )
    job = {
        "project_id": str(project_id),
        "job_id": str(pipeline_job_id),
        "profile_snapshot": {
            "output_location_id": location["output_location_id"],
        },
        "provenance": {"artifact_root": dict(resolved.binding)},
    }
    resolver = AcceptedSourceResolver(source)
    service = CurrentReportPublicationService(
        owned_publication_root=(
            bridge_root / "current-report-publications"
        ),
        application_service=resolver,
        artifact_root_registry=registry,
        pipeline_job_resolver=lambda project, job_id: (
            job
            if project == project_id and job_id == pipeline_job_id
            else (_ for _ in ()).throw(KeyError("job not found"))
        ),
        disk_usage=disk_usage or publication_module.shutil.disk_usage,
        safety_reserve_bytes=0,
    )
    return {
        "project_id": project_id,
        "pipeline_job_id": pipeline_job_id,
        "analysis_job_id": analysis_job_id,
        "bridge_root": bridge_root,
        "selected_root": selected_root,
        "artifact_root": resolved.root,
        "source": source,
        "resolver": resolver,
        "registry": registry,
        "job": job,
        "service": service,
    }


def _publish(setup):
    return setup["service"].publish_accepted(
        project_id=setup["project_id"],
        pipeline_job_id=setup["pipeline_job_id"],
        application_id=APPLICATION_ID,
    )


def _restart(setup, *, disk_usage=None):
    return CurrentReportPublicationService(
        owned_publication_root=(
            setup["bridge_root"] / "current-report-publications"
        ),
        application_service=setup["resolver"],
        artifact_root_registry=setup["registry"],
        pipeline_job_resolver=lambda _project, _job: setup["job"],
        disk_usage=disk_usage or publication_module.shutil.disk_usage,
        safety_reserve_bytes=0,
    )


def _category(setup) -> Path:
    return (
        setup["artifact_root"]
        / "pipeline-jobs"
        / str(setup["pipeline_job_id"])
        / "reports"
    )


def _ledger(setup) -> Path:
    return (
        setup["bridge_root"]
        / "current-report-publications"
        / str(setup["project_id"])
        / str(setup["pipeline_job_id"])
        / APPLICATION_ID
        / "record.json"
    )


def test_accepted_report_is_published_once_with_path_free_ledger(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)

    first = _publish(setup)
    second = _publish(setup)

    category = _category(setup)
    published_hwpx = category / first.hwpx_file_name
    published_pdf = category / first.pdf_file_name
    assert first.state == "published"
    assert first.path_exposed is False
    assert first.idempotent_replay is False
    assert second.idempotent_replay is True
    assert tuple(sorted(path.name for path in category.iterdir())) == (
        first.hwpx_file_name,
        first.pdf_file_name,
    )
    assert _sha256(published_hwpx) == setup["source"].output_sha256
    assert _sha256(published_pdf) == setup["source"].pdf_sha256

    serialized = json.dumps(
        {
            "metadata": second.__dict__,
            "ledger": json.loads(_ledger(setup).read_text("utf-8")),
        },
        ensure_ascii=False,
    )
    assert str(setup["source"].output_path) not in serialized
    assert str(setup["source"].pdf_path) not in serialized
    assert str(setup["selected_root"]) not in serialized
    assert str(setup["artifact_root"]) not in serialized
    assert "artifact_root_path" not in serialized
    assert "output_path" not in serialized


def test_publication_cannot_start_before_accepted_source_exists(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)
    setup["resolver"].error = CurrentReportApplicationError(
        "accepted_publication_source_unavailable",
        "not accepted",
        application_id=APPLICATION_ID,
    )

    with pytest.raises(CurrentReportPublicationError) as caught:
        _publish(setup)

    assert caught.value.code == "accepted_publication_source_unavailable"
    assert not (
        setup["artifact_root"] / "pipeline-jobs"
    ).exists()
    assert not _ledger(setup).exists()


def test_capacity_preflight_happens_before_staging_or_publish(
    tmp_path: Path,
) -> None:
    setup = _setup(
        tmp_path,
        disk_usage=lambda _path: DiskUsage(1, 1, 0),
    )

    with pytest.raises(CurrentReportPublicationError) as caught:
        _publish(setup)

    assert caught.value.code == "publication_capacity_insufficient"
    category = _category(setup)
    assert category.is_dir()
    assert tuple(category.iterdir()) == ()
    record = json.loads(_ledger(setup).read_text("utf-8"))
    assert record["state"] == "publishing"


def test_foreign_destination_is_never_overwritten(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)
    category = _category(setup)
    category.mkdir(parents=True)
    foreign = category / "current-report-aaaaaaaaaaaaaaaa.hwpx"
    foreign.write_bytes(b"foreign-owner")

    with pytest.raises(CurrentReportPublicationError) as caught:
        _publish(setup)

    assert caught.value.code == "publication_target_conflict"
    assert foreign.read_bytes() == b"foreign-owner"
    assert not (
        category / "current-report-aaaaaaaaaaaaaaaa.pdf"
    ).exists()


def test_interrupted_pair_publication_resumes_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path)
    original = publication_module._atomic_publish_no_replace
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        destination = Path(args[1])
        if destination.parent == _category(setup):
            calls += 1
            if calls == 2:
                raise CurrentReportPublicationError(
                    "simulated_interruption",
                    "simulated interruption",
                )
        return original(*args, **kwargs)

    monkeypatch.setattr(
        publication_module,
        "_atomic_publish_no_replace",
        fail_second,
    )
    with pytest.raises(CurrentReportPublicationError) as caught:
        _publish(setup)
    assert caught.value.code == "simulated_interruption"
    assert (
        _category(setup)
        / "current-report-aaaaaaaaaaaaaaaa.hwpx"
    ).is_file()
    assert not (
        _category(setup)
        / "current-report-aaaaaaaaaaaaaaaa.pdf"
    ).exists()

    monkeypatch.setattr(
        publication_module,
        "_atomic_publish_no_replace",
        original,
    )
    restarted = _restart(setup)
    result = restarted.publish_accepted(
        project_id=setup["project_id"],
        pipeline_job_id=setup["pipeline_job_id"],
        application_id=APPLICATION_ID,
    )

    assert result.state == "published"
    assert (
        _category(setup) / result.hwpx_file_name
    ).read_bytes() == setup["source"].output_path.read_bytes()
    assert (
        _category(setup) / result.pdf_file_name
    ).read_bytes() == setup["source"].pdf_path.read_bytes()


def test_rollback_deletes_only_hash_identical_owned_publication(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)
    published = _publish(setup)
    category = _category(setup)
    hwpx = category / published.hwpx_file_name
    pdf = category / published.pdf_file_name
    expected_pdf = pdf.read_bytes()
    pdf.write_bytes(b"changed-after-publication")

    with pytest.raises(CurrentReportPublicationError) as caught:
        setup["service"].rollback(
            project_id=setup["project_id"],
            pipeline_job_id=setup["pipeline_job_id"],
            application_id=APPLICATION_ID,
        )

    assert caught.value.code == "publication_rollback_blocked"
    assert hwpx.is_file()
    assert pdf.read_bytes() == b"changed-after-publication"
    pdf.write_bytes(expected_pdf)

    rolled_back = setup["service"].rollback(
        project_id=setup["project_id"],
        pipeline_job_id=setup["pipeline_job_id"],
        application_id=APPLICATION_ID,
    )
    replay = setup["service"].rollback(
        project_id=setup["project_id"],
        pipeline_job_id=setup["pipeline_job_id"],
        application_id=APPLICATION_ID,
    )

    assert rolled_back.state == "rolled_back"
    assert rolled_back.rollback_completed is True
    assert replay.idempotent_replay is True
    assert not hwpx.exists()
    assert not pdf.exists()
    assert setup["source"].output_path.is_file()
    assert setup["source"].pdf_path.is_file()

    setup["resolver"].error = CurrentReportApplicationError(
        "accepted_publication_source_unavailable",
        "internal accepted source was rolled back",
        application_id=APPLICATION_ID,
    )
    replay_without_source = setup["service"].rollback(
        project_id=setup["project_id"],
        pipeline_job_id=setup["pipeline_job_id"],
        application_id=APPLICATION_ID,
    )
    assert replay_without_source.state == "rolled_back"
    assert replay_without_source.idempotent_replay is True


def test_identical_foreign_destination_is_not_adopted_or_deleted(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)
    category = _category(setup)
    category.mkdir(parents=True)
    foreign = category / "current-report-aaaaaaaaaaaaaaaa.hwpx"
    foreign.write_bytes(setup["source"].output_path.read_bytes())

    with pytest.raises(CurrentReportPublicationError) as caught:
        _publish(setup)

    assert caught.value.code == "publication_target_conflict"
    assert foreign.read_bytes() == setup["source"].output_path.read_bytes()


def test_capacity_is_counted_per_missing_artifact_not_per_stage_total(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path)
    original = publication_module._atomic_publish_no_replace
    final_count = 0

    def stop_before_pdf(*args, **kwargs):
        nonlocal final_count
        destination = Path(args[1])
        if destination.parent == _category(setup):
            final_count += 1
            if final_count == 2:
                raise CurrentReportPublicationError(
                    "simulated_interruption",
                    "simulated interruption",
                )
        return original(*args, **kwargs)

    monkeypatch.setattr(
        publication_module,
        "_atomic_publish_no_replace",
        stop_before_pdf,
    )
    with pytest.raises(CurrentReportPublicationError):
        _publish(setup)
    monkeypatch.setattr(
        publication_module,
        "_atomic_publish_no_replace",
        original,
    )

    record = json.loads(_ledger(setup).read_text("utf-8"))
    stage = (
        _category(setup)
        / (
            ".current-report-stage-"
            + record["publication_id"].removeprefix(
                "current-report-publication-"
            )
        )
    )
    (stage / "current-report.pdf").unlink()
    (stage / "current-report.hwpx").write_bytes(
        setup["source"].output_path.read_bytes()
    )
    pdf_size = setup["source"].pdf_size_bytes
    capacity_limited = _restart(
        setup,
        disk_usage=lambda _path: DiskUsage(
            pdf_size,
            1,
            pdf_size - 1,
        ),
    )

    with pytest.raises(CurrentReportPublicationError) as caught:
        capacity_limited.publish_accepted(
            project_id=setup["project_id"],
            pipeline_job_id=setup["pipeline_job_id"],
            application_id=APPLICATION_ID,
        )

    assert caught.value.code == "publication_capacity_insufficient"
    assert not (
        _category(setup)
        / "current-report-aaaaaaaaaaaaaaaa.pdf"
    ).exists()


def test_dangling_stage_link_is_rejected_without_writing_outside(
    tmp_path: Path,
) -> None:
    setup = _setup(
        tmp_path,
        disk_usage=lambda _path: DiskUsage(1, 1, 0),
    )
    with pytest.raises(CurrentReportPublicationError):
        _publish(setup)
    record = json.loads(_ledger(setup).read_text("utf-8"))
    stage = (
        _category(setup)
        / (
            ".current-report-stage-"
            + record["publication_id"].removeprefix(
                "current-report-publication-"
            )
        )
    )
    stage.mkdir()
    outside = tmp_path / "outside-created-by-link.pdf"
    try:
        (stage / "current-report.pdf").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks are unavailable")

    with pytest.raises(CurrentReportPublicationError) as caught:
        _restart(setup).publish_accepted(
            project_id=setup["project_id"],
            pipeline_job_id=setup["pipeline_job_id"],
            application_id=APPLICATION_ID,
        )

    assert caught.value.code == "publication_staging_conflict"
    assert not outside.exists()


def test_partial_rollback_resumes_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path)
    published = _publish(setup)
    original = publication_module._atomic_quarantine_owned_file
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise CurrentReportPublicationError(
                "simulated_rollback_interruption",
                "simulated rollback interruption",
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(
        publication_module,
        "_atomic_quarantine_owned_file",
        fail_second,
    )
    with pytest.raises(CurrentReportPublicationError) as caught:
        setup["service"].rollback(
            project_id=setup["project_id"],
            pipeline_job_id=setup["pipeline_job_id"],
            application_id=APPLICATION_ID,
        )
    assert caught.value.code == "simulated_rollback_interruption"
    assert json.loads(_ledger(setup).read_text("utf-8"))["state"] == (
        "rolling_back"
    )

    monkeypatch.setattr(
        publication_module,
        "_atomic_quarantine_owned_file",
        original,
    )
    resumed = _restart(setup).rollback(
        project_id=setup["project_id"],
        pipeline_job_id=setup["pipeline_job_id"],
        application_id=APPLICATION_ID,
    )
    assert resumed.state == "rolled_back"
    assert not (_category(setup) / published.hwpx_file_name).exists()
    assert not (_category(setup) / published.pdf_file_name).exists()
    assert not list(_category(setup).glob(".current-report-rollback-*"))


def test_rollback_resumes_when_final_ledger_transition_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path)
    _publish(setup)
    original = publication_module._replace_record
    failed = False

    def fail_rolled_back(path, record):
        nonlocal failed
        if record["state"] == "rolled_back" and not failed:
            failed = True
            raise CurrentReportPublicationError(
                "simulated_ledger_interruption",
                "simulated ledger interruption",
            )
        return original(path, record)

    monkeypatch.setattr(
        publication_module,
        "_replace_record",
        fail_rolled_back,
    )
    with pytest.raises(CurrentReportPublicationError) as caught:
        setup["service"].rollback(
            project_id=setup["project_id"],
            pipeline_job_id=setup["pipeline_job_id"],
            application_id=APPLICATION_ID,
        )
    assert caught.value.code == "simulated_ledger_interruption"
    assert json.loads(_ledger(setup).read_text("utf-8"))["state"] == (
        "rolling_back"
    )
    assert len(
        list(_category(setup).glob(".current-report-rollback-*"))
    ) == 2

    monkeypatch.setattr(
        publication_module,
        "_replace_record",
        original,
    )
    resumed = _restart(setup).rollback(
        project_id=setup["project_id"],
        pipeline_job_id=setup["pipeline_job_id"],
        application_id=APPLICATION_ID,
    )
    assert resumed.state == "rolled_back"
    assert not list(_category(setup).glob(".current-report-rollback-*"))


def test_failed_copy_promotion_leaves_no_partial_stage_and_restarts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path)
    original = publication_module._atomic_publish_no_replace
    failed = False

    def fail_first_stage_promotion(*args, **kwargs):
        nonlocal failed
        destination = Path(args[1])
        if (
            not failed
            and destination.name == "current-report.hwpx"
            and destination.parent.name.startswith(
                ".current-report-stage-"
            )
        ):
            failed = True
            raise CurrentReportPublicationError(
                "simulated_copy_interruption",
                "simulated copy interruption",
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(
        publication_module,
        "_atomic_publish_no_replace",
        fail_first_stage_promotion,
    )
    with pytest.raises(CurrentReportPublicationError) as caught:
        _publish(setup)
    assert caught.value.code == "simulated_copy_interruption"
    assert not list(_category(setup).rglob(".copy-*.tmp"))

    monkeypatch.setattr(
        publication_module,
        "_atomic_publish_no_replace",
        original,
    )
    result = _restart(setup).publish_accepted(
        project_id=setup["project_id"],
        pipeline_job_id=setup["pipeline_job_id"],
        application_id=APPLICATION_ID,
    )
    assert result.state == "published"
    assert not list(_category(setup).rglob(".copy-*.tmp"))


def test_pipeline_job_resolution_failure_is_path_free(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)
    service = CurrentReportPublicationService(
        owned_publication_root=(
            setup["bridge_root"] / "other-publication-ledger"
        ),
        application_service=setup["resolver"],
        artifact_root_registry=setup["registry"],
        pipeline_job_resolver=lambda *_args: (_ for _ in ()).throw(
            RuntimeError(str(setup["selected_root"]))
        ),
        safety_reserve_bytes=0,
    )

    with pytest.raises(CurrentReportPublicationError) as caught:
        service.publish_accepted(
            project_id=setup["project_id"],
            pipeline_job_id=setup["pipeline_job_id"],
            application_id=APPLICATION_ID,
        )

    assert caught.value.code == "publication_output_location_unavailable"
    assert str(setup["selected_root"]) not in str(caught.value)
