from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.approved_reference_annotation_lineage import (
    ApprovedReferenceAnnotationLineageError,
    build_approved_reference_annotation_lineage,
    verify_annotation_assist_reference_lineage,
)


CANDIDATE_ID = "0123456789abcdefabcd"
SOURCE_REF = "reference:sample-3q:case-01"


def _proposal(*, source_refs=None):
    payload = {
        "schema_version": "trafficvisson.structured-vision-proposal.v1",
        "candidate_id": CANDIDATE_ID,
        "issue_type": "pothole",
        "maintenance_required": True,
        "action": "노면 보수",
        "recommended_severity": "moderate",
        "rationale": "승인된 3분기 사례와 현재 검지 근거가 유사합니다.",
        "ocr_verification": {
            "status": "not_applicable",
            "normalized_text": None,
            "observation_refs": [],
        },
        "uncertainty": {"score": 0.2, "reasons": ["bounded"]},
        "source_refs": (
            list(source_refs)
            if source_refs is not None
            else [
                f"analysis:{CANDIDATE_ID}:dino-01",
                SOURCE_REF,
            ]
        ),
        "comparison_mode": "metadata_comparison_only",
        "review_state": "proposed",
        "review_only": True,
        "automatic_annotation_allowed": False,
        "automatic_incident_allowed": False,
        "automatic_training_membership_allowed": False,
        "automatic_report_truth_allowed": False,
    }
    return payload


def _binding(*, source_refs=None):
    proposal = _proposal(source_refs=source_refs)
    proposal_sha256 = hashlib.sha256(
        json.dumps(
            proposal,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    similar_case = {
        "source_id": "sample-3q",
        "case_id": "case-01",
        "quarter": "3Q",
        "reference_state": "approved_reference",
        "approved_reference": True,
        "review_required": False,
        "training_eligible": True,
        "approval_record_sha256": "1" * 64,
        "document_sha256": "2" * 64,
        "photo_sha256": "3" * 64,
    }
    event = SimpleNamespace(
        representative_candidate_id=CANDIDATE_ID,
        event_id="rve_" + "4" * 32,
        event_revision="5" * 64,
        similar_cases=(similar_case,),
        checkpoint_revisions={
            "visual_reference_revision": "6" * 64,
        },
        structured_proposal={
            "status": "proposed",
            "provider": "codex",
            "proposal_sha256": proposal_sha256,
            "proposal": proposal,
            "execution_provenance": {
                "provider": "codex",
                "model": "test-vision-model",
                "input_sha256": "7" * 64,
            },
        },
    )
    event_set = SimpleNamespace(
        review_event_set_id="res_" + "8" * 32,
        review_event_set_revision="9" * 64,
        manifest_sha256="a" * 64,
    )
    return event_set, event


def test_approved_sample_reference_is_bound_to_annotation_lineage():
    event_set, event = _binding()

    proof = build_approved_reference_annotation_lineage(
        candidate_id=CANDIDATE_ID,
        review_event_set=event_set,
        review_event=event,
    )

    assert proof["visual_reference_revision"] == "6" * 64
    assert proof["structured_input_sha256"] == "7" * 64
    assert proof["cited_approved_references"] == [
        {
            "source_id": "sample-3q",
            "case_id": "case-01",
            "source_ref": SOURCE_REF,
            "quarter": "3Q",
            "approval_record_sha256": "1" * 64,
            "document_sha256": "2" * 64,
            "photo_sha256": "3" * 64,
        }
    ]
    assert len(proof["lineage_sha256"]) == 64


def test_uncited_or_weak_sample_case_cannot_authorize_llm_annotation():
    event_set, event = _binding(
        source_refs=[f"analysis:{CANDIDATE_ID}:dino-01"]
    )

    with pytest.raises(
        ApprovedReferenceAnnotationLineageError,
        match="did not cite an approved",
    ):
        build_approved_reference_annotation_lineage(
            candidate_id=CANDIDATE_ID,
            review_event_set=event_set,
            review_event=event,
        )

    event_set, event = _binding()
    weak = dict(event.similar_cases[0])
    weak.update(
        {
            "reference_state": "weak_unconfirmed",
            "approved_reference": False,
            "review_required": True,
            "training_eligible": False,
            "approval_record_sha256": None,
        }
    )
    event.similar_cases = (weak,)
    with pytest.raises(
        ApprovedReferenceAnnotationLineageError,
        match="did not cite an approved",
    ):
        build_approved_reference_annotation_lineage(
            candidate_id=CANDIDATE_ID,
            review_event_set=event_set,
            review_event=event,
        )


def test_annotation_assist_provenance_must_resolve_the_same_event():
    event_set, event = _binding()
    assist = {
        "candidate_id": CANDIDATE_ID,
        "review_event": {
            "review_event_set_id": event_set.review_event_set_id,
            "review_event_set_revision": (
                event_set.review_event_set_revision
            ),
            "review_event_set_manifest_sha256": (
                event_set.manifest_sha256
            ),
            "review_event_id": event.event_id,
            "review_event_revision": event.event_revision,
        },
        "provider": "codex",
        "model": "test-vision-model",
        "input_sha256": "7" * 64,
        "proposal_sha256": event.structured_proposal[
            "proposal_sha256"
        ],
    }

    proof = verify_annotation_assist_reference_lineage(
        assist_provenance=assist,
        review_event_set=event_set,
        review_event=event,
    )
    assert proof["cited_approved_references"][0][
        "approval_record_sha256"
    ] == "1" * 64

    tampered = deepcopy(assist)
    tampered["review_event"]["review_event_revision"] = "f" * 64
    with pytest.raises(
        ApprovedReferenceAnnotationLineageError,
        match="different review event",
    ):
        verify_annotation_assist_reference_lineage(
            assist_provenance=tampered,
            review_event_set=event_set,
            review_event=event,
        )
