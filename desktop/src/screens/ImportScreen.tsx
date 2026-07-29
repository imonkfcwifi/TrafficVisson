import { useRef, useState } from "react";
import {
  Check,
  CircleAlert,
  FileSearch,
  FileVideo2,
  FolderOpen,
  Link2,
  Network,
  ShieldCheck,
  UploadCloud,
} from "lucide-react";
import type { ScreenId } from "../demo";
import { requireActiveProjectId } from "../projectCatalog";
import { registerImport } from "../runtimeState";
import {
  Badge,
  Button,
  GlassPanel,
  PanelHeader,
  ProgressBar,
  StatusDot,
} from "../components/ui";

const NETWORK_PATH =
  "\\\\192.168.0.234\\도로연구실\\9. 한국교통연구원\\01.2025년 민자고속도로 기술부문 실사 위탁용역\\2. 연구수행\\03. 3분기 실사점검\\새 폴더";

type InspectionState = "idle" | "checking" | "waiting" | "ready";

type MediaCandidate = {
  name: string;
  size: string;
  changed: string;
  state: string;
};

type SnapshotResponse = {
  snapshot_id: string;
  snapshot_digest: string;
  file_count: number;
  total_size_bytes: number;
  files: Array<{
    name: string;
    size_bytes: number;
    created_time_ns: number;
    modified_time_ns: number;
  }>;
};

type StabilityResponse = {
  stable: boolean;
  reason: string;
  stable_snapshot_id: string | null;
  file_count: number;
  total_size_bytes: number;
  observed_interval_seconds: [number, number];
};

type ProbeResponse = {
  snapshot_id?: string;
  snapshot_digest?: string;
  file_name: string;
  size_bytes: number;
  duration_seconds: number;
  video_streams: Array<{
    codec_name: string;
    width: number;
    height: number;
    frame_rate: string;
  }>;
  ffprobe_status: "validated";
  content_hash_status: "not_computed";
};

type HashJobResponse = {
  job_id: string;
  snapshot_id: string;
  file_name: string;
  status:
    | "queued"
    | "running"
    | "cancelling"
    | "completed"
    | "cancelled"
    | "failed";
  bytes_hashed: number;
  total_bytes: number;
  sha256_lower: string | null;
  content_hash_status: "pending" | "computed" | "not_available";
  error_code: "file_changed" | "read_failed" | "internal_error" | null;
};

type ImportInspectionResponse = {
  inspected_source_id: string;
};

type ImportCreateResponse = {
  import_id: string;
  status: "queued";
  source_type:
    | "local_selection"
    | "direct_url"
    | "google_drive"
    | "dropbox"
    | "onedrive"
    | "youtube"
    | "webpage";
  provenance_status: "pending_download_hash";
};

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function wait(milliseconds: number) {
  return new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds));
}

function formatBytes(value: number) {
  if (value >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(2)} GB`;
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)} MB`;
  return `${Math.max(0, value).toLocaleString("ko-KR")} B`;
}

function safeErrorMessage(error: unknown) {
  return error instanceof Error ? error.message : "입력 검사를 완료하지 못했습니다.";
}

function compactSourceTail(value: string) {
  const parts = value.split(/[\\/]+/).filter(Boolean);
  if (parts.length === 0) return value;
  return parts.at(-1) ?? value;
}

export function ImportScreen({
  onNavigate,
}: {
  onNavigate: (screen: ScreenId) => void;
}) {
  const [sourceType, setSourceType] = useState<"network" | "local" | "url">("network");
  const [source, setSource] = useState(NETWORK_PATH);
  const [rightsConfirmed, setRightsConfirmed] = useState(false);
  const [inspection, setInspection] = useState<InspectionState>("idle");
  const [selectedFile, setSelectedFile] = useState("");
  const [candidates, setCandidates] = useState<MediaCandidate[]>([]);
  const [inspectionProgress, setInspectionProgress] = useState(0);
  const [inspectionMessage, setInspectionMessage] = useState("검사 전");
  const [inspectionError, setInspectionError] = useState<string | null>(null);
  const [probeResult, setProbeResult] = useState<ProbeResponse | null>(null);
  const [stableSummary, setStableSummary] = useState<StabilityResponse | null>(null);
  const [hashJob, setHashJob] = useState<HashJobResponse | null>(null);
  const [registered, setRegistered] = useState(false);
  const [localSelectionId, setLocalSelectionId] = useState<string | null>(null);
  const [inspectedSourceId, setInspectedSourceId] = useState<string | null>(
    null,
  );
  const fileInput = useRef<HTMLInputElement>(null);
  const inspectionRun = useRef(0);

  async function resolveProjectId() {
    return requireActiveProjectId();
  }

  async function cancelStaleHash(jobToCancel: HashJobResponse | null) {
    if (
      !window.trafficVisson ||
      !jobToCancel ||
      !["queued", "running"].includes(jobToCancel.status)
    ) {
      return;
    }
    try {
      const projectId = await resolveProjectId();
      await window.trafficVisson.media.cancelHash({
        projectId,
        jobId: jobToCancel.job_id,
      });
    } catch {
      // The old result is invalidated locally even if its worker already ended.
    }
  }

  function invalidateInspection({
    preserveCandidates = false,
    message = "입력이 변경되었습니다. 다시 검사하세요.",
  }: {
    preserveCandidates?: boolean;
    message?: string;
  } = {}) {
    inspectionRun.current += 1;
    void cancelStaleHash(hashJob);
    setInspection("idle");
    setInspectionProgress(0);
    setInspectionMessage(message);
    setInspectionError(null);
    setProbeResult(null);
    setStableSummary(null);
    setHashJob(null);
    setRegistered(false);
    setInspectedSourceId(null);
    if (!preserveCandidates) setCandidates([]);
  }

  async function inspectSource() {
    if (!rightsConfirmed || !source.trim()) return;
    const run = inspectionRun.current + 1;
    inspectionRun.current = run;
    setInspection("checking");
    setInspectionError(null);
    setProbeResult(null);
    setStableSummary(null);
    setHashJob(null);
    setRegistered(false);
    setInspectionProgress(8);
    setInspectionMessage("첫 번째 메타데이터 스냅샷을 읽는 중");

    try {
      if (!window.trafficVisson) {
        if (run !== inspectionRun.current) return;
        setInspectionProgress(0);
        setInspectionMessage("설치형 앱에서 실제 입력 검사가 필요합니다.");
        setInspectionError(
          "브라우저 화면은 파일·프로젝트·SHA-256 검증 결과를 만들지 않습니다. TrafficVisson 설치형 앱에서 다시 검사하세요.",
        );
        setInspection("waiting");
        return;
      }

      if (sourceType === "network") {
        const projectId = await resolveProjectId();
        if (run !== inspectionRun.current) return;
        const snapshots: SnapshotResponse[] = [];
        for (let index = 0; index < 3; index += 1) {
          if (run !== inspectionRun.current) return;
          const snapshot = (await window.trafficVisson.media.captureSnapshot({
            projectId,
            directory: source,
          })) as SnapshotResponse;
          if (run !== inspectionRun.current) return;
          snapshots.push(snapshot);
          setCandidates(
            snapshot.files.map((file) => ({
              name: file.name,
              size: formatBytes(file.size_bytes),
              changed: new Date(file.modified_time_ns / 1_000_000).toLocaleString("ko-KR"),
              state: `${index + 1}/3 스냅샷`,
            })),
          );
          setInspectionProgress(20 + index * 28);
          if (index < 2) {
            setInspectionMessage(
              `${index + 1}/3 완료 · 다음 크기 비교까지 10초`,
            );
            await wait(10_050);
          }
        }
        if (run !== inspectionRun.current) return;
        setInspectionMessage("A/B/C 안정성을 비교하는 중");
        const stability = (await window.trafficVisson.media.compareStability({
          projectId,
          snapshotIds: snapshots.map((snapshot) => snapshot.snapshot_id),
        })) as StabilityResponse;
        if (run !== inspectionRun.current) return;
        setStableSummary(stability);
        if (!stability.stable || !stability.stable_snapshot_id) {
          setInspection("waiting");
          setInspectionProgress(100);
          setInspectionMessage(`안정성 미확인 · ${stability.reason}`);
          return;
        }
        const names = snapshots[2].files.map((file) => file.name);
        const preferred = names.includes(selectedFile) ? selectedFile : names[0];
        if (!preferred) {
          setInspection("waiting");
          setInspectionMessage("지원되는 영상이 없습니다.");
          return;
        }
        setSelectedFile(preferred);
        setInspectionMessage(`${preferred} 미디어 스트림 검증 중`);
        setInspectionProgress(92);
        const probe = (await window.trafficVisson.media.probe({
          projectId,
          snapshotId: stability.stable_snapshot_id,
          fileName: preferred,
          timeoutSeconds: 30,
        })) as ProbeResponse;
        if (run !== inspectionRun.current) return;
        setProbeResult(probe);
        setCandidates((current) =>
          current.map((file) => ({ ...file, state: "A/B/C 안정" })),
        );
        setInspectionProgress(100);
        setInspectionMessage("안정성·스트림 검증 완료");
        setInspection("ready");
        return;
      }

      if (sourceType === "local" && localSelectionId) {
        const projectId = await resolveProjectId();
        if (run !== inspectionRun.current) return;
        const inspected = (await window.trafficVisson.imports.inspect({
          projectId,
          sourceType: "local_selection",
          localSelectionId,
        })) as ImportInspectionResponse;
        if (
          typeof inspected.inspected_source_id !== "string" ||
          !UUID_PATTERN.test(inspected.inspected_source_id)
        ) {
          throw new Error("입력 검사 식별자를 검증하지 못했습니다.");
        }
        setInspectedSourceId(inspected.inspected_source_id);
        if (run !== inspectionRun.current) return;
        setInspectionProgress(35);
        setInspectionMessage("선택 파일을 안전한 미디어 입력으로 등록하는 중");
        const snapshot =
          (await window.trafficVisson.media.registerLocalSelection({
            projectId,
            localSelectionId,
          })) as SnapshotResponse;
        if (run !== inspectionRun.current) return;
        const selected = snapshot.files[0];
        if (!selected || snapshot.file_count !== 1) {
          throw new Error("선택한 로컬 영상을 등록하지 못했습니다.");
        }
        setSelectedFile(selected.name);
        setCandidates([
          {
            name: selected.name,
            size: formatBytes(selected.size_bytes),
            changed: new Date(
              selected.modified_time_ns / 1_000_000,
            ).toLocaleString("ko-KR"),
            state: "로컬 선택 등록",
          },
        ]);
        const stability: StabilityResponse = {
          stable: true,
          reason: "stable",
          stable_snapshot_id: snapshot.snapshot_id,
          file_count: 1,
          total_size_bytes: selected.size_bytes,
          observed_interval_seconds: [0, 0],
        };
        setStableSummary(stability);
        setInspectionProgress(65);
        setInspectionMessage(`${selected.name} 미디어 스트림 검증 중`);
        const probe = (await window.trafficVisson.media.probe({
          projectId,
          snapshotId: snapshot.snapshot_id,
          fileName: selected.name,
          timeoutSeconds: 30,
        })) as ProbeResponse;
        if (run !== inspectionRun.current) return;
        setProbeResult(probe);
        setInspectionProgress(100);
        setInspectionMessage("로컬 선택·스트림 검증 완료");
        setInspection("ready");
        return;
      }

      if (sourceType === "url") {
        const projectId = await resolveProjectId();
        if (run !== inspectionRun.current) return;
        const inspected = (await window.trafficVisson.imports.inspect({
          projectId,
          sourceType: "direct_url",
          sourceUrl: source,
        })) as ImportInspectionResponse;
        if (
          typeof inspected.inspected_source_id !== "string" ||
          !UUID_PATTERN.test(inspected.inspected_source_id)
        ) {
          throw new Error("URL 검사 식별자를 검증하지 못했습니다.");
        }
        setInspectedSourceId(inspected.inspected_source_id);
        if (run !== inspectionRun.current) return;
        setCandidates([]);
        setStableSummary(null);
        setProbeResult(null);
        setInspectionProgress(100);
        setInspectionMessage(
          "URL 형식·접근 정책 검사 완료 · 다운로드와 프로젝트 등록은 아직 연결되지 않음",
        );
        setInspection("waiting");
        return;
      }

      throw new Error("선택한 로컬 파일 핸들이 없습니다. 파일을 다시 선택하세요.");
    } catch (error) {
      if (run !== inspectionRun.current) return;
      setInspectionError(safeErrorMessage(error));
      setInspectionMessage("검사 실패");
      setInspection("waiting");
    }
  }

  async function persistVerifiedImport(
    projectId: string,
    snapshotId: string,
    sha256: string,
    hashJobId?: string,
  ) {
    if (!probeResult) {
      throw new Error("스트림 검증 결과가 없어 등록할 수 없습니다.");
    }
    if (inspectedSourceId && window.trafficVisson) {
      const persisted = (await window.trafficVisson.imports.create({
        projectId,
        inspectedSourceId,
        rightsConfirmed: true,
      })) as ImportCreateResponse;
      if (
        typeof persisted.import_id !== "string" ||
        !UUID_PATTERN.test(persisted.import_id) ||
        persisted.status !== "queued" ||
        persisted.provenance_status !== "pending_download_hash"
      ) {
        throw new Error("프로젝트 입력 등록 응답을 검증하지 못했습니다.");
      }
    }
    registerImport({
      projectId,
      fileName: probeResult.file_name,
      sizeBytes: probeResult.size_bytes,
      durationSeconds: probeResult.duration_seconds,
      snapshotId,
      snapshotDigest: probeResult.snapshot_digest,
      sha256,
      hashJobId,
      registeredAt: new Date().toISOString(),
    });
    setRegistered(true);
    setInspectionMessage("SHA-256 검증 및 프로젝트 등록 완료");
  }

  async function hashAndRegister() {
    if (registered) {
      onNavigate("analysis");
      return;
    }
    if (!probeResult || !stableSummary?.stable_snapshot_id) return;

    const projectId = await resolveProjectId();
    if (!window.trafficVisson) return;

    const run = inspectionRun.current;
    setInspectionError(null);
    setInspectionMessage(`${probeResult.file_name} SHA-256 계산 시작`);
    try {
      let current = (await window.trafficVisson.media.startHash({
        projectId,
        snapshotId: stableSummary.stable_snapshot_id,
        fileName: probeResult.file_name,
      })) as HashJobResponse;
      if (run !== inspectionRun.current) {
        void cancelStaleHash(current);
        return;
      }
      setHashJob(current);

      while (
        run === inspectionRun.current &&
        ["queued", "running", "cancelling"].includes(current.status)
      ) {
        await wait(350);
        if (run !== inspectionRun.current) return;
        current = (await window.trafficVisson.media.getHash({
          projectId,
          jobId: current.job_id,
        })) as HashJobResponse;
        if (run !== inspectionRun.current) return;
        setHashJob(current);
      }

      if (run !== inspectionRun.current) return;
      if (
        current.status !== "completed" ||
        current.content_hash_status !== "computed" ||
        !current.sha256_lower
      ) {
        const reason =
          current.status === "cancelled"
            ? "사용자가 SHA-256 계산을 취소했습니다."
            : `SHA-256 계산 실패 · ${current.error_code ?? "알 수 없는 오류"}`;
        throw new Error(reason);
      }
      await persistVerifiedImport(
        projectId,
        current.snapshot_id,
        current.sha256_lower,
        current.job_id,
      );
    } catch (error) {
      if (run !== inspectionRun.current) return;
      setInspectionError(safeErrorMessage(error));
      setInspectionMessage("SHA-256 계산을 완료하지 못했습니다.");
    }
  }

  async function cancelHash() {
    if (!window.trafficVisson || !hashJob) return;
    const projectId = await resolveProjectId();
    const next = (await window.trafficVisson.media.cancelHash({
      projectId,
      jobId: hashJob.job_id,
    })) as HashJobResponse;
    setHashJob(next);
  }

  async function chooseLocalFile() {
    if (window.trafficVisson) {
      const selection = await window.trafficVisson.dialogs.selectVideos();
      if (selection.files.length > 0) {
        invalidateInspection();
        setLocalSelectionId(selection.selectionId);
        setSource(selection.files[0].name);
        setSourceType("local");
      }
      return;
    }
    fileInput.current?.click();
  }

  return (
    <div className="screen screen--import">
      <header className="screen-header">
        <div>
          <p className="eyebrow">SAFE MEDIA INTAKE</p>
          <h1>영상 가져오기</h1>
          <p>입력 위치를 먼저 검사하고 권리·무결성·업로드 완료 여부를 확인합니다.</p>
        </div>
        <div className="header-actions">
          <Badge tone="success">안전한 파일 선택</Badge>
          <Badge tone="neutral">SHA-256 기록</Badge>
        </div>
      </header>

      <div className="import-layout">
        <GlassPanel className="source-panel">
          <PanelHeader eyebrow="1 · SOURCE" title="어디에서 가져올까요?" />
          <div className="source-tabs" role="group" aria-label="입력 유형">
            <button
              type="button"
              className={sourceType === "network" ? "selected" : ""}
              onClick={() => {
                invalidateInspection();
                setSourceType("network");
                setSource(NETWORK_PATH);
                setLocalSelectionId(null);
              }}
            >
              <Network size={19} /> 네트워크 폴더
            </button>
            <button
              type="button"
              className={sourceType === "local" ? "selected" : ""}
              onClick={chooseLocalFile}
            >
              <FolderOpen size={19} /> 로컬 파일
            </button>
            <button
              type="button"
              className={sourceType === "url" ? "selected" : ""}
              onClick={() => {
                invalidateInspection();
                setSourceType("url");
                setSource("");
                setLocalSelectionId(null);
              }}
            >
              <Link2 size={19} /> 영상 링크 · 검사 전용
            </button>
          </div>

          <label className="field">
            <span className="field-label-row">
              <span>{sourceType === "url" ? "영상 URL" : "파일 또는 폴더 경로"}</span>
              {sourceType !== "url" && source.trim() ? (
                <code className="source-path-tail" title={source}>
                  대상 · {compactSourceTail(source)}
                </code>
              ) : null}
            </span>
            <div className="input-with-icon">
              {sourceType === "url" ? <Link2 size={18} /> : <Network size={18} />}
              <input
                value={source}
                title={source || undefined}
                readOnly={sourceType === "local"}
                onChange={(event) => {
                  invalidateInspection();
                  setSource(event.target.value);
                }}
                placeholder={
                  sourceType === "url"
                    ? "https://drive.google.com/…"
                    : "영상이 있는 경로를 선택하세요"
                }
              />
            </div>
          </label>
          <input
            ref={fileInput}
            hidden
            type="file"
            accept="video/mp4,video/quicktime,.mp4,.mov"
            onChange={(event) => {
              const file = event.currentTarget.files?.[0];
              if (file) {
                invalidateInspection();
                setSource(file.name);
                setSourceType("local");
              }
            }}
          />

          <label className="rights-check">
            <input
              type="checkbox"
              checked={rightsConfirmed}
              onChange={(event) => {
                if (!event.target.checked) invalidateInspection();
                setRightsConfirmed(event.target.checked);
              }}
            />
            <span className="check-visual" aria-hidden="true">
              <Check size={14} />
            </span>
            <span>
              <strong>이 영상을 다운로드·분석할 권리가 있습니다.</strong>
              <small>필수 확인 · 원본 사이트 스크립트나 실행 파일은 절대 실행하지 않습니다.</small>
            </span>
          </label>

          <Button
            variant="primary"
            className="full-width"
            onClick={inspectSource}
            disabled={!rightsConfirmed || !source.trim() || inspection === "checking"}
          >
            <FileSearch size={18} />
            {inspection === "checking" ? "안전하게 검사 중…" : "입력 검사"}
          </Button>

          <div className="support-tiers">
            <h3>지원 등급</h3>
            {[
              ["연결됨", "로컬 MP4/MOV · 네트워크 폴더", "success"],
              ["검사 전용", "직접 URL · Drive · YouTube · 일반 웹페이지", "warning"],
              ["미구현", "URL 다운로드·프로젝트 등록", "neutral"],
              ["제외", "DRM · 암호화 · 권한 없는 비공개", "danger"],
            ].map(([tier, description, tone]) => (
              <div key={tier}>
                <Badge tone={tone as "success" | "warning" | "neutral" | "danger"}>
                  {tier}
                </Badge>
                <span>{description}</span>
              </div>
            ))}
          </div>
        </GlassPanel>

        <GlassPanel className="inspection-panel">
          <PanelHeader
            eyebrow="2 · INSPECT"
            title="업로드 완료 확인"
            action={
              inspection === "idle" ? (
                <Badge tone="neutral">검사 전</Badge>
              ) : inspection === "checking" ? (
                <Badge tone="accent">검사 중</Badge>
              ) : inspection === "ready" ? (
                <Badge tone="success">
                  입력 안정 · {candidates.length}개
                </Badge>
              ) : sourceType === "url" && !inspectionError ? (
                <Badge tone="warning">URL 검사만 완료</Badge>
              ) : (
                <Badge tone="warning">대기 필요</Badge>
              )
            }
          />

          {inspection === "idle" ? (
            <div className="empty-state">
              <UploadCloud size={36} strokeWidth={1.5} />
              <h3>안전 검사를 시작하세요</h3>
              <p>파일명·용량·MIME·magic·수정 시각을 읽고 완료된 영상만 후보로 표시합니다.</p>
            </div>
          ) : inspection === "checking" ? (
            <div className="inspection-loading" aria-live="polite">
              <FileSearch size={32} />
              <h3>{inspectionMessage}</h3>
              <ProgressBar
                value={inspectionProgress}
                label={`입력 검사 진행률 ${inspectionProgress}%`}
              />
              <p>A/B/C 간격 비교 중에는 영상 본문을 읽지 않고 메타데이터만 확인합니다.</p>
            </div>
          ) : (
            <>
              <div
                className={`notice ${
                  inspection === "ready" ? "notice--success" : "notice--warning"
                }`}
              >
                {inspection === "ready" ? <Check size={20} /> : <CircleAlert size={20} />}
                <div>
                  <strong>
                    {inspection === "ready"
                      ? "업로드 안정성 확인 완료"
                      : sourceType === "url" && !inspectionError
                        ? "URL 안전성 검사 완료"
                        : "재검사 필요"}
                  </strong>
                  <p>
                    {inspectionError ??
                      (stableSummary
                        ? `${stableSummary.file_count}개 · ${formatBytes(
                            stableSummary.total_size_bytes,
                          )} · 간격 ${stableSummary.observed_interval_seconds
                            .map((value) => value.toFixed(1))
                            .join("초 / ")}초`
                        : inspectionMessage)}
                  </p>
                </div>
              </div>

              {candidates.length ? (
                <div className="file-candidates" aria-label={`발견된 영상 ${candidates.length}개`}>
                  {candidates.map((file) => (
                  <button
                    type="button"
                    key={file.name}
                    className={selectedFile === file.name ? "selected" : ""}
                    disabled={hashJob?.status === "running" || hashJob?.status === "queued"}
                    onClick={() => {
                      if (file.name === selectedFile) return;
                      invalidateInspection({ preserveCandidates: true });
                      setSelectedFile(file.name);
                      setInspection("waiting");
                      setInspectionMessage("선택한 파일을 다시 검사하세요.");
                    }}
                  >
                    <FileVideo2 size={20} />
                    <span>
                      <strong>{file.name}</strong>
                      <small>{file.size} · {file.changed}</small>
                    </span>
                    <Badge tone="warning">{file.state}</Badge>
                  </button>
                  ))}
                </div>
              ) : null}

              {sourceType !== "url" ? (
                <div className="stability-rule">
                  <ShieldCheck size={20} />
                  <div>
                    <strong>완료 판정 규칙</strong>
                    <p>2회 연속 크기·수정 시각 동일 → 파일 잠금 해제 → ffprobe 성공 → SHA-256 확정</p>
                  </div>
                </div>
              ) : (
                <div className="stability-rule">
                  <CircleAlert size={20} />
                  <div>
                    <strong>현재 URL 기능은 검사 전용입니다.</strong>
                    <p>URL 다운로드, 영상 저장, 프로젝트 등록은 아직 연결되지 않았습니다.</p>
                  </div>
                </div>
              )}

              <Button
                variant="primary"
                className="full-width"
                onClick={inspectSource}
              >
                {sourceType === "url"
                  ? "URL 다시 검사"
                  : sourceType === "local"
                  ? "선택 파일 다시 검사"
                  : "A/B/C 다시 검사 · 약 20초"}
              </Button>
            </>
          )}

          {inspection === "ready" ? (
            <div className="notice notice--success" aria-live="polite">
              <Check size={20} />
              <div>
                <strong>입력 등록 준비 완료</strong>
                <p>
                  {probeResult
                    ? `${probeResult.file_name} · ${probeResult.video_streams[0]?.codec_name.toUpperCase()} · ${
                        probeResult.video_streams[0]?.width
                      }×${probeResult.video_streams[0]?.height} · ${Math.round(
                        probeResult.duration_seconds,
                      )}초`
                    : `${selectedFile} · 메타데이터 검사 완료`}
                </p>
                {hashJob ? (
                  <div className="hash-progress">
                    <ProgressBar
                      value={
                        hashJob.total_bytes > 0
                          ? (hashJob.bytes_hashed / hashJob.total_bytes) * 100
                          : 0
                      }
                      label={`SHA-256 ${formatBytes(hashJob.bytes_hashed)} / ${formatBytes(
                        hashJob.total_bytes,
                      )}`}
                    />
                    <small>
                      {hashJob.status === "completed"
                        ? `검증 완료 · ${hashJob.sha256_lower?.slice(0, 16)}…`
                        : `${formatBytes(hashJob.bytes_hashed)} / ${formatBytes(
                            hashJob.total_bytes,
                          )} · ${hashJob.status}`}
                    </small>
                  </div>
                ) : null}
                <Button
                  variant="positive"
                  onClick={hashAndRegister}
                  disabled={
                    !probeResult ||
                    hashJob?.status === "queued" ||
                    hashJob?.status === "running" ||
                    hashJob?.status === "cancelling"
                  }
                >
                  {registered
                    ? "등록 완료 · 분석 화면으로"
                    : hashJob?.status === "queued" || hashJob?.status === "running"
                      ? "SHA-256 계산 중"
                      : "SHA-256 계산 및 프로젝트 등록"}
                </Button>
                {hashJob?.status === "queued" || hashJob?.status === "running" ? (
                  <Button variant="danger" onClick={cancelHash}>
                    해시 계산 취소
                  </Button>
                ) : null}
              </div>
            </div>
          ) : null}
        </GlassPanel>

        <GlassPanel className="gps-panel">
          <PanelHeader eyebrow="3 · GPS" title="시간 동기화" />
          <div className="gps-illustration">
            <FileVideo2 size={34} />
            <span>
              {probeResult
                ? `${probeResult.file_name} · 첫 프레임 미생성`
                : "검증된 영상 없음"}
            </span>
          </div>
          <div className="notice">
            <CircleAlert size={19} />
            <div>
              <strong>GPS 자료 미연결</strong>
              <p>현재 입력에는 GPS 트랙을 등록하지 않았습니다. 오프셋과 경로 누락 구간을 아직 판정하지 않습니다.</p>
            </div>
          </div>
          <StatusDot tone="neutral">
            GPS 가져오기 기능 연결 대기
          </StatusDot>
        </GlassPanel>
      </div>
    </div>
  );
}
