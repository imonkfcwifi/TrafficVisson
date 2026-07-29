const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const CANDIDATE_ID_RE = /^[0-9a-f]{20}$/;

export function buildCandidateEvidenceUrl(
  projectId: string,
  jobId: string,
  candidateId: string,
  kind: "image" | "clip",
) {
  if (
    !UUID_RE.test(projectId) ||
    !UUID_RE.test(jobId) ||
    !CANDIDATE_ID_RE.test(candidateId)
  ) {
    throw new Error("Candidate evidence identity is invalid");
  }
  return `trafficvisson-evidence://artifact/projects/${projectId}/jobs/${jobId}/candidates/${candidateId}/${kind}`;
}
