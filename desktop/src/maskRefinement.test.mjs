import assert from "node:assert/strict";
import test from "node:test";
import { createServer } from "vite";

const vite = await createServer({
  server: { middlewareMode: true },
  appType: "custom",
  logLevel: "silent",
});
const refinement = await vite.ssrLoadModule("/src/maskRefinement.ts");

const expected = {
  projectId: "11111111-1111-4111-8111-111111111111",
  jobId: "22222222-2222-4222-8222-222222222222",
  questionId: "44444444-4444-4444-8444-444444444441",
  candidateId: "0123456789abcdefabcd",
};
const manifest = {
  pack_id: "sam2.1-hiera-small",
  pack_version: "1.0.0",
  manifest_sha256: "a".repeat(64),
};

function identity() {
  return {
    project_id: expected.projectId,
    analysis_job_id: expected.jobId,
    question_id: expected.questionId,
    candidate_id: expected.candidateId,
  };
}

test("candidate question UUIDs remain scoped to the exact analysis response", () => {
  assert.deepEqual(
    refinement.candidateQuestionIds(
      {
        project_id: expected.projectId,
        analysis_job_id: expected.jobId,
        questions: [
          {
            candidate_id: expected.candidateId,
            question_id: expected.questionId,
            ignored: "server-owned candidate content",
          },
        ],
      },
      {
        projectId: expected.projectId,
        jobId: expected.jobId,
        candidateIds: [expected.candidateId],
      },
    ),
    { [expected.candidateId]: expected.questionId },
  );
  assert.deepEqual(
    refinement.candidateQuestionIds(
      {
        project_id: "99999999-9999-4999-8999-999999999999",
        analysis_job_id: expected.jobId,
        questions: [],
      },
      {
        projectId: expected.projectId,
        jobId: expected.jobId,
        candidateIds: [expected.candidateId],
      },
    ),
    {},
  );
});

test("capability parser keeps ready and model-unavailable distinct", () => {
  assert.deepEqual(
    refinement.parseMaskRefinementCapability(
      {
        schema_version:
          "trafficvisson.sam2-mask-refinement-capability.v1",
        ...identity(),
        state: "ready",
        error_code: null,
        message: "ready",
        model_manifest: manifest,
        click_prompt_enabled: true,
      },
      expected,
    ),
    {
      state: "ready",
      message: "ready",
      modelIdentity: "sam2.1-hiera-small@1.0.0",
      manifestSha256: "a".repeat(64),
    },
  );
  assert.deepEqual(
    refinement.parseMaskRefinementCapability(
      {
        schema_version:
          "trafficvisson.sam2-mask-refinement-capability.v1",
        ...identity(),
        state: "model_unavailable",
        error_code: "model_pack_not_configured",
        message: "모델팩 필요",
        model_manifest: null,
        click_prompt_enabled: false,
      },
      expected,
    ),
    {
      state: "model_unavailable",
      message: "모델팩 필요",
      errorCode: "model_pack_not_configured",
    },
  );
});

test("successful refinement is bounded, path-free, and confirmation-only", () => {
  const parsed = refinement.parseMaskRefinementProposal(
    {
      schema_version: "trafficvisson.sam2-mask-refinement.v1",
      ...identity(),
      status: "refined",
      error_code: null,
      message: "사용자 확인 필요",
      refinement_id: "smr_0123456789abcdef01234567",
      click: { x: 0.5, y: 0.6 },
      polygon: [
        [0.2, 0.3],
        [0.7, 0.3],
        [0.7, 0.8],
      ],
      normalized_box: { x: 0.2, y: 0.3, width: 0.5, height: 0.5 },
      mask_artifact_ref: "sam2mask_0123456789abcdef01234567",
      tracked_frame_refs: ["track_before_01"],
      input_sha256: "b".repeat(64),
      output_sha256: "c".repeat(64),
      model_manifest: manifest,
      requires_user_confirmation: true,
      automatic_annotation_allowed: false,
      automatic_incident_creation_allowed: false,
      training_membership_allowed: false,
    },
    expected,
  );
  assert.equal(parsed.status, "refined");
  assert.equal(parsed.proposal.refinementId, "smr_0123456789abcdef01234567");
  assert.equal(parsed.proposal.modelIdentity, "sam2.1-hiera-small@1.0.0");
  assert.equal(JSON.stringify(parsed).includes("C:\\private"), false);
});

test("parser rejects path leakage, identity swaps, and approval claims", () => {
  const valid = {
    schema_version: "trafficvisson.sam2-mask-refinement.v1",
    ...identity(),
    status: "refined",
    error_code: null,
    message: "사용자 확인 필요",
    refinement_id: "smr_0123456789abcdef01234567",
    click: { x: 0.5, y: 0.6 },
    polygon: [
      [0.2, 0.3],
      [0.7, 0.3],
      [0.7, 0.8],
    ],
    normalized_box: { x: 0.2, y: 0.3, width: 0.5, height: 0.5 },
    mask_artifact_ref: "sam2mask_0123456789abcdef01234567",
    tracked_frame_refs: [],
    input_sha256: "b".repeat(64),
    output_sha256: "c".repeat(64),
    model_manifest: manifest,
    requires_user_confirmation: true,
    automatic_annotation_allowed: false,
    automatic_incident_creation_allowed: false,
    training_membership_allowed: false,
  };
  assert.throws(
    () =>
      refinement.parseMaskRefinementProposal(
        { ...valid, image_path: "C:\\private\\candidate.jpg" },
        expected,
      ),
    /검증하지 못했습니다/,
  );
  assert.throws(
    () =>
      refinement.parseMaskRefinementProposal(
        { ...valid, project_id: "99999999-9999-4999-8999-999999999999" },
        expected,
      ),
    /검증하지 못했습니다/,
  );
  assert.throws(
    () =>
      refinement.parseMaskRefinementProposal(
        { ...valid, automatic_annotation_allowed: true },
        expected,
      ),
    /검증하지 못했습니다/,
  );
});

test.after(async () => {
  await vite.close();
});

