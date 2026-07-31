import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Check,
  LoaderCircle,
  ShieldAlert,
  X,
} from "lucide-react";
import {
  buildProviderVisualEvidencePrivacyReviewRequest,
  parseProviderVisualEvidenceArtifact,
  parseProviderVisualEvidenceList,
  parseProviderVisualEvidencePreparation,
  parseProviderVisualEvidencePrivacyReviewResponse,
  type ProviderVisualEvidenceAttachment,
  type ProviderVisualEvidencePreparation,
} from "../providerVisualEvidence";
import {
  publishPipelineJob,
  type PipelineJob,
} from "../pipelineJobs";
import { Badge, Button, GlassPanel } from "./ui";

type ReviewImage = {
  attachment: ProviderVisualEvidenceAttachment;
  url: string;
};

function shortHash(value: string) {
  return `${value.slice(0, 10)}…${value.slice(-8)}`;
}

function errorMessage(error: unknown) {
  return error instanceof Error
    ? error.message
    : "비식별 이미지 검토 자료를 불러오지 못했습니다.";
}

export function ProviderVisualEvidenceReview({
  job,
}: {
  job: PipelineJob;
}) {
  const [preparations, setPreparations] = useState<
    ProviderVisualEvidencePreparation[]
  >([]);
  const [loadState, setLoadState] = useState<
    "loading" | "ready" | "failed"
  >("loading");
  const [images, setImages] = useState<ReviewImage[]>([]);
  const [imageState, setImageState] = useState<
    "idle" | "loading" | "ready" | "failed"
  >("idle");
  const [busyDecision, setBusyDecision] = useState<
    "approve" | "reject" | null
  >(null);
  const [message, setMessage] = useState<string | null>(null);

  const pending = useMemo(
    () =>
      preparations.filter(
        (item) => item.state === "awaiting_deidentification_review",
      ),
    [preparations],
  );
  const current = pending[0] ?? null;
  const completedCount = preparations.length - pending.length;
  const rejectedCount = preparations.filter(
    (item) => item.state === "rejected_by_privacy_review",
  ).length;

  useEffect(() => {
    const service = window.trafficSuperVision?.providerVisualEvidence;
    if (!service) {
      setLoadState("failed");
      setMessage(
        "설치형 앱의 개인정보 검토 연결을 사용할 수 없습니다.",
      );
      return;
    }
    let active = true;
    setLoadState("loading");
    setMessage(null);
    void service
      .list({ projectId: job.projectId, jobId: job.jobId })
      .then(parseProviderVisualEvidenceList)
      .then((items) => {
        if (!active) return;
        if (
          items.some(
            (item) =>
              item.projectId !== job.projectId ||
              item.pipelineJobId !== job.jobId,
          )
        ) {
          throw new Error(
            "개인정보 검토 자료의 PipelineJob이 일치하지 않습니다.",
          );
        }
        setPreparations(items);
        setLoadState("ready");
      })
      .catch((error) => {
        if (!active) return;
        setLoadState("failed");
        setMessage(errorMessage(error));
      });
    return () => {
      active = false;
    };
  }, [job.jobId, job.projectId, job.recordSha256]);

  useEffect(() => {
    const service = window.trafficSuperVision?.providerVisualEvidence;
    if (!service || !current) {
      setImages([]);
      setImageState("idle");
      return;
    }
    let active = true;
    const objectUrls: string[] = [];
    setImages([]);
    setImageState("loading");
    setMessage(null);
    void service
      .get({
        projectId: current.projectId,
        jobId: current.pipelineJobId,
        candidateId: current.candidateId,
        preparationId: current.preparationId,
      })
      .then(parseProviderVisualEvidencePreparation)
      .then(async (fresh) => {
        if (
          fresh.preparationRevision !== current.preparationRevision ||
          fresh.manifestSha256 !== current.manifestSha256
        ) {
          throw new Error(
            "검토 준비본이 바뀌었습니다. 목록을 다시 확인해 주세요.",
          );
        }
        const loaded = await Promise.all(
          fresh.attachments.map(async (attachment) => {
            const artifact = parseProviderVisualEvidenceArtifact(
              await service.artifact({
                projectId: fresh.projectId,
                jobId: fresh.pipelineJobId,
                candidateId: fresh.candidateId,
                preparationId: fresh.preparationId,
                order: attachment.order,
              }),
            );
            if (artifact.sha256 !== attachment.outputSha256) {
              throw new Error(
                "비식별 이미지의 SHA-256이 준비 기록과 일치하지 않습니다.",
              );
            }
            const bytes = artifact.bytes.slice();
            const url = URL.createObjectURL(
              new Blob([bytes.buffer], { type: artifact.mediaType }),
            );
            objectUrls.push(url);
            return { attachment, url };
          }),
        );
        if (!active) return;
        setImages(loaded);
        setImageState("ready");
      })
      .catch((error) => {
        if (!active) return;
        setImageState("failed");
        setMessage(errorMessage(error));
      });
    return () => {
      active = false;
      objectUrls.forEach((url) => URL.revokeObjectURL(url));
    };
  }, [
    current?.candidateId,
    current?.manifestSha256,
    current?.pipelineJobId,
    current?.preparationId,
    current?.preparationRevision,
    current?.projectId,
  ]);

  async function recordDecision(approved: boolean) {
    const service = window.trafficSuperVision?.providerVisualEvidence;
    if (!service || !current || imageState !== "ready") return;
    setBusyDecision(approved ? "approve" : "reject");
    setMessage(null);
    try {
      const response = parseProviderVisualEvidencePrivacyReviewResponse(
        await service.review(
          buildProviderVisualEvidencePrivacyReviewRequest(
            current,
            approved,
          ),
        ),
      );
      setPreparations((items) =>
        items.map((item) =>
          item.candidateId === response.preparation.candidateId &&
          item.preparationId === response.preparation.preparationId
            ? response.preparation
            : item,
        ),
      );
      publishPipelineJob(response.pipelineJob);
      setMessage(
        response.pipelineResumeRequested
          ? "모든 비식별 자료를 확인했습니다. 자동 분석을 같은 단계에서 재개했습니다."
          : approved
            ? "이 후보를 승인했습니다. 다음 후보를 확인해 주세요."
            : "전송을 거절했습니다. 이 이미지는 Provider에 전달되지 않습니다.",
      );
    } catch (error) {
      setMessage(errorMessage(error));
    } finally {
      setBusyDecision(null);
    }
  }

  const candidate = images.find(
    (item) => item.attachment.role === "candidate_representative",
  );
  const references = images.filter(
    (item) => item.attachment.role === "similar_case",
  );

  return (
    <GlassPanel
      className="provider-visual-review"
      aria-labelledby="provider-visual-review-title"
    >
      <div className="provider-visual-review__header">
        <div>
          <p className="eyebrow">자동 분석 중 사용자 확인</p>
          <h2 id="provider-visual-review-title">
            Codex 전송 전 비식별 이미지 확인
          </h2>
          <p>
            자동 가림은 검토 제안일 뿐 개인정보 안전을 자동으로
            보장하지 않습니다. 후보 1장과 3~5개 유사 사례를 직접
            확인해 주세요.
          </p>
        </div>
        <Badge tone="warning">
          {loadState === "loading"
            ? "검토 목록 확인 중"
            : `${completedCount} / ${preparations.length} 확인`}
        </Badge>
      </div>

      {loadState === "loading" ? (
        <div className="provider-visual-review__loading" role="status">
          <LoaderCircle className="spin" size={28} aria-hidden="true" />
          해시로 고정된 비식별 준비본을 불러오고 있습니다.
        </div>
      ) : loadState === "failed" ? (
        <div className="notice notice--danger" role="alert">
          <AlertTriangle size={18} aria-hidden="true" />
          <div>
            <strong>개인정보 검토 자료를 열지 못했습니다</strong>
            <p>{message}</p>
          </div>
        </div>
      ) : current ? (
        <>
          <div className="provider-visual-review__evidence">
            <section className="provider-visual-review__candidate">
              <div className="provider-visual-review__section-title">
                <strong>현재 영상 후보 crop</strong>
                <span>후보 {completedCount + 1}</span>
              </div>
              {imageState === "loading" ? (
                <div className="provider-visual-review__image-loading">
                  <LoaderCircle
                    className="spin"
                    size={24}
                    aria-hidden="true"
                  />
                  비식별 이미지 검증 중
                </div>
              ) : candidate ? (
                <ReviewFigure image={candidate} />
              ) : (
                <div className="provider-visual-review__image-loading">
                  <AlertTriangle size={22} aria-hidden="true" />
                  후보 이미지를 표시할 수 없습니다.
                </div>
              )}
            </section>
            <section className="provider-visual-review__references">
              <div className="provider-visual-review__section-title">
                <strong>3·4분기 유사 사례</strong>
                <span>{references.length || current.attachmentCount - 1}장</span>
              </div>
              <div className="provider-visual-review__reference-grid">
                {references.map((image) => (
                  <ReviewFigure key={image.attachment.order} image={image} />
                ))}
              </div>
            </section>
          </div>

          <div className="provider-visual-review__warning">
            <ShieldAlert size={17} aria-hidden="true" />
            <span>
              자동 감지 0건은 얼굴·번호판이 없다는 증명이 아닙니다.
              화면에서 남은 식별 정보를 직접 확인해야 합니다.
            </span>
            <code title={current.manifestSha256}>
              manifest {shortHash(current.manifestSha256)}
            </code>
          </div>

          {message ? (
            <p
              className="provider-visual-review__message"
              role="status"
            >
              {message}
            </p>
          ) : null}
          <div className="provider-visual-review__actions">
            <Button
              variant="ghost"
              disabled={busyDecision !== null || imageState !== "ready"}
              onClick={() => void recordDecision(false)}
            >
              {busyDecision === "reject" ? (
                <LoaderCircle className="spin" size={16} />
              ) : (
                <X size={16} />
              )}
              Provider 전송 거절
            </Button>
            <Button
              variant="primary"
              disabled={busyDecision !== null || imageState !== "ready"}
              onClick={() => void recordDecision(true)}
            >
              {busyDecision === "approve" ? (
                <LoaderCircle className="spin" size={16} />
              ) : (
                <Check size={16} />
              )}
              얼굴·번호판 가림 확인 · Codex 전송 승인
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
            <AlertTriangle size={18} aria-hidden="true" />
          ) : (
            <Check size={18} aria-hidden="true" />
          )}
          <div>
            <strong>
              {rejectedCount
                ? "거절된 비식별 자료가 있습니다"
                : "개인정보 검토를 완료했습니다"}
            </strong>
            <p>
              {rejectedCount
                ? "거절한 이미지는 Provider에 전송되지 않습니다. 현재 PipelineJob은 이미지 비교를 완료로 표시하지 않습니다."
                : "서버가 같은 PipelineJob의 자동 재개 상태를 확인하고 있습니다."}
            </p>
          </div>
        </div>
      )}
    </GlassPanel>
  );
}

function ReviewFigure({ image }: { image: ReviewImage }) {
  const { attachment } = image;
  const detections =
    attachment.faceDetectionCount + attachment.plateDetectionCount;
  return (
    <figure
      className={
        attachment.role === "candidate_representative"
          ? "provider-review-figure provider-review-figure--candidate"
          : "provider-review-figure"
      }
    >
      <img
        src={image.url}
        alt={
          attachment.role === "candidate_representative"
            ? "비식별 처리된 현재 영상 후보 crop"
            : `비식별 처리된 3·4분기 유사 사례 ${attachment.order}`
        }
      />
      <figcaption>
        <strong>
          {attachment.role === "candidate_representative"
            ? "현재 후보"
            : `유사 사례 ${attachment.order}`}
        </strong>
        <span>
          자동 가림 후보 {detections}건 · 얼굴{" "}
          {attachment.faceDetectionCount} · 번호판{" "}
          {attachment.plateDetectionCount}
        </span>
        <code title={attachment.outputSha256}>
          SHA {shortHash(attachment.outputSha256)}
        </code>
      </figcaption>
    </figure>
  );
}
