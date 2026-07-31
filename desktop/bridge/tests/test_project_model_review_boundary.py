from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.dense_candidate_discovery import (
    ActiveProjectModelDenseProposalProvider,
    DenseFrameEvidence,
)
from app.project_model_lifecycle import (
    DeterministicTestTrainer,
    ProjectModelLifecycleStore,
)
from app.store import DEMO_PROJECT_ID
from test_project_catalog_v2 import _write_completed_analysis
from test_project_model_lifecycle_api import (
    AUTH,
    _SecondVideoProjectInference,
    _approved_reference,
    _configured_app,
    _dataset,
    _start_payload,
    _wait_training_job_api,
)


def test_promoted_model_hint_without_full_survey_qa_stays_annotation_only(
    tmp_path,
):
    """A promoted-model hint remains audit-only until review is opened.

    The deterministic trainer and tiny image are test doubles; the test does
    not claim a CUDA training run or a real road finding.  It pins the
    production API/state contract that a separately identified survey analysis
    receives promoted-model provenance. Annotation still requires the ordinary
    local-user review boundary, while Incident additionally requires the full
    independent PipelineJob survey and missed-scene QA authority.
    """

    bridge_root = (tmp_path / "bridge").resolve()
    survey = _write_completed_analysis(
        bridge_root,
        project_id=DEMO_PROJECT_ID,
        source_seed="independent-promoted-model-survey",
    )
    dataset, annotations = _dataset(DEMO_PROJECT_ID)
    reference = _approved_reference(DEMO_PROJECT_ID)
    lifecycle = ProjectModelLifecycleStore(
        tmp_path / "models",
        trainer=DeterministicTestTrainer(),
        allow_test_trainer=True,
    )
    app = _configured_app(
        tmp_path,
        lifecycle,
        dataset=dataset,
        annotations=annotations,
        reference=reference,
    )

    with TestClient(app) as client:
        started = client.post(
            f"/projects/{DEMO_PROJECT_ID}/model-training-jobs",
            headers=AUTH,
            json=_start_payload(
                dataset,
                key="review-boundary-training-0001",
            ),
        )
        assert started.status_code == 202, started.text
        queued = started.json()
        assert queued["status"] == "queued"
        assert _wait_training_job_api(
            client,
            queued["training_job_id"],
        )["status"] == "succeeded"

        model = client.get(
            f"/projects/{DEMO_PROJECT_ID}/model-versions",
            headers=AUTH,
        ).json()["models"][0]
        promoted = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/model-versions/"
                f"{model['model_version_id']}/promote"
            ),
            headers=AUTH,
            json={
                "expected_revision": 1,
                "idempotency_key": "review-boundary-promote-0001",
                "user_confirmed": True,
            },
        )
        assert promoted.status_code == 200, promoted.text

        candidate = survey["payload"]["result"]["candidates"][0]
        image = candidate["representative_image"]
        image_path = (
            Path(survey["artifact_root"]) / image["relative_path"]
        )
        frame = DenseFrameEvidence(
            frame_id="a" * 24,
            frame_index=0,
            timestamp_seconds=0.5,
            file_name=image_path.name,
            size_bytes=image["size_bytes"],
            sha256=image["sha256"],
            media_type=image["media_type"],
            path=image_path,
        )
        inference = _SecondVideoProjectInference()
        provider = ActiveProjectModelDenseProposalProvider(
            lifecycle.resolve_active_model,
            inference,
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
        assert proposal.provenance is not None
        assert (
            proposal.provenance["survey_analysis_job_id"]
            == str(survey["job_id"])
        )
        assert (
            proposal.provenance["model_version_id"]
            == model["model_version_id"]
        )

        # The completed analysis is already loaded by create_app.  Enrich its
        # immutable test-double candidate with the exact promoted-model
        # proposal provenance before the renderer resolves the question.
        loaded = app.state.analysis_jobs._jobs[survey["job_id"]]
        assert loaded.result is not None
        loaded_candidate = loaded.result["candidates"][0]
        loaded_candidate.update(
            {
                "issue_type": proposal.issue_type,
                "confidence": proposal.score,
                "proposal_source": proposal.proposal_source,
                "representative_proposal_provenance": dict(
                    proposal.provenance
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
        answer = client.post(
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
            },
        )
        assert answer.status_code == 201, answer.text
        audit = app.state.candidate_labels.audit_records(
            DEMO_PROJECT_ID,
            analysis_job_id=survey["job_id"],
        )
        assert len(audit) == 1
        assert audit[0]["ai_original_candidate"][
            "representative_proposal_provenance"
        ]["model_version_id"] == model["model_version_id"]
        assert app.state.store.list_current_annotations(
            DEMO_PROJECT_ID
        ) == []
        assert client.get(
            f"/projects/{DEMO_PROJECT_ID}/incidents",
            headers=AUTH,
        ).json()["count"] == 0

        # Restore the persisted fixture candidate's public response shape. Its
        # promoted-model evidence was already copied into the immutable answer
        # record above.
        for transient_key in (
            "issue_type",
            "confidence",
            "proposal_source",
            "representative_proposal_provenance",
            "review_only",
        ):
            loaded_candidate.pop(transient_key, None)

        # Re-entering or refreshing the review screen must not turn an
        # answer-only CandidateProposal into training/report truth while the
        # owning PipelineJob is still awaiting its explicit review-complete
        # boundary.
        refreshed_questions = client.get(
            (
                f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/"
                f"{survey['job_id']}/label-questions"
            ),
            headers=AUTH,
        )
        assert refreshed_questions.status_code == 200, (
            refreshed_questions.text
        )
        assert app.state.store.list_current_annotations(
            DEMO_PROJECT_ID
        ) == []
        assert app.state.store.list_incidents(DEMO_PROJECT_ID) == []

        gate["closed"] = False
        incidents = client.get(
            f"/projects/{DEMO_PROJECT_ID}/incidents",
            headers=AUTH,
        )
        assert incidents.status_code == 200, incidents.text
        assert incidents.json()["count"] == 0
        current = app.state.store.list_current_annotations(
            DEMO_PROJECT_ID
        )
        assert len(current) == 1
        assert current[0]["analysis_job_id"] == str(survey["job_id"])
        assert current[0]["candidate_id"] == candidate_id
        assert current[0]["answered_by"] == "local_user"
        assert current[0]["user_confirmed"] is True
