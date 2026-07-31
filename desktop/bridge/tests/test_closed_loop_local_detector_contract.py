from __future__ import annotations

import hashlib
import time
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from app.candidate_labels import (
    CandidateLabelStore,
    CompletedCandidateMetadata,
)
from app.dataset_versions import canonical_json_bytes
from app.dense_candidate_discovery import (
    ActiveProjectModelDenseProposalProvider,
    DenseFrameEvidence,
)
from app.local_object_detector_inference import (
    PromotedLocalObjectDetectorInference,
)
from app.local_object_detector_trainer import (
    LOCAL_OBJECT_DETECTOR_MODEL_FORMAT,
    LocalCudaObjectDetectorTrainer,
    ResolvedDetectorArtifact,
)
from app.main import create_app
from app.project_catalog import ProjectCatalog
from app.project_model_lifecycle import ProjectModelLifecycleStore
from app.source_candidate_probe import SourceCandidateProbe
from app.store import DEMO_PROJECT_ID
from app.training_geometry import SCHEMA_VERSION, seal_training_geometry
from test_local_object_detector_inference import (
    _FakeBackend as FakeDetectorInferenceBackend,
)
from test_local_object_detector_trainer import (
    _FakeBackend as FakeCudaTrainingBackend,
)
from test_local_object_detector_trainer import _write_pack
from test_project_catalog_v2 import _project, _write_completed_analysis
from test_project_model_lifecycle_api import _approved_reference


TOKEN = "closed-loop-detector-contract-token-with-256-bits-000"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BOX = {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _rewrite_fixture_with_unique_images(
    fixture: dict,
    *,
    source_index: int,
) -> None:
    """Keep every train/validation image group unique across source videos."""

    payload = fixture["payload"]
    artifact_root = Path(fixture["artifact_root"])
    for candidate_index, candidate in enumerate(
        payload["result"]["candidates"]
    ):
        body = (
            b"\xff\xd8\xff"
            + f"source={source_index};candidate={candidate_index}".encode(
                "ascii"
            )
            + b"\xff\xd9"
        )
        image = candidate["representative_image"]
        path = artifact_root / image["relative_path"]
        path.write_bytes(body)
        image["size_bytes"] = len(body)
        image["sha256"] = _sha(body)
    payload["result"]["total_artifact_bytes"] = sum(
        candidate["representative_image"]["size_bytes"]
        + candidate["preview_clip"]["size_bytes"]
        for candidate in payload["result"]["candidates"]
    )
    encoded = canonical_json_bytes(payload)
    Path(fixture["record_path"]).write_bytes(encoded)
    Path(fixture["digest_path"]).write_text(
        _sha(encoded),
        encoding="ascii",
    )


def _training_geometry(candidate: dict) -> dict:
    image = candidate["representative_image"]
    proposal_sha256 = _sha(canonical_json_bytes(candidate))
    return seal_training_geometry(
        {
            "schema_version": SCHEMA_VERSION,
            "source_kind": "accepted_ai_proposal",
            "approval_authority": "local_user",
            "image_sha256": image["sha256"],
            "image_artifact_ref": image["artifact_ref"],
            "normalized_box": BOX,
            "polygon": None,
            "mask_artifact_ref": None,
            "mask_sha256": None,
            "tracked_frame_refs": [],
            "proposal_sha256": proposal_sha256,
            "sam2_output_sha256": None,
            "sam2_model_manifest_sha256": None,
        }
    )


def test_approved_seed_to_fake_cuda_detector_to_independent_reviewed_incident(
    tmp_path,
):
    """Exercise the closed loop with real stores and explicit test doubles.

    This contract test performs no real GPU, NAS, or HWP/COM operation.  The
    CUDA trainer and promoted-detector inference backends are deterministic
    fakes, and the trainer is explicitly marked test-only before lifecycle
    registration.  Production claims therefore still require the actual
    hardware and E2E evidence recorded in the QA ledger.
    """

    bridge_root = (tmp_path / "bridge").resolve()
    bridge_root.mkdir()
    training_fixtures = []
    for source_index in range(13):
        fixture = _write_completed_analysis(
            bridge_root,
            project_id=DEMO_PROJECT_ID,
            source_seed=f"closed-loop-training-{source_index:02d}",
            candidate_count=10,
        )
        _rewrite_fixture_with_unique_images(
            fixture,
            source_index=source_index,
        )
        training_fixtures.append(fixture)
    survey = _write_completed_analysis(
        bridge_root,
        project_id=DEMO_PROJECT_ID,
        source_seed="closed-loop-independent-survey",
        candidate_count=1,
    )
    _rewrite_fixture_with_unique_images(survey, source_index=999)

    catalog = ProjectCatalog(
        bridge_root / "catalog.sqlite3",
        _project(project_id=DEMO_PROJECT_ID),
    )
    artifacts: dict[
        tuple[str, str, str, str],
        ResolvedDetectorArtifact,
    ] = {}
    label_candidates = {}
    for source_index, fixture in enumerate(training_fixtures):
        for candidate_index, candidate in enumerate(
            fixture["payload"]["result"]["candidates"]
        ):
            review_seed = (
                f"{fixture['job_id']}:{candidate['candidate_id']}"
            )
            metadata = {
                **candidate,
                "evidence_overlay": {
                    "schema_version": (
                        "trafficvisson.candidate-evidence-overlay.v1"
                    ),
                    "overlay_id": (
                        "ceo_" + _sha(
                            f"{review_seed}:overlay".encode("utf-8")
                        )[:32]
                    ),
                    "pipeline_job_id": str(fixture["job_id"]),
                    "manifest_sha256": _sha(
                        f"{review_seed}:overlay-manifest".encode(
                            "utf-8"
                        )
                    ),
                    "candidate_set_metadata_sha256": _sha(
                        f"{review_seed}:candidate-set".encode(
                            "utf-8"
                        )
                    ),
                    "review_only": True,
                },
                "review_event_binding": {
                    "schema_version": (
                        "trafficvisson.review-event-binding.v1"
                    ),
                    "review_event_set_id": (
                        "res_"
                        + _sha(
                            f"{review_seed}:review-set".encode(
                                "utf-8"
                            )
                        )[:32]
                    ),
                    "review_event_set_revision": _sha(
                        f"{review_seed}:review-set-revision".encode(
                            "utf-8"
                        )
                    ),
                    "review_event_set_manifest_sha256": _sha(
                        f"{review_seed}:review-set-manifest".encode(
                            "utf-8"
                        )
                    ),
                    "review_event_id": (
                        "rve_"
                        + _sha(
                            f"{review_seed}:review-event".encode(
                                "utf-8"
                            )
                        )[:32]
                    ),
                    "review_event_revision": _sha(
                        f"{review_seed}:review-event-revision".encode(
                            "utf-8"
                        )
                    ),
                    "representative_candidate_id": candidate[
                        "candidate_id"
                    ],
                    "review_only": True,
                },
            }
            label_candidates[
                (
                    str(DEMO_PROJECT_ID),
                    str(fixture["job_id"]),
                    candidate["candidate_id"],
                )
            ] = {
                "metadata": metadata,
                "source_sha256": fixture["source_sha256"],
            }
            image = candidate["representative_image"]
            path = (
                Path(fixture["artifact_root"])
                / image["relative_path"]
            ).resolve()
            artifacts[
                (
                    str(DEMO_PROJECT_ID),
                    str(fixture["job_id"]),
                    candidate["candidate_id"],
                    image["artifact_ref"],
                )
            ] = ResolvedDetectorArtifact(
                path=path,
                media_type=image["media_type"],
                size_bytes=image["size_bytes"],
                sha256=image["sha256"],
                project_id=str(DEMO_PROJECT_ID),
                analysis_job_id=str(fixture["job_id"]),
                candidate_id=candidate["candidate_id"],
                artifact_ref=image["artifact_ref"],
            )

    def resolve_label_candidate(
        project_id,
        analysis_job_id,
        candidate_id,
    ):
        metadata = label_candidates[
            (str(project_id), str(analysis_job_id), candidate_id)
        ]
        return {
            "status": "completed",
            "project_id": str(project_id),
            "analysis_job_id": str(analysis_job_id),
            "candidate_id": candidate_id,
            "source_sha256": metadata["source_sha256"],
            "candidate_metadata_sha256": _sha(
                canonical_json_bytes(
                    {
                        key: value
                        for key, value in metadata[
                            "metadata"
                        ].items()
                        if key != "review_event_binding"
                    }
                )
            ),
            "metadata": metadata["metadata"],
        }

    labels = CandidateLabelStore(
        bridge_root,
        resolve_label_candidate,
        audit_provenance=True,
    )
    for fixture in training_fixtures:
        for candidate in fixture["payload"]["result"]["candidates"]:
            labels.record_answer(
                DEMO_PROJECT_ID,
                fixture["job_id"],
                candidate["candidate_id"],
                answer="yes",
                selected_issue_type="pothole",
                severity="moderate",
                maintenance_action="포트홀 보수",
                normalized_box=BOX,
                answered_by="local_user",
                training_geometry=_training_geometry(candidate),
            )
    records = labels.audit_records(DEMO_PROJECT_ID)
    assert len(records) == 130
    projected = catalog.project_label_records(
        DEMO_PROJECT_ID,
        records,
    )
    assert len(projected) == 130
    assert len(catalog.list_current_annotations(DEMO_PROJECT_ID)) == 130

    reference = _approved_reference(DEMO_PROJECT_ID)
    dataset = catalog.create_dataset_version(
        DEMO_PROJECT_ID,
        approved_reference_records=[reference],
    )
    annotations = catalog.list_dataset_annotations(
        DEMO_PROJECT_ID,
        UUID(dataset["dataset_version_id"]),
    )
    assert dataset["training_member_count"] == 130
    assert dataset["train_count"] == 100
    assert dataset["validation_count"] == 30
    assert dataset["approved_reference_member_count"] == 1
    assert (
        dataset["approved_references_count_toward_training_gate"]
        is False
    )
    assert all(row["training_geometry"] for row in annotations)

    pack_manifest, _pack = _write_pack(tmp_path / "model-pack")
    fake_cuda = FakeCudaTrainingBackend()

    def resolve_training_artifact(
        project_id,
        analysis_job_id,
        candidate_id,
        artifact_ref,
    ):
        return artifacts[
            (
                str(project_id),
                str(analysis_job_id),
                candidate_id,
                artifact_ref,
            )
        ]

    trainer = LocalCudaObjectDetectorTrainer(
        model_pack_manifest=pack_manifest,
        artifact_resolver=resolve_training_artifact,
        backend=fake_cuda,
    )
    # The fake reports CUDA-shaped provenance so lifecycle integration can be
    # exercised, but the persisted trainer identity must remain test-only.
    trainer.test_only = True
    lifecycle = ProjectModelLifecycleStore(
        tmp_path / "model-lifecycle",
        trainer=trainer,
        allow_test_trainer=True,
    )
    training = lifecycle.start_training(
        DEMO_PROJECT_ID,
        dataset_version=dataset,
        current_annotations=annotations,
        auxiliary_references=dataset["approved_reference_members"],
        base_model_version="common-initial@1.0.0",
        required_issue_types=(),
        idempotency_key="closed-loop-fake-cuda-training-0001",
        user_confirmed=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        training = lifecycle.get_training_job(
            DEMO_PROJECT_ID,
            UUID(training["training_job_id"]),
        )
        if training["status"] not in {"queued", "running"}:
            break
        time.sleep(0.01)
    assert training["status"] == "succeeded"
    assert training["request"]["training_gate_member_count"] == 100
    assert training["request"]["validation_sample_counts"] == {
        "pothole": 30
    }
    assert (
        training["request"]["approved_reference_training_member_count"]
        == 1
    )
    assert training["request"]["trainer"]["test_only"] is True
    assert len(fake_cuda.requests) == 1
    assert len(fake_cuda.requests[0].train_samples) == 100
    assert len(fake_cuda.requests[0].validation_samples) == 30

    model = lifecycle.list_model_versions(
        DEMO_PROJECT_ID
    )["models"][0]
    assert model["model_format"] == LOCAL_OBJECT_DETECTOR_MODEL_FORMAT
    assert model["state"] == "candidate"
    assert model["validation_decision"] == "eligible"
    promoted = lifecycle.promote(
        DEMO_PROJECT_ID,
        UUID(model["model_version_id"]),
        expected_revision=1,
        idempotency_key="closed-loop-explicit-promote-0001",
        user_confirmed=True,
    )
    assert promoted["automatic_transition"] is False
    assert (
        promoted["after_state"]["active_model_id"]
        == model["model_version_id"]
    )

    survey_candidate = survey["payload"]["result"]["candidates"][0]
    image = survey_candidate["representative_image"]
    image_path = (
        Path(survey["artifact_root"]) / image["relative_path"]
    ).resolve()
    frame = DenseFrameEvidence(
        frame_id="f" * 24,
        frame_index=0,
        timestamp_seconds=0.5,
        file_name=image_path.name,
        size_bytes=image["size_bytes"],
        sha256=image["sha256"],
        media_type=image["media_type"],
        path=image_path,
    )
    fake_inference = FakeDetectorInferenceBackend()
    promoted_inference = PromotedLocalObjectDetectorInference(
        fake_inference
    )
    provider = ActiveProjectModelDenseProposalProvider(
        lifecycle.resolve_active_model,
        detector_inference=promoted_inference,
    )
    provider.bind_survey_context(
        DEMO_PROJECT_ID,
        survey["job_id"],
    )
    provider_state = provider.status(DEMO_PROJECT_ID)
    assert provider_state["state"] == "ready"
    assert provider_state["independent_survey_verified"] is True
    proposals = provider.propose(
        DEMO_PROJECT_ID,
        [frame],
        frames_root=image_path.parent,
        cancel=lambda: False,
    )
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.proposal_source == (
        "promoted_project_model_dense_2fps"
    )
    assert proposal.normalized_box == BOX
    assert proposal.provenance is not None
    assert (
        proposal.provenance["model_version_id"]
        == model["model_version_id"]
    )
    assert (
        proposal.provenance["survey_analysis_job_id"]
        == str(survey["job_id"])
    )
    assert str(survey["job_id"]) not in {
        *model["training_analysis_job_ids"],
        *model["fixed_validation_analysis_job_ids"],
    }

    app = create_app(
        TOKEN,
        fixture_mode=False,
        allow_test_client=True,
        bridge_data_root=str(bridge_root),
        source_candidate_probe=SourceCandidateProbe(None),
        project_model_lifecycle_store=lifecycle,
    )
    loaded = app.state.analysis_jobs._jobs[survey["job_id"]]
    assert loaded.result is not None
    loaded_candidate = loaded.result["candidates"][0]
    proposal_provenance = dict(proposal.provenance)
    loaded_candidate.update(
        {
            "issue_type": proposal.issue_type,
            "confidence": proposal.score,
            "normalized_box": dict(proposal.normalized_box),
            "proposal_source": proposal.proposal_source,
            "proposal_provenance_sha256": _sha(
                canonical_json_bytes(proposal_provenance)
            ),
            "representative_proposal_provenance": (
                proposal_provenance
            ),
            "review_only": True,
        }
    )
    candidate_id = loaded_candidate["candidate_id"]
    gate = {"closed": True}
    app.state.pipeline_jobs.is_analysis_projection_gated = (
        lambda project_id, analysis_job_id: (
            gate["closed"]
            and project_id == DEMO_PROJECT_ID
            and analysis_job_id == survey["job_id"]
        )
    )

    with TestClient(app) as client:
        survey_annotations = [
            row
            for row in app.state.store.list_current_annotations(
                DEMO_PROJECT_ID
            )
            if row["analysis_job_id"] == str(survey["job_id"])
        ]
        assert survey_annotations == []
        before_review = client.get(
            f"/projects/{DEMO_PROJECT_ID}/incidents",
            headers=AUTH,
            params={"analysis_job_id": str(survey["job_id"])},
        )
        assert before_review.status_code == 200, before_review.text
        assert before_review.json()["count"] == 0

        gate["closed"] = False
        approved = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/"
                f"{survey['job_id']}/candidates/{candidate_id}/answer"
            ),
            headers=AUTH,
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": str(survey["job_id"]),
                "answer": "yes",
                "selected_issue_type": "pothole",
                "severity": "moderate",
                "maintenance_action": "포트홀 보수",
                "normalized_box": BOX,
            },
        )
        assert approved.status_code == 201, approved.text
        assert approved.json()["answered_by"] == "local_user"

        # A local-user answer remains training Annotation until the durable
        # PipelineJob verifies the independent promoted-model survey and final
        # QA provenance. This test uses detector/lifecycle doubles, so emulate
        # only that already-verified derived-output projection boundary here.
        training_only = client.get(
            f"/projects/{DEMO_PROJECT_ID}/incidents",
            headers=AUTH,
            params={"analysis_job_id": str(survey["job_id"])},
        )
        assert training_only.status_code == 200, training_only.text
        assert training_only.json()["count"] == 0
        survey_audit = (
            client.app.state.candidate_labels.audit_records(
                DEMO_PROJECT_ID,
                analysis_job_id=survey["job_id"],
            )
        )
        client.app.state.store.project_label_records(
            DEMO_PROJECT_ID,
            survey_audit,
            materialize_analysis_job_ids=[survey["job_id"]],
            incident_analysis_job_ids=[survey["job_id"]],
        )

        after_review = client.get(
            f"/projects/{DEMO_PROJECT_ID}/incidents",
            headers=AUTH,
            params={"analysis_job_id": str(survey["job_id"])},
        )
        assert after_review.status_code == 200, after_review.text
        assert after_review.json()["count"] == 1
        incident = after_review.json()["incidents"][0]
        assert incident["analysis_job_id"] == str(survey["job_id"])
        assert incident["candidate_id"] == candidate_id
        assert incident["issue_type"] == "pothole"

        current = [
            row
            for row in app.state.store.list_current_annotations(
                DEMO_PROJECT_ID
            )
            if row["analysis_job_id"] == str(survey["job_id"])
        ]
        assert len(current) == 1
        assert current[0]["answered_by"] == "local_user"
        assert current[0]["user_confirmed"] is True
        assert current[0]["training_geometry"]["normalized_box"] == BOX
