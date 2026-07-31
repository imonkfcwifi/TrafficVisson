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
const provider = await vite.ssrLoadModule(
  "/src/providerVisualEvidence.ts",
);
const componentSource = fs.readFileSync(
  path.join(directory, "components", "ProviderVisualEvidenceReview.tsx"),
  "utf8",
);
const importSource = fs.readFileSync(
  path.join(directory, "screens", "ImportScreen.tsx"),
  "utf8",
);
const labelingSource = fs.readFileSync(
  path.join(directory, "screens", "LabelingScreen.tsx"),
  "utf8",
);
const shellSource = fs.readFileSync(
  path.join(directory, "components", "AppShell.tsx"),
  "utf8",
);

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const JOB_ID = "22222222-2222-4222-8222-222222222222";
const ANALYSIS_JOB_ID = "33333333-3333-4333-8333-333333333333";
const CANDIDATE_ID = "c".repeat(20);
const PREPARATION_ID = "d".repeat(24);

function preparation() {
  return {
    schema_version:
      "trafficvisson.provider-visual-evidence-preparation.v1",
    project_id: PROJECT_ID,
    pipeline_job_id: JOB_ID,
    analysis_job_id: ANALYSIS_JOB_ID,
    candidate_id: CANDIDATE_ID,
    preparation_id: PREPARATION_ID,
    input_revision: "1".repeat(64),
    state: "awaiting_deidentification_review",
    manifest_sha256: "2".repeat(64),
    engine_identity_sha256: "3".repeat(64),
    attachment_count: 4,
    attachments: [
      {
        order: 0,
        role: "candidate_representative",
        source_sha256: "4".repeat(64),
        output_sha256: "5".repeat(64),
        media_type: "image/png",
        size_bytes: 1000,
        width: 320,
        height: 180,
        face_detection_count: 1,
        plate_detection_count: 2,
      },
      ...[1, 2, 3].map((order) => ({
        order,
        role: "similar_case",
        source_sha256: ["6", "7", "8"][order - 1].repeat(64),
        output_sha256: ["9", "a", "b"][order - 1].repeat(64),
        media_type: "image/png",
        size_bytes: 800 + order,
        width: 240,
        height: 160,
        face_detection_count: 0,
        plate_detection_count: 0,
      })),
    ],
    automatic_redaction_performed: true,
    automatic_privacy_verification: false,
    human_verified: false,
    review_required: true,
    review_record_sha256: null,
    preparation_revision: "c".repeat(64),
  };
}

test.after(async () => {
  await vite.close();
});

test("renderer parser accepts only the minimized path-free preparation contract", () => {
  const parsed = provider.parseProviderVisualEvidencePreparation(
    preparation(),
  );
  assert.equal(parsed.state, "awaiting_deidentification_review");
  assert.equal(parsed.attachments.length, 4);
  assert.equal(parsed.attachments[0].role, "candidate_representative");
  assert.equal(parsed.automaticPrivacyVerification, false);
  assert.doesNotMatch(JSON.stringify(parsed), /path|source_ref/i);

  assert.throws(
    () =>
      provider.parseProviderVisualEvidencePreparation({
        ...preparation(),
        original_path: "C:\\private\\frame.png",
      }),
    /응답 형식/,
  );
  assert.throws(
    () =>
      provider.parseProviderVisualEvidencePreparation({
        ...preparation(),
        automatic_privacy_verification: true,
      }),
    /자동 개인정보 검증/,
  );
  assert.throws(
    () =>
      provider.parseProviderVisualEvidencePreparation({
        ...preparation(),
        attachments: preparation().attachments.slice(0, 3),
        attachment_count: 3,
      }),
    /응답 형식/,
  );
});

test("renderer list, artifact and explicit privacy-review action fail closed", () => {
  const parsed = provider.parseProviderVisualEvidenceList({
    preparations: [preparation()],
  });
  assert.equal(parsed.length, 1);
  const request =
    provider.buildProviderVisualEvidencePrivacyReviewRequest(
      parsed[0],
      true,
    );
  assert.deepEqual(request, {
    projectId: PROJECT_ID,
    jobId: JOB_ID,
    candidateId: CANDIDATE_ID,
    preparationId: PREPARATION_ID,
    expectedManifestSha256: "2".repeat(64),
    approved: true,
    explicitUserConfirmation: true,
  });
  assert.throws(
    () =>
      provider.parseProviderVisualEvidenceList({
        preparations: [preparation()],
        path: "C:\\private",
      }),
    /응답 형식/,
  );
  const bytes = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]);
  assert.deepEqual(
    provider.parseProviderVisualEvidenceArtifact({
      media_type: "image/png",
      sha256: "d".repeat(64),
      bytes,
    }),
    {
      mediaType: "image/png",
      sha256: "d".repeat(64),
      bytes,
    },
  );
  assert.throws(
    () =>
      provider.parseProviderVisualEvidenceArtifact({
        media_type: "image/png",
        sha256: "d".repeat(64),
        bytes,
        localPath: "C:\\private\\crop.png",
      }),
    /응답 형식/,
  );
});

test("only the exact vision-proposal privacy block becomes the review interruption", () => {
  const interrupted = {
    status: "blocked",
    currentStage: "vision_proposal",
    outcomeCode: "partial",
    errorCode: "awaiting_deidentification_review",
  };
  assert.equal(
    provider.isProviderVisualPrivacyReviewInterruption(interrupted),
    true,
  );
  assert.equal(
    provider.isProviderVisualPrivacyReviewInterruption({
      ...interrupted,
      errorCode: "structured_llm_failed",
    }),
    false,
  );
  assert.equal(
    provider.isProviderVisualPrivacyReviewInterruption({
      ...interrupted,
      currentStage: "derived_outputs",
    }),
    false,
  );
});

test("Import and Labeling show one batch interruption and the shell routes to it", () => {
  assert.match(
    componentSource,
    /providerVisualEvidence[\s\S]*\.list\(/,
  );
  assert.match(
    componentSource,
    /\.get\([\s\S]*\.artifact\([\s\S]*\.review\(/,
  );
  assert.match(
    componentSource,
    /buildProviderVisualEvidencePrivacyReviewRequest/,
  );
  assert.match(
    componentSource,
    /자동 가림은 검토 제안일 뿐 개인정보 안전을 자동으로[\s\S]*보장하지 않습니다/,
  );
  assert.match(componentSource, /후보 1장과 3~5개 유사 사례/);
  assert.doesNotMatch(
    componentSource,
    /sourcePath|originalPath|rawVideo|authToken|shell|\bCOM\b/,
  );
  assert.match(
    importSource,
    /isProviderVisualPrivacyReviewInterruption\(currentPipeline\)/,
  );
  assert.match(
    labelingSource,
    /isProviderVisualPrivacyReviewInterruption\(currentPipelineJob\)/,
  );
  assert.match(
    shellSource,
    /isProviderVisualPrivacyReviewInterruption\(job\)[\s\S]*onScreenChange\("label"\)/,
  );
  assert.match(
    importSource,
    /비식별 후보·유사 사례 crop은[\s\S]*Codex에만 전송 가능[\s\S]*Claude는 이미지 없이 메타데이터만/,
  );
});
