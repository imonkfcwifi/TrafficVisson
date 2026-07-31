from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.analysis_jobs import (
    ANALYSIS_ARTIFACT_CATEGORY,
    AnalysisJobService,
    ResolvedAnalysisArtifactRoot,
)
from app.media_intake import CompletedMediaGrant
from app.project_model_lifecycle import (
    PROJECT_MODEL_ARTIFACT_CATEGORY,
    DeterministicTestTrainer,
    ProjectModelLifecycleError,
    ProjectModelLifecycleStore,
    ResolvedModelArtifactRoot,
)
from app.dataset_versions import canonical_json_bytes
from test_analysis_jobs import _limits, _make_video
from test_project_model_lifecycle import (
    _approved_reference,
    _dataset,
    _freeze_references,
)


def _job_binding(project_id: UUID, *, location_seed: str = "1"):
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": (
            "trafficvisson.project-artifact-root-job-binding.v1"
        ),
        "project_id": str(project_id),
        "profile_revision": 1,
        "output_location_id": (
            f"project-output-{location_seed * 32}"
        ),
        "registry_revision": 1,
        "root_identity_sha256": hashlib.sha256(
            f"root:{project_id}:{location_seed}".encode()
        ).hexdigest(),
        "marker_sha256": hashlib.sha256(
            f"marker:{project_id}:{location_seed}".encode()
        ).hexdigest(),
        "configured_free_bytes": 10 * 1024**3,
        "configured_at": now,
        "job_start_free_bytes": 9 * 1024**3,
        "job_start_at": now,
        "path_exposed": False,
    }


class _StaticMediaIntake:
    def __init__(self, grant: CompletedMediaGrant) -> None:
        self.grant = grant

    def resolve_completed_media(self, *args):
        expected = (
            self.grant.project_id,
            self.grant.snapshot_id,
            self.grant.file_name,
            self.grant.hash_job_id,
            self.grant.sha256_lower,
        )
        if args != expected:
            raise AssertionError("unexpected media grant lookup")
        return self.grant


class _AnalysisRootResolver:
    def __init__(self, base: Path, binding) -> None:
        self.base = base
        self.binding = dict(binding)
        self.calls = []

    def __call__(self, project_id, owner_id, category, binding):
        assert category == ANALYSIS_ARTIFACT_CATEGORY
        returned_binding = dict(binding or self.binding)
        root = (
            self.base
            / str(owner_id)
            / category
        )
        root.mkdir(parents=True, exist_ok=True)
        self.calls.append((project_id, owner_id, category, binding))
        return ResolvedAnalysisArtifactRoot(
            root=root.resolve(),
            binding=returned_binding,
        )


class _ModelRootResolver:
    def __init__(self, base: Path, binding) -> None:
        self.base = base
        self.binding = dict(binding)
        self.calls = []

    def __call__(self, project_id, owner_id, category, binding):
        assert category == PROJECT_MODEL_ARTIFACT_CATEGORY
        returned_binding = dict(binding or self.binding)
        root = (
            self.base
            / str(project_id)
            / category
            / str(owner_id)
        )
        root.mkdir(parents=True, exist_ok=True)
        self.calls.append((project_id, owner_id, category, binding))
        return ResolvedModelArtifactRoot(
            root=root.resolve(),
            binding=returned_binding,
        )


class _ChangedBindingModelRootResolver(_ModelRootResolver):
    def __init__(self, base: Path, binding, changed_binding) -> None:
        super().__init__(base, binding)
        self.changed_binding = dict(changed_binding)

    def __call__(self, project_id, owner_id, category, binding):
        result = super().__call__(
            project_id,
            owner_id,
            category,
            binding,
        )
        return ResolvedModelArtifactRoot(
            root=result.root,
            binding=self.changed_binding,
        )


def _analysis_service(
    tmp_path: Path,
    *,
    external_base: Path,
):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    project_id = uuid4()
    source_bytes = source.read_bytes()
    stat_result = source.stat()
    grant = CompletedMediaGrant(
        project_id=project_id,
        snapshot_id=uuid4(),
        hash_job_id=uuid4(),
        file_name=source.name,
        media_path=source.resolve(),
        size_bytes=stat_result.st_size,
        modified_time_ns=stat_result.st_mtime_ns,
        sha256_lower=hashlib.sha256(source_bytes).hexdigest(),
    )
    binding = _job_binding(project_id)
    pipeline_job_id = uuid4()
    resolver = _AnalysisRootResolver(external_base, binding)
    service = AnalysisJobService(
        _StaticMediaIntake(grant),
        data_root=(tmp_path / "bridge").resolve(),
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        extraction_limits=_limits(),
        artifact_root_resolver=resolver,
    )
    return service, grant, binding, pipeline_job_id, resolver


def _wait_analysis(service, project_id, job_id):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        row = service.get(project_id, job_id)
        if row["status"] in {"completed", "cancelled", "failed"}:
            return row
        time.sleep(0.02)
    raise AssertionError("analysis did not finish")


def test_analysis_heavy_artifacts_use_bound_external_root_and_restart(
    tmp_path,
):
    external = (tmp_path / "large-output").resolve()
    service, grant, binding, pipeline_job_id, resolver = _analysis_service(
        tmp_path,
        external_base=external,
    )
    started = service.start(
        grant.project_id,
        grant.snapshot_id,
        grant.file_name,
        grant.hash_job_id,
        grant.sha256_lower,
        artifact_root_binding=binding,
        artifact_root_owner_id=pipeline_job_id,
    )
    terminal = _wait_analysis(
        service,
        grant.project_id,
        UUID(str(started["job_id"])),
    )
    assert terminal["status"] == "completed", terminal
    job_id = UUID(str(terminal["job_id"]))
    category_root = (
        external
        / str(pipeline_job_id)
        / ANALYSIS_ARTIFACT_CATEGORY
    )
    run = next(category_root.glob("candidate-extraction-*"))
    assert (run / "artifact-manifest.json").is_file()
    assert list(
        (
            tmp_path
            / "bridge"
            / "projects"
            / str(grant.project_id)
            / "candidate-extractions"
        ).glob("candidate-extraction-*")
    ) == []
    record = (
        tmp_path
        / "bridge"
        / "projects"
        / str(grant.project_id)
        / "analysis-job-records"
        / f"{job_id}.json"
    )
    record_text = record.read_text(encoding="utf-8")
    assert str(external) not in record_text
    stored = json.loads(record_text)
    locator = stored["artifact_locator"]
    assert locator["output_location_id"] == binding[
        "output_location_id"
    ]
    assert locator["root_identity_sha256"] == binding[
        "root_identity_sha256"
    ]
    assert len(locator["artifact_manifest_sha256"]) == 64

    service.shutdown()
    restarted_resolver = _AnalysisRootResolver(external, binding)
    restarted = AnalysisJobService(
        _StaticMediaIntake(grant),
        data_root=(tmp_path / "bridge").resolve(),
        ffmpeg_path=None,
        ffprobe_path=None,
        extraction_limits=_limits(),
        artifact_root_resolver=restarted_resolver,
    )
    restored = restarted.get(grant.project_id, job_id)
    assert restored["result"]["candidates"] == terminal["result"][
        "candidates"
    ]
    assert str(restored["result"]["artifact_set_id"]) == str(
        terminal["result"]["artifact_set_id"]
    )
    candidate = restored["result"]["candidates"][0]
    resolved = restarted.resolve_artifact(
        grant.project_id,
        job_id,
        candidate["candidate_id"],
        "image",
    )
    assert resolved.path.is_relative_to(category_root)
    restarted.shutdown()


def test_analysis_manifest_or_rebound_root_tamper_fails_closed(tmp_path):
    external = (tmp_path / "large-output").resolve()
    service, grant, binding, pipeline_job_id, _resolver = _analysis_service(
        tmp_path,
        external_base=external,
    )
    started = service.start(
        grant.project_id,
        grant.snapshot_id,
        grant.file_name,
        grant.hash_job_id,
        grant.sha256_lower,
        artifact_root_binding=binding,
        artifact_root_owner_id=pipeline_job_id,
    )
    terminal = _wait_analysis(
        service,
        grant.project_id,
        UUID(str(started["job_id"])),
    )
    assert terminal["status"] == "completed"
    job_id = UUID(str(terminal["job_id"]))
    candidate = terminal["result"]["candidates"][0]
    run = next(
        (
            external
            / str(pipeline_job_id)
            / ANALYSIS_ARTIFACT_CATEGORY
        ).glob("candidate-extraction-*")
    )
    manifest = run / "artifact-manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    with pytest.raises(Exception):
        service.resolve_artifact(
            grant.project_id,
            job_id,
            candidate["candidate_id"],
            "image",
        )
    service.shutdown()

    moved_resolver = _AnalysisRootResolver(
        (tmp_path / "other-output").resolve(),
        binding,
    )
    restarted = AnalysisJobService(
        _StaticMediaIntake(grant),
        data_root=(tmp_path / "bridge").resolve(),
        artifact_root_resolver=moved_resolver,
    )
    assert restarted.list(grant.project_id) == []
    restarted.shutdown()


def _train_external_model(
    tmp_path: Path,
    *,
    trainer=None,
):
    project_id = uuid4()
    dataset, annotations = _dataset(project_id)
    reference = _approved_reference(project_id)
    frozen_dataset, frozen_references = _freeze_references(
        project_id,
        dataset,
        [reference],
    )
    binding = _job_binding(project_id)
    external = (tmp_path / "model-output").resolve()
    resolver = _ModelRootResolver(external, binding)
    store = ProjectModelLifecycleStore(
        (tmp_path / "bridge").resolve(),
        trainer=trainer or DeterministicTestTrainer(),
        allow_test_trainer=True,
        artifact_root_resolver=resolver,
    )
    job = store.start_training(
        project_id,
        dataset_version=frozen_dataset,
        current_annotations=annotations,
        auxiliary_references=frozen_references,
        base_model_version="common-initial@1.0.0",
        required_issue_types=(),
        idempotency_key="external-model-training-0001",
        user_confirmed=True,
        artifact_root_binding=binding,
    )
    replayed = store.start_training(
        project_id,
        dataset_version=frozen_dataset,
        current_annotations=annotations,
        auxiliary_references=frozen_references,
        base_model_version="common-initial@1.0.0",
        required_issue_types=(),
        idempotency_key="external-model-training-0001",
        user_confirmed=True,
        artifact_root_binding=binding,
    )
    assert replayed["training_job_id"] == job["training_job_id"]
    assert replayed["idempotent_replay"] is True
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = store.get_training_job(
            project_id,
            UUID(job["training_job_id"]),
        )
        if job["status"] not in {"queued", "running"}:
            break
        time.sleep(0.01)
    return (
        store,
        project_id,
        frozen_dataset,
        annotations,
        frozen_references,
        binding,
        external,
        resolver,
        job,
    )


def test_model_artifact_binding_restart_promotion_and_rejected_preservation(
    tmp_path,
):
    (
        store,
        project_id,
        _dataset_version,
        _annotations,
        _references,
        binding,
        external,
        _resolver,
        job,
    ) = _train_external_model(tmp_path)
    assert job["status"] == "succeeded"
    model = store.list_model_versions(project_id)["models"][0]
    locator = model["artifact_storage"]
    assert locator["output_location_id"] == binding[
        "output_location_id"
    ]
    assert locator["root_identity_sha256"] == binding[
        "root_identity_sha256"
    ]
    model_root = (
        external
        / str(project_id)
        / PROJECT_MODEL_ARTIFACT_CATEGORY
        / job["training_job_id"]
    )
    directory = model_root / Path(model["artifact_directory"])
    assert (
        directory / model["primary_artifact_relative_path"]
    ).is_file()
    assert not any(
        (
            tmp_path
            / "bridge"
            / "project-model-lifecycle"
            / "artifacts"
        ).rglob("centroid-head.json")
    )

    store.promote(
        project_id,
        UUID(model["model_version_id"]),
        expected_revision=1,
        idempotency_key="external-promote-0001",
        user_confirmed=True,
    )
    restarted = ProjectModelLifecycleStore(
        (tmp_path / "bridge").resolve(),
        trainer=DeterministicTestTrainer(),
        allow_test_trainer=True,
        artifact_root_resolver=_ModelRootResolver(external, binding),
    )
    active = restarted.resolve_active_model(project_id)
    assert active.output_location_id == binding["output_location_id"]
    assert active.root_identity_sha256 == binding[
        "root_identity_sha256"
    ]
    assert active.primary_artifact_path.is_relative_to(model_root)

    rejected = _train_external_model(
        tmp_path / "rejected",
        trainer=DeterministicTestTrainer(candidate_mode="empty"),
    )
    rejected_model = rejected[0].list_model_versions(
        rejected[1]
    )["models"][0]
    rejected_directory = (
        rejected[6]
        / str(rejected[1])
        / PROJECT_MODEL_ARTIFACT_CATEGORY
        / rejected[8]["training_job_id"]
        / Path(rejected_model["artifact_directory"])
    )
    assert rejected_model["state"] == "rejected"
    assert (
        rejected_directory
        / rejected_model["primary_artifact_relative_path"]
    ).is_file()


def test_model_wrong_binding_root_and_manifest_tamper_fail_closed(tmp_path):
    (
        store,
        project_id,
        _dataset_version,
        _annotations,
        _references,
        binding,
        external,
        _resolver,
        job,
    ) = _train_external_model(tmp_path)
    model = store.list_model_versions(project_id)["models"][0]
    moved = ProjectModelLifecycleStore(
        (tmp_path / "bridge").resolve(),
        trainer=DeterministicTestTrainer(),
        allow_test_trainer=True,
        artifact_root_resolver=_ModelRootResolver(
            (tmp_path / "moved").resolve(),
            binding,
        ),
    )
    with pytest.raises(ProjectModelLifecycleError) as changed:
        moved.promote(
            project_id,
            UUID(model["model_version_id"]),
            expected_revision=1,
            idempotency_key="wrong-root-promote-0001",
            user_confirmed=True,
        )
    assert changed.value.code == "artifact_root_binding_changed"

    directory = (
        external
        / str(project_id)
        / PROJECT_MODEL_ARTIFACT_CATEGORY
        / job["training_job_id"]
        / Path(model["artifact_directory"])
    )
    manifest = directory / "artifact-manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    with pytest.raises(ProjectModelLifecycleError) as tampered:
        store.promote(
            project_id,
            UUID(model["model_version_id"]),
            expected_revision=1,
            idempotency_key="tampered-external-model-0001",
            user_confirmed=True,
        )
    assert tampered.value.code == "artifact_integrity_failed"

    wrong_project = uuid4()
    wrong_binding = {**binding, "project_id": str(wrong_project)}
    dataset, annotations = _dataset(project_id)
    frozen, references = _freeze_references(
        project_id,
        dataset,
        [_approved_reference(project_id)],
    )
    with pytest.raises(ProjectModelLifecycleError) as crossing:
        store.start_training(
            project_id,
            dataset_version=frozen,
            current_annotations=annotations,
            auxiliary_references=references,
            base_model_version="common-initial@1.0.0",
            required_issue_types=(),
            idempotency_key="wrong-project-root-0001",
            user_confirmed=True,
            artifact_root_binding=wrong_binding,
        )
    assert crossing.value.code == "artifact_root_binding_invalid"


def test_model_profile_binding_locator_and_lineage_tamper_fail_closed(
    tmp_path,
):
    (
        store,
        project_id,
        _dataset_version,
        _annotations,
        _references,
        binding,
        external,
        _resolver,
        _job,
    ) = _train_external_model(tmp_path)
    model = store.list_model_versions(project_id)["models"][0]
    changed_binding = {
        **binding,
        "profile_revision": 2,
        "output_location_id": f"project-output-{'2' * 32}",
        "root_identity_sha256": hashlib.sha256(
            b"changed-profile-root"
        ).hexdigest(),
        "marker_sha256": hashlib.sha256(
            b"changed-profile-marker"
        ).hexdigest(),
    }
    changed = ProjectModelLifecycleStore(
        (tmp_path / "bridge").resolve(),
        trainer=DeterministicTestTrainer(),
        allow_test_trainer=True,
        artifact_root_resolver=_ChangedBindingModelRootResolver(
            external,
            binding,
            changed_binding,
        ),
    )
    with pytest.raises(ProjectModelLifecycleError) as swapped:
        changed.promote(
            project_id,
            UUID(model["model_version_id"]),
            expected_revision=1,
            idempotency_key="profile-root-swap-0001",
            user_confirmed=True,
        )
    assert swapped.value.code == "artifact_root_binding_changed"

    database = (
        tmp_path
        / "bridge"
        / "project-model-lifecycle"
        / "lifecycle.sqlite3"
    )
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT payload_json FROM model_versions WHERE id = ?",
            (model["model_version_id"],),
        ).fetchone()
        payload = json.loads(row[0])
        payload["artifact_storage"]["training_job_id"] = str(uuid4())
        encoded = canonical_json_bytes(payload).decode("utf-8")
        connection.execute(
            """
            UPDATE model_versions
            SET payload_json = ?, payload_sha256 = ?
            WHERE id = ?
            """,
            (
                encoded,
                hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                model["model_version_id"],
            ),
        )
        connection.commit()
    with pytest.raises(ProjectModelLifecycleError) as crossing:
        store.promote(
            project_id,
            UUID(model["model_version_id"]),
            expected_revision=1,
            idempotency_key="cross-training-locator-0001",
            user_confirmed=True,
        )
    assert crossing.value.code == "artifact_root_binding_invalid"


def test_external_service_roots_reject_linked_category_roots(
    tmp_path,
):
    project_id = uuid4()
    binding = _job_binding(project_id)
    real_parent = (tmp_path / "real").resolve()
    real_root = (
        real_parent
        / PROJECT_MODEL_ARTIFACT_CATEGORY
        / str(uuid4())
    )
    real_root.mkdir(parents=True)
    linked_parent = (tmp_path / "linked").resolve()
    linked_parent.mkdir()
    linked_root = (
        linked_parent
        / PROJECT_MODEL_ARTIFACT_CATEGORY
        / real_root.name
    )
    linked_root.parent.mkdir()
    try:
        linked_root.symlink_to(real_root, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")

    class _LinkedResolver:
        def __call__(self, _project_id, _owner_id, _category, value):
            return ResolvedModelArtifactRoot(
                root=linked_root,
                binding=dict(value),
            )

    dataset, annotations = _dataset(project_id)
    frozen, references = _freeze_references(
        project_id,
        dataset,
        [_approved_reference(project_id)],
    )
    store = ProjectModelLifecycleStore(
        (tmp_path / "linked-store").resolve(),
        trainer=DeterministicTestTrainer(),
        allow_test_trainer=True,
        artifact_root_resolver=_LinkedResolver(),
    )
    unsafe = store.start_training(
        project_id,
        dataset_version=frozen,
        current_annotations=annotations,
        auxiliary_references=references,
        base_model_version="common-initial@1.0.0",
        required_issue_types=(),
        idempotency_key="linked-model-root-0001",
        user_confirmed=True,
        artifact_root_binding=binding,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        unsafe = store.get_training_job(
            project_id,
            UUID(unsafe["training_job_id"]),
        )
        if unsafe["status"] not in {"queued", "running"}:
            break
        time.sleep(0.01)
    assert unsafe["status"] == "failed"
    assert unsafe["error"]["code"] in {
        "artifact_root_unsafe",
        "artifact_root_unavailable",
    }
