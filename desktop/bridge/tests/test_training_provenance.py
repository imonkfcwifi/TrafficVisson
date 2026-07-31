from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from uuid import UUID, uuid4, uuid5

import pytest

from app.candidate_labels import CandidateLabelStore
from app.dataset_versions import (
    DATASET_SCHEMA_VERSION,
    DatasetVersionError,
    build_dataset_snapshot,
    canonical_json_bytes,
    normalize_label_record,
)
from app.training_provenance import (
    TrainingProvenanceError,
    build_annotation_provenance_graph,
    build_dataset_provenance_graph,
    canonical_annotation_provenance_graph,
)
from app.training_geometry import (
    SCHEMA_VERSION as TRAINING_GEOMETRY_SCHEMA,
    seal_training_geometry,
)
from app.project_model_lifecycle import (
    DeterministicTestTrainer,
    ProjectModelLifecycleStore,
)
from test_project_model_lifecycle import (
    _approved_reference,
    _dataset,
    _freeze_references,
)
from structured_assist_fixtures import assist_provenance


def _sha(seed: object) -> str:
    if isinstance(seed, str):
        payload = seed.encode("utf-8")
    else:
        payload = canonical_json_bytes(seed)
    return hashlib.sha256(payload).hexdigest()


def _candidate_metadata(
    *,
    pipeline_job_id: UUID,
    candidate_id: str,
    set_seed: str,
) -> dict:
    return {
        "candidate_id": candidate_id,
        "candidate_kind": "dense_candidate_evidence_overlay",
        "proposal_provenance_sha256": _sha(
            f"{set_seed}:proposal"
        ),
        "representative_image": {
            "artifact_ref": (
                f"candidate-evidence:{candidate_id}:image"
            ),
            "sha256": _sha(f"{set_seed}:image"),
        },
        "evidence_overlay": {
            "schema_version": (
                "trafficvisson.candidate-evidence-overlay.v1"
            ),
            "overlay_id": f"ceo_{_sha(f'{set_seed}:overlay')[:32]}",
            "pipeline_job_id": str(pipeline_job_id),
            "manifest_sha256": _sha(f"{set_seed}:overlay-manifest"),
            "candidate_set_metadata_sha256": _sha(
                f"{set_seed}:candidate-set"
            ),
            "review_only": True,
        },
        "review_event_binding": {
            "schema_version": (
                "trafficvisson.review-event-binding.v1"
            ),
            "review_event_set_id": (
                f"res_{_sha(f'{set_seed}:review-set')[:32]}"
            ),
            "review_event_set_revision": _sha(
                f"{set_seed}:review-set-revision"
            ),
            "review_event_set_manifest_sha256": _sha(
                f"{set_seed}:review-set-manifest"
            ),
            "review_event_id": (
                f"rve_{_sha(f'{set_seed}:review-event')[:32]}"
            ),
            "review_event_revision": _sha(
                f"{set_seed}:review-event-revision"
            ),
            "representative_candidate_id": candidate_id,
            "review_only": True,
        },
    }


def _candidate_digest(metadata: dict) -> str:
    base = {
        key: value
        for key, value in metadata.items()
        if key != "review_event_binding"
    }
    return _sha(base)


def _annotation_graph(
    *,
    project_id: UUID,
    analysis_job_id: UUID,
    pipeline_job_id: UUID,
    seed: str,
    source_sha256: str | None = None,
    review_set_id: str | None = None,
    review_set_revision: str | None = None,
) -> tuple[dict, dict]:
    candidate_id = _sha(f"{seed}:candidate")[:20]
    question_id = str(uuid5(analysis_job_id, candidate_id))
    answer_record_id = str(
        uuid5(project_id, f"{seed}:answer-record")
    )
    metadata = _candidate_metadata(
        pipeline_job_id=pipeline_job_id,
        candidate_id=candidate_id,
        set_seed=seed,
    )
    if review_set_id is not None:
        metadata["review_event_binding"][
            "review_event_set_id"
        ] = review_set_id
    if review_set_revision is not None:
        metadata["review_event_binding"][
            "review_event_set_revision"
        ] = review_set_revision
    graph = build_annotation_provenance_graph(
        project_id=str(project_id),
        analysis_job_id=str(analysis_job_id),
        question_id=question_id,
        candidate_id=candidate_id,
        answer_record_id=answer_record_id,
        answer_revision=1,
        include_in_training=True,
        answered_by="local_user",
        candidate_metadata=metadata,
        source_sha256=(
            source_sha256 or _sha(f"{seed}:source")
        ),
        candidate_metadata_sha256=_candidate_digest(metadata),
        training_geometry=None,
        assist_provenance=None,
    )
    assert graph is not None
    member = {
        "annotation_id": answer_record_id,
        "answer_record_id": answer_record_id,
        "answer_revision": 1,
        "analysis_job_id": str(analysis_job_id),
        "candidate_id": candidate_id,
        "question_id": question_id,
        "provenance_graph": graph,
        "split": "train",
    }
    return graph, member


def test_annotation_graph_is_self_digested_and_project_scoped():
    project_id = uuid4()
    graph, _member = _annotation_graph(
        project_id=project_id,
        analysis_job_id=uuid4(),
        pipeline_job_id=uuid4(),
        seed="canonical",
    )

    assert canonical_annotation_provenance_graph(graph) == graph
    assert graph["project_id"] == str(project_id)
    assert graph["answer_authority"] == {
        "answered_by": "local_user",
        "include_in_training": True,
        "user_confirmed": True,
    }

    tampered = deepcopy(graph)
    tampered["review_event_set_revision"] = _sha("stale")
    with pytest.raises(
        TrainingProvenanceError,
        match="digest does not match",
    ):
        canonical_annotation_provenance_graph(tampered)


def test_annotation_graph_freezes_image_mask_and_proposal_lineage():
    project_id = uuid4()
    analysis_job_id = uuid4()
    pipeline_job_id = uuid4()
    candidate_id = _sha("geometry-candidate")[:20]
    metadata = _candidate_metadata(
        pipeline_job_id=pipeline_job_id,
        candidate_id=candidate_id,
        set_seed="geometry",
    )
    geometry = seal_training_geometry(
        {
            "schema_version": TRAINING_GEOMETRY_SCHEMA,
            "source_kind": "user_sam2_refinement",
            "approval_authority": "local_user",
            "image_sha256": metadata[
                "representative_image"
            ]["sha256"],
            "image_artifact_ref": metadata[
                "representative_image"
            ]["artifact_ref"],
            "normalized_box": {
                "x": 0.1,
                "y": 0.2,
                "width": 0.3,
                "height": 0.2,
            },
            "polygon": [
                [0.1, 0.2],
                [0.4, 0.2],
                [0.4, 0.4],
            ],
            "mask_artifact_ref": (
                f"sam2:{candidate_id}:mask"
            ),
            "mask_sha256": _sha("sam2-output"),
            "tracked_frame_refs": [],
            "proposal_sha256": metadata[
                "proposal_provenance_sha256"
            ],
            "sam2_output_sha256": _sha("sam2-output"),
            "sam2_model_manifest_sha256": _sha(
                "sam2-model"
            ),
        }
    )
    graph = build_annotation_provenance_graph(
        project_id=str(project_id),
        analysis_job_id=str(analysis_job_id),
        question_id=str(
            uuid5(analysis_job_id, candidate_id)
        ),
        candidate_id=candidate_id,
        answer_record_id=str(uuid4()),
        answer_revision=1,
        include_in_training=True,
        answered_by="local_user",
        candidate_metadata=metadata,
        source_sha256=_sha("geometry-source"),
        candidate_metadata_sha256=_candidate_digest(metadata),
        training_geometry=geometry,
        assist_provenance=None,
    )

    assert graph is not None
    assert graph["image_sha256"] == geometry["image_sha256"]
    assert graph["mask_sha256"] == geometry["mask_sha256"]
    assert graph["proposal_sha256"] == geometry[
        "proposal_sha256"
    ]
    assert graph["geometry_sha256"] == geometry[
        "geometry_sha256"
    ]


def test_annotation_graph_freezes_delegation_authority():
    project_id = uuid4()
    analysis_job_id = uuid4()
    candidate_id = _sha("delegated-candidate")[:20]
    assist = assist_provenance(
        project_id,
        analysis_job_id,
        candidate_id,
        authority_kind="per_item_user_grant",
    )
    review = assist["review_event"]
    metadata = _candidate_metadata(
        pipeline_job_id=UUID(review["pipeline_job_id"]),
        candidate_id=candidate_id,
        set_seed="delegated",
    )
    metadata["evidence_overlay"].update(
        {
            "overlay_id": review["overlay_id"],
            "pipeline_job_id": review["pipeline_job_id"],
        }
    )
    metadata["review_event_binding"].update(
        {
            "review_event_set_id": review[
                "review_event_set_id"
            ],
            "review_event_set_revision": review[
                "review_event_set_revision"
            ],
            "review_event_set_manifest_sha256": review[
                "review_event_set_manifest_sha256"
            ],
            "review_event_id": review["review_event_id"],
            "review_event_revision": review[
                "review_event_revision"
            ],
        }
    )
    graph = build_annotation_provenance_graph(
        project_id=str(project_id),
        analysis_job_id=str(analysis_job_id),
        question_id=str(
            uuid5(analysis_job_id, candidate_id)
        ),
        candidate_id=candidate_id,
        answer_record_id=str(uuid4()),
        answer_revision=1,
        include_in_training=True,
        answered_by="structured_llm_assisted",
        candidate_metadata=metadata,
        source_sha256=review[
            "review_event_set_source_sha256"
        ],
        candidate_metadata_sha256=_candidate_digest(metadata),
        training_geometry=None,
        assist_provenance=assist,
    )

    assert graph is not None
    assert graph["answer_authority"]["user_confirmed"] is False
    assert graph["delegation_authority"][
        "authority_kind"
    ] == "per_item_user_grant"
    assert graph["delegation_authority"][
        "assist_provenance_sha256"
    ] == assist["provenance_sha256"]


def test_dataset_graph_rejects_cross_project_and_cross_job():
    project_id = uuid4()
    other_project_id = uuid4()
    pipeline_job_id = uuid4()
    _first_graph, first = _annotation_graph(
        project_id=project_id,
        analysis_job_id=uuid4(),
        pipeline_job_id=pipeline_job_id,
        seed="first",
    )
    _second_graph, second = _annotation_graph(
        project_id=project_id,
        analysis_job_id=uuid4(),
        pipeline_job_id=pipeline_job_id,
        seed="second",
    )

    with pytest.raises(
        TrainingProvenanceError,
        match="crossed a dataset member boundary",
    ):
        build_dataset_provenance_graph(
            project_id=str(other_project_id),
            members=[first],
        )
    with pytest.raises(
        TrainingProvenanceError,
        match="pipeline job crossed analysis jobs",
    ):
        build_dataset_provenance_graph(
            project_id=str(project_id),
            members=[first, second],
        )


def test_dataset_graph_rejects_stale_review_set_revision():
    project_id = uuid4()
    review_set_id = f"res_{_sha('shared-set')[:32]}"
    _first_graph, first = _annotation_graph(
        project_id=project_id,
        analysis_job_id=uuid4(),
        pipeline_job_id=uuid4(),
        seed="revision-a",
        review_set_id=review_set_id,
        review_set_revision=_sha("revision-a"),
    )
    _second_graph, second = _annotation_graph(
        project_id=project_id,
        analysis_job_id=uuid4(),
        pipeline_job_id=uuid4(),
        seed="revision-b",
        review_set_id=review_set_id,
        review_set_revision=_sha("revision-b"),
    )

    with pytest.raises(
        TrainingProvenanceError,
        match="review event set revision is stale",
    ):
        build_dataset_provenance_graph(
            project_id=str(project_id),
            members=[first, second],
        )


def test_dataset_graph_rejects_train_validation_source_overlap():
    project_id = uuid4()
    shared_source = _sha("same-video")
    _train_graph, train = _annotation_graph(
        project_id=project_id,
        analysis_job_id=uuid4(),
        pipeline_job_id=uuid4(),
        seed="train",
        source_sha256=shared_source,
    )
    _validation_graph, validation = _annotation_graph(
        project_id=project_id,
        analysis_job_id=uuid4(),
        pipeline_job_id=uuid4(),
        seed="validation",
        source_sha256=shared_source,
    )
    validation["split"] = "validation"

    with pytest.raises(
        TrainingProvenanceError,
        match="fixed-validation provenance overlap",
    ):
        build_dataset_provenance_graph(
            project_id=str(project_id),
            members=[train, validation],
        )


def test_audited_answer_without_pipeline_binding_is_not_trainable(
    tmp_path,
):
    project_id = uuid4()
    analysis_job_id = uuid4()
    candidate_id = _sha("fallback-candidate")[:20]

    def resolver(project, analysis, candidate):
        return {
            "status": "completed",
            "project_id": str(project),
            "analysis_job_id": str(analysis),
            "candidate_id": candidate,
            "metadata": {"candidate_id": candidate},
        }

    record = CandidateLabelStore(
        tmp_path,
        resolver,
        audit_provenance=True,
    ).record_answer(
        project_id,
        analysis_job_id,
        candidate_id,
        answer="no",
    )
    projection = normalize_label_record(record)
    annotation = json.loads(projection.annotation_json)
    row = {
        **annotation,
        "annotation_id": projection.annotation_id,
        "annotation_hash": projection.annotation_hash,
    }

    assert record["schema_version"] == "1.3"
    assert record["provenance_graph"] is None
    assert projection.training_authorized is False
    with pytest.raises(
        DatasetVersionError,
        match="not authorized for training",
    ):
        build_dataset_snapshot(str(project_id), [row])


def test_audited_answer_freezes_graph_into_dataset_manifest(tmp_path):
    project_id = uuid4()
    analysis_job_id = uuid4()
    pipeline_job_id = uuid4()
    candidate_id = _sha("reviewed-candidate")[:20]
    metadata = _candidate_metadata(
        pipeline_job_id=pipeline_job_id,
        candidate_id=candidate_id,
        set_seed="reviewed",
    )
    source_sha256 = _sha("reviewed-source")

    def resolver(project, analysis, candidate):
        assert project == project_id
        assert analysis == analysis_job_id
        assert candidate == candidate_id
        return {
            "status": "completed",
            "project_id": str(project),
            "analysis_job_id": str(analysis),
            "candidate_id": candidate,
            "source_sha256": source_sha256,
            "candidate_metadata_sha256": _candidate_digest(
                metadata
            ),
            "metadata": metadata,
        }

    record = CandidateLabelStore(
        tmp_path,
        resolver,
        audit_provenance=True,
    ).record_answer(
        project_id,
        analysis_job_id,
        candidate_id,
        answer="no",
    )
    projection = normalize_label_record(record)
    annotation = json.loads(projection.annotation_json)
    row = {
        **annotation,
        "annotation_id": projection.annotation_id,
        "annotation_hash": projection.annotation_hash,
    }
    snapshot = build_dataset_snapshot(str(project_id), [row])
    manifest = json.loads(snapshot.manifest_json)

    assert projection.training_authorized is True
    assert (
        manifest["provenance_graph"][
            "members"
        ][0]["provenance_graph_sha256"]
        == record["provenance_graph"][
            "provenance_graph_sha256"
        ]
    )


def test_training_job_and_model_version_preserve_dataset_graph(
    tmp_path,
):
    project_id = uuid4()
    dataset, annotations = _dataset(project_id)
    graph_members = []
    assert len(annotations) == len(dataset["memberships"])
    for annotation, member in zip(
        annotations, dataset["memberships"]
    ):
        analysis_job_id = UUID(annotation["analysis_job_id"])
        pipeline_job_id = uuid5(
            project_id,
            f"pipeline:{analysis_job_id}",
        )
        seed = f"lifecycle:{annotation['candidate_id']}"
        metadata = _candidate_metadata(
            pipeline_job_id=pipeline_job_id,
            candidate_id=annotation["candidate_id"],
            set_seed=seed,
        )
        graph = build_annotation_provenance_graph(
            project_id=str(project_id),
            analysis_job_id=str(analysis_job_id),
            question_id=annotation["question_id"],
            candidate_id=annotation["candidate_id"],
            answer_record_id=annotation["answer_record_id"],
            answer_revision=annotation["answer_revision"],
            include_in_training=True,
            answered_by="local_user",
            candidate_metadata=metadata,
            source_sha256=_sha(
                f"source:{analysis_job_id}"
            ),
            candidate_metadata_sha256=_candidate_digest(
                metadata
            ),
            training_geometry=None,
            assist_provenance=None,
        )
        assert graph is not None
        annotation["provenance_graph"] = graph
        graph_members.append(
            {
                **member,
                "provenance_graph": graph,
            }
        )
    dataset_graph = build_dataset_provenance_graph(
        project_id=str(project_id),
        members=graph_members,
    )

    frozen_dataset, frozen_references = _freeze_references(
        project_id,
        dataset,
        [_approved_reference(project_id)],
    )
    fingerprint = {
        "approved_reference_members": frozen_dataset[
            "approved_reference_members"
        ],
        "members": frozen_dataset["memberships"],
        "project_id": str(project_id),
        "provenance_graph": dataset_graph,
        "schema_version": DATASET_SCHEMA_VERSION,
        "split_strategy": frozen_dataset["manifest"][
            "split_strategy"
        ],
        "validation_fraction": frozen_dataset["manifest"][
            "validation_fraction"
        ],
    }
    dataset_hash = _sha(fingerprint)
    dataset_id = str(
        uuid5(
            project_id,
            (
                f"{DATASET_SCHEMA_VERSION}:"
                f"{dataset_hash}"
            ),
        )
    )
    frozen_dataset.update(
        {
            "dataset_hash": dataset_hash,
            "dataset_version_id": dataset_id,
            "manifest": {
                **frozen_dataset["manifest"],
                **fingerprint,
                "dataset_hash": dataset_hash,
                "dataset_id": dataset_id,
            },
        }
    )

    store = ProjectModelLifecycleStore(
        tmp_path,
        trainer=DeterministicTestTrainer(),
        allow_test_trainer=True,
    )
    started = store.start_training(
        project_id,
        dataset_version=frozen_dataset,
        current_annotations=annotations,
        auxiliary_references=frozen_references,
        base_model_version="common-initial@1.0.0",
        required_issue_types=["pothole"],
        idempotency_key="provenance-training-0001",
        user_confirmed=True,
    )
    deadline = time.monotonic() + 10
    job = started
    while (
        job["status"] in {"queued", "running"}
        and time.monotonic() < deadline
    ):
        job = store.get_training_job(
            project_id,
            UUID(started["training_job_id"]),
        )
        time.sleep(0.01)

    assert job["status"] == "succeeded"
    assert job["request"][
        "dataset_provenance_graph_sha256"
    ] == dataset_graph["provenance_graph_sha256"]
    model = store.list_model_versions(project_id)["models"][0]
    assert model["dataset_provenance_graph"] == dataset_graph
    assert model["dataset_provenance_graph_sha256"] == (
        dataset_graph["provenance_graph_sha256"]
    )
    artifact_root = (
        tmp_path
        / "project-model-lifecycle"
        / model["artifact_directory"]
    )
    artifact_manifest = json.loads(
        (artifact_root / "artifact-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert artifact_manifest["dataset_provenance_graph"] == (
        dataset_graph
    )
    state = store.get_project_state(project_id)
    assert state is not None
    store.promote(
        project_id,
        UUID(model["model_version_id"]),
        expected_revision=state["revision"],
        idempotency_key="provenance-promote-0001",
        user_confirmed=True,
    )
    active = store.resolve_active_model(project_id)
    assert active.dataset_provenance_graph_sha256 == (
        dataset_graph["provenance_graph_sha256"]
    )
