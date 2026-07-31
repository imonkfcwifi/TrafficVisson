from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from app.analysis_jobs import ResolvedAnalysisArtifact
from app.main import create_app
from app.pipeline_jobs import BridgePipelineRuntime
from app.provider_visual_evidence import (
    OpenCvPrivacyRedactor,
    ProviderVisualEvidencePreparationService,
)
from app.report_generation import GenerationCommandResult
from app.structured_vision_judgment import (
    COMPARISON_MODE,
    DIRECT_VISUAL_COMPARISON_MODE,
    REQUEST_SCHEMA_VERSION,
    StructuredVisionJudgmentService,
)


CANDIDATE_ID = "a" * 20
RESCAN_EVIDENCE_IDS = (
    "1" * 20,
    "2" * 20,
    "3" * 20,
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sam2_checkpoint(
    normalized_box: dict[str, float] | None = None,
) -> dict[str, Any]:
    box = normalized_box or {
        "x": 0.49,
        "y": 0.19,
        "width": 0.32,
        "height": 0.42,
    }
    polygon = [
        [0.49, 0.19],
        [0.81, 0.19],
        [0.81, 0.61],
        [0.49, 0.61],
    ]
    tracked_frames = [
        {
            "evidence_ref": f"rescan:{evidence_id}",
            "timestamp_seconds": timestamp,
            "polygon": polygon,
        }
        for evidence_id, timestamp in zip(
            RESCAN_EVIDENCE_IDS,
            (12.0, 12.5, 13.0),
        )
    ]
    proposal_body = {
        "candidate_id": CANDIDATE_ID,
        "prompt": {
            "kind": "detector_box_center_point",
            "x": round(box["x"] + box["width"] / 2, 8),
            "y": round(box["y"] + box["height"] / 2, 8),
            "prompt_frame_ref": (
                f"rescan:{RESCAN_EVIDENCE_IDS[1]}"
            ),
        },
        "representative_polygon": polygon,
        "normalized_box": dict(box),
        "tracked_frames": tracked_frames,
        "tracking_performed": True,
        "review_only": True,
        "requires_user_confirmation": True,
        "automatic_annotation_allowed": False,
        "automatic_incident_creation_allowed": False,
        "training_membership_allowed": False,
    }
    proposal = {
        **proposal_body,
        "proposal_sha256": hashlib.sha256(
            _canonical_json(proposal_body)
        ).hexdigest(),
    }
    checkpoint_body = {
        "schema_version": (
            "trafficvisson.pipeline-sam2-review.v1"
        ),
        "state": "completed",
        "model_manifest": {
            "pack_id": "sam2-v1",
            "pack_version": "1",
            "manifest_sha256": "b" * 64,
            "model_artifact_sha256": "c" * 64,
            "provenance_verified": True,
            "review_only": True,
        },
        "proposals": [proposal],
        "failures": [],
        "mask_generated": True,
        "tracking_performed": True,
        "review_only": True,
        "requires_user_confirmation": True,
        "automatic_annotation_allowed": False,
        "automatic_incident_creation_allowed": False,
        "training_membership_allowed": False,
        "annotation_count": 0,
        "incident_count": 0,
    }
    return {
        **checkpoint_body,
        "sam2_revision": hashlib.sha256(
            _canonical_json(checkpoint_body)
        ).hexdigest(),
    }


def _install_rescan_evidence(
    runtime: BridgePipelineRuntime,
    tmp_path: Path,
    *,
    project_id,
    analysis_job_id,
) -> None:
    candidates = []
    artifacts = {}
    for index, (evidence_id, timestamp) in enumerate(
        zip(
            RESCAN_EVIDENCE_IDS,
            (12.0, 12.5, 13.0),
        )
    ):
        output = io.BytesIO()
        Image.new(
            "RGB",
            (640, 360),
            (70 + index * 20, 105, 145),
        ).save(output, format="PNG")
        body = output.getvalue()
        path = (tmp_path / f"rescan-{index}.png").resolve()
        path.write_bytes(body)
        candidates.append(
            {
                "candidate_id": evidence_id,
                "representative_timestamp_seconds": timestamp,
            }
        )
        artifacts[evidence_id] = ResolvedAnalysisArtifact(
            path=path,
            media_type="image/png",
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
        )

    def resolve(
        requested_project_id,
        requested_analysis_job_id,
        evidence_id,
    ):
        assert requested_project_id == project_id
        assert requested_analysis_job_id == analysis_job_id
        return artifacts[evidence_id]

    runtime._high_frequency_evidence_for_context = (
        lambda requested_project_id,
        requested_analysis_job_id,
        _context: (
            candidates,
            {
                evidence_id: CANDIDATE_ID
                for evidence_id in RESCAN_EVIDENCE_IDS
            },
            resolve,
            {
                "source": "candidate_window_rescan_5_10fps",
                "manifest_sha256": "d" * 64,
                "input_revision": "e" * 64,
                "selected_frame_count": 3,
                "sampling_fps": 8,
            },
        )
        if (
            requested_project_id == project_id
            and requested_analysis_job_id == analysis_job_id
        )
        else None
    )


def _inference() -> dict[str, Any]:
    return {
        "schema_version": "trafficvisson.vision-inference.v1",
        "mode": "local_model",
        "status": "classified",
        "provider": "onnx_yolo_v8",
        "pack_id": "road-ocr-v1",
        "pack_version": "1.0.0",
        "manifest_sha256": hashlib.sha256(
            b"road-ocr-v1"
        ).hexdigest(),
        "capabilities": ["defect_detection", "ocr"],
        "predictions": [
            {
                "issue_type": "sign_vegetation_occlusion",
                "confidence": 0.84,
                "box": {
                    "x": 0.5,
                    "y": 0.2,
                    "width": 0.3,
                    "height": 0.4,
                },
                "source_label": "obscured sign",
                "calibration": "model_score",
            }
        ],
        "ocr_observations": [
            {
                "text": "시험터널",
                "confidence": 0.74,
                "box": {
                    "x": 0.57,
                    "y": 0.25,
                    "width": 0.18,
                    "height": 0.1,
                },
                "kind": "sign_or_tunnel_text_unverified",
            }
        ],
        "duration_ms": 10,
        "warning_code": None,
    }


def _approval(index: int) -> dict[str, Any]:
    source_id = f"sample-hwpx-3q-{index:016x}"
    case_id = f"hwpx-weak-case-{index:032x}"
    return {
        "source_id": source_id,
        "case_id": case_id,
        "record_sha256": f"{index:064x}",
        "reference_state": "approved_reference",
        "approved_reference": True,
        "user_confirmed": True,
        "document_sha256": f"{index + 10:064x}",
        "image_sha256": f"{index + 20:064x}",
        "quarter": 3,
        "route": "시험 고속도로",
        "caption": f"표지판 식생 가림 {index}",
        "opinion": "표지판 주변 삭초 필요",
        "issue_type": "sign_vegetation_occlusion",
        "maintenance_action": "표지판 주변 삭초",
    }


class FakeApprovals:
    def __init__(self) -> None:
        self.records = [_approval(index) for index in (1, 2, 3)]

    def list_project(self, _project_id):
        return {"records": list(self.records)}


class EmptyApprovals:
    def list_project(self, _project_id):
        return {"records": []}


class FakeAnalysisJobs:
    def __init__(
        self,
        project_id,
        analysis_job_id,
        source_sha256,
        artifact=None,
    ) -> None:
        self.project_id = project_id
        self.analysis_job_id = analysis_job_id
        self.artifact = artifact
        self.row = {
            "job_id": analysis_job_id,
            "project_id": project_id,
            "status": "completed",
            "source_sha256": source_sha256,
            "result": {
                "candidates": [
                    {
                        "candidate_id": CANDIDATE_ID,
                        "representative_timestamp_seconds": 12.5,
                        "ai_inference": _inference(),
                    }
                ]
            },
        }

    def get(self, project_id, analysis_job_id):
        assert project_id == self.project_id
        assert analysis_job_id == self.analysis_job_id
        return self.row

    def resolve_artifact(
        self,
        project_id,
        analysis_job_id,
        candidate_id,
        kind,
    ):
        assert project_id == self.project_id
        assert analysis_job_id == self.analysis_job_id
        assert candidate_id == CANDIDATE_ID
        assert kind == "image"
        if self.artifact is None:
            raise AssertionError("artifact was not configured")
        return self.artifact


class FakeJudgmentResult:
    def __init__(self, provider: str, candidate_id: str) -> None:
        self.provider = provider
        self.candidate_id = candidate_id

    def as_dict(self):
        proposal = {
            "candidate_id": self.candidate_id,
            "issue_type": "sign_vegetation_occlusion",
            "maintenance_required": True,
            "action": "표지판 주변 삭초",
            "recommended_severity": "moderate",
            "rationale": "승인 사례와 검지·OCR·마스크 요약이 일치합니다.",
            "ocr_verification": {
                "status": "uncertain",
                "normalized_text": "시험터널",
                "observation_refs": [
                    f"analysis:{self.candidate_id}:ocr-1"
                ],
            },
            "uncertainty": {
                "score": 0.25,
                "reasons": ["메타데이터 비교 결과"],
            },
            "source_refs": [
                f"analysis:{self.candidate_id}:dedicated-detector-1",
                "reference:sample-hwpx-3q-0000000000000001:"
                "hwpx-weak-case-00000000000000000000000000000001",
            ],
            "comparison_mode": COMPARISON_MODE,
            "review_state": "proposed",
            "review_only": True,
            "automatic_annotation_allowed": False,
            "automatic_incident_allowed": False,
            "automatic_training_membership_allowed": False,
            "automatic_report_truth_allowed": False,
        }
        return {
            "schema_version": (
                "trafficvisson.structured-vision-proposal.v1"
            ),
            "status": "proposed",
            "provider": self.provider,
            "candidate_id": self.candidate_id,
            "comparison_mode": COMPARISON_MODE,
            "review_state": "proposed",
            "review_only": True,
            "automatic_annotation_allowed": False,
            "automatic_incident_allowed": False,
            "automatic_training_membership_allowed": False,
            "automatic_report_truth_allowed": False,
            "llm_bulk_frame_detection_used": False,
            "direct_visual_inspection_claimed": False,
            "provider_command_invoked": True,
            "proposal": proposal,
            "proposal_sha256": "9" * 64,
            "error_code": None,
        }


class FakeStructuredJudgment:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def propose(
        self,
        provider,
        payload,
        *,
        transmission_consent,
        attachments=(),
        cancel_event=None,
    ):
        self.calls.append(
            {
                "provider": provider,
                "payload": payload,
                "transmission_consent": transmission_consent,
                "cancelled": (
                    cancel_event.is_set()
                    if cancel_event is not None
                    else False
                ),
                "attachment_count": len(attachments),
            }
        )
        return FakeJudgmentResult(provider, payload["candidate_id"])


class FakeDirectJudgmentResult(FakeJudgmentResult):
    def __init__(
        self,
        provider: str,
        candidate_id: str,
        attachments,
    ) -> None:
        super().__init__(provider, candidate_id)
        self.attachments = tuple(attachments)

    def as_dict(self):
        result = super().as_dict()
        result["comparison_mode"] = DIRECT_VISUAL_COMPARISON_MODE
        result["direct_visual_inspection_claimed"] = True
        result["attachment_count"] = len(self.attachments)
        result["attachment_sha256s"] = [
            item.sha256 for item in self.attachments
        ]
        result["attachment_manifest_sha256"] = "8" * 64
        result["proposal"] = {
            **result["proposal"],
            "comparison_mode": DIRECT_VISUAL_COMPARISON_MODE,
        }
        return result


class FakeDirectStructuredJudgment(FakeStructuredJudgment):
    def propose(
        self,
        provider,
        payload,
        *,
        transmission_consent,
        attachments=(),
        cancel_event=None,
    ):
        assert len(attachments) == 4
        self.calls.append(
            {
                "provider": provider,
                "payload": payload,
                "transmission_consent": transmission_consent,
                "attachment_count": len(attachments),
                "cancelled": (
                    cancel_event.is_set()
                    if cancel_event is not None
                    else False
                ),
            }
        )
        return FakeDirectJudgmentResult(
            provider,
            payload["candidate_id"],
            attachments,
        )


class FakeVerifiedVisualEvidence:
    def __init__(self) -> None:
        self.prepare_calls = []
        self.resolve_calls = []
        self.attachments = tuple(
            SimpleNamespace(sha256=f"{index + 30:064x}")
            for index in range(4)
        )

    def prepare_from_body(self, **kwargs):
        self.prepare_calls.append(kwargs)
        return {
            "schema_version": (
                "trafficvisson.provider-visual-evidence-preparation.v1"
            ),
            "project_id": str(kwargs["project_id"]),
            "pipeline_job_id": str(kwargs["pipeline_job_id"]),
            "analysis_job_id": str(kwargs["analysis_job_id"]),
            "candidate_id": kwargs["candidate_id"],
            "preparation_id": "c" * 24,
            "input_revision": "1" * 64,
            "state": "verified_for_provider_transmission",
            "manifest_sha256": "2" * 64,
            "engine_identity_sha256": "3" * 64,
            "attachment_count": 4,
            "attachments": [],
            "automatic_redaction_performed": True,
            "automatic_privacy_verification": False,
            "human_verified": True,
            "review_required": False,
            "review_record_sha256": "4" * 64,
            "path_exposed": False,
            "preparation_revision": "5" * 64,
        }

    def resolve_verified_attachments(self, **kwargs):
        self.resolve_calls.append(kwargs)
        return self.attachments


class DummyWeakCaseCatalog:
    def resolve_case_image_for_visual_index(
        self,
        _source_id,
        _case_id,
    ):
        raise AssertionError("fake preparation owns this test resolver")


def _context(
    *,
    project_id,
    analysis_job_id,
    source_sha256,
    consent: bool,
) -> dict[str, Any]:
    approvals = [_approval(index) for index in (1, 2, 3)]
    matches = [
        {
            "source_id": item["source_id"],
            "case_id": item["case_id"],
            "reference_state": "approved_reference",
            "approved_reference": True,
            "approval_record_sha256": item["record_sha256"],
        }
        for item in approvals
    ]
    detection_revision = hashlib.sha256(b"detection").hexdigest()
    temporal_revision = hashlib.sha256(b"temporal").hexdigest()
    return {
        "job_id": str(uuid4()),
        "project_id": str(project_id),
        "snapshot_id": str(uuid4()),
        "file_name": "drive.mp4",
        "profile": {
            "detector_pack_id": "road-ocr-v1",
            "ocr_pack_id": "road-ocr-v1",
            "sam2_pack_id": "sam2-v1",
            "report_provider": "codex",
            "report_transmission_consent": consent,
            "candidate_rescan_fps": 8,
            "review_mode": "precision",
            "question_policy": "learning_assist",
        },
        "capabilities": {},
        "checkpoints": {
            "candidate_rescan_5_10fps": {
                "analysis_job_id": str(analysis_job_id),
                "source_sha256": source_sha256,
                "high_frequency_rescan_performed": False,
            },
            "detection_ocr": {
                "analysis_job_id": str(analysis_job_id),
                "source_sha256": source_sha256,
                "detection_revision": detection_revision,
                "evaluation_id": None,
                "proposal_sources": ["embedded_local_vision"],
                "proposal_candidate_count": 1,
                "dedicated_detector_status": "completed",
                "ocr_status": "completed",
                "ocr_observation_count": 1,
                "ocr_candidate_ids": [CANDIDATE_ID],
                "detected_issue_types": [
                    "sign_vegetation_occlusion"
                ],
            },
            "sam2_segmentation_tracking": _sam2_checkpoint(),
            "temporal_merge": {
                "analysis_job_id": str(analysis_job_id),
                "source_sha256": source_sha256,
                "detection_revision": detection_revision,
                "temporal_merge_revision": temporal_revision,
                "question_candidate_ids": [CANDIDATE_ID],
                "groups": [
                    {
                        "representative_candidate_id": CANDIDATE_ID,
                        "evidence_candidate_ids": [CANDIDATE_ID],
                        "start_seconds": 12.0,
                        "end_seconds": 13.0,
                        "detection_count": 1,
                    }
                ],
            },
            "similar_case_search": {
                "state": "ready",
                "visual_reference_revision": (
                    hashlib.sha256(b"references").hexdigest()
                ),
                "matches_by_candidate": {
                    CANDIDATE_ID: matches
                },
                "approved_reference_count": 3,
            },
            "metadata_gps_preflight": {
                "timed_track_status": "missing"
            },
        },
        "review_completion_id": None,
    }


def _runtime(tmp_path, *, consent: bool, provider: str = "codex"):
    project_id = uuid4()
    analysis_job_id = uuid4()
    source_sha256 = hashlib.sha256(b"video").hexdigest()
    structured = FakeStructuredJudgment()
    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        analysis_jobs=FakeAnalysisJobs(
            project_id, analysis_job_id, source_sha256
        ),
        weak_case_approvals=FakeApprovals(),
        weak_case_visual_index=object(),
        structured_vision_judgment=structured,
        report_provider_status=lambda _provider: {
            "state": "ready",
            "capabilities": [
                "structured_judgment",
                "report_draft",
                "document_edit_plan",
            ],
        },
    )
    context = _context(
        project_id=project_id,
        analysis_job_id=analysis_job_id,
        source_sha256=source_sha256,
        consent=consent,
    )
    context["profile"]["report_provider"] = provider
    context["capabilities"] = runtime.capability_snapshot(
        context["profile"]
    )
    return runtime, structured, context


def test_temporal_review_candidate_selects_exact_peak_sam2_alias():
    review_candidate_id = "d" * 20
    prompt_candidate_id = "e" * 20
    peak_proposal_id = "dino_" + "f" * 24

    selected = (
        BridgePipelineRuntime
        ._sam2_proposal_candidate_for_review(
            candidate_id=review_candidate_id,
            temporal={
                "dino_temporal_groups_by_candidate": {
                    review_candidate_id: {
                        "peak_evidence": {
                            "dino_proposal_id": peak_proposal_id,
                        },
                        "sam2_prompt_candidate_ids": [
                            prompt_candidate_id
                        ],
                    }
                }
            },
            checkpoint={
                "proposals": [
                    {"candidate_id": prompt_candidate_id}
                ],
                "dino_prompt_bindings": [
                    {
                        "dino_proposal_id": peak_proposal_id,
                        "prompt_candidate_id": (
                            prompt_candidate_id
                        ),
                        "sam2_status": "available",
                        "sam2_error_code": None,
                    }
                ],
            },
        )
    )

    assert selected == prompt_candidate_id


def test_pipeline_calls_structured_provider_once_per_merged_candidate(
    tmp_path,
) -> None:
    runtime, structured, context = _runtime(
        tmp_path,
        consent=True,
        provider="claude",
    )

    assert context["capabilities"]["vision_proposal"] == "ready"
    assert context["capabilities"][
        "selected_provider_image_transport"
    ] == "metadata_only"
    assert context["capabilities"][
        "provider_visual_evidence_automatic_verification"
    ] is False
    vision = runtime.execute(
        "vision_proposal", context, lambda: False
    )

    assert vision.outcome == "success"
    assert vision.error_code is None
    assert vision.checkpoint[
        "structured_llm_semantics_status"
    ] == "completed"
    assert vision.checkpoint["structured_proposal_count"] == 1
    assert vision.checkpoint["provider_invocation_count"] == 1
    assert vision.checkpoint[
        "metadata_only_provider_downgrade_count"
    ] == 1
    assert vision.checkpoint["annotation_count"] == 0
    assert vision.checkpoint["incident_count"] == 0
    assert vision.checkpoint["training_membership_count"] == 0
    assert vision.checkpoint["report_incident_count"] == 0
    assert len(structured.calls) == 1

    transmitted = structured.calls[0]
    payload = transmitted["payload"]
    assert transmitted["provider"] == "claude"
    assert transmitted["transmission_consent"] is True
    assert transmitted["cancelled"] is False
    assert payload["schema_version"] == REQUEST_SCHEMA_VERSION
    assert payload["candidate_id"] == CANDIDATE_ID
    assert len(payload["similar_cases"]) == 3
    assert all(
        case["approved_reference"] is True
        and case["user_verified"] is True
        for case in payload["similar_cases"]
    )
    assert payload["detector_proposals"][0]["role"] == (
        "dedicated_road_model"
    )
    assert payload["ocr_observations"][0]["text"] == "시험터널"
    assert payload["sam2_summary"]["status"] == "available"
    assert payload["sam2_summary"]["tracked_frame_count"] == 3
    assert payload["temporal_summary"]["gps_state"] == "gpx_required"
    assert "path" not in str(payload).casefold()
    assert "base64" not in str(payload).casefold()

    context["checkpoints"]["vision_proposal"] = dict(
        vision.checkpoint
    )
    queue = runtime.execute(
        "question_queue", context, lambda: False
    )
    assert queue.checkpoint[
        "structured_llm_semantics_status"
    ] == "completed"
    assert queue.checkpoint[
        "structured_judgments_by_candidate"
    ][CANDIDATE_ID]["proposal"]["recommended_severity"] == (
        "moderate"
    )
    assert "structured_vision_llm" not in queue.checkpoint[
        "capability_missing"
    ]
    assert "structured_vision_llm" not in queue.checkpoint[
        "capability_failed"
    ]
    assert queue.checkpoint["annotation_count"] == 0
    assert queue.checkpoint["incident_count"] == 0
    assert queue.checkpoint["training_membership_count"] == 0
    assert queue.checkpoint["report_incident_count"] == 0


def test_pipeline_passes_only_verified_prepared_images_to_codex(
    tmp_path: Path,
) -> None:
    project_id = uuid4()
    analysis_job_id = uuid4()
    source_sha256 = hashlib.sha256(b"visual-video").hexdigest()
    candidate_path = tmp_path / "candidate.jpg"
    candidate_path.write_bytes(b"safe-test-artifact")
    artifact_sha256 = hashlib.sha256(
        b"safe-test-artifact"
    ).hexdigest()
    analysis = FakeAnalysisJobs(
        project_id,
        analysis_job_id,
        source_sha256,
        artifact=SimpleNamespace(
            path=candidate_path.resolve(),
            media_type="image/jpeg",
            size_bytes=candidate_path.stat().st_size,
            sha256=artifact_sha256,
        ),
    )
    structured = FakeDirectStructuredJudgment()
    prepared = FakeVerifiedVisualEvidence()
    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        analysis_jobs=analysis,
        weak_case_catalog=DummyWeakCaseCatalog(),
        weak_case_approvals=FakeApprovals(),
        weak_case_visual_index=object(),
        structured_vision_judgment=structured,
        provider_visual_evidence=prepared,
        report_provider_status=lambda _provider: {
            "state": "ready",
            "capabilities": ["structured_judgment"],
        },
    )
    _install_rescan_evidence(
        runtime,
        tmp_path,
        project_id=project_id,
        analysis_job_id=analysis_job_id,
    )
    context = _context(
        project_id=project_id,
        analysis_job_id=analysis_job_id,
        source_sha256=source_sha256,
        consent=True,
    )
    context["capabilities"] = runtime.capability_snapshot(
        context["profile"]
    )

    result = runtime.execute(
        "vision_proposal",
        context,
        lambda: False,
    )

    assert result.outcome == "success"
    assert result.error_code is None
    assert result.checkpoint[
        "structured_llm_semantics_status"
    ] == "completed"
    assert result.checkpoint[
        "privacy_verification_status"
    ] == "verified"
    assert result.checkpoint["comparison_mode"] == (
        DIRECT_VISUAL_COMPARISON_MODE
    )
    assert result.checkpoint[
        "direct_visual_inspection_claimed"
    ] is True
    assert result.checkpoint["provider_invocation_count"] == 1
    assert result.checkpoint["annotation_count"] == 0
    assert result.checkpoint["incident_count"] == 0
    assert len(prepared.prepare_calls) == 1
    assert len(prepared.resolve_calls) == 1
    assert len(structured.calls) == 1
    assert structured.calls[0]["attachment_count"] == 4
    payload = structured.calls[0]["payload"]
    assert payload["candidate_evidence_refs"][0].startswith(
        f"deid:{CANDIDATE_ID}:provider-montage-"
    )
    assert (
        prepared.prepare_calls[0]["candidate_media_type"]
        == "image/png"
    )
    assert prepared.prepare_calls[0]["target_box"] == {
        "x": 0.0,
        "y": 0.0,
        "width": 1.0,
        "height": 1.0,
    }
    public_preparation = result.checkpoint[
        "provider_visual_evidence_by_candidate"
    ][CANDIDATE_ID]
    assert public_preparation["human_verified"] is True
    assert public_preparation["path_exposed"] is False


def test_pipeline_prepares_minimum_context_before_privacy_review(
    tmp_path: Path,
) -> None:
    project_id = uuid4()
    analysis_job_id = uuid4()
    source_sha256 = hashlib.sha256(b"context-video").hexdigest()
    candidate_path = tmp_path / "candidate.png"
    Image.new("RGB", (1600, 900), (70, 110, 150)).save(
        candidate_path,
        format="PNG",
    )
    candidate_raw = candidate_path.read_bytes()
    candidate_sha256 = hashlib.sha256(candidate_raw).hexdigest()
    analysis = FakeAnalysisJobs(
        project_id,
        analysis_job_id,
        source_sha256,
        artifact=SimpleNamespace(
            path=candidate_path.resolve(),
            media_type="image/png",
            size_bytes=len(candidate_raw),
            sha256=candidate_sha256,
        ),
    )
    tiny_box = {
        "x": 0.97,
        "y": 0.95,
        "width": 0.02,
        "height": 0.03,
    }
    analysis.row["result"]["candidates"][0]["ai_inference"][
        "predictions"
    ][0]["box"] = dict(tiny_box)

    resolved = {}
    approvals = []
    for index in (1, 2, 3):
        output = io.BytesIO()
        Image.new(
            "RGB",
            (360, 240),
            (30 + index * 20, 90, 140),
        ).save(output, format="PNG")
        body = output.getvalue()
        digest = hashlib.sha256(body).hexdigest()
        record = _approval(index)
        record["image_sha256"] = digest
        approvals.append(record)
        resolved[(record["source_id"], record["case_id"])] = (
            SimpleNamespace(
                body=body,
                media_type="image/png",
                size_bytes=len(body),
                sha256=digest,
            )
        )

    class ApprovedReferences:
        def list_project(self, _project_id):
            return {"records": list(approvals)}

    class ReferenceCatalog:
        def resolve_case_image_for_visual_index(
            self,
            source_id,
            case_id,
        ):
            return resolved[(source_id, case_id)]

    structured = FakeStructuredJudgment()
    preparations = ProviderVisualEvidencePreparationService(
        tmp_path / "provider-evidence",
        redactor=OpenCvPrivacyRedactor(
            detected_boxes=lambda _image: {
                "faces": [],
                "plates": [],
            }
        ),
    )
    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        analysis_jobs=analysis,
        weak_case_catalog=ReferenceCatalog(),
        weak_case_approvals=ApprovedReferences(),
        weak_case_visual_index=object(),
        structured_vision_judgment=structured,
        provider_visual_evidence=preparations,
        report_provider_status=lambda _provider: {
            "state": "ready",
            "capabilities": [
                "structured_judgment",
                "report_draft",
                "document_edit_plan",
            ],
        },
    )
    _install_rescan_evidence(
        runtime,
        tmp_path,
        project_id=project_id,
        analysis_job_id=analysis_job_id,
    )
    context = _context(
        project_id=project_id,
        analysis_job_id=analysis_job_id,
        source_sha256=source_sha256,
        consent=True,
    )
    context["checkpoints"]["sam2_segmentation_tracking"] = (
        _sam2_checkpoint(tiny_box)
    )
    context["capabilities"] = runtime.capability_snapshot(
        context["profile"]
    )

    result = runtime.execute(
        "vision_proposal",
        context,
        lambda: False,
    )

    assert result.outcome == "partial"
    assert result.error_code == "awaiting_deidentification_review"
    assert result.metrics["provider_invocation_count"] == 0
    assert result.metrics["annotation_count"] == 0
    assert result.metrics["incident_count"] == 0
    assert result.metrics["training_membership_count"] == 0
    assert result.metrics["report_incident_count"] == 0
    assert structured.calls == []
    prepared = result.checkpoint[
        "provider_visual_evidence_by_candidate"
    ][CANDIDATE_ID]
    assert (
        prepared["attachments"][0]["width"],
        prepared["attachments"][0]["height"],
    ) == (1024, 624)
    assert prepared[
        "candidate_evidence_resolution"
    ]["privacy_state"] == "unreviewed"
    assert prepared[
        "candidate_evidence_resolution"
    ]["provider_transmission_allowed"] is False
    assert prepared["state"] == "awaiting_deidentification_review"
    assert prepared["human_verified"] is False
    assert prepared["automatic_privacy_verification"] is False
    assert prepared["review_required"] is True


def test_pipeline_compares_unconfirmed_sample_cases_without_promoting_them(
    tmp_path,
) -> None:
    project_id = uuid4()
    analysis_job_id = uuid4()
    source_sha256 = hashlib.sha256(b"weak-video").hexdigest()
    structured = FakeStructuredJudgment()
    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        analysis_jobs=FakeAnalysisJobs(
            project_id,
            analysis_job_id,
            source_sha256,
        ),
        weak_case_approvals=EmptyApprovals(),
        weak_case_visual_index=object(),
        structured_vision_judgment=structured,
        report_provider_status=lambda _provider: {
            "state": "ready",
            "capabilities": ["structured_judgment"],
        },
    )
    context = _context(
        project_id=project_id,
        analysis_job_id=analysis_job_id,
        source_sha256=source_sha256,
        consent=True,
    )
    weak_matches = []
    for index in (1, 2, 3):
        approved = _approval(index)
        weak_matches.append(
            {
                "source_id": approved["source_id"],
                "case_id": approved["case_id"],
                "approval_record_sha256": None,
                "reference_state": "weak_unconfirmed",
                "approved_reference": False,
                "review_required": True,
                "training_eligible": False,
                "document_sha256": approved["document_sha256"],
                "image_sha256": approved["image_sha256"],
                "quarter": approved["quarter"],
                "route": approved["route"],
                "caption": approved["caption"],
                "opinion": approved["opinion"],
                "issue_type": approved["issue_type"],
            }
        )
    context["checkpoints"]["similar_case_search"] = {
        "state": "weak_reference_only",
        "visual_reference_revision": hashlib.sha256(
            b"weak-references"
        ).hexdigest(),
        "references": weak_matches,
        "approved_reference_count": 0,
    }
    context["profile"]["report_provider"] = "claude"
    context["capabilities"] = runtime.capability_snapshot(
        context["profile"]
    )

    vision = runtime.execute(
        "vision_proposal",
        context,
        lambda: False,
    )

    assert vision.checkpoint[
        "structured_llm_semantics_status"
    ] == "completed"
    assert vision.checkpoint["provider_invocation_count"] == 1
    assert len(structured.calls) == 1
    transmitted = structured.calls[0]["payload"]["similar_cases"]
    assert len(transmitted) == 3
    assert all(
        case["reference_state"] == "weak_unconfirmed"
        and case["approved_reference"] is False
        and case["user_verified"] is False
        and case["approval_record_sha256"] is None
        for case in transmitted
    )


def test_pipeline_consent_absent_is_not_run_and_never_calls_cli(
    tmp_path,
) -> None:
    runtime, structured, context = _runtime(
        tmp_path,
        consent=False,
    )

    assert context["capabilities"]["vision_proposal"] == "missing"
    vision = runtime.execute(
        "vision_proposal", context, lambda: False
    )

    assert vision.outcome == "success"
    assert vision.error_code == "structured_llm_not_run"
    assert vision.checkpoint[
        "structured_llm_semantics_status"
    ] == "not_run"
    assert vision.checkpoint["provider_invocation_count"] == 0
    assert vision.checkpoint[
        "structured_judgments_by_candidate"
    ][CANDIDATE_ID]["error_code"] == (
        "transmission_consent_required"
    )
    assert structured.calls == []


def test_pipeline_without_deidentified_images_fails_closed_before_codex(
    tmp_path,
) -> None:
    project_id = uuid4()
    analysis_job_id = uuid4()
    source_sha256 = hashlib.sha256(b"video").hexdigest()
    cli_path = tmp_path / "codex.exe"
    cli_path.write_bytes(b"MZ")
    output = FakeJudgmentResult(
        "codex", CANDIDATE_ID
    ).as_dict()["proposal"]
    model_output = {
        key: output[key]
        for key in (
            "candidate_id",
            "issue_type",
            "maintenance_required",
            "action",
            "recommended_severity",
            "rationale",
            "ocr_verification",
            "uncertainty",
            "source_refs",
        )
    }
    events = [
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": json.dumps(
                    model_output, ensure_ascii=False
                ),
                "model": "gpt-5.6-codex",
            },
        },
        {"type": "turn.completed"},
    ]

    class Runner:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, _argv, **_kwargs):
            self.calls += 1
            return GenerationCommandResult(
                0,
                stdout=(
                    "\n".join(json.dumps(item) for item in events)
                    + "\n"
                ).encode(),
            )

    runner = Runner()
    service = StructuredVisionJudgmentService(
        explicit_paths={"codex": cli_path},
        provider_ready={"codex": True},
        runner=runner,
        environ={"SYSTEMROOT": r"C:\Windows"},
        timeout_seconds=5,
    )
    runtime = BridgePipelineRuntime(
        object(),
        data_root=tmp_path.resolve(),
        analysis_jobs=FakeAnalysisJobs(
            project_id, analysis_job_id, source_sha256
        ),
        weak_case_approvals=FakeApprovals(),
        weak_case_visual_index=object(),
        structured_vision_judgment=service,
        report_provider_status=lambda _provider: {
            "state": "ready",
            "capabilities": ["structured_judgment"],
        },
    )
    context = _context(
        project_id=project_id,
        analysis_job_id=analysis_job_id,
        source_sha256=source_sha256,
        consent=True,
    )
    context["capabilities"] = runtime.capability_snapshot(
        context["profile"]
    )

    result = runtime.execute(
        "vision_proposal", context, lambda: False
    )

    assert result.outcome == "partial"
    assert result.error_code == (
        "provider_visual_evidence_unavailable"
    )
    assert result.checkpoint[
        "structured_llm_semantics_status"
    ] == "awaiting_deidentification_review"
    assert result.checkpoint["structured_proposal_count"] == 0
    assert result.checkpoint["structured_not_run_count"] == 1
    assert result.checkpoint["structured_failure_count"] == 0
    assert result.checkpoint["provider_invocation_count"] == 0
    judgment = result.checkpoint[
        "structured_judgments_by_candidate"
    ][CANDIDATE_ID]
    assert judgment["error_code"] == (
        "provider_visual_evidence_unavailable"
    )
    assert judgment["provider_command_invoked"] is False
    assert judgment["automatic_annotation_allowed"] is False
    assert judgment["automatic_incident_allowed"] is False
    assert runner.calls == 0


def test_create_app_injects_structured_service_into_default_runtime(
    tmp_path,
) -> None:
    structured = FakeStructuredJudgment()

    class ProviderStatus:
        def get_status(self, _provider):
            return {
                "state": "ready",
                "capabilities": [
                    "structured_judgment",
                    "report_draft",
                    "document_edit_plan",
                ],
            }

        def launch_login(self, _provider):
            raise AssertionError("login is not part of this test")

    with TestClient(
        create_app(
            "s" * 64,
            fixture_mode=True,
            allow_test_client=True,
            bridge_data_root=str(tmp_path.resolve()),
            provider_status_service=ProviderStatus(),
            structured_vision_judgment_service=structured,
        )
    ) as client:
        assert (
            client.app.state.structured_vision_judgment
            is structured
        )
        runtime = client.app.state.pipeline_jobs._runtime
        capabilities = runtime.capability_snapshot(
            {
                "project_id": "00000000-0000-0000-0000-000000000001",
                "detector_pack_id": "detector",
                "ocr_pack_id": "ocr",
                "sam2_pack_id": "sam2",
                "report_provider": "codex",
                "report_transmission_consent": True,
                "default_template_id": "sample-4q-v1",
                "default_template_sha256": "0" * 64,
                "template_semantic_map_id": "unconfigured",
                "template_semantic_map_sha256": "0" * 64,
                "template_semantic_map_ready": False,
            }
        )
        assert capabilities["structured_provider_ready"] is True
        assert capabilities["vision_proposal"] == "ready"
        assert capabilities["provider_visual_evidence_ready"] is True
        assert capabilities[
            "provider_visual_evidence_human_review_required"
        ] is True
        assert capabilities[
            "provider_visual_evidence_automatic_verification"
        ] is False
        assert capabilities[
            "provider_visual_evidence_coverage_guaranteed"
        ] is False
        assert capabilities[
            "selected_provider_image_transport"
        ] == "verified_minimized_attachments"
        assert capabilities[
            "structured_provider_comparison_mode"
        ] == COMPARISON_MODE
