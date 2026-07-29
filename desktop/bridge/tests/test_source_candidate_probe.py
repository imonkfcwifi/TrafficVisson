from __future__ import annotations

import hashlib

from app import source_candidate_probe as probe_module
from app.source_candidate_probe import SourceCandidateProbe


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def test_probe_distinguishes_absent_unverified_and_verified_evidence(
    tmp_path, monkeypatch
):
    pack = b'{"pack":"verified"}'
    summary = b"# verified evaluation"
    manifest = b'{"evaluation":"verified"}'
    spec = {
        "test-source": {
            "files": (
                ("ModelPacks/test/pack.json", _digest(pack)),
                ("ModelEvaluations/test/README.md", _digest(summary)),
                (
                    "ModelEvaluations/test/manifest.json",
                    _digest(manifest),
                ),
            ),
            "evaluation": {
                "evaluated_frame_count": 30,
                "proposal_frame_count": 25,
                "proposal_count": 47,
                "false_positive_count": None,
                "verdict": "review_only",
            },
        }
    }
    monkeypatch.setattr(probe_module, "SOURCE_EVIDENCE_SPECS", spec)
    probe = SourceCandidateProbe(tmp_path)

    assert probe.snapshot()["sources"]["test-source"] == {
        "state": "absent",
        "evaluation": None,
    }

    pack_path = tmp_path / "ModelPacks" / "test" / "pack.json"
    pack_path.parent.mkdir(parents=True)
    pack_path.write_bytes(pack)
    assert probe.snapshot()["sources"]["test-source"]["state"] == (
        "unverified_local"
    )

    summary_path = (
        tmp_path / "ModelEvaluations" / "test" / "README.md"
    )
    summary_path.parent.mkdir(parents=True)
    summary_path.write_bytes(summary)
    manifest_path = summary_path.with_name("manifest.json")
    manifest_path.write_bytes(b"tampered")
    assert probe.snapshot()["sources"]["test-source"]["state"] == (
        "unverified_local"
    )

    manifest_path.write_bytes(manifest)
    assert probe.snapshot()["sources"]["test-source"] == {
        "state": "verified_snapshot",
        "evaluation": spec["test-source"]["evaluation"],
    }


def test_probe_never_returns_local_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(
        probe_module,
        "SOURCE_EVIDENCE_SPECS",
        {
            "test-source": {
                "files": (("../outside.json", "a" * 64),),
                "evaluation": {
                    "evaluated_frame_count": 1,
                    "proposal_frame_count": 1,
                    "proposal_count": 1,
                    "false_positive_count": None,
                    "verdict": "review_only",
                },
            }
        },
    )
    snapshot = SourceCandidateProbe(tmp_path).snapshot()

    assert snapshot["sources"]["test-source"]["state"] == "absent"
    assert str(tmp_path) not in str(snapshot)
