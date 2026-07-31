import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { createServer } from "vite";

const directory = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(directory, "..");
const vite = await createServer({
  root,
  logLevel: "silent",
  server: { middlewareMode: true },
  appType: "custom",
});
const reportEvidence = await vite.ssrLoadModule(
  "/src/reportDeidentifiedEvidence.ts",
);
const componentSource = fs.readFileSync(
  path.join(
    directory,
    "components",
    "ReportDeidentifiedEvidenceReview.tsx",
  ),
  "utf8",
);
const reportScreenSource = fs.readFileSync(
  path.join(directory, "screens", "ReportScreen.tsx"),
  "utf8",
);

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const JOB_ID = "22222222-2222-4222-8222-222222222222";
const ANALYSIS_JOB_ID = "33333333-3333-4333-8333-333333333333";
const INCIDENT_ID = "44444444-4444-4444-8444-444444444444";
const CANDIDATE_ID = "c".repeat(20);
const DERIVATIVE_ID = `rde_${"d".repeat(32)}`;

function preparation() {
  return {
    schemaVersion:
      "trafficvisson.report-deidentified-evidence.v1",
    projectId: PROJECT_ID,
    pipelineJobId: JOB_ID,
    analysisJobId: ANALYSIS_JOB_ID,
    sourceSha256: "1".repeat(64),
    incidentId: INCIDENT_ID,
    candidateId: CANDIDATE_ID,
    derivativeId: DERIVATIVE_ID,
    deidentifiedRef: `report-deid:${INCIDENT_ID.replaceAll("-", "")}:${DERIVATIVE_ID.slice(4)}`,
    state: "awaiting_deidentification_review",
    inputRevision: "2".repeat(64),
    manifestSha256: "3".repeat(64),
    originalSha256: "4".repeat(64),
    deidentifiedSha256: "5".repeat(64),
    mediaType: "image/jpeg",
    sizeBytes: 2048,
    width: 640,
    height: 360,
    detector: {
      detectorName: "opencv_haar_report_privacy_proposal",
      detectorVersion: "4.10.0",
      modelId: "opencv-haar-face-license-plate-cascade-set",
      modelSha256: "6".repeat(64),
      coverageGuaranteed: false,
      humanReviewRequired: true,
    },
    detectorIdentitySha256: "7".repeat(64),
    redactionRegions: [
      {
        kind: "face",
        x: 100,
        y: 60,
        width: 80,
        height: 90,
      },
      {
        kind: "license_plate",
        x: 240,
        y: 260,
        width: 120,
        height: 32,
      },
    ],
    redactionRegionCount: 2,
    faceDetectionCount: 1,
    plateDetectionCount: 1,
    automaticRedactionPerformed: true,
    automaticPrivacyVerification: false,
    humanVerified: false,
    reviewRequired: true,
    reviewerDecision: null,
    reviewerActor: null,
    reviewedAt: null,
    reviewRevision: 0,
    reviewRecordSha256: null,
    deidentificationRecordSha256: null,
    preparationRevision: "8".repeat(64),
  };
}

test.after(async () => {
  await vite.close();
});

test("renderer accepts only a path-free human-review-required derivative", () => {
  const parsed =
    reportEvidence.parseReportDeidentifiedPreparation(preparation());
  assert.equal(parsed.state, "awaiting_deidentification_review");
  assert.equal(parsed.redactionRegionCount, 2);
  assert.equal(parsed.automaticPrivacyVerification, false);
  assert.equal(parsed.humanVerified, false);
  assert.doesNotMatch(JSON.stringify(parsed), /[A-Z]:\\|original_path/i);

  assert.throws(
    () =>
      reportEvidence.parseReportDeidentifiedPreparation({
        ...preparation(),
        originalPath: "C:\\private\\frame.jpg",
      }),
    /응답 형식/,
  );
  assert.throws(
    () =>
      reportEvidence.parseReportDeidentifiedPreparation({
        ...preparation(),
        automaticPrivacyVerification: true,
      }),
    /응답 형식/,
  );
});

test("list, JPEG and explicit decision helpers fail closed", () => {
  const listed =
    reportEvidence.parseReportDeidentifiedEvidenceList({
      schemaVersion:
        "trafficvisson.report-deidentified-evidence-list.v1",
      projectId: PROJECT_ID,
      pipelineJobId: JOB_ID,
      analysisJobId: ANALYSIS_JOB_ID,
      state: "prepared_review_required",
      count: 1,
      preparations: [preparation()],
    });
  assert.equal(listed.count, 1);
  assert.deepEqual(
    reportEvidence.buildReportDeidentifiedReviewRequest(
      listed.preparations[0],
      true,
    ),
    {
      projectId: PROJECT_ID,
      jobId: JOB_ID,
      incidentId: INCIDENT_ID,
      derivativeId: DERIVATIVE_ID,
      expectedManifestSha256: "3".repeat(64),
      approved: true,
      explicitUserConfirmation: true,
    },
  );
  const bytes = new Uint8Array([0xff, 0xd8, 0xff, 0xdb]);
  assert.deepEqual(
    reportEvidence.parseReportDeidentifiedArtifact({
      media_type: "image/jpeg",
      sha256: "9".repeat(64),
      bytes,
    }),
    {
      mediaType: "image/jpeg",
      sha256: "9".repeat(64),
      bytes,
    },
  );
});

test("only the exact shared derived-output block becomes report review", () => {
  const interrupted = {
    status: "blocked",
    currentStage: "derived_outputs",
    outcomeCode: "report_preview_blocked",
    errorCode: "deidentified_evidence_required",
    profileSnapshot: {
      reportEvidenceMode: "shared_deidentified",
    },
  };
  assert.equal(
    reportEvidence.isReportDeidentifiedReviewInterruption(interrupted),
    true,
  );
  assert.equal(
    reportEvidence.isReportDeidentifiedReviewInterruption({
      ...interrupted,
      errorCode: "provider_report_preview_validation_failed",
    }),
    false,
  );
  assert.equal(
    reportEvidence.isReportDeidentifiedReviewInterruption({
      ...interrupted,
      profileSnapshot: {
        reportEvidenceMode: "internal_original",
      },
    }),
    false,
  );
});

test("ReportScreen replaces the normal report workbench with one review boundary", () => {
  assert.match(
    reportScreenSource,
    /isReportDeidentifiedReviewInterruption\(job\)/,
  );
  assert.match(
    reportScreenSource,
    /reportDeidentifiedReviewJob \? \([\s\S]*ReportDeidentifiedEvidenceReview[\s\S]*\) : \(/,
  );
  assert.match(
    componentSource,
    /reportDeidentifiedEvidence[\s\S]*\.prepare\([\s\S]*\.artifact\([\s\S]*\.review\(/,
  );
  assert.match(
    componentSource,
    /자동 검지 0건도 안전 판정이 아닙니다/,
  );
  assert.match(
    componentSource,
    /사람의 승인 전에는 HWPX 사진 칸에[\s\S]*삽입되지 않습니다/,
  );
  assert.match(
    componentSource,
    /publishPipelineJob\(response\.pipelineJob\)/,
  );
  assert.doesNotMatch(
    componentSource,
    /originalPath|sourcePath|authToken|shell|\bCOM\b/,
  );
});
