import type { AnalysisCandidate } from "./runtimeState";

function selectionKey(projectId: string, jobId: string) {
  return `trafficvisson:selected-candidate:${projectId}:${jobId}`;
}

export function loadSelectedCandidateId(
  projectId: string,
  jobId: string,
  candidates: AnalysisCandidate[],
) {
  try {
    const candidateId = window.localStorage.getItem(selectionKey(projectId, jobId));
    return candidates.some((candidate) => candidate.candidateId === candidateId)
      ? candidateId
      : null;
  } catch {
    return null;
  }
}

export function saveSelectedCandidateId(
  projectId: string,
  jobId: string,
  candidateId: string,
) {
  try {
    window.localStorage.setItem(selectionKey(projectId, jobId), candidateId);
  } catch {
    // Cross-screen selection is a convenience; storage failure must not block review.
  }
}

export function clearSelectedCandidateId(projectId: string, jobId: string) {
  try {
    window.localStorage.removeItem(selectionKey(projectId, jobId));
  } catch {
    // The destination screen already has the selected candidate in memory.
  }
}
