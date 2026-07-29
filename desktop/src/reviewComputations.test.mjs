import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test, { after } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const JOB_ID = "22222222-2222-4222-8222-222222222222";
const SHA = "a".repeat(64);
const desktopRoot = fileURLToPath(new URL("..", import.meta.url));
const vite = await createServer({
  configFile: false,
  root: desktopRoot,
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
});
const module = await vite.ssrLoadModule("/src/reviewComputations.ts");
const issueTypes = await vite.ssrLoadModule("/src/issueTypes.ts");
const labelingSource = await readFile(
  new URL("./screens/LabelingScreen.tsx", import.meta.url),
  "utf8",
);

after(async () => {
  await vite.close();
});

function temporalResponse(overrides = {}) {
  return {
    schema_version: "trafficvisson.temporal-review-api.v1",
    project_id: PROJECT_ID,
    computation_mode: "pure_review_only",
    proposal_count: 1,
    proposals: [
      {
        schema_version: "trafficvisson.temporal-incident-proposal.v1",
        proposal_id: `tip_${"b".repeat(24)}`,
        proposal_status: "awaiting_user_review",
        issue_type: "pothole",
        start_seconds: 1,
        end_seconds: 1,
        duration_seconds: 0,
        detection_count: 1,
        evidence_detection_ids: ["frame-001"],
        peak_evidence: {
          detection_id: "frame-001",
          timestamp_seconds: 1,
          score: 0,
          normalized_box: null,
          track_hint: null,
          frame_index: 0,
          score_semantics: "model_score_not_probability",
        },
        merge_reasons: ["single_detection"],
        merge_links: [],
        review_only: true,
        automatic_incident_creation_allowed: false,
        automatic_annotation_allowed: false,
        automatic_maintenance_action_allowed: false,
        automatic_training_allowed: false,
        provenance: {
          algorithm: "temporal-spatial-review-grouping.v1",
          source: {
            project_id: PROJECT_ID,
            analysis_job_id: JOB_ID,
            source_file_sha256: SHA,
            source: "classified_review_proposals",
          },
          source_fingerprint_sha256: "c".repeat(64),
          evidence_fingerprint_sha256: "d".repeat(64),
          grouping_policy: {
            max_gap_seconds: 2,
            min_iou: 0.1,
            max_center_distance: 0.15,
            strict_track_separation: true,
            allow_temporal_only_without_spatial_hints: false,
          },
        },
      },
    ],
    state_changes_performed: false,
    incidents_created: 0,
    annotations_created: 0,
    training_members_created: 0,
    model_versions_changed: 0,
    ...overrides,
  };
}

function metric(issueType, overrides = {}) {
  return {
    issue_type: issueType,
    validation_sample_count: 0,
    positive_sample_count: 0,
    negative_sample_count: 0,
    truth_event_count: 0,
    prediction_event_count: 0,
    true_positive_count: 0,
    false_positive_count: 0,
    false_negative_count: 0,
    duplicate_prediction_count: 0,
    precision: 0,
    recall: 0,
    false_positive_rate: 0,
    duplicate_burden: 0,
    ...overrides,
  };
}

function report(modelVersion, overrides = {}) {
  return {
    schema_version: "trafficvisson.validation-metrics.v1",
    model_version: modelVersion,
    validation_set_sha256: "e".repeat(64),
    prediction_set_sha256: "f".repeat(64),
    per_type: issueTypes.CANONICAL_ISSUE_TYPES.map(({ apiValue }) =>
      metric(apiValue),
    ),
    ...overrides,
  };
}

test("시간축 요청은 legacy 유형을 표준 유형으로 바꾸고 안전 플래그를 고정한다", () => {
  const request = module.buildTemporalIncidentReviewRequest({
    projectId: PROJECT_ID,
    analysisJobId: JOB_ID,
    sourceFileSha256: SHA,
    detections: [
      {
        detectionId: "frame-001",
        timestampSeconds: 1,
        issueType: "color_guidance_line_faded",
        score: 0,
        frameIndex: 0,
      },
    ],
  });
  assert.equal(request.detections[0].issueType, "colored_guidance_faded");
  assert.equal(request.detections[0].score, 0);
  assert.equal(request.detections[0].reviewOnly, true);
  assert.equal(request.detections[0].automaticLabelingAllowed, false);
  assert.equal(request.policy.strictTrackSeparation, true);
});

test("시간축 응답은 false와 0을 정확히 검증하고 요청 근거만 허용한다", () => {
  const parsed = module.parseTemporalIncidentReviewResponse(
    temporalResponse(),
    { projectId: PROJECT_ID, detectionIds: ["frame-001"] },
  );
  assert.equal(parsed.proposals[0].peakEvidence.score, 0);
  assert.equal(parsed.proposals[0].peakEvidence.frameIndex, 0);
  assert.equal(parsed.stateChangesPerformed, false);
  assert.equal(parsed.incidentsCreated, 0);

  assert.throws(
    () =>
      module.parseTemporalIncidentReviewResponse(
        temporalResponse({ incidents_created: false }),
      ),
    /읽기 전용/,
  );
  assert.throws(
    () =>
      module.parseTemporalIncidentReviewResponse(temporalResponse(), {
        detectionIds: ["another-frame"],
      }),
    /요청하지 않은/,
  );
});

test("같은 사건은 대표 질문 하나로 정렬하되 전체 근거 질문을 보존한다", () => {
  const parsed = module.parseTemporalIncidentReviewResponse(
    temporalResponse({
      proposals: [
        {
          ...temporalResponse().proposals[0],
          start_seconds: 1,
          end_seconds: 2,
          duration_seconds: 1,
          detection_count: 2,
          evidence_detection_ids: ["frame-001", "frame-002"],
          peak_evidence: {
            ...temporalResponse().proposals[0].peak_evidence,
            detection_id: "frame-002",
            timestamp_seconds: 2,
          },
          merge_reasons: [
            "bounded_gap_without_comparable_spatial_hint",
          ],
          merge_links: [
            {
              from_detection_id: "frame-001",
              to_detection_id: "frame-002",
              gap_seconds: 1,
              reason: "bounded_gap_without_comparable_spatial_hint",
              iou: null,
              center_distance: null,
            },
          ],
        },
      ],
    }),
    {
      projectId: PROJECT_ID,
      detectionIds: ["frame-001", "frame-002"],
    },
  );
  const presentation = module.buildTemporalQuestionOrder(
    ["q-1", "q-2", "scene-q"],
    { "frame-001": "q-1", "frame-002": "q-2" },
    parsed,
  );
  assert.deepEqual(presentation.orderedQuestionIds, ["q-2", "scene-q"]);
  assert.deepEqual(
    presentation.groupsByRepresentativeId["q-2"].evidenceQuestionIds,
    ["q-1", "q-2"],
  );
  assert.equal(
    presentation.groupsByRepresentativeId["q-2"].peakEvidenceIndex,
    1,
  );
});

test("검증 요청 builder는 12개 유형만 허용하고 alias를 표준화한다", () => {
  const request = module.buildModelValidationReviewRequest({
    projectId: PROJECT_ID,
    modelVersion: "road-model@0.1.0",
    truthRecords: [
      {
        sampleId: "sample-1",
        issueType: "sign_identity",
        truthEventIds: [],
      },
    ],
    predictionRecords: [],
  });
  assert.equal(request.truthRecords[0].issueType, "guide_sign_identity");
  assert.throws(
    () =>
      module.buildModelValidationReviewRequest({
        projectId: PROJECT_ID,
        modelVersion: "road-model@0.1.0",
        truthRecords: [
          { sampleId: "sample-1", issueType: "other", truthEventIds: [] },
        ],
        predictionRecords: [],
      }),
    /표준 12개/,
  );
});

test("모델 검증 parser는 표준 12개 지표와 0 분모 비율을 보존한다", () => {
  const value = {
    schema_version: "trafficvisson.model-validation-review-api.v1",
    project_id: PROJECT_ID,
    computation_mode: "pure_review_only",
    report: report("road-model@0.1.0"),
    state_changes_performed: false,
    annotations_created: 0,
    training_members_created: 0,
    model_versions_changed: 0,
  };
  const parsed = module.parseModelValidationReviewResponse(value, {
    projectId: PROJECT_ID,
    modelVersion: "road-model@0.1.0",
  });
  assert.equal(parsed.report.perType.length, 12);
  assert.equal(parsed.report.perType[0].precision, 0);
  assert.equal(parsed.modelVersionsChanged, 0);

  const malformed = structuredClone(value);
  malformed.report.per_type[0].precision = 0.1;
  assert.throws(
    () => module.parseModelValidationReviewResponse(malformed),
    /집계와 비율/,
  );
});

test("승격 검토 parser는 eligible여도 자동 승격을 절대 허용하지 않는다", () => {
  const eligiblePothole = metric("pothole", {
    validation_sample_count: 30,
    positive_sample_count: 30,
    truth_event_count: 30,
    prediction_event_count: 30,
    true_positive_count: 30,
    precision: 1,
    recall: 1,
  });
  const active = report("active@1.0.0");
  const candidate = report("candidate@2.0.0");
  active.per_type[0] = eligiblePothole;
  candidate.per_type[0] = eligiblePothole;
  const value = {
    schema_version: "trafficvisson.model-promotion-review-api.v1",
    project_id: PROJECT_ID,
    computation_mode: "pure_review_only",
    decision: {
      schema_version: "trafficvisson.promotion-review-policy.v1",
      decision: "eligible",
      active_model_version: "active@1.0.0",
      candidate_model_version: "candidate@2.0.0",
      validation_set_sha256: "e".repeat(64),
      required_issue_types: ["pothole"],
      minimum_samples_per_type: 30,
      maximum_recall_drop: 0.02,
      active_report: active,
      candidate_report: candidate,
      reasons: [],
      auto_promotion_allowed: false,
      requires_user_review: true,
    },
    state_changes_performed: false,
    annotations_created: 0,
    training_members_created: 0,
    model_versions_changed: 0,
    automatic_promotion_performed: false,
  };
  const parsed = module.parseModelPromotionReviewResponse(value, {
    projectId: PROJECT_ID,
    activeModelVersion: "active@1.0.0",
    candidateModelVersion: "candidate@2.0.0",
  });
  assert.equal(parsed.decision.decision, "eligible");
  assert.equal(parsed.decision.autoPromotionAllowed, false);
  assert.equal(parsed.decision.requiresUserReview, true);
  assert.equal(parsed.automaticPromotionPerformed, false);

  const unsafe = structuredClone(value);
  unsafe.automatic_promotion_performed = 0;
  assert.throws(
    () => module.parseModelPromotionReviewResponse(unsafe),
    /읽기 전용/,
  );
});

test("승격 검토 parser는 예측 비율이 낮아져도 FP·중복 절대 증가를 거부한다", () => {
  const active = report("active@1.0.0");
  const candidate = report("candidate@2.0.0");
  active.per_type[0] = metric("pothole", {
    validation_sample_count: 30,
    positive_sample_count: 30,
    truth_event_count: 30,
    prediction_event_count: 6,
    true_positive_count: 5,
    false_positive_count: 1,
    false_negative_count: 25,
    duplicate_prediction_count: 1,
    precision: 5 / 6,
    recall: 5 / 30,
    false_positive_rate: 1 / 6,
    duplicate_burden: 1 / 6,
  });
  candidate.per_type[0] = metric("pothole", {
    validation_sample_count: 30,
    positive_sample_count: 30,
    truth_event_count: 30,
    prediction_event_count: 32,
    true_positive_count: 30,
    false_positive_count: 2,
    duplicate_prediction_count: 2,
    precision: 30 / 32,
    recall: 1,
    false_positive_rate: 2 / 32,
    duplicate_burden: 2 / 32,
  });
  const value = {
    schema_version: "trafficvisson.model-promotion-review-api.v1",
    project_id: PROJECT_ID,
    computation_mode: "pure_review_only",
    decision: {
      schema_version: "trafficvisson.promotion-review-policy.v1",
      decision: "rejected",
      active_model_version: "active@1.0.0",
      candidate_model_version: "candidate@2.0.0",
      validation_set_sha256: "e".repeat(64),
      required_issue_types: ["pothole"],
      minimum_samples_per_type: 30,
      maximum_recall_drop: 0.02,
      active_report: active,
      candidate_report: candidate,
      reasons: [
        {
          code: "false_positive_rate_increased",
          issue_type: "pothole",
          message: "pothole: false-positive per-validation-sample burden increased",
          active_value: 1 / 30,
          candidate_value: 2 / 30,
          limit_value: 1 / 30,
        },
        {
          code: "duplicate_burden_increased",
          issue_type: "pothole",
          message: "pothole: duplicate per-validation-sample burden increased",
          active_value: 1 / 30,
          candidate_value: 2 / 30,
          limit_value: 1 / 30,
        },
      ],
      auto_promotion_allowed: false,
      requires_user_review: true,
    },
    state_changes_performed: false,
    annotations_created: 0,
    training_members_created: 0,
    model_versions_changed: 0,
    automatic_promotion_performed: false,
  };
  const parsed = module.parseModelPromotionReviewResponse(value);
  assert.equal(parsed.decision.decision, "rejected");
  assert.deepEqual(
    parsed.decision.reasons.map(({ code }) => code),
    ["false_positive_rate_increased", "duplicate_burden_increased"],
  );
});

test("라벨링 화면은 검증된 AI 제안만 시간축 병합하고 실패 시 원래 큐를 유지한다", () => {
  assert.match(
    labelingSource,
    /!item\.experimental\s*\|\|\s*item\.confidence === null\s*\|\|\s*!issueType/,
  );
  assert.match(
    labelingSource,
    /service\.temporalIncidents\(request\)[\s\S]*parseTemporalIncidentReviewResponse/,
  );
  assert.match(
    labelingSource,
    /buildTemporalQuestionOrder\([\s\S]*orderedQuestionIds:\s*null,[\s\S]*기존 질문 순서를 유지/,
  );
  assert.match(labelingSource, /temporalGroup\.detectionCount/);
  assert.match(labelingSource, /temporalGroup\.startSeconds/);
  assert.doesNotMatch(
    labelingSource,
    /reviewComputations\.(?:createIncident|createAnnotation|train|promote)/,
  );
});
