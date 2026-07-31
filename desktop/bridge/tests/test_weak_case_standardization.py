from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.contracts import WeakCaseStandardizationProposalResponse
from app.main import create_app
from app.report_generation import GenerationCommandResult
from app.store import DEMO_PROJECT_ID
from app.weak_case_catalog import WeakCaseCatalog, default_sample_root
from app.weak_case_standardization import (
    EVIDENCE_TRANSPORT_POLICY,
    OfficialCliWeakCaseStandardizationProvider,
    WeakCaseStandardizationError,
    WeakCaseStandardizationProposalStore,
    WeakCaseStandardizationProviderResult,
)


TOKEN = "s" * 64
SOURCE_ID = "sample-hwpx-4q-0123456789abcdef"
CASE_1 = "hwpx-weak-case-" + "1" * 32
CASE_2 = "hwpx-weak-case-" + "2" * 32
GROUP_ID = "sample-evidence-group-" + "a" * 24
DOCUMENT_SHA256 = "b" * 64
INDEX_SHA256 = "c" * 64
IMAGE_1 = "d" * 64
IMAGE_2 = "e" * 64


class FakeCatalog:
    maximum_stream_image_bytes = 1024

    def __init__(self, *, duplicate: bool = False) -> None:
        duplicate_refs = (
            [
                f"{SOURCE_ID}/{CASE_1}",
                "sample-hwpx-3q-fedcba9876543210/"
                + "hwpx-weak-case-"
                + "9" * 32,
            ]
            if duplicate
            else None
        )
        self.rows = [
            _row(
                CASE_1,
                1,
                IMAGE_1,
                duplicate_refs=duplicate_refs,
            ),
            _row(CASE_2, 2, IMAGE_2),
        ]
        self.attestations = {
            row["case_id"]: {
                **deepcopy(row),
                "sample_layout_binding": {
                    "sample_evidence_group_id": GROUP_ID,
                    "group_photo_index": row["group_photo_index"],
                    "group_photo_count": row["group_photo_count"],
                },
            }
            for row in self.rows
        }

    def case_attestation(
        self,
        source_id: str,
        case_id: str,
    ) -> dict[str, Any]:
        assert source_id == SOURCE_ID
        return deepcopy(self.attestations[case_id])

    def visual_index_snapshot(self) -> dict[str, Any]:
        return {"cases": deepcopy(self.rows)}


class FakeProvider:
    def __init__(
        self,
        proposal: Mapping[str, str] | None = None,
    ) -> None:
        self.proposal = proposal or {
            "issue_type": "roadside_mowing_required",
            "maintenance_action": "갓길 및 비탈면 삭초",
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def propose(
        self,
        provider: str,
        payload: Mapping[str, Any],
    ) -> WeakCaseStandardizationProviderResult:
        self.calls.append((provider, deepcopy(dict(payload))))
        return WeakCaseStandardizationProviderResult(
            proposal=self.proposal,
            model="test-model",
            usage={"input_tokens": 20, "output_tokens": 4},
        )


class RecordingRunner:
    def __init__(self, result: GenerationCommandResult) -> None:
        self.result = result
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def __call__(self, argv: Any, **kwargs: Any) -> GenerationCommandResult:
        self.calls.append((tuple(argv), kwargs))
        return self.result


def _ready_provider_status() -> dict[str, Any]:
    return {
        "state": "ready",
        "installed": True,
        "authentication_state": "ready",
        "network_state": "not_checked",
        "capabilities": (
            "connection_status",
            "structured_judgment",
        ),
    }


def _row(
    case_id: str,
    group_photo_index: int,
    image_sha256: str,
    *,
    duplicate_refs: list[str] | None = None,
) -> dict[str, Any]:
    refs = duplicate_refs or [f"{SOURCE_ID}/{case_id}"]
    refs = sorted(refs)
    return {
        "source_id": SOURCE_ID,
        "case_id": case_id,
        "quarter": 4,
        "document_sha256": DOCUMENT_SHA256,
        "index_manifest_sha256": INDEX_SHA256,
        "caption": "LCS 고장구간 다수 발견",
        "route": "테스트 노선",
        "opinion": "LCS 점검 및 보수 필요",
        "sample_evidence_group_id": GROUP_ID,
        "group_photo_index": group_photo_index,
        "group_photo_count": 2,
        "photo_slot_id": (
            "sample-photo-slot-"
            + str(group_photo_index) * 24
        ),
        "picture_target_id": (
            "hwpx-picture-" + str(group_photo_index) * 24
        ),
        "image_sha256": image_sha256,
        "sample_layout_binding_sha256": (
            str(group_photo_index + 3) * 64
        ),
        "semantic_attestation_sha256": (
            str(group_photo_index + 5) * 64
        ),
        "duplicate_image_occurrence_count": len(refs),
        "duplicate_image_review_required": len(refs) > 1,
        "duplicate_image_case_refs": refs,
    }


def _request(case_id: str = CASE_1, image_sha256: str = IMAGE_1) -> dict:
    return {
        "project_id": str(DEMO_PROJECT_ID),
        "source_id": SOURCE_ID,
        "case_id": case_id,
        "provider": "codex",
        "expected_document_sha256": DOCUMENT_SHA256,
        "expected_index_manifest_sha256": INDEX_SHA256,
        "expected_image_sha256": image_sha256,
        "metadata_transmission_confirmed": True,
    }


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_multi_photo_event_gets_one_review_only_proposal(
    tmp_path: Path,
) -> None:
    catalog = FakeCatalog()
    provider = FakeProvider()
    store = WeakCaseStandardizationProposalStore(
        tmp_path.resolve(),
        catalog,
        provider,
    )

    first = store.propose(DEMO_PROJECT_ID, _request())
    parsed = WeakCaseStandardizationProposalResponse(**first)
    second = store.propose(
        DEMO_PROJECT_ID,
        _request(CASE_2, IMAGE_2),
    )
    listed = store.list_project(DEMO_PROJECT_ID)

    assert parsed.status == "proposed"
    assert parsed.reference_state == "weak_unconfirmed"
    assert parsed.approved_reference is False
    assert parsed.training_eligible is False
    assert parsed.annotation_count == 0
    assert parsed.incident_count == 0
    assert parsed.report_fact_count == 0
    assert parsed.user_confirmed is False
    assert parsed.source_snapshot.caption == "LCS 고장구간 다수 발견"
    assert parsed.source_snapshot.opinion == "LCS 점검 및 보수 필요"
    assert parsed.proposal.model_dump() == {
        "issue_type": "roadside_mowing_required",
        "maintenance_action": "갓길 및 비탈면 삭초",
    }
    assert parsed.sample_evidence_group_id == GROUP_ID
    assert len(parsed.photo_provenance) == 2
    assert [photo.group_photo_index for photo in parsed.photo_provenance] == [
        1,
        2,
    ]
    assert {photo.image_sha256 for photo in parsed.photo_provenance} == {
        IMAGE_1,
        IMAGE_2,
    }
    assert second["proposal_id"] == first["proposal_id"]
    assert second["idempotent_replay"] is True
    assert len(provider.calls) == 1
    assert listed["proposal_count"] == 1
    assert listed["evidence_event_count"] == 1
    assert listed["training_eligible_count"] == 0


def test_proposals_are_isolated_per_project(tmp_path: Path) -> None:
    provider = FakeProvider()
    store = WeakCaseStandardizationProposalStore(
        tmp_path.resolve(),
        FakeCatalog(),
        provider,
    )
    another_project = uuid4()

    first = store.propose(DEMO_PROJECT_ID, _request())
    second = store.propose(another_project, _request())

    assert first["proposal_id"] != second["proposal_id"]
    assert first["project_id"] == str(DEMO_PROJECT_ID)
    assert second["project_id"] == str(another_project)
    assert store.list_project(DEMO_PROJECT_ID)["proposal_count"] == 1
    assert store.list_project(another_project)["proposal_count"] == 1
    assert len(provider.calls) == 2


def test_provider_receives_only_text_structure_and_hash_provenance(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    store = WeakCaseStandardizationProposalStore(
        tmp_path.resolve(),
        FakeCatalog(duplicate=True),
        provider,
    )

    result = store.propose(DEMO_PROJECT_ID, _request())
    _, payload = provider.calls[0]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    assert payload["evidence_transport_policy"] == (
        EVIDENCE_TRANSPORT_POLICY
    )
    assert "image_bytes" not in encoded
    assert "image_path" not in encoded
    assert "document_name" not in encoded
    assert "C:\\\\" not in encoded
    assert "\\\\\\\\" not in encoded
    assert payload["evidence_transport_policy"][
        "local_paths_sent"
    ] is False
    assert payload["evidence_transport_policy"][
        "raw_video_sent"
    ] is False
    assert result["photo_provenance"][0][
        "duplicate_image_review_required"
    ] is True
    assert len(
        result["photo_provenance"][0][
            "duplicate_image_case_refs"
        ]
    ) == 2
    assert result["case_refs"] == [
        f"{SOURCE_ID}/{CASE_1}",
        f"{SOURCE_ID}/{CASE_2}",
    ]


def test_invalid_taxonomy_action_fails_without_persisting(
    tmp_path: Path,
) -> None:
    store = WeakCaseStandardizationProposalStore(
        tmp_path.resolve(),
        FakeCatalog(),
        FakeProvider(
            {
                "issue_type": "roadside_mowing_required",
                "maintenance_action": "임의 작업 실행",
            }
        ),
    )

    with pytest.raises(WeakCaseStandardizationError) as caught:
        store.propose(DEMO_PROJECT_ID, _request())

    assert caught.value.code == "provider_output_invalid"
    assert store.list_project(DEMO_PROJECT_ID)["proposal_count"] == 0


def test_official_codex_adapter_uses_bounded_metadata_only_prompt(
    tmp_path: Path,
) -> None:
    cli = tmp_path / "codex.exe"
    cli.write_bytes(b"MZ")
    events = [
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": json.dumps(
                    {
                        "issue_type": "roadside_mowing_required",
                        "maintenance_action": "갓길 및 비탈면 삭초",
                    },
                    ensure_ascii=False,
                ),
                "model": "gpt-test",
            },
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "output_tokens": 3},
        },
    ]
    runner = RecordingRunner(
        GenerationCommandResult(
            0,
            stdout=(
                "\n".join(json.dumps(event) for event in events)
                + "\n"
            ).encode("utf-8"),
        )
    )
    provider = OfficialCliWeakCaseStandardizationProvider(
        explicit_paths={"codex": cli},
        status_resolver=lambda _provider: _ready_provider_status(),
        runner=runner,
        environ={"SYSTEMROOT": r"C:\Windows", "SECRET": "not-sent"},
        timeout_seconds=5,
    )
    store = WeakCaseStandardizationProposalStore(
        (tmp_path / "store").resolve(),
        FakeCatalog(),
        provider,
    )

    result = store.propose(DEMO_PROJECT_ID, _request())
    argv, call = runner.calls[0]

    assert argv[1:] == (
        "exec",
        "--json",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "-",
    )
    assert call["env"] == {"SYSTEMROOT": r"C:\Windows"}
    assert str(tmp_path).encode("utf-8") not in call["stdin"]
    assert b"--image" not in b" ".join(
        item.encode("utf-8") for item in argv
    )
    assert b"image_pixels_sent" in call["stdin"]
    assert result["provider_model"] == "gpt-test"
    assert result["provider_usage"] == {
        "input_tokens": 10,
        "output_tokens": 3,
    }


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (
            {
                "state": "not_installed",
                "installed": False,
                "authentication_state": "unknown",
                "capabilities": ("connection_status",),
            },
            "provider_not_installed",
        ),
        (
            {
                "state": "not_connected",
                "installed": True,
                "authentication_state": "signed_out",
                "capabilities": ("connection_status",),
            },
            "provider_not_connected",
        ),
        (
            {
                "state": "expired",
                "installed": True,
                "authentication_state": "expired",
                "capabilities": ("connection_status",),
            },
            "provider_authentication_expired",
        ),
        (
            {
                "state": "network_unavailable",
                "installed": True,
                "authentication_state": "unknown",
                "capabilities": ("connection_status",),
            },
            "provider_network_unavailable",
        ),
    ],
)
def test_official_provider_current_status_fails_closed_before_cli(
    tmp_path: Path,
    status: dict[str, Any],
    expected_code: str,
) -> None:
    cli = tmp_path / "codex.exe"
    cli.write_bytes(b"MZ")
    runner = RecordingRunner(GenerationCommandResult(0, stdout=b"{}"))
    provider = OfficialCliWeakCaseStandardizationProvider(
        explicit_paths={"codex": cli},
        status_resolver=lambda _provider: status,
        runner=runner,
        environ={"SYSTEMROOT": r"C:\Windows"},
        timeout_seconds=5,
    )
    store = WeakCaseStandardizationProposalStore(
        (tmp_path / "store").resolve(),
        FakeCatalog(),
        provider,
    )

    with pytest.raises(WeakCaseStandardizationError) as caught:
        store.propose(DEMO_PROJECT_ID, _request())

    assert caught.value.code == expected_code
    assert runner.calls == []
    assert store.list_project(DEMO_PROJECT_ID)["proposal_count"] == 0


@pytest.mark.parametrize(
    ("stderr", "expected_code"),
    [
        (
            b"401 Unauthorized: failed to refresh token",
            "provider_authentication_expired",
        ),
        (
            b"network is unreachable",
            "provider_network_unavailable",
        ),
    ],
)
def test_official_provider_runtime_failure_is_not_empty_success(
    tmp_path: Path,
    stderr: bytes,
    expected_code: str,
) -> None:
    cli = tmp_path / "codex.exe"
    cli.write_bytes(b"MZ")
    runner = RecordingRunner(
        GenerationCommandResult(1, stderr=stderr)
    )
    provider = OfficialCliWeakCaseStandardizationProvider(
        explicit_paths={"codex": cli},
        status_resolver=lambda _provider: _ready_provider_status(),
        runner=runner,
        environ={"SYSTEMROOT": r"C:\Windows"},
        timeout_seconds=5,
    )
    store = WeakCaseStandardizationProposalStore(
        (tmp_path / "store").resolve(),
        FakeCatalog(),
        provider,
    )

    with pytest.raises(WeakCaseStandardizationError) as caught:
        store.propose(DEMO_PROJECT_ID, _request())

    assert caught.value.code == expected_code
    assert len(runner.calls) == 1
    assert store.list_project(DEMO_PROJECT_ID)["proposal_count"] == 0


def test_actual_sample_multi_photo_and_duplicate_provenance_are_preserved(
    tmp_path: Path,
) -> None:
    catalog = WeakCaseCatalog(default_sample_root())
    snapshot = catalog.visual_index_snapshot()
    multi_group_id = next(
        row["sample_evidence_group_id"]
        for row in snapshot["cases"]
        if row["group_photo_count"] > 1
    )
    multi_rows = [
        row
        for row in snapshot["cases"]
        if row["sample_evidence_group_id"] == multi_group_id
    ]
    duplicate_row = next(
        row
        for row in snapshot["cases"]
        if row["duplicate_image_review_required"]
    )
    provider = FakeProvider()
    store = WeakCaseStandardizationProposalStore(
        tmp_path.resolve(),
        catalog,
        provider,
    )

    multi_result = store.propose(
        DEMO_PROJECT_ID,
        {
            **_request(
                multi_rows[0]["case_id"],
                multi_rows[0]["image_sha256"],
            ),
            "source_id": multi_rows[0]["source_id"],
            "expected_document_sha256": multi_rows[0][
                "document_sha256"
            ],
            "expected_index_manifest_sha256": multi_rows[0][
                "index_manifest_sha256"
            ],
        },
    )
    duplicate_result = store.propose(
        DEMO_PROJECT_ID,
        {
            **_request(
                duplicate_row["case_id"],
                duplicate_row["image_sha256"],
            ),
            "source_id": duplicate_row["source_id"],
            "expected_document_sha256": duplicate_row[
                "document_sha256"
            ],
            "expected_index_manifest_sha256": duplicate_row[
                "index_manifest_sha256"
            ],
        },
    )

    assert len(multi_result["photo_provenance"]) == 2
    assert {
        photo["photo_slot_id"]
        for photo in multi_result["photo_provenance"]
    } == {row["photo_slot_id"] for row in multi_rows}
    assert {
        photo["image_sha256"]
        for photo in multi_result["photo_provenance"]
    } == {row["image_sha256"] for row in multi_rows}
    assert duplicate_result["sample_evidence_group_id"] == (
        duplicate_row["sample_evidence_group_id"]
    )
    assert len(duplicate_result["photo_provenance"]) == 1
    assert duplicate_result["photo_provenance"][0][
        "duplicate_image_occurrence_count"
    ] == 2
    assert duplicate_result["photo_provenance"][0][
        "duplicate_image_review_required"
    ] is True
    assert store.list_project(DEMO_PROJECT_ID)[
        "evidence_event_count"
    ] == 2


def test_api_exposes_proposal_but_keeps_approval_confirmation_boundary(
    tmp_path: Path,
) -> None:
    catalog = FakeCatalog()
    store = WeakCaseStandardizationProposalStore(
        (tmp_path / "proposal-store").resolve(),
        catalog,
        FakeProvider(),
    )
    app = create_app(
        TOKEN,
        fixture_mode=True,
        allow_test_client=True,
        bridge_data_root=str(tmp_path / "bridge-data"),
        media_stability_min_interval_seconds=0,
        weak_case_catalog=catalog,
        weak_case_standardization_store=store,
    )

    with TestClient(app) as client:
        proposed = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/reference-cases/"
                "standardization-proposals"
            ),
            headers=_headers(),
            json=_request(),
        )
        assert proposed.status_code == 200, proposed.text
        body = proposed.json()
        assert body["reference_state"] == "weak_unconfirmed"
        assert body["approved_reference"] is False
        assert body["training_eligible"] is False
        assert body["annotation_count"] == 0
        assert body["incident_count"] == 0
        assert body["report_fact_count"] == 0
        assert body["evidence_transport_policy"][
            "image_pixels_sent"
        ] is False

        listed = client.get(
            (
                f"/projects/{DEMO_PROJECT_ID}/reference-cases/"
                "standardization-proposals"
            ),
            headers=_headers(),
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()["proposal_count"] == 1
        assert listed.json()["approved_reference_count"] == 0

        not_confirmed = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reference-cases/approvals",
            headers=_headers(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "source_id": SOURCE_ID,
                "case_id": CASE_1,
                "expected_document_sha256": DOCUMENT_SHA256,
                "expected_index_manifest_sha256": INDEX_SHA256,
                "expected_image_sha256": IMAGE_1,
                "issue_type": body["proposal"]["issue_type"],
                "maintenance_action": body["proposal"][
                    "maintenance_action"
                ],
                "user_confirmed": False,
            },
        )
        assert not_confirmed.status_code == 422
