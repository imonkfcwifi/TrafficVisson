from __future__ import annotations

import hashlib
import io
import json

from PIL import Image
import pytest

from app.provider_candidate_evidence_montage import (
    CandidateEvidenceMontageError,
    ROLE_ORDER,
    build_candidate_evidence_montage,
)


CANDIDATE_ID = "a" * 20


def _image(seed: int) -> bytes:
    output = io.BytesIO()
    Image.new(
        "RGB",
        (640, 360),
        (20 + seed * 35, 90, 150),
    ).save(output, format="PNG")
    return output.getvalue()


def _components():
    result = []
    for index, role in enumerate(ROLE_ORDER):
        body = _image(index)
        result.append(
            {
                "role": role,
                "source_ref": (
                    f"evidence:{CANDIDATE_ID}:{role}-{index}"
                ),
                "source_sha256": hashlib.sha256(body).hexdigest(),
                "media_type": "image/png",
                "size_bytes": len(body),
                "body": body,
            }
        )
    return result


def _box():
    return {"x": 0.25, "y": 0.2, "width": 0.4, "height": 0.5}


def test_four_evidence_roles_become_one_unreviewed_path_free_montage():
    first = build_candidate_evidence_montage(
        candidate_id=CANDIDATE_ID,
        components=_components(),
        target_box=_box(),
    )
    second = build_candidate_evidence_montage(
        candidate_id=CANDIDATE_ID,
        components=_components(),
        target_box=_box(),
    )

    assert first.body.startswith(b"\x89PNG\r\n\x1a\n")
    assert first.sha256 == hashlib.sha256(first.body).hexdigest()
    assert first.sha256 == second.sha256
    assert first.manifest == second.manifest
    assert first.manifest["layout"]["role_order"] == list(ROLE_ORDER)
    assert [
        item["role"] for item in first.manifest["components"]
    ] == list(ROLE_ORDER)
    assert first.manifest["privacy_state"] == "unreviewed"
    assert first.manifest["provider_transmission_allowed"] is False
    assert (
        first.manifest["requires_provider_visual_evidence_review"]
        is True
    )
    serialized = json.dumps(
        first.manifest, sort_keys=True, separators=(",", ":")
    )
    assert "path" not in serialized.casefold()
    assert "body" not in serialized


def test_source_tamper_and_cross_candidate_refs_fail_closed():
    components = _components()
    components[0]["body"] += b"tamper"
    with pytest.raises(CandidateEvidenceMontageError) as caught:
        build_candidate_evidence_montage(
            candidate_id=CANDIDATE_ID,
            components=components,
            target_box=_box(),
        )
    assert caught.value.code == "candidate_montage_component_invalid"

    components = _components()
    components[2]["source_ref"] = (
        f"evidence:{'b' * 20}:mask-overlay"
    )
    with pytest.raises(CandidateEvidenceMontageError) as caught:
        build_candidate_evidence_montage(
            candidate_id=CANDIDATE_ID,
            components=components,
            target_box=_box(),
        )
    assert caught.value.code == "candidate_montage_component_invalid"


@pytest.mark.parametrize(
    "mutator",
    (
        lambda rows: rows[:-1],
        lambda rows: [
            {**row, "role": "representative_frame"}
            if index == 0
            else row
            for index, row in enumerate(rows)
        ],
        lambda rows: [
            {**row, "source_ref": rows[0]["source_ref"]}
            if index == 1
            else row
            for index, row in enumerate(rows)
        ],
    ),
)
def test_missing_reordered_or_duplicate_evidence_fails_closed(mutator):
    with pytest.raises(CandidateEvidenceMontageError):
        build_candidate_evidence_montage(
            candidate_id=CANDIDATE_ID,
            components=mutator(_components()),
            target_box=_box(),
        )


def test_invalid_target_box_fails_before_output():
    with pytest.raises(CandidateEvidenceMontageError) as caught:
        build_candidate_evidence_montage(
            candidate_id=CANDIDATE_ID,
            components=_components(),
            target_box={
                "x": 0.9,
                "y": 0.2,
                "width": 0.4,
                "height": 0.5,
            },
        )
    assert caught.value.code == "candidate_montage_box_invalid"
