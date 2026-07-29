export type MaskRefinementIdentity = {
  projectId: string;
  jobId: string;
  questionId: string;
  candidateId: string;
};

export type MaskRefinementCapability =
  | {
      state: "ready";
      message: string;
      modelIdentity: string;
      manifestSha256: string;
    }
  | {
      state: "model_unavailable";
      message: string;
      errorCode: "model_pack_not_configured";
    };

export type MaskRefinementProposal = {
  refinementId: string;
  click: { x: number; y: number };
  polygon: Array<[number, number]>;
  normalizedBox: {
    x: number;
    y: number;
    width: number;
    height: number;
  };
  maskArtifactRef: string;
  trackedFrameRefs: string[];
  inputSha256: string;
  outputSha256: string;
  modelIdentity: string;
  manifestSha256: string;
  message: string;
};

const UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const CANDIDATE = /^[a-f0-9]{20}$/;
const REFINEMENT = /^smr_[a-f0-9]{24}$/;
const OPAQUE_REF = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const PACK_ID = /^[a-z0-9][a-z0-9._-]{0,63}$/;
const PACK_VERSION = /^[0-9A-Za-z][0-9A-Za-z._+-]{0,31}$/;
const SHA256 = /^[a-f0-9]{64}$/;

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function exactKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
) {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return (
    actual.length === expected.length &&
    actual.every((key, index) => key === expected[index])
  );
}

function identityMatches(
  row: Record<string, unknown>,
  expected: MaskRefinementIdentity,
) {
  return (
    row.project_id === expected.projectId &&
    row.analysis_job_id === expected.jobId &&
    row.question_id === expected.questionId &&
    row.candidate_id === expected.candidateId
  );
}

function finiteUnit(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isFinite(value) &&
    value >= 0 &&
    value <= 1
  );
}

function modelManifest(value: unknown) {
  const row = record(value);
  if (
    !row ||
    !exactKeys(row, ["pack_id", "pack_version", "manifest_sha256"]) ||
    typeof row.pack_id !== "string" ||
    !PACK_ID.test(row.pack_id) ||
    typeof row.pack_version !== "string" ||
    !PACK_VERSION.test(row.pack_version) ||
    typeof row.manifest_sha256 !== "string" ||
    !SHA256.test(row.manifest_sha256)
  ) {
    return null;
  }
  return {
    modelIdentity: `${row.pack_id}@${row.pack_version}`,
    manifestSha256: row.manifest_sha256,
  };
}

export function candidateQuestionIds(
  value: unknown,
  expected: {
    projectId: string;
    jobId: string;
    candidateIds: readonly string[];
  },
) {
  const root = record(value);
  if (
    !root ||
    root.project_id !== expected.projectId ||
    root.analysis_job_id !== expected.jobId ||
    !Array.isArray(root.questions)
  ) {
    return {};
  }
  const allowed = new Set(
    expected.candidateIds.filter((candidateId) =>
      CANDIDATE.test(candidateId),
    ),
  );
  const result: Record<string, string> = {};
  for (const raw of root.questions) {
    const row = record(raw);
    if (
      !row ||
      typeof row.candidate_id !== "string" ||
      !allowed.has(row.candidate_id) ||
      typeof row.question_id !== "string" ||
      !UUID.test(row.question_id) ||
      result[row.candidate_id]
    ) {
      continue;
    }
    result[row.candidate_id] = row.question_id;
  }
  return result;
}

export function parseMaskRefinementCapability(
  value: unknown,
  expected: MaskRefinementIdentity,
): MaskRefinementCapability {
  const row = record(value);
  if (
    !row ||
    !exactKeys(row, [
      "schema_version",
      "project_id",
      "analysis_job_id",
      "question_id",
      "candidate_id",
      "state",
      "error_code",
      "message",
      "model_manifest",
      "click_prompt_enabled",
    ]) ||
    row.schema_version !==
      "trafficvisson.sam2-mask-refinement-capability.v1" ||
    !identityMatches(row, expected) ||
    typeof row.message !== "string" ||
    row.message.length < 1 ||
    row.message.length > 500
  ) {
    throw new Error("SAM2 영역 수정 상태 응답을 검증하지 못했습니다.");
  }
  if (
    row.state === "model_unavailable" &&
    row.error_code === "model_pack_not_configured" &&
    row.model_manifest === null &&
    row.click_prompt_enabled === false
  ) {
    return {
      state: "model_unavailable",
      message: row.message,
      errorCode: row.error_code,
    };
  }
  const manifest = modelManifest(row.model_manifest);
  if (
    row.state !== "ready" ||
    row.error_code !== null ||
    row.click_prompt_enabled !== true ||
    !manifest
  ) {
    throw new Error("SAM2 영역 수정 상태가 일관되지 않습니다.");
  }
  return {
    state: "ready",
    message: row.message,
    ...manifest,
  };
}

export function parseMaskRefinementProposal(
  value: unknown,
  expected: MaskRefinementIdentity,
):
  | { status: "model_unavailable"; message: string }
  | { status: "refined"; proposal: MaskRefinementProposal } {
  const row = record(value);
  const allowedKeys = [
    "schema_version",
    "project_id",
    "analysis_job_id",
    "question_id",
    "candidate_id",
    "status",
    "error_code",
    "message",
    "refinement_id",
    "click",
    "polygon",
    "normalized_box",
    "mask_artifact_ref",
    "tracked_frame_refs",
    "input_sha256",
    "output_sha256",
    "model_manifest",
    "requires_user_confirmation",
    "automatic_annotation_allowed",
    "automatic_incident_creation_allowed",
    "training_membership_allowed",
  ] as const;
  if (
    !row ||
    !exactKeys(row, allowedKeys) ||
    row.schema_version !== "trafficvisson.sam2-mask-refinement.v1" ||
    !identityMatches(row, expected) ||
    typeof row.message !== "string" ||
    row.message.length < 1 ||
    row.message.length > 500 ||
    row.requires_user_confirmation !== true ||
    row.automatic_annotation_allowed !== false ||
    row.automatic_incident_creation_allowed !== false ||
    row.training_membership_allowed !== false
  ) {
    throw new Error("SAM2 영역 수정 결과를 검증하지 못했습니다.");
  }
  if (
    row.status === "model_unavailable" &&
    row.error_code === "model_pack_not_configured" &&
    row.refinement_id === null &&
    row.polygon === null &&
    row.normalized_box === null &&
    row.mask_artifact_ref === null &&
    row.input_sha256 === null &&
    row.output_sha256 === null &&
    row.model_manifest === null &&
    Array.isArray(row.tracked_frame_refs) &&
    row.tracked_frame_refs.length === 0
  ) {
    return { status: "model_unavailable", message: row.message };
  }
  const click = record(row.click);
  const box = record(row.normalized_box);
  const manifest = modelManifest(row.model_manifest);
  if (
    row.status !== "refined" ||
    row.error_code !== null ||
    typeof row.refinement_id !== "string" ||
    !REFINEMENT.test(row.refinement_id) ||
    !click ||
    !exactKeys(click, ["x", "y"]) ||
    !finiteUnit(click.x) ||
    !finiteUnit(click.y) ||
    !Array.isArray(row.polygon) ||
    row.polygon.length < 3 ||
    row.polygon.length > 256 ||
    !box ||
    !exactKeys(box, ["x", "y", "width", "height"]) ||
    !finiteUnit(box.x) ||
    !finiteUnit(box.y) ||
    !finiteUnit(box.width) ||
    !finiteUnit(box.height) ||
    box.width <= 0 ||
    box.height <= 0 ||
    box.x + box.width > 1 ||
    box.y + box.height > 1 ||
    typeof row.mask_artifact_ref !== "string" ||
    !OPAQUE_REF.test(row.mask_artifact_ref) ||
    !Array.isArray(row.tracked_frame_refs) ||
    row.tracked_frame_refs.length > 32 ||
    !row.tracked_frame_refs.every(
      (item) => typeof item === "string" && OPAQUE_REF.test(item),
    ) ||
    new Set(row.tracked_frame_refs).size !== row.tracked_frame_refs.length ||
    typeof row.input_sha256 !== "string" ||
    !SHA256.test(row.input_sha256) ||
    typeof row.output_sha256 !== "string" ||
    !SHA256.test(row.output_sha256) ||
    !manifest
  ) {
    throw new Error("SAM2 영역 수정 결과의 마스크·출처가 잘못되었습니다.");
  }
  const polygon: Array<[number, number]> = [];
  for (const rawPoint of row.polygon) {
    if (
      !Array.isArray(rawPoint) ||
      rawPoint.length !== 2 ||
      !finiteUnit(rawPoint[0]) ||
      !finiteUnit(rawPoint[1])
    ) {
      throw new Error("SAM2 영역 수정 폴리곤을 검증하지 못했습니다.");
    }
    polygon.push([rawPoint[0], rawPoint[1]]);
  }
  return {
    status: "refined",
    proposal: {
      refinementId: row.refinement_id,
      click: { x: click.x, y: click.y },
      polygon,
      normalizedBox: {
        x: box.x,
        y: box.y,
        width: box.width,
        height: box.height,
      },
      maskArtifactRef: row.mask_artifact_ref,
      trackedFrameRefs: [...row.tracked_frame_refs],
      inputSha256: row.input_sha256,
      outputSha256: row.output_sha256,
      ...manifest,
      message: row.message,
    },
  };
}

