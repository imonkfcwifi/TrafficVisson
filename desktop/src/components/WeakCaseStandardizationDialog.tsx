import { useEffect, useRef } from "react";
import { Check, LoaderCircle, ShieldCheck, Sparkles, X } from "lucide-react";
import { issueLabelKo } from "../issueTypes";
import type {
  WeakCaseReference,
  WeakCaseStandardizationProposal,
  WeakCaseStandardizationProviderStatus,
} from "../weakCases";
import { Button, IconButton } from "./ui";

type StandardizationDialogState = {
  provider: "codex" | "claude";
  metadataConsent: boolean;
  duplicateImageConfirmed: boolean;
  status: "idle" | "loading" | "ready" | "requesting" | "failed";
  providerStatuses: WeakCaseStandardizationProviderStatus[];
  proposal: WeakCaseStandardizationProposal | null;
  message: string | null;
};

type Props = {
  reference: WeakCaseReference;
  state: StandardizationDialogState;
  approvalSaving: boolean;
  onClose(): void;
  onProviderChange(provider: "codex" | "claude"): void;
  onMetadataConsentChange(confirmed: boolean): void;
  onDuplicateImageConfirmedChange(confirmed: boolean): void;
  onRequest(): void;
  onApprove(): void;
};

function providerStateLabel(
  status: WeakCaseStandardizationProviderStatus | undefined,
) {
  if (!status) return "상태 확인 실패";
  if (status.availability === "ready") {
    return `준비됨${status.version ? ` · ${status.version}` : ""}`;
  }
  if (status.availability === "not_installed") return "CLI 미설치";
  if (status.availability === "not_logged_in") return "로그인 필요";
  if (status.availability === "expired") return "로그인 만료";
  if (status.availability === "network_unavailable") {
    return "네트워크 연결 실패";
  }
  return "현재 사용 불가";
}

function proposalRationale(proposal: WeakCaseStandardizationProposal) {
  const evidenceNames = [
    proposal.sourceSnapshot.caption ? "원본 캡션" : null,
    proposal.sourceSnapshot.opinion ? "원본 점검 의견" : null,
    proposal.sourceSnapshot.route ? "노선 문구" : null,
    "표·사진 위치",
  ].filter(Boolean);
  return `${proposal.provider === "codex" ? "Codex CLI" : "Claude Code"}가 사진 픽셀 없이 ${evidenceNames.join("·")} 메타데이터만 비교해 표준 유형과 허용 조치 조합을 제안했습니다. 사용자 확인 전에는 학습 참조·Annotation·Incident·보고서 사실이 아닙니다.`;
}

export function WeakCaseStandardizationDialog({
  reference,
  state,
  approvalSaving,
  onClose,
  onProviderChange,
  onMetadataConsentChange,
  onDuplicateImageConfirmedChange,
  onRequest,
  onApprove,
}: Props) {
  const dialogRef = useRef<HTMLElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const selectedProviderStatus = state.providerStatuses.find(
    ({ provider }) => provider === state.provider,
  );
  const canRequest =
    state.metadataConsent &&
    selectedProviderStatus?.availability === "ready" &&
    state.status !== "requesting" &&
    state.status !== "loading";
  const canApprove =
    Boolean(state.proposal) &&
    !approvalSaving &&
    (!reference.duplicateImageReviewRequired ||
      state.duplicateImageConfirmed);

  useEffect(() => {
    const root = dialogRef.current;
    if (!root) return;
    const focusableSelector =
      "button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex='-1'])";
    const focusable = Array.from(
      root.querySelectorAll<HTMLElement>(focusableSelector),
    );
    focusable[0]?.focus();
    const trapFocus = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const current = Array.from(
        root.querySelectorAll<HTMLElement>(focusableSelector),
      );
      const first = current[0];
      const last = current.at(-1);
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    root.addEventListener("keydown", trapFocus);
    return () => root.removeEventListener("keydown", trapFocus);
  }, []);

  return (
    <div
      className="weak-case-standardization-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        ref={dialogRef}
        className="weak-case-standardization-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="weak-case-standardization-title"
        aria-describedby="weak-case-standardization-boundary"
      >
        <header className="weak-case-standardization-dialog__header">
          <span>
            <Sparkles size={18} aria-hidden="true" />
            <span>
              <strong id="weak-case-standardization-title">
                3·4분기 사례 LLM 표준화
              </strong>
              <small>
                {reference.quarter}분기 · {reference.pageHint}쪽 · 표{" "}
                {reference.tableIndex ?? "—"} / 행 {reference.cellRow ?? "—"} /
                열 {reference.cellColumn ?? "—"}
              </small>
            </span>
          </span>
          <IconButton label="표준화 창 닫기" onClick={onClose}>
            <X size={17} aria-hidden="true" />
          </IconButton>
        </header>

        <div
          id="weak-case-standardization-boundary"
          className="weak-case-standardization-dialog__boundary"
        >
          <ShieldCheck size={17} aria-hidden="true" />
          <p>
            원본 사진·파일 경로·문서 bytes·인증정보는 전송하지 않습니다.
            Provider는 원문과 구조 메타데이터로 검토 전용 제안만 만듭니다.
          </p>
        </div>

        <div className="weak-case-standardization-dialog__grid">
          <section aria-label="변경하지 않는 원본">
            <h3>변경하지 않는 원본</h3>
            <dl className="weak-case-standardization-original">
              <div>
                <dt>사진 캡션</dt>
                <dd>{reference.caption ?? "원문 없음"}</dd>
              </div>
              <div>
                <dt>점검 의견</dt>
                <dd>{reference.opinion ?? "원문 없음"}</dd>
              </div>
              <div>
                <dt>노선·위치</dt>
                <dd>
                  {[reference.route, reference.location]
                    .filter(Boolean)
                    .join(" · ") || "원문 없음"}
                </dd>
              </div>
            </dl>
            <code title={reference.documentSha256}>
              문서 {reference.documentSha256.slice(0, 12)}… · 사진{" "}
              {reference.imageSha256?.slice(0, 12) ?? "검증 불가"}…
            </code>
          </section>

          <section aria-label="공식 CLI 상태와 전송 동의">
            <h3>공식 CLI와 전송 동의</h3>
            <div className="weak-case-standardization-providers">
              {(["codex", "claude"] as const).map((provider) => {
                const status = state.providerStatuses.find(
                  (item) => item.provider === provider,
                );
                return (
                  <button
                    type="button"
                    key={provider}
                    className={state.provider === provider ? "active" : ""}
                    aria-pressed={state.provider === provider}
                    onClick={() => onProviderChange(provider)}
                    disabled={state.status === "requesting"}
                    title={status?.message}
                  >
                    <strong>
                      {provider === "codex" ? "Codex CLI" : "Claude Code"}
                    </strong>
                    <small
                      data-provider-availability={
                        status?.availability ?? "unavailable"
                      }
                    >
                      {providerStateLabel(status)}
                    </small>
                  </button>
                );
              })}
            </div>
            <label className="weak-case-standardization-consent">
              <input
                type="checkbox"
                checked={state.metadataConsent}
                onChange={(event) =>
                  onMetadataConsentChange(event.target.checked)
                }
                disabled={state.status === "requesting"}
              />
              <span>
                원본 캡션·점검 의견·노선·표/사진 위치 메타데이터만 선택한
                공식 CLI에 전송함을 확인했습니다.
              </span>
            </label>
            <Button
              variant="primary"
              onClick={onRequest}
              disabled={!canRequest}
            >
              {state.status === "requesting" ? (
                <LoaderCircle className="spin" size={16} aria-hidden="true" />
              ) : (
                <Sparkles size={16} aria-hidden="true" />
              )}
              {state.status === "requesting"
                ? "제안 생성 중"
                : state.proposal
                  ? "제안 다시 생성"
                  : "표준화 제안 요청"}
            </Button>
          </section>
        </div>

        <section
          className="weak-case-standardization-result"
          aria-label="LLM 표준화 제안"
        >
          <header>
            <h3>검토 전용 제안</h3>
            {state.proposal ? (
              <span>
                {state.proposal.provider === "codex"
                  ? "Codex CLI"
                  : "Claude Code"}{" "}
                · revision {state.proposal.revision}
              </span>
            ) : (
              <span>아직 제안 없음</span>
            )}
          </header>
          {state.proposal ? (
            <>
              <div className="weak-case-standardization-result__facts">
                <span>
                  <small>표준 유형</small>
                  <strong>
                    {issueLabelKo(state.proposal.suggestion.issueType) ??
                      state.proposal.suggestion.issueType}
                  </strong>
                </span>
                <span>
                  <small>권장 조치</small>
                  <strong>
                    {state.proposal.suggestion.maintenanceAction}
                  </strong>
                </span>
              </div>
              <p>{proposalRationale(state.proposal)}</p>
              <small>
                제안 단독 결과 · approved_reference 0 · Annotation{" "}
                {state.proposal.annotationCount} · Incident{" "}
                {state.proposal.incidentCount} · 보고서 사실{" "}
                {state.proposal.reportFactCount}
              </small>
            </>
          ) : (
            <p>
              Provider 상태와 전송 범위를 확인한 뒤 제안을 요청하세요. 원본
              캡션과 점검 의견은 변경되지 않습니다.
            </p>
          )}
        </section>

        {reference.duplicateImageReviewRequired ? (
          <label className="weak-case-standardization-consent weak-case-standardization-consent--duplicate">
            <input
              type="checkbox"
              checked={state.duplicateImageConfirmed}
              onChange={(event) =>
                onDuplicateImageConfirmedChange(event.target.checked)
              }
            />
            <span>
              동일 사진이 다른 노선·쪽에서{" "}
              {reference.duplicateImageOccurrenceCount}회 사용된 provenance를
              확인했습니다.
            </span>
          </label>
        ) : null}

        <footer className="weak-case-standardization-dialog__footer">
          <p
            role={state.status === "failed" ? "alert" : "status"}
            aria-live="polite"
          >
            {state.message}
          </p>
          <span>
            <Button variant="ghost" onClick={onClose}>
              취소
            </Button>
            <Button
              variant="primary"
              onClick={onApprove}
              disabled={!canApprove}
            >
              {approvalSaving ? (
                <LoaderCircle className="spin" size={16} aria-hidden="true" />
              ) : (
                <Check size={16} aria-hidden="true" />
              )}
              사용자 확인 후 승인 사례로 승격
            </Button>
          </span>
        </footer>
      </section>
    </div>
  );
}
