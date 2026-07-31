import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Check,
  LoaderCircle,
  ShieldAlert,
  X,
} from "lucide-react";
import {
  buildReportDeidentifiedReviewRequest,
  parseReportDeidentifiedArtifact,
  parseReportDeidentifiedEvidenceList,
  parseReportDeidentifiedReviewResponse,
  type ReportDeidentifiedPreparation,
} from "../reportDeidentifiedEvidence";
import {
  publishPipelineJob,
  type PipelineJob,
} from "../pipelineJobs";
import { Badge, Button, GlassPanel } from "./ui";

function shortHash(value: string) {
  return `${value.slice(0, 10)}…${value.slice(-8)}`;
}

function messageFrom(error: unknown) {
  return error instanceof Error
    ? error.message
    : "공유 보고서 비식별 근거를 불러오지 못했습니다.";
}

export function ReportDeidentifiedEvidenceReview({
  job,
}: {
  job: PipelineJob;
}) {
  const [preparations, setPreparations] = useState<
    ReportDeidentifiedPreparation[]
  >([]);
  const [loadState, setLoadState] = useState<
    "loading" | "ready" | "failed"
  >("loading");
  const [imageUrl, setImageUrl] = useState<string | null>(null);
  const [imageState, setImageState] = useState<
    "idle" | "loading" | "ready" | "failed"
  >("idle");
  const [busyDecision, setBusyDecision] = useState<
    "approve" | "reject" | null
  >(null);
  const [message, setMessage] = useState<string | null>(null);
  const [reloadRevision, setReloadRevision] = useState(0);

  const pending = useMemo(
    () =>
      preparations.filter(
        (item) =>
          item.state === "awaiting_deidentification_review",
      ),
    [preparations],
  );
  const current = pending[0] ?? null;
  const completedCount = preparations.length - pending.length;
  const rejectedCount = preparations.filter(
    (item) =>
      item.state === "rejected_by_deidentification_review",
  ).length;

  useEffect(() => {
    const api =
      window.trafficSuperVision?.reportDeidentifiedEvidence;
    if (!api) {
      setLoadState("failed");
      setMessage(
        "설치형 앱의 공유 보고서 비식별 검토 연결을 사용할 수 없습니다.",
      );
      return;
    }
    let active = true;
    setLoadState("loading");
    setMessage(null);
    void api
      .prepare({
        projectId: job.projectId,
        jobId: job.jobId,
      })
      .then(parseReportDeidentifiedEvidenceList)
      .then((result) => {
        if (!active) return;
        if (
          result.projectId !== job.projectId ||
          result.pipelineJobId !== job.jobId ||
          result.count < 1 ||
          result.preparations.some(
            (item) =>
              item.projectId !== job.projectId ||
              item.pipelineJobId !== job.jobId,
          )
        ) {
          throw new Error(
            "공유 보고서 비식별 근거의 PipelineJob이 일치하지 않습니다.",
          );
        }
        setPreparations([...result.preparations]);
        setLoadState("ready");
      })
      .catch((error) => {
        if (!active) return;
        setLoadState("failed");
        setMessage(messageFrom(error));
      });
    return () => {
      active = false;
    };
  }, [
    job.jobId,
    job.projectId,
    job.recordSha256,
    reloadRevision,
  ]);

  useEffect(() => {
    const api =
      window.trafficSuperVision?.reportDeidentifiedEvidence;
    if (!api || !current) {
      setImageUrl(null);
      setImageState("idle");
      return;
    }
    let active = true;
    let objectUrl: string | null = null;
    setImageUrl(null);
    setImageState("loading");
    setMessage(null);
    void api
      .artifact({
        projectId: current.projectId,
        jobId: current.pipelineJobId,
        incidentId: current.incidentId,
        derivativeId: current.derivativeId,
      })
      .then(parseReportDeidentifiedArtifact)
      .then((artifact) => {
        if (
          artifact.sha256 !== current.deidentifiedSha256
        ) {
          throw new Error(
            "비식별 JPEG의 SHA-256이 준비 기록과 일치하지 않습니다.",
          );
        }
        const bytes = artifact.bytes.slice();
        objectUrl = URL.createObjectURL(
          new Blob([bytes.buffer], { type: artifact.mediaType }),
        );
        if (!active) return;
        setImageUrl(objectUrl);
        setImageState("ready");
      })
      .catch((error) => {
        if (!active) return;
        setImageState("failed");
        setMessage(messageFrom(error));
      });
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [
    current?.deidentifiedSha256,
    current?.derivativeId,
    current?.incidentId,
    current?.pipelineJobId,
    current?.preparationRevision,
    current?.projectId,
  ]);

  async function recordDecision(approved: boolean) {
    const api =
      window.trafficSuperVision?.reportDeidentifiedEvidence;
    if (!api || !current || imageState !== "ready") return;
    setBusyDecision(approved ? "approve" : "reject");
    setMessage(null);
    try {
      const response = parseReportDeidentifiedReviewResponse(
        await api.review(
          buildReportDeidentifiedReviewRequest(
            current,
            approved,
          ),
        ),
      );
      setPreparations((items) =>
        items.map((item) =>
          item.incidentId === response.preparation.incidentId &&
          item.derivativeId === response.preparation.derivativeId
            ? response.preparation
            : item,
        ),
      );
      publishPipelineJob(response.pipelineJob);
      setMessage(
        response.pipelineResumeRequested
          ? "모든 공유용 근거를 확인했습니다. 같은 PipelineJob의 보고서 미리보기 생성을 자동 재개했습니다."
          : approved
            ? "이 사건의 공유용 근거를 승인했습니다. 다음 사건을 확인해 주세요."
            : "이 파생본을 거절했습니다. 공유용 HWPX 보고서에는 들어가지 않으며 파이프라인도 완료로 표시되지 않습니다.",
      );
    } catch (error) {
      setMessage(messageFrom(error));
    } finally {
      setBusyDecision(null);
    }
  }

  return (
    <GlassPanel
      className="report-deidentified-review"
      aria-labelledby="report-deidentified-review-title"
    >
      <header className="report-deidentified-review__header">
        <div>
          <p className="eyebrow">공유용 HWPX 근거 승인</p>
          <h2 id="report-deidentified-review-title">
            얼굴·번호판 비식별 결과 확인
          </h2>
          <p>
            현재 영상의 사용자 승인 사건만 전체 프레임 JPEG로
            변환했습니다. 사람의 승인 전에는 HWPX 사진 칸에
            삽입되지 않습니다.
          </p>
        </div>
        <Badge tone="warning">
          {loadState === "loading"
            ? "파생본 자동 준비 중"
            : `${completedCount} / ${preparations.length} 확인`}
        </Badge>
      </header>

      {loadState === "loading" ? (
        <div
          className="report-deidentified-review__loading"
          role="status"
        >
          <LoaderCircle className="spin" size={28} />
          승인 사건과 원본 SHA-256을 확인하고 공유용 JPEG를
          준비하고 있습니다.
        </div>
      ) : loadState === "failed" ? (
        <div className="notice notice--danger" role="alert">
          <AlertTriangle size={18} />
          <div>
            <strong>공유용 비식별 근거를 준비하지 못했습니다</strong>
            <p>{message}</p>
          </div>
          <Button
            variant="ghost"
            onClick={() =>
              setReloadRevision((currentValue) => currentValue + 1)
            }
          >
            다시 준비
          </Button>
        </div>
      ) : current ? (
        <>
          <div className="report-deidentified-review__content">
            <figure className="report-deidentified-review__figure">
              {imageState === "loading" ? (
                <div className="report-deidentified-review__loading">
                  <LoaderCircle className="spin" size={24} />
                  해시가 일치하는 JPEG를 여는 중
                </div>
              ) : imageUrl ? (
                <img
                  src={imageUrl}
                  alt="얼굴과 번호판 자동 가림이 적용된 공유용 도로점검 근거"
                />
              ) : (
                <div className="report-deidentified-review__loading">
                  <AlertTriangle size={22} />
                  비식별 JPEG를 표시할 수 없습니다.
                </div>
              )}
              <figcaption>
                <strong>사건 {completedCount + 1}</strong>
                <span>
                  얼굴 {current.faceDetectionCount} · 번호판{" "}
                  {current.plateDetectionCount} · 자동 가림 영역{" "}
                  {current.redactionRegionCount}
                </span>
                <code title={current.deidentifiedSha256}>
                  SHA {shortHash(current.deidentifiedSha256)}
                </code>
              </figcaption>
            </figure>

            <aside className="report-deidentified-review__facts">
              <div>
                <span>사건 ID</span>
                <code title={current.incidentId}>
                  {shortHash(current.incidentId.replaceAll("-", ""))}
                </code>
              </div>
              <div>
                <span>검지기</span>
                <strong>
                  {current.detector.detectorName} · v
                  {current.detector.detectorVersion}
                </strong>
              </div>
              <div>
                <span>모델</span>
                <code title={current.detector.modelSha256}>
                  {current.detector.modelId} ·{" "}
                  {shortHash(current.detector.modelSha256)}
                </code>
              </div>
              <div>
                <span>원본 → 파생본</span>
                <code>
                  {shortHash(current.originalSha256)} →{" "}
                  {shortHash(current.deidentifiedSha256)}
                </code>
              </div>
              <div>
                <span>보고서 용도</span>
                <strong>sample HWPX 사진 칸 전체 프레임 근거</strong>
              </div>
            </aside>
          </div>

          <div className="report-deidentified-review__warning">
            <ShieldAlert size={18} />
            <div>
              <strong>자동 검지 0건도 안전 판정이 아닙니다.</strong>
              <p>
                남아 있는 얼굴·차량번호·개인정보가 없는지 화면을
                직접 확인하세요. 승인 후에도 이 파생본은 현재
                사건·분석·PipelineJob에만 사용할 수 있습니다.
              </p>
            </div>
          </div>

          {message ? (
            <p
              className="report-deidentified-review__message"
              role="status"
            >
              {message}
            </p>
          ) : null}

          <div className="report-deidentified-review__actions">
            <Button
              variant="ghost"
              disabled={
                busyDecision !== null || imageState !== "ready"
              }
              onClick={() => void recordDecision(false)}
            >
              {busyDecision === "reject" ? (
                <LoaderCircle className="spin" size={16} />
              ) : (
                <X size={16} />
              )}
              가림 불충분 · 보고서 사용 거절
            </Button>
            <Button
              variant="primary"
              disabled={
                busyDecision !== null || imageState !== "ready"
              }
              onClick={() => void recordDecision(true)}
            >
              {busyDecision === "approve" ? (
                <LoaderCircle className="spin" size={16} />
              ) : (
                <Check size={16} />
              )}
              가림 확인 · 공유용 HWPX 근거 승인
            </Button>
          </div>
        </>
      ) : (
        <div
          className={`notice ${
            rejectedCount ? "notice--warning" : "notice--success"
          }`}
          role="status"
        >
          {rejectedCount ? (
            <AlertTriangle size={18} />
          ) : (
            <Check size={18} />
          )}
          <div>
            <strong>
              {rejectedCount
                ? "거절된 공유용 근거가 있습니다"
                : "공유용 근거 검토를 완료했습니다"}
            </strong>
            <p>
              {rejectedCount
                ? "거절된 파생본은 HWPX에 들어가지 않습니다. 수정된 비식별 파생본이 준비되기 전에는 보고서 단계도 완료되지 않습니다."
                : "서버가 승인된 파생본 해시를 정확 보고서 바인더에 연결하고 PipelineJob을 재개하고 있습니다."}
            </p>
          </div>
        </div>
      )}
    </GlassPanel>
  );
}
