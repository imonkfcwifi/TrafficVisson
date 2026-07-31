"""Project-scoped conversion of safe structured proposals into training labels.

The caller supplies only server-owned candidate identities. Proposal contents
are resolved again from the immutable ReviewEventSet, so neither the renderer
nor a resumed PipelineJob can inject an answer, type, severity, action, OCR
value, provider, or provenance digest.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID

from .candidate_labels import (
    CandidateLabelError,
    CandidateLabelStore,
)
from .structured_llm_labels import (
    StructuredLlmTrainingLabelError,
)
from .structured_assist_provenance import (
    StructuredAssistProvenanceError,
    canonical_structured_assist_provenance,
    project_profile_delegation_authority,
    seal_structured_assist_provenance,
)
from .training_geometry import (
    TrainingGeometryError,
    training_geometry_from_review,
)


SCHEMA_VERSION = (
    "trafficvisson.pipeline-structured-label-delegation.v1"
)
_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class PipelineStructuredLabelDelegationError(RuntimeError):
    """The durable training-label delegation could not be reconciled."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class PipelineStructuredLabelDelegationService:
    """Idempotently persist only safe, nonurgent structured training labels."""

    def __init__(
        self,
        *,
        candidate_labels: CandidateLabelStore,
        proposal_resolver: Callable[
            [UUID, UUID, str], Mapping[str, Any]
        ],
        projection_sink: Callable[
            [UUID, list[dict[str, Any]], list[UUID]],
            Sequence[Mapping[str, Any]],
        ],
        delegation_authority_resolver: Callable[
            [UUID], Mapping[str, Any]
        ],
    ) -> None:
        if (
            not callable(proposal_resolver)
            or not callable(projection_sink)
            or not callable(delegation_authority_resolver)
        ):
            raise ValueError("delegation dependencies must be callable")
        self._candidate_labels = candidate_labels
        self._proposal_resolver = proposal_resolver
        self._projection_sink = projection_sink
        self._delegation_authority_resolver = (
            delegation_authority_resolver
        )

    def apply(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        candidate_ids: Sequence[str],
    ) -> dict[str, Any]:
        if not isinstance(project_id, UUID) or not isinstance(
            analysis_job_id, UUID
        ):
            raise ValueError("delegation identities must be UUIDs")
        if (
            isinstance(candidate_ids, (str, bytes))
            or not isinstance(candidate_ids, Sequence)
            or not candidate_ids
            or len(candidate_ids) != len(set(candidate_ids))
            or any(
                not isinstance(candidate_id, str)
                or _CANDIDATE_ID.fullmatch(candidate_id) is None
                for candidate_id in candidate_ids
            )
        ):
            raise ValueError("delegation candidate_ids are invalid")

        try:
            profile = self._delegation_authority_resolver(project_id)
            delegation_authority = (
                project_profile_delegation_authority(
                    profile,
                    project_id=project_id,
                )
            )
            delegated_provider = profile.get("report_provider")
        except Exception as error:
            raise PipelineStructuredLabelDelegationError(
                "project structured-label delegation is not authorized"
            ) from error

        try:
            latest = self._candidate_labels.latest_answers(
                project_id,
                analysis_job_id=analysis_job_id,
            )
        except CandidateLabelError as error:
            raise PipelineStructuredLabelDelegationError(
                "candidate label ledger is unavailable"
            ) from error
        latest_by_candidate = {
            str(record["candidate_id"]): record for record in latest
        }
        reasons: dict[str, str] = {}

        for candidate_id in candidate_ids:
            existing = latest_by_candidate.get(candidate_id)
            if existing is not None:
                actor = existing.get("final_answer", {}).get(
                    "answered_by"
                )
                if actor == "structured_llm_assisted":
                    existing_assist = existing.get(
                        "assist_provenance"
                    )
                    try:
                        canonical_existing_assist = (
                            canonical_structured_assist_provenance(
                                existing_assist
                            )
                        )
                    except StructuredAssistProvenanceError:
                        canonical_existing_assist = None
                    if (
                        canonical_existing_assist is not None
                        and canonical_existing_assist["authority"]
                        == delegation_authority
                    ):
                        continue
                elif actor == "local_user":
                    reasons[candidate_id] = "existing_human_answer"
                    continue
                elif actor != "structured_llm_assisted":
                    raise PipelineStructuredLabelDelegationError(
                        "candidate label actor is invalid"
                    )
            try:
                answer = dict(
                    self._proposal_resolver(
                        project_id,
                        analysis_job_id,
                        candidate_id,
                    )
                )
            except StructuredLlmTrainingLabelError as error:
                # Missing/invalid proposals and mandatory debris/emergency
                # candidates remain ordinary human questions.
                reasons[candidate_id] = error.code
                continue
            try:
                assist_evidence = answer.get("assist_evidence")
                if not isinstance(assist_evidence, Mapping):
                    raise StructuredAssistProvenanceError(
                        "structured assist execution evidence is unavailable"
                    )
                if assist_evidence.get("provider") != delegated_provider:
                    # A project-wide grant is scoped to the provider selected
                    # in the exact consented profile revision.  A proposal
                    # produced under another provider remains review evidence,
                    # but it cannot silently become a training Annotation.
                    reasons[candidate_id] = (
                        "delegation_provider_mismatch"
                    )
                    continue
                attachment_hashes = assist_evidence.get(
                    "attachment_sha256s"
                )
                verification_hashes = assist_evidence.get(
                    "attachment_verification_record_sha256s"
                )
                if (
                    not isinstance(attachment_hashes, list)
                    or not isinstance(verification_hashes, list)
                    or not 4 <= len(attachment_hashes) <= 6
                    or len(attachment_hashes)
                    != len(verification_hashes)
                ):
                    # Metadata-only judgments can help rank a question, but
                    # they are not a substitute for the locally reviewed,
                    # deidentified candidate + 3-5 comparison images required
                    # for delegated training truth.
                    reasons[candidate_id] = (
                        "verified_visual_evidence_required"
                    )
                    continue
                assist_provenance = (
                    seal_structured_assist_provenance(
                        {
                            **dict(assist_evidence),
                            "authority": delegation_authority,
                        }
                    )
                )
                resolved_candidate = (
                    self._candidate_labels.resolve_question(
                        project_id,
                        analysis_job_id,
                        candidate_id,
                    )
                )
                candidate_metadata = resolved_candidate.get(
                    "ai_original_candidate"
                )
                training_geometry = (
                    training_geometry_from_review(
                        candidate_metadata=candidate_metadata,
                        answer=answer["answer"],
                        answered_by="structured_llm_assisted",
                    )
                    if isinstance(candidate_metadata, Mapping)
                    else None
                )
                record = self._candidate_labels.record_answer(
                    project_id,
                    analysis_job_id,
                    candidate_id,
                    answer=answer["answer"],
                    selected_issue_type=answer[
                        "selected_issue_type"
                    ],
                    severity=answer["severity"],
                    maintenance_action=answer[
                        "maintenance_action"
                    ],
                    ocr_text=answer["ocr_text"],
                    normalized_box=(
                        training_geometry.get("normalized_box")
                        if training_geometry is not None
                        else None
                    ),
                    answered_by="structured_llm_assisted",
                    training_geometry=training_geometry,
                    assist_provenance=assist_provenance,
                )
            except (
                CandidateLabelError,
                KeyError,
                StructuredAssistProvenanceError,
                TrainingGeometryError,
            ) as error:
                raise PipelineStructuredLabelDelegationError(
                    "structured training label persistence failed"
                ) from error
            latest_by_candidate[candidate_id] = record

        try:
            audit_records = self._candidate_labels.audit_records(
                project_id
            )
            if audit_records:
                self._projection_sink(
                    project_id,
                    audit_records,
                    [analysis_job_id],
                )
            latest = self._candidate_labels.latest_answers(
                project_id,
                analysis_job_id=analysis_job_id,
            )
        except CandidateLabelError as error:
            raise PipelineStructuredLabelDelegationError(
                "structured training annotation projection failed"
            ) from error
        except Exception as error:
            raise PipelineStructuredLabelDelegationError(
                "structured training annotation projection failed"
            ) from error

        latest_by_candidate = {
            str(record["candidate_id"]): record for record in latest
        }
        delegated_proofs: list[dict[str, Any]] = []
        delegated_candidate_ids: list[str] = []
        for candidate_id in candidate_ids:
            record = latest_by_candidate.get(candidate_id)
            if (
                record is None
                or record.get("final_answer", {}).get("answered_by")
                != "structured_llm_assisted"
            ):
                continue
            record_hash = record.get("record_hash")
            revision = record.get("answer_revision")
            record_id = record.get("record_id")
            if (
                not isinstance(record_hash, str)
                or _SHA256.fullmatch(record_hash) is None
                or isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
                or not isinstance(record_id, str)
            ):
                raise PipelineStructuredLabelDelegationError(
                    "structured training label proof is invalid"
                )
            delegated_candidate_ids.append(candidate_id)
            delegated_proofs.append(
                {
                    "candidate_id": candidate_id,
                    "record_id": record_id,
                    "record_hash": record_hash,
                    "answer_revision": revision,
                    "assist_provenance_sha256": record[
                        "assist_provenance"
                    ]["provenance_sha256"],
                }
            )

        delegated_set = set(delegated_candidate_ids)
        human_question_candidate_ids = [
            candidate_id
            for candidate_id in candidate_ids
            if candidate_id not in delegated_set
        ]
        for candidate_id in human_question_candidate_ids:
            reasons.setdefault(candidate_id, "proposal_unavailable")
        mandatory_human_candidate_ids = [
            candidate_id
            for candidate_id in human_question_candidate_ids
            if reasons[candidate_id] == "mandatory_human_review"
        ]
        identity = {
            "schema_version": SCHEMA_VERSION,
            "project_id": str(project_id),
            "analysis_job_id": str(analysis_job_id),
            "candidate_ids": list(candidate_ids),
            "delegation_authority": delegation_authority,
            "delegated_proofs": delegated_proofs,
            "human_question_reasons": {
                candidate_id: reasons[candidate_id]
                for candidate_id in human_question_candidate_ids
            },
        }
        return {
            **identity,
            "delegation_revision": hashlib.sha256(
                _canonical_json(identity)
            ).hexdigest(),
            "delegated_candidate_ids": delegated_candidate_ids,
            "human_question_candidate_ids": (
                human_question_candidate_ids
            ),
            "mandatory_human_candidate_ids": (
                mandatory_human_candidate_ids
            ),
            "structured_training_label_count": len(
                delegated_candidate_ids
            ),
            "training_annotation_count": len(
                delegated_candidate_ids
            ),
            "incident_count": 0,
            "report_incident_count": 0,
            "fixed_validation_membership_count": 0,
            "training_only": True,
            "automatic_training_started": False,
        }


__all__ = [
    "PipelineStructuredLabelDelegationError",
    "PipelineStructuredLabelDelegationService",
    "SCHEMA_VERSION",
]
