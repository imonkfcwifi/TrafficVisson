import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  BrainCircuit,
  Check,
  Database,
  ShieldAlert,
  Sparkles,
} from "lucide-react";
import {
  Badge,
  Button,
  GlassPanel,
  PanelHeader,
} from "../components/ui";
import {
  buildDatasetCreateRequest,
  buildProjectScopeRequest,
  parseDatasetVersion,
  parseDatasetVersionList,
  parseProjectLabelStats,
  parseSourceCandidateCatalog,
  type DatasetVersion,
  type ProjectLabelStats,
  type SourceCandidate,
  type SourceCandidateCatalog,
} from "../modelRegistry";
import { getActiveProjectId, useProjectCatalog } from "../projectCatalog";

async function readModelRegistry(projectId: string) {
  const api = window.trafficVisson;
  if (!api?.labels || !api.datasets) {
    throw new Error("모델 저장소는 설치형 Electron 앱에서 연결됩니다.");
  }
  const request = buildProjectScopeRequest(projectId);
  const [statsValue, versionsValue] = await Promise.all([
    api.labels.stats(request),
    api.datasets.list(request),
  ]);
  return {
    stats: parseProjectLabelStats(statsValue, projectId),
    versions: parseDatasetVersionList(versionsValue, projectId).versions,
  };
}

async function readSourceCandidates() {
  const api = window.trafficVisson;
  if (!api?.sources) {
    throw new Error("외부 소스 카탈로그는 설치형 Electron 앱에서 연결됩니다.");
  }
  return parseSourceCandidateCatalog(await api.sources.candidates());
}

function errorMessage(error: unknown) {
  return error instanceof Error
    ? error.message
    : "프로젝트 모델 정보를 불러오지 못했습니다.";
}

function formatCreatedAt(value: string) {
  return new Intl.DateTimeFormat("ko-KR", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function sourceCapabilityLabel(candidate: SourceCandidate) {
  switch (candidate.capability) {
    case "natural_language_box_proposals":
      return "자연어 박스 후보";
    case "prompted_mask_refinement":
      return "클릭·박스 마스크 보정";
    case "semantic_candidate_scoring":
      return "자연어 의미 후보 점수";
    case "pothole_box_proposals":
      return "포트홀 박스 후보";
    case "labeled_training_reference":
      return "라벨 학습 참고";
  }
}

function sourceDecisionLabel(candidate: SourceCandidate) {
  switch (candidate.catalogStatus) {
    case "evaluation_complete":
      return "평가 완료 · 검증용";
    case "unverified_local":
      return "검증 실패 · 격리";
    case "rejected":
      return candidate.installationStatus === "not_installed"
        ? "거부됨 · 미설치"
        : "거부됨 · 격리";
    case "review_candidate":
      return "검토 후보 · 미설치";
  }
}

function sourceDecisionSummary(candidate: SourceCandidate) {
  if (candidate.evaluation?.verdict === "rejected") {
    return `오탐 ${candidate.evaluation.falsePositiveCount} / ${candidate.evaluation.proposalCount} · 사용 거부`;
  }
  if (candidate.evaluation) {
    return `평가 ${candidate.evaluation.evaluatedFrameCount}프레임 · 후보 ${candidate.evaluation.proposalCount} · 자동확정 금지`;
  }
  if (candidate.observationBasis === "unverified_local_files") {
    return "고정 증거 해시 불일치 · 사용 금지";
  }
  if (candidate.catalogStatus === "rejected") {
    return "배포 후보 거부 · 자동 사용 금지";
  }
  return candidate.sourceType === "labeled_dataset"
    ? "미설치 · 품질·중복·분할 검증 전"
    : "미설치 · 자동 활성화 금지";
}

export function ModelScreen() {
  const projectCatalog = useProjectCatalog();
  const activeProject = projectCatalog.projects.find(
    (project) => project.id === projectCatalog.activeProjectId,
  );
  const projectId = activeProject?.id ?? null;
  const [stats, setStats] = useState<ProjectLabelStats | null>(null);
  const [versions, setVersions] = useState<DatasetVersion[]>([]);
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sourceCatalog, setSourceCatalog] =
    useState<SourceCandidateCatalog | null>(null);
  const [sourceLoading, setSourceLoading] = useState(true);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [message, setMessage] = useState(
    "선택한 프로젝트의 라벨과 데이터셋 상태를 확인합니다.",
  );

  useEffect(() => {
    let active = true;
    setSourceLoading(true);
    setSourceError(null);
    void readSourceCandidates()
      .then((catalog) => {
        if (active) setSourceCatalog(catalog);
      })
      .catch((reason) => {
        if (!active) return;
        setSourceCatalog(null);
        setSourceError(
          reason instanceof Error
            ? reason.message
            : "외부 소스 카탈로그를 불러오지 못했습니다.",
        );
      })
      .finally(() => {
        if (active) setSourceLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    setStats(null);
    setVersions([]);
    setCreating(false);
    setError(null);
    if (!projectId) {
      setLoading(false);
      setMessage("프로젝트를 선택하면 모델 준비 상태를 확인할 수 있습니다.");
      return () => {
        active = false;
      };
    }
    setLoading(true);
    setMessage("현재 프로젝트의 라벨과 데이터셋을 동기화하는 중입니다.");
    void readModelRegistry(projectId)
      .then((registry) => {
        if (!active) return;
        setStats(registry.stats);
        setVersions(registry.versions);
        setMessage(
          registry.stats.currentAnswerCount > 0
            ? `사용자 확정 답변 ${registry.stats.currentAnswerCount}건을 불러왔습니다.`
            : "사용자 확정 답변이 1건 이상이면 데이터셋 스냅샷을 만들 수 있습니다.",
        );
      })
      .catch((reason) => {
        if (!active) return;
        setError(errorMessage(reason));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [projectId]);

  const latestDataset = useMemo(
    () =>
      versions.reduce<DatasetVersion | null>((latest, version) => {
        if (!latest) return version;
        return Date.parse(version.createdAt) >= Date.parse(latest.createdAt)
          ? version
          : latest;
      }, null),
    [versions],
  );
  const trainingIncludedCount = stats?.trainingIncludedCount ?? 0;
  const validationCount = latestDataset?.validationCount ?? 0;
  const enoughConfirmed = trainingIncludedCount >= 100;
  const validationReady = validationCount >= 30;
  const quantityGateReady = enoughConfirmed && validationReady;
  const canCreateSnapshot =
    Boolean(projectId) &&
    Boolean(stats && stats.currentAnswerCount > 0) &&
    !loading &&
    !creating;
  const snapshotTitle = !projectId
    ? "먼저 프로젝트를 선택하세요."
    : loading
      ? "프로젝트 라벨을 불러오는 중입니다."
      : !stats || stats.currentAnswerCount <= 0
        ? "사용자가 확정한 답변이 1건 이상 필요합니다."
        : creating
          ? "데이터셋 스냅샷을 저장하는 중입니다."
          : "현재 프로젝트의 라벨로 불변 데이터셋 버전을 만듭니다.";

  async function createDatasetSnapshot() {
    const api = window.trafficVisson;
    if (!projectId || !stats || stats.currentAnswerCount <= 0 || !api) return;
    setCreating(true);
    setError(null);
    setMessage("현재 확정 답변으로 데이터셋 스냅샷을 만드는 중입니다.");
    try {
      const created = parseDatasetVersion(
        await api.datasets.create(buildDatasetCreateRequest(projectId)),
        projectId,
      );
      const registry = await readModelRegistry(projectId);
      if (getActiveProjectId() !== projectId) return;
      setStats(registry.stats);
      setVersions(registry.versions);
      setMessage(
        created.reused
          ? "현재 라벨과 동일한 DatasetVersion을 다시 사용합니다."
          : "새 DatasetVersion을 저장했습니다. 학습은 자동으로 시작되지 않습니다.",
      );
    } catch (reason) {
      if (getActiveProjectId() === projectId) {
        setError(errorMessage(reason));
      }
    } finally {
      if (getActiveProjectId() === projectId) {
        setCreating(false);
      }
    }
  }

  return (
    <div className="screen screen--model">
      <header className="screen-header">
        <div>
          <p className="eyebrow">PROJECT MODEL REGISTRY</p>
          <h1>모델 관리</h1>
          <p>프로젝트의 확정 라벨만 사용하며 다른 프로젝트 데이터는 자동으로 섞지 않습니다.</p>
        </div>
        <div className="header-actions">
          <Button
            disabled={!canCreateSnapshot}
            onClick={createDatasetSnapshot}
            title={snapshotTitle}
          >
            <Database size={17} />{" "}
            {creating ? "스냅샷 저장 중" : "데이터셋 스냅샷"}
          </Button>
          <Button
            variant="primary"
            disabled
            title="현재는 학습 실행 API가 연결되지 않았습니다. 확정 라벨·고정 검증 세트·자원 사전점검이 모두 필요합니다."
          >
            <Sparkles size={17} /> 새 모델 학습
          </Button>
        </div>
      </header>

      <div className="model-layout">
        <div className="model-sidebar">
          <GlassPanel>
            <PanelHeader
              eyebrow="TRAINING GATE"
              title="학습 가능 조건"
              action={
                <Badge tone={quantityGateReady ? "success" : "warning"}>
                  {loading
                    ? "동기화 중"
                    : quantityGateReady
                      ? "학습 API 대기"
                      : "기준 확인"}
                </Badge>
              }
            />
            <ul className="gate-list">
              <li className={enoughConfirmed ? "pass" : "warn"}>
                {enoughConfirmed ? <Check size={17} /> : <AlertTriangle size={17} />}
                <span>학습 포함 라벨 100건 이상</span>
                <strong>{trainingIncludedCount} / 100</strong>
              </li>
              <li className={validationReady ? "pass" : "warn"}>
                {validationReady ? <Check size={17} /> : <AlertTriangle size={17} />}
                <span>고정 검증 표본 30건 이상</span>
                <strong>{validationCount} / 30</strong>
              </li>
              <li className={latestDataset ? "pass" : "warn"}>
                {latestDataset ? <Check size={17} /> : <AlertTriangle size={17} />}
                <span>불변 DatasetVersion</span>
                <strong>{latestDataset ? `${versions.length}개` : "생성 전"}</strong>
              </li>
              <li className={activeProject ? "pass" : "warn"}>
                {activeProject ? <Check size={17} /> : <AlertTriangle size={17} />}
                <span>프로젝트 데이터 격리</span>
                <strong>
                  {activeProject ? `${activeProject.name}만` : "선택 필요"}
                </strong>
              </li>
            </ul>
            <div className="notice notice--warning">
              <ShieldAlert size={19} />
              <div>
                <strong>현재는 학습을 시작하지 않습니다.</strong>
                <p>
                  학습 실행 API와 유형별 검증 분포가 아직 연결되지 않았습니다.
                  기준을 충족해도 예상 GPU·디스크를 확인하기 전에는 학습 버튼을
                  활성화하지 않습니다.
                </p>
              </div>
            </div>
          </GlassPanel>

          <GlassPanel>
            <PanelHeader
              eyebrow="CURRENT LABELS"
              title="현재 라벨 분류"
              action={
                <Badge tone={stats?.currentAnswerCount ? "success" : "neutral"}>
                  최신 답변 {stats?.currentAnswerCount ?? 0}
                </Badge>
              }
            />
            <ul className="gate-list">
              <li className={stats?.partitions.approved ? "pass" : "warn"}>
                <Check size={17} />
                <span>승인 라벨</span>
                <strong>{stats?.partitions.approved ?? 0}</strong>
              </li>
              <li className={stats?.partitions.negative ? "pass" : "warn"}>
                <Check size={17} />
                <span>음성 라벨 · 해당 없음</span>
                <strong>{stats?.partitions.negative ?? 0}</strong>
              </li>
              <li className="warn">
                <AlertTriangle size={17} />
                <span>판단 보류</span>
                <strong>{stats?.partitions.held ?? 0}</strong>
              </li>
              <li className="warn">
                <AlertTriangle size={17} />
                <span>학습 제외</span>
                <strong>{stats?.partitions.excluded ?? 0}</strong>
              </li>
            </ul>
            <p className="model-message">
              질문 {stats?.questionCount ?? 0} · 답변 이력{" "}
              {stats?.answerRevisionCount ?? 0} · 출처 분석{" "}
              {stats?.sourceAnalysisJobCount ?? 0}건
            </p>
          </GlassPanel>
        </div>

        <div className="model-main">
          <GlassPanel className="model-comparison">
            <PanelHeader
              eyebrow="READ-ONLY SOURCE CATALOG"
              title="외부 모델·데이터 소스 후보"
              action={
                <Badge
                  tone={
                    sourceError
                      ? "danger"
                      : sourceCatalog
                        ? "warning"
                        : "neutral"
                  }
                >
                  {sourceLoading
                    ? "불러오는 중"
                    : sourceError
                      ? "연결 오류"
                      : `${sourceCatalog?.count ?? 0}개 · 비활성`}
                </Badge>
              }
            />
            {sourceLoading ? (
              <p className="model-message" role="status">
                검토 전용 후보 메타데이터를 불러오는 중입니다.
              </p>
            ) : sourceError ? (
              <p className="model-message" role="alert">
                {sourceError}
              </p>
            ) : (
              <>
                <div className="model-table-wrap">
                  <table className="data-table model-table">
                    <thead>
                      <tr>
                        <th scope="col">소스</th>
                        <th scope="col">라이선스</th>
                        <th scope="col">용도</th>
                        <th scope="col">현재 결정 · 이유</th>
                      </tr>
                    </thead>
                    <tbody>
                      {sourceCatalog?.candidates.map((candidate) => (
                        <tr
                          key={candidate.sourceId}
                          className={
                            candidate.catalogStatus === "rejected"
                              ? "regression"
                              : undefined
                          }
                        >
                          <th scope="row" title={candidate.originUrl}>
                            <strong>{candidate.displayName}</strong>
                            <p>{candidate.publisher}</p>
                          </th>
                          <td>
                            <code>{candidate.licenseSpdxId}</code>
                            {candidate.decisionReason.includes("OpenRAIL") ? (
                              <p>원 데이터셋 OpenRAIL 검토</p>
                            ) : null}
                          </td>
                          <td>{sourceCapabilityLabel(candidate)}</td>
                          <td title={candidate.decisionReason}>
                            <Badge
                              tone={
                                candidate.catalogStatus === "rejected"
                                  ? "danger"
                                  : candidate.catalogStatus ===
                                      "unverified_local"
                                    ? "danger"
                                  : candidate.catalogStatus ===
                                      "evaluation_complete"
                                    ? "accent"
                                    : "warning"
                              }
                            >
                              {sourceDecisionLabel(candidate)}
                            </Badge>
                            <p
                              className={
                                candidate.catalogStatus === "rejected" ||
                                candidate.catalogStatus === "unverified_local"
                                  ? "negative"
                                  : undefined
                              }
                            >
                              {sourceDecisionSummary(candidate)}
                            </p>
                            <span className="sr-only">
                              {candidate.decisionReason}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="notice notice--warning">
                  <ShieldAlert size={19} />
                  <div>
                    <strong>현재 프로젝트의 활성 모델이 아닙니다.</strong>
                    <p>
                      설치된 검증용 팩도 자동 활성화하지 않습니다. 이 조회는
                      다운로드·가중치 실행·자동 라벨을 하지 않으며 외부 소스를
                      프로젝트 학습과 분리합니다.
                    </p>
                  </div>
                </div>
              </>
            )}
          </GlassPanel>

          <div className="model-versions">
            <GlassPanel className="model-version-card">
              <BrainCircuit size={19} />
              <div>
                <strong>활성 모델</strong>
                <Badge tone="neutral">BASELINE</Badge>
              </div>
              <p>
                {activeProject?.activeModelVersion ?? "공통 초기 모델"} · 프로젝트
                후보 모델 미학습
              </p>
              <code>성능 지표 미산출</code>
            </GlassPanel>
            <GlassPanel className="model-version-card">
              <Database size={19} />
              <div>
                <strong>
                  {latestDataset
                    ? `DatasetVersion ${versions.length}`
                    : "데이터셋 없음"}
                </strong>
                <Badge tone={validationCount > 0 ? "success" : "warning"}>
                  검증 {validationCount}
                </Badge>
              </div>
              <p>
                {latestDataset
                  ? `학습 ${latestDataset.trainCount} · 검증 ${latestDataset.validationCount} · 제외 ${latestDataset.excludedCount}`
                  : "사용자 확정 답변으로 첫 스냅샷을 만드세요."}
              </p>
              <code>
                {latestDataset
                  ? `${formatCreatedAt(latestDataset.createdAt)} · 무결성 ${latestDataset.datasetHash.slice(0, 10)}…`
                  : "아직 저장된 DatasetVersion이 없습니다."}
              </code>
            </GlassPanel>
          </div>
          <p className="model-message" role={error ? "alert" : "status"}>
            {error ??
              (projectCatalog.loading
                ? "프로젝트 목록을 불러오는 중입니다."
                : message)}
          </p>
        </div>
      </div>
    </div>
  );
}
