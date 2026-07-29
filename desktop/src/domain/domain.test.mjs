import test, { after } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const desktopRoot = fileURLToPath(new URL("../..", import.meta.url));
const vite = await createServer({
  configFile: false,
  root: desktopRoot,
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
});
const domain = await vite.ssrLoadModule("/src/domain/index.ts");

after(async () => {
  await vite.close();
});

const {
  DEMO_PROJECT_ID,
  convertAnswerToAnnotations,
  datasetBelongsToProject,
  demoAcceptedAnswer,
  demoActiveModel,
  demoCandidateModel,
  demoDatasetVersion,
  demoDomainFixture,
  demoExportArtifacts,
  demoGpsGaps,
  demoLaneQuestion,
  demoManifest,
  demoProject,
  demoQuestion,
  demoTrainingGateInput,
  evaluateModelPromotion,
  evaluateProjectIsolation,
  evaluateTrainingGate,
  getMandatoryQuestionReasons,
  isMandatoryQuestion,
  severityRank,
  shouldQueueQuestion,
} = domain;

test("demo fixture keeps its core records inside one project boundary", () => {
  const references = [
    {
      entityType: "Trip",
      entityId: demoDomainFixture.trip.tripId,
      projectId: demoDomainFixture.trip.projectId,
    },
    {
      entityType: "Incident",
      entityId: demoDomainFixture.incident.incidentId,
      projectId: demoDomainFixture.incident.projectId,
    },
    {
      entityType: "DatasetVersion",
      entityId: demoDomainFixture.datasetVersion.datasetVersionId,
      projectId: demoDomainFixture.datasetVersion.projectId,
    },
    ...demoDomainFixture.questions.map((question) => ({
      entityType: "LabelQuestion",
      entityId: question.questionId,
      projectId: question.projectId,
    })),
    ...demoDomainFixture.models.map((model) => ({
      entityType: "ModelVersion",
      entityId: model.modelVersionId,
      projectId: model.projectId,
    })),
  ];

  assert.deepEqual(
    evaluateProjectIsolation(DEMO_PROJECT_ID, references),
    {
      ok: true,
      expectedProjectId: DEMO_PROJECT_ID,
      violations: [],
    },
  );

  const result = evaluateProjectIsolation(DEMO_PROJECT_ID, [
    ...references,
    {
      entityType: "Annotation",
      entityId: "annotation-from-another-project",
      projectId: "project-busan-2026",
    },
  ]);

  assert.equal(result.ok, false);
  assert.equal(result.violations.length, 1);
  assert.equal(result.violations[0].entityId, "annotation-from-another-project");
});

test("road debris and urgent proposals are mandatory in every question mode", () => {
  assert.equal(isMandatoryQuestion(demoQuestion), true);
  assert.deepEqual(
    new Set(getMandatoryQuestionReasons(demoQuestion)),
    new Set(["ROAD_DEBRIS", "URGENT_SEVERITY"]),
  );

  const urgentLaneQuestion = {
    ...demoLaneQuestion,
    proposedSeverity: "URGENT",
    policyMode: "REVIEW_UNCERTAIN",
    confidence: 0.99,
  };

  assert.equal(isMandatoryQuestion(urgentLaneQuestion), true);
  assert.equal(
    shouldQueueQuestion(urgentLaneQuestion, {
      policyMode: "REVIEW_UNCERTAIN",
      reviewedCountForClass: 1_000,
      recentAgreementRate: 1,
      confidenceThreshold: 0.7,
      modelDisagreement: false,
      newCaptureEnvironment: false,
      qualitySampleSelected: false,
    }),
    true,
  );
});

test("assisted-learning queue policy asks initial, uncertain and sampled cases", () => {
  const stableContext = {
    policyMode: "ASSISTED_LEARNING",
    reviewedCountForClass: 45,
    recentAgreementRate: 0.93,
    confidenceThreshold: 0.7,
    modelDisagreement: false,
    newCaptureEnvironment: false,
    qualitySampleSelected: false,
  };

  assert.equal(shouldQueueQuestion(demoLaneQuestion, stableContext), false);
  assert.equal(
    shouldQueueQuestion(demoLaneQuestion, {
      ...stableContext,
      reviewedCountForClass: 29,
    }),
    true,
  );
  assert.equal(
    shouldQueueQuestion(
      { ...demoLaneQuestion, confidence: 0.61 },
      stableContext,
    ),
    true,
  );
  assert.equal(
    shouldQueueQuestion(demoLaneQuestion, {
      ...stableContext,
      qualitySampleSelected: true,
    }),
    true,
  );
});

test("accepted edited answer creates distinct geometry, condition and maintenance labels", () => {
  const bundle = convertAnswerToAnnotations(
    demoQuestion,
    demoAcceptedAnswer,
  );

  assert.equal(bundle.excludedFromTraining, false);
  assert.equal(bundle.projectId, DEMO_PROJECT_ID);
  assert.equal(bundle.artifacts.length, 3);

  const geometry = bundle.artifacts.find(
    (artifact) => "annotationId" in artifact,
  );
  const condition = bundle.artifacts.find(
    (artifact) => "assessmentId" in artifact,
  );
  const maintenance = bundle.artifacts.find(
    (artifact) => "decisionId" in artifact,
  );

  assert.ok(geometry);
  assert.equal(geometry.verdict, "POSITIVE");
  assert.deepEqual(geometry.geometry, demoAcceptedAnswer.finalGeometry);
  assert.equal(geometry.source, "USER_EDITED");

  assert.ok(condition);
  assert.equal(condition.condition, "OBSTRUCTION");
  assert.equal(condition.outcome, "PRESENT");
  assert.equal(condition.severity, "URGENT");

  assert.ok(maintenance);
  assert.equal(maintenance.required, true);
  assert.equal(
    maintenance.action,
    "즉시 도로 순찰대 출동 및 낙하물 제거",
  );
});

test("rejection becomes hard-negative labels while unsure is excluded", () => {
  const rejectedAnswer = {
    ...demoAcceptedAnswer,
    answerId: "answer-road-debris-rejected",
    response: "REJECT",
    finalGeometry: undefined,
    selectedSeverity: undefined,
    selectedAction: undefined,
    note: "도로 표면의 그림자로 확인",
    updatedAt: "2026-07-18T01:30:00.000Z",
  };
  const rejectedBundle = convertAnswerToAnnotations(
    demoQuestion,
    rejectedAnswer,
  );
  const negativeGeometry = rejectedBundle.artifacts.find(
    (artifact) => "annotationId" in artifact,
  );
  const absentCondition = rejectedBundle.artifacts.find(
    (artifact) => "assessmentId" in artifact,
  );
  const noMaintenance = rejectedBundle.artifacts.find(
    (artifact) => "decisionId" in artifact,
  );

  assert.ok(negativeGeometry);
  assert.equal(negativeGeometry.verdict, "NEGATIVE");
  assert.equal(negativeGeometry.geometry, undefined);
  assert.equal(negativeGeometry.source, "USER_NEGATIVE");
  assert.ok(absentCondition);
  assert.equal(absentCondition.outcome, "ABSENT");
  assert.ok(noMaintenance);
  assert.equal(noMaintenance.required, false);

  const unsureBundle = convertAnswerToAnnotations(demoQuestion, {
    ...demoAcceptedAnswer,
    answerId: "answer-road-debris-unsure",
    response: "UNSURE",
    includeInTraining: true,
  });

  assert.equal(unsureBundle.excludedFromTraining, true);
  assert.equal(unsureBundle.exclusionReason, "UNSURE");
  assert.deepEqual(unsureBundle.artifacts, []);
});

test("answer conversion refuses cross-project or wrong-question input", () => {
  assert.throws(
    () =>
      convertAnswerToAnnotations(demoQuestion, {
        ...demoAcceptedAnswer,
        projectId: "project-from-another-workspace",
      }),
    /Project mismatch/,
  );

  assert.throws(
    () =>
      convertAnswerToAnnotations(demoQuestion, {
        ...demoAcceptedAnswer,
        questionId: "different-question",
      }),
    /Question mismatch/,
  );
});

test("training gate requires project isolation and minimum label evidence", () => {
  const ready = evaluateTrainingGate(demoTrainingGateInput);
  assert.equal(ready.canTrain, true);
  assert.deepEqual(ready.reasons, []);

  const blocked = evaluateTrainingGate({
    ...demoTrainingGateInput,
    newConfirmedLabelCount: 99,
    datasetProjectId: "project-from-another-workspace",
    validationSamplesByClass: {
      ...demoTrainingGateInput.validationSamplesByClass,
      ROAD_DEBRIS: 29,
    },
  });

  assert.equal(blocked.canTrain, false);
  assert.deepEqual(
    new Set(blocked.reasons.map((reason) => reason.code)),
    new Set([
      "INSUFFICIENT_NEW_LABELS",
      "INSUFFICIENT_VALIDATION_SAMPLES",
      "PROJECT_MISMATCH",
    ]),
  );
});

test("model promotion passes improvements and blocks 2pp recall regression", () => {
  const goodDecision = evaluateModelPromotion(
    demoActiveModel,
    demoCandidateModel,
  );
  assert.equal(goodDecision.canPromote, true);
  assert.deepEqual(goodDecision.blocks, []);

  const regressedCandidate = {
    ...demoCandidateModel,
    modelVersionId: "model-road-v3.5.0-regressed",
    classMetrics: demoCandidateModel.classMetrics.map((metric) =>
      metric.incidentType === "POTHOLE"
        ? {
            ...metric,
            recall: demoActiveModel.classMetrics[0].recall - 0.02,
            duplicateRate:
              demoActiveModel.classMetrics[0].duplicateRate + 0.001,
          }
        : metric,
    ),
  };
  const blockedDecision = evaluateModelPromotion(
    demoActiveModel,
    regressedCandidate,
  );

  assert.equal(blockedDecision.canPromote, false);
  assert.equal(
    blockedDecision.blocks.some(
      (block) =>
        block.code === "RECALL_REGRESSION" &&
        block.incidentType === "POTHOLE",
    ),
    true,
  );
  assert.equal(
    blockedDecision.blocks.some(
      (block) =>
        block.code === "DUPLICATE_RATE_INCREASE" &&
        block.incidentType === "POTHOLE",
    ),
    true,
  );
});

test("dataset, route-gap and export fixtures preserve the master-plan contract", () => {
  assert.equal(
    datasetBelongsToProject(demoDatasetVersion, demoProject.projectId),
    true,
  );
  assert.equal(
    demoDatasetVersion.split.validationAnnotationIds.length,
    60,
  );
  assert.equal(demoDatasetVersion.split.testAnnotationIds.length, 100);
  assert.equal(demoGpsGaps[0].renderMode, "DASHED_WARNING");

  const expectedFiles = new Set([
    "raw_detections.csv",
    "reviewed_incidents.csv",
    "label_questions.csv",
    "annotations.csv",
    "ocr_observations.csv",
    "route_points.csv",
    "maintenance_actions.csv",
    "report_payload.json",
    "manifest.json",
  ]);
  assert.deepEqual(
    new Set(demoExportArtifacts.map((artifact) => artifact.fileName)),
    expectedFiles,
  );
  assert.deepEqual(
    new Set(demoManifest.exportArtifactIds),
    new Set(
      demoExportArtifacts.map((artifact) => artifact.exportArtifactId),
    ),
  );
});

test("severity ordering is deterministic", () => {
  assert.deepEqual(
    ["LOW", "MEDIUM", "HIGH", "URGENT"].map(severityRank),
    [0, 1, 2, 3],
  );
});
