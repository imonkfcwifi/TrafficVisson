import { useEffect, useRef, useState } from "react";
import {
  Check,
  Download,
  FileArchive,
  FileCheck2,
  FolderOpen,
  Hash,
  ShieldCheck,
} from "lucide-react";
import {
  Badge,
  Button,
  GlassPanel,
  PanelHeader,
  ProgressBar,
} from "../components/ui";
import { useRuntimeState } from "../runtimeState";
import {
  EXPORT_ARTIFACTS,
  buildExportDisplayRows,
  parseCompletedExport,
  type CompletedExport,
} from "../exportResults";

type ExportState = "idle" | "running" | "done" | "error";

export function buildExportScopeKey(
  projectId: string | null | undefined,
  analysisJobId: string | null | undefined,
) {
  return `${projectId ?? "no-project"}:${analysisJobId ?? "no-analysis"}`;
}

export function isCurrentExportOperation(
  requestOperationId: number,
  activeOperationId: number,
  requestScopeKey: string,
  activeScopeKey: string,
) {
  return (
    requestOperationId === activeOperationId &&
    requestScopeKey === activeScopeKey
  );
}

function formatBytes(value: number) {
  if (value >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(2)} GB`;
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)} MB`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)} KB`;
  return `${value.toLocaleString("ko-KR")} B`;
}

export function ExportScreen() {
  const runtime = useRuntimeState();
  const exportScopeKey = buildExportScopeKey(
    runtime.activeImport?.projectId,
    runtime.analysisSummary?.jobId,
  );
  const latestScopeKeyRef = useRef(exportScopeKey);
  const activeExportOperationRef = useRef(0);
  latestScopeKeyRef.current = exportScopeKey;
  const selected = new Set(EXPORT_ARTIFACTS.map((item) => item.artifact));
  const [stateScopeKey, setStateScopeKey] = useState(exportScopeKey);
  const [exportState, setExportState] = useState<ExportState>("idle");
  const [completedExport, setCompletedExport] = useState<CompletedExport | null>(null);
  const [completedDestinationName, setCompletedDestinationName] =
    useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [destination, setDestination] =
    useState<TrafficSuperVisionDirectorySelection | null>(null);
  const stateBelongsToCurrentScope = stateScopeKey === exportScopeKey;
  const currentExportState = stateBelongsToCurrentScope ? exportState : "idle";
  const currentCompletedExport = stateBelongsToCurrentScope
    ? completedExport
    : null;
  const currentCompletedDestinationName = stateBelongsToCurrentScope
    ? completedDestinationName
    : null;
  const currentErrorMessage = stateBelongsToCurrentScope ? errorMessage : null;
  const currentDestination = stateBelongsToCurrentScope ? destination : null;
  const artifactRows = buildExportDisplayRows(currentCompletedExport);

  useEffect(() => {
    activeExportOperationRef.current += 1;
    setStateScopeKey(exportScopeKey);
    setExportState("idle");
    setCompletedExport(null);
    setCompletedDestinationName(null);
    setErrorMessage(null);
    setDestination(null);
  }, [exportScopeKey]);

  async function selectDestination() {
    if (!window.trafficSuperVision) {
      setExportState("error");
      setErrorMessage("저장 폴더 선택은 설치형 Electron 앱에서 사용할 수 있습니다.");
      return;
    }
    const requestScopeKey = exportScopeKey;
    try {
      const selection =
        await window.trafficSuperVision.dialogs.selectExportDirectory();
      if (latestScopeKeyRef.current !== requestScopeKey) return;
      if (selection.selectionId && selection.directory) {
        setStateScopeKey(requestScopeKey);
        setDestination(selection);
        setCompletedExport(null);
        setCompletedDestinationName(null);
        setExportState("idle");
        setErrorMessage(null);
      }
    } catch (error) {
      if (latestScopeKeyRef.current !== requestScopeKey) return;
      setExportState("error");
      setErrorMessage(
        error instanceof Error
          ? error.message
          : "저장 폴더를 선택하지 못했습니다.",
      );
    }
  }

  async function startExport() {
    if (!runtime.activeImport || !runtime.analysisSummary) {
      setExportState("error");
      setErrorMessage("먼저 검증된 영상을 등록하고 실제 후보 분석을 완료하세요.");
      return;
    }
    if (!window.trafficSuperVision) {
      setExportState("error");
      setErrorMessage("실파일 내보내기는 설치형 Electron 앱에서 실행할 수 있습니다.");
      return;
    }
    if (!currentDestination?.selectionId) {
      setExportState("error");
      setErrorMessage("추적 패키지를 저장할 전용 폴더를 먼저 선택하세요.");
      return;
    }
    const requestScopeKey = exportScopeKey;
    const requestDestination = currentDestination;
    const requestDestinationName =
      requestDestination.directory?.name ?? "선택한 폴더";
    const requestOperationId = activeExportOperationRef.current + 1;
    activeExportOperationRef.current = requestOperationId;
    setStateScopeKey(requestScopeKey);
    setExportState("running");
    setErrorMessage(null);
    setCompletedExport(null);
    setCompletedDestinationName(requestDestinationName);
    setDestination((current) =>
      current?.selectionId === requestDestination.selectionId
        ? { ...current, selectionId: "" }
        : current,
    );
    try {
      const response = parseCompletedExport(
        await window.trafficSuperVision.exports.create({
          projectId: runtime.activeImport.projectId,
          analysisJobId: runtime.analysisSummary.jobId,
          artifacts: [...selected],
          destinationSelectionId: requestDestination.selectionId,
        }),
      );
      if (
        !isCurrentExportOperation(
          requestOperationId,
          activeExportOperationRef.current,
          requestScopeKey,
          latestScopeKeyRef.current,
        )
      ) {
        return;
      }
      setCompletedExport(response);
      setExportState("done");
    } catch (error) {
      if (
        !isCurrentExportOperation(
          requestOperationId,
          activeExportOperationRef.current,
          requestScopeKey,
          latestScopeKeyRef.current,
        )
      ) {
        return;
      }
      setExportState("error");
      const cause =
        error instanceof Error
          ? error.message
          : "내보내기를 완료하지 못했습니다.";
      setErrorMessage(
        `${cause} 저장 폴더를 다시 선택한 뒤 재시도하세요.`,
      );
    }
  }

  return (
    <div className="screen screen--export">
      <header className="screen-header">
        <div>
          <p className="eyebrow">TRACEABLE PROJECT PACKAGE</p>
          <h1>내보내기</h1>
          <p>AI 원본값과 사용자 최종값을 분리하고 모든 산출물의 SHA-256을 기록합니다.</p>
        </div>
        <div className="header-actions">
          <Button
            onClick={() => void selectDestination()}
            disabled={currentExportState === "running"}
          >
            <FolderOpen size={17} />
            {currentExportState === "running" &&
            currentCompletedDestinationName
              ? `${currentCompletedDestinationName}에 저장 중`
              : currentDestination?.selectionId
                ? currentDestination.directory?.name
                : currentCompletedDestinationName
                ? "저장 폴더 다시 선택"
                : "저장 폴더 선택"}
          </Button>
          <Button
            variant="primary"
            onClick={startExport}
            disabled={
              currentExportState === "running" ||
              !currentDestination?.selectionId ||
              !runtime.analysisSummary
            }
            title={
              !runtime.analysisSummary
                ? "실제 후보 분석을 완료한 뒤 내보낼 수 있습니다."
                : !currentDestination?.selectionId
                  ? "저장 폴더를 먼저 선택하세요."
                  : undefined
            }
          >
            <Download size={17} />
            {currentExportState === "running"
              ? "패키지 생성 중…"
              : "전체 추적 패키지 내보내기"}
          </Button>
        </div>
      </header>

      <div className="export-layout">
        <GlassPanel className="package-options">
          <PanelHeader eyebrow="TRIP PACKAGE" title="포함할 항목" />
          <div className="package-summary">
            <FileArchive size={30} />
            <div>
              <strong>
                전체 추적 패키지 ZIP
              </strong>
              <span>
                {currentDestination?.selectionId &&
                currentDestination.directory?.name
                  ? `${currentDestination.directory.name}에 생성 예정 · `
                  : currentExportState === "running" &&
                      currentCompletedDestinationName
                    ? `${currentCompletedDestinationName}에 생성 중 · `
                    : currentExportState === "done" &&
                        currentCompletedDestinationName
                      ? `${currentCompletedDestinationName}에 생성 완료 · `
                      : currentExportState === "error" &&
                          currentCompletedDestinationName
                        ? `${currentCompletedDestinationName} 생성 실패 · 다시 선택 필요 · `
                        : "저장 폴더 선택 필요 · "}
                승인 사건 근거 JPEG·MP4 포함 · 원본 전체 영상 제외
              </span>
            </div>
          </div>
          <div className="artifact-checks">
            {artifactRows.map((row) => (
              <label key={row.artifact}>
                <input
                  type="checkbox"
                  checked
                  disabled
                  readOnly
                />
                <span className="check-visual" aria-hidden="true"><Check size={13} /></span>
                <span>
                  <strong>{row.fileName}</strong>
                  <small>{row.description} · 실제 프로젝트 행만 기록</small>
                </span>
                <code>
                  {row.rowCount === null || row.sizeBytes === null
                    ? "계산 전"
                    : `${row.rowCount.toLocaleString("ko-KR")}건 · ${formatBytes(
                        row.sizeBytes,
                      )}`}
                </code>
              </label>
            ))}
          </div>
          <label className="original-video-option">
            <input type="checkbox" disabled />
            <span className="check-visual" aria-hidden="true" />
            <span>
              <strong>원본 영상은 현재 패키지에서 제외</strong>
              <small>승인 사건의 근거 사진·클립만 복사하며 전체 촬영 영상은 포함하지 않습니다.</small>
            </span>
          </label>
        </GlassPanel>

        <GlassPanel
          className={`artifact-table-panel${
            currentExportState === "done"
              ? " artifact-table-panel--completed"
              : ""
          }`}
        >
          <PanelHeader
            eyebrow="SHA-256 PROVENANCE"
            title="산출물"
            action={<Badge tone="neutral">UTF-8 BOM · WGS84 계약</Badge>}
          />
          <div className="artifact-table-wrap">
            <table className="data-table artifact-table">
              <caption className="sr-only">선택한 내보내기 산출물과 실제 생성 상태</caption>
              <thead>
                <tr>
                  <th>파일</th>
                  <th>행</th>
                  <th>크기</th>
                  <th>SHA-256 / 생성 주체</th>
                </tr>
              </thead>
              <tbody>
                {artifactRows.map((row) => (
                  <tr key={row.artifact}>
                    <th>{row.fileName}</th>
                    <td>
                      {row.rowCount === null
                        ? "생성 시 집계"
                        : `${row.rowCount.toLocaleString("ko-KR")}건`}
                    </td>
                    <td>
                      {row.sizeBytes === null
                        ? "계산 전"
                        : formatBytes(row.sizeBytes)}
                    </td>
                    <td
                      className="mono"
                      title={
                        row.sha256 ?? "실파일 생성 후 SHA-256 표시"
                      }
                    >
                      {row.sha256 ?? "실파일 생성 후 SHA-256 표시"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="export-principles">
            {[
              "AI 원본값 / 사용자 최종값 열 분리",
              "ISO 8601 + 영상 타임코드 병기",
              "사건 행 ↔ 근거 이미지·클립 연결",
              "dataset·model·input provenance",
            ].map((principle) => (
              <span key={principle}><ShieldCheck size={16} /> {principle}</span>
            ))}
          </div>

          {currentExportState === "running" ? (
            <div className="export-progress" role="status">
              <FileArchive size={21} />
              <div>
                <strong>CSV·JSON·manifest와 ZIP을 원자적으로 생성하는 중</strong>
                <ProgressBar value={0} label="브리지의 완료 응답을 기다리는 중" />
              </div>
            </div>
          ) : null}
          {currentExportState === "error" ? (
            <div className="notice notice--danger" role="alert">
              <FileArchive size={21} />
              <div>
                <strong>내보내기 완료 아님</strong>
                <p>{currentErrorMessage}</p>
              </div>
            </div>
          ) : null}
          {currentExportState === "done" && currentCompletedExport ? (
            <div className="export-success" role="status">
              <FileCheck2 size={24} />
              <div>
                <strong>내보내기 완료</strong>
                <p>
                  근거 JPEG·MP4{" "}
                  {currentCompletedExport.evidenceFileCount.toLocaleString("ko-KR")}개
                  {" "}
                   ({formatBytes(currentCompletedExport.evidenceSizeBytes)})
                  {" · "}
                   승인 사건 {currentCompletedExport.evidenceIncidentCount.toLocaleString("ko-KR")}건
                  {" · "}원본 전체 영상 제외
                  {" · "}
                   {currentCompletedExport.archiveFileName} · {formatBytes(currentCompletedExport.archiveSizeBytes)}
                  {" · "}
                   구성 {currentCompletedExport.artifactCount.toLocaleString("ko-KR")}개
                </p>
              </div>
              <Badge tone="success">
                 <Hash size={13} /> {currentCompletedExport.archiveSha256.slice(0, 12)}…
              </Badge>
              <Badge tone="neutral">
                 <FolderOpen size={13} /> {currentCompletedDestinationName}
              </Badge>
            </div>
          ) : null}
        </GlassPanel>
      </div>
    </div>
  );
}
