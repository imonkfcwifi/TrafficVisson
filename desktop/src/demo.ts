export type ScreenId =
  | "home"
  | "import"
  | "label"
  | "analysis"
  | "review"
  | "models"
  | "history"
  | "report"
  | "export";

export type Severity = "긴급" | "심각" | "보통" | "경미" | "미판정";

export type IncidentEvidence = {
  image: string;
  sourceFile: string;
  sourceSha256: string;
  sourceDurationMs: number;
  frameTimestampMs: number;
  extractionMethod: string;
  ocrText: string;
  ocrStatus: string;
  gpsStatus: string;
  positionBasis: string;
  classificationStatus: "후보·미검증";
  suggestedRegionSource?: string;
};

export type Incident = {
  id: string;
  timeMs: number;
  timecode: string;
  type: string;
  typeCode: string;
  severity: Severity;
  status: "확정" | "답변 완료" | "검증됨" | "질문 대기" | "후보·미검증";
  road: string;
  action: string;
  confidence: number | null;
  coordinates: [number, number];
  box?: { x: number; y: number; width: number; height: number };
  evidence: IncidentEvidence;
};

export type LabelQuestion = {
  id: string;
  prompt: string;
  typeCode: string;
  confidence: number;
  forced: boolean;
  rationale: string;
  timecode?: string;
  sourceFile?: string;
  evidenceImage?: string;
  previewVideo?: string;
  roiImage?: string;
  adjacentFrames?: string[];
  ocrCandidate?: string;
  locationStatus?: string;
  box?: { x: number; y: number; width: number; height: number };
};

export const evidenceImage = "/assets/korean-highway-evidence.png";
export const realTunnelEvidenceImage = "/assets/real-trip/tunnel-approach-222s.jpg";
export const realTunnelPreviewVideo = "/assets/real-trip/tunnel-approach-preview-5s.mp4";

export const incidents: Incident[] = [
  {
    id: "CAND-REAL-0060",
    timeMs: 60_000,
    timecode: "00:01:00.000",
    type: "미분류 장면 후보",
    typeCode: "FRAME_CANDIDATE",
    severity: "미판정",
    status: "후보·미검증",
    road: "천안논산 천안방향 · 위치 미검증",
    action: "사용자 검토 후 유지보수 판정",
    confidence: null,
    coordinates: [127.9321, 36.783],
    evidence: {
      image: "/assets/real-trip/review-candidates/candidate-001-060s.jpg",
      sourceFile: "천안논산 천안방향.mp4",
      sourceSha256: "E263AD878EED64E980DF26C2347E4482196AAE0B13865761138727E9E5858D74",
      sourceDurationMs: 583_448,
      frameTimestampMs: 60_000,
      extractionMethod: "ffmpeg 단일 프레임 추출 · 1280×720",
      ocrText: "OCR 미실행",
      ocrStatus: "텍스트 후보 없음 · 미검증",
      gpsStatus: "원본 영상에 연결된 GPS 없음",
      positionBasis: "지도 동기화용 시연 좌표 · 원본 위치로 사용 금지",
      classificationStatus: "후보·미검증",
    },
  },
  {
    id: "CAND-REAL-0180",
    timeMs: 180_000,
    timecode: "00:03:00.000",
    type: "텍스트 포함 장면 후보",
    typeCode: "TEXT_FRAME_CANDIDATE",
    severity: "미판정",
    status: "후보·미검증",
    road: "천안논산 천안방향 · 위치 미검증",
    action: "OCR·유형 사용자 확인 필요",
    confidence: null,
    coordinates: [127.9256, 36.7772],
    evidence: {
      image: "/assets/real-trip/review-candidates/candidate-002-180s.jpg",
      sourceFile: "천안논산 천안방향.mp4",
      sourceSha256: "E263AD878EED64E980DF26C2347E4482196AAE0B13865761138727E9E5858D74",
      sourceDurationMs: 583_448,
      frameTimestampMs: 180_000,
      extractionMethod: "ffmpeg 단일 프레임 추출 · 1280×720",
      ocrText: "OCR 미실행",
      ocrStatus: "표지 텍스트가 보이나 아직 판독하지 않음",
      gpsStatus: "원본 영상에 연결된 GPS 없음",
      positionBasis: "지도 동기화용 시연 좌표 · 원본 위치로 사용 금지",
      classificationStatus: "후보·미검증",
    },
  },
  {
    id: "CAND-REAL-0222",
    timeMs: 222_600,
    timecode: "00:03:42.600",
    type: "터널명 OCR 후보",
    typeCode: "TUNNEL_TEXT_CANDIDATE",
    severity: "미판정",
    status: "후보·미검증",
    road: "천안논산 천안방향 · 위치 미검증",
    action: "터널명·가림 여부 사용자 확인 필요",
    confidence: 0.42,
    coordinates: [127.9179, 36.7709],
    box: { x: 39, y: 19, width: 21, height: 27 },
    evidence: {
      image: realTunnelEvidenceImage,
      sourceFile: "천안논산 천안방향.mp4",
      sourceSha256: "E263AD878EED64E980DF26C2347E4482196AAE0B13865761138727E9E5858D74",
      sourceDurationMs: 583_448,
      frameTimestampMs: 222_600,
      extractionMethod: "ffmpeg 단일 프레임·OCR ROI 추출 · 1280×720",
      ocrText: "판독 불가 · 사용자 확인 필요",
      ocrStatus: "저신뢰 OCR 후보 · 확정 아님",
      gpsStatus: "원본 영상에 연결된 GPS 없음",
      positionBasis: "지도 동기화용 시연 좌표 · 원본 위치로 사용 금지",
      classificationStatus: "후보·미검증",
      suggestedRegionSource: "질문 Q-REAL-0001의 AI 제안 영역 · 사용자 미승인",
    },
  },
  {
    id: "CAND-REAL-0300",
    timeMs: 300_000,
    timecode: "00:05:00.000",
    type: "미분류 장면 후보",
    typeCode: "FRAME_CANDIDATE",
    severity: "미판정",
    status: "후보·미검증",
    road: "천안논산 천안방향 · 위치 미검증",
    action: "사용자 검토 후 유지보수 판정",
    confidence: null,
    coordinates: [127.9088, 36.7658],
    evidence: {
      image: "/assets/real-trip/review-candidates/candidate-003-300s.jpg",
      sourceFile: "천안논산 천안방향.mp4",
      sourceSha256: "E263AD878EED64E980DF26C2347E4482196AAE0B13865761138727E9E5858D74",
      sourceDurationMs: 583_448,
      frameTimestampMs: 300_000,
      extractionMethod: "ffmpeg 단일 프레임 추출 · 1280×720",
      ocrText: "OCR 미실행",
      ocrStatus: "텍스트 후보 없음 · 미검증",
      gpsStatus: "원본 영상에 연결된 GPS 없음",
      positionBasis: "지도 동기화용 시연 좌표 · 원본 위치로 사용 금지",
      classificationStatus: "후보·미검증",
    },
  },
  {
    id: "CAND-REAL-0420",
    timeMs: 420_000,
    timecode: "00:07:00.000",
    type: "텍스트 포함 장면 후보",
    typeCode: "TEXT_FRAME_CANDIDATE",
    severity: "미판정",
    status: "후보·미검증",
    road: "천안논산 천안방향 · 위치 미검증",
    action: "OCR·유형 사용자 확인 필요",
    confidence: null,
    coordinates: [127.901, 36.7595],
    evidence: {
      image: "/assets/real-trip/review-candidates/candidate-005-420s.jpg",
      sourceFile: "천안논산 천안방향.mp4",
      sourceSha256: "E263AD878EED64E980DF26C2347E4482196AAE0B13865761138727E9E5858D74",
      sourceDurationMs: 583_448,
      frameTimestampMs: 420_000,
      extractionMethod: "ffmpeg 단일 프레임 추출 · 1280×720",
      ocrText: "OCR 미실행",
      ocrStatus: "표지 텍스트가 보이나 아직 판독하지 않음",
      gpsStatus: "원본 영상에 연결된 GPS 없음",
      positionBasis: "지도 동기화용 시연 좌표 · 원본 위치로 사용 금지",
      classificationStatus: "후보·미검증",
    },
  },
  {
    id: "CAND-REAL-0540",
    timeMs: 540_000,
    timecode: "00:09:00.000",
    type: "미분류 장면 후보",
    typeCode: "FRAME_CANDIDATE",
    severity: "미판정",
    status: "후보·미검증",
    road: "천안논산 천안방향 · 위치 미검증",
    action: "사용자 검토 후 유지보수 판정",
    confidence: null,
    coordinates: [127.8849, 36.7482],
    evidence: {
      image: "/assets/real-trip/review-candidates/candidate-006-540s.jpg",
      sourceFile: "천안논산 천안방향.mp4",
      sourceSha256: "E263AD878EED64E980DF26C2347E4482196AAE0B13865761138727E9E5858D74",
      sourceDurationMs: 583_448,
      frameTimestampMs: 540_000,
      extractionMethod: "ffmpeg 단일 프레임 추출 · 1280×720",
      ocrText: "OCR 미실행",
      ocrStatus: "텍스트 후보 없음 · 미검증",
      gpsStatus: "원본 영상에 연결된 GPS 없음",
      positionBasis: "지도 동기화용 시연 좌표 · 원본 위치로 사용 금지",
      classificationStatus: "후보·미검증",
    },
  },
];

export const labelQuestions: LabelQuestion[] = [
  {
    id: "Q-REAL-0001",
    prompt: "AI가 표시한 영역이 터널명 표지가 맞나요?",
    typeCode: "TUNNEL_IDENTITY",
    confidence: 0.42,
    forced: false,
    rationale:
      "실제 업로드 영상에서 뽑은 OCR 저신뢰 후보입니다. 글자가 흐리면 판단 어려움을 눌러 학습에서 제외합니다.",
    timecode: "00:03:42.600",
    sourceFile: "천안논산 천안방향.mp4",
    evidenceImage: realTunnelEvidenceImage,
    previewVideo: realTunnelPreviewVideo,
    roiImage: "/assets/real-trip/tunnel-ocr-roi-222s.png",
    adjacentFrames: Array.from(
      { length: 7 },
      (_, index) => `/assets/real-trip/tunnel-frames/frame-${String(index).padStart(2, "0")}.jpg`,
    ),
    ocrCandidate: "판독 불가 · 사용자 확인 필요",
    locationStatus: "GPS 없음 · 수동 위치 검증 필요",
    box: { x: 39, y: 19, width: 21, height: 27 },
  },
  {
    id: "Q-8834",
    prompt: "도로 위 물체가 낙하물인가요?",
    typeCode: "ROAD_DEBRIS",
    confidence: 0.84,
    forced: true,
    rationale: "낙하물과 긴급 제안은 모든 검토 모드에서 반드시 확인합니다.",
    timecode: "00:06:28",
    sourceFile: "demo_trip_002.mp4",
    box: { x: 59, y: 67, width: 16, height: 17 },
  },
  {
    id: "Q-8835",
    prompt: "이 차선은 재도색이 필요한 상태인가요?",
    typeCode: "LANE_MARKING_FADED",
    confidence: 0.61,
    forced: false,
    rationale: "초기 30건 전수 질문 대상이며 답변은 상태·조치 라벨로 분리됩니다.",
  },
  {
    id: "Q-8836",
    prompt: "표지판이 식생에 의해 가려졌나요?",
    typeCode: "SIGN_VEGETATION_OCCLUSION",
    confidence: 0.73,
    forced: false,
    rationale: "표지판 영역과 식생 겹침률이 42%로 측정됐습니다.",
  },
  {
    id: "Q-8837",
    prompt: "이 구간은 삭초가 필요한가요?",
    typeCode: "ROADSIDE_MOWING_REQUIRED",
    confidence: 0.66,
    forced: false,
    rationale: "갓길 안전시거 기준과 식생 높이를 함께 검토합니다.",
  },
  {
    id: "Q-8838",
    prompt: "AI가 표시한 영역이 정확한가요?",
    typeCode: "POTHOLE",
    confidence: 0.91,
    forced: false,
    rationale: "승인된 영역은 다음 데이터셋 버전에만 포함되며 재학습은 자동 시작되지 않습니다.",
  },
];

export const recentTrips = [
  {
    id: "trip_003",
    route: "고속국도 1호선 하행 · 312k→288k",
    file: "20250916_073358.mp4",
    duration: "02:58:24",
    gps: "GPX 확인",
    incidents: "분석 중",
    status: "분석 62%",
  },
  {
    id: "trip_002",
    route: "고속국도 1호선 하행 · 320k→288k",
    file: "20250905_103501.mp4",
    duration: "00:31:04",
    gps: "1구간 누락",
    incidents: "9",
    status: "검토 중",
  },
  {
    id: "trip_001",
    route: "천안논산 천안방향 · 논산시점",
    file: "천안논산 천안방향 논산시점.mp4",
    duration: "00:12:40",
    gps: "수동 위치",
    incidents: "14",
    status: "보고서",
  },
];

export const importFiles = [
  {
    name: "천안논산 천안방향.mp4",
    size: "152.35 MB",
    changed: "공유 생성 13:37:26",
    state: "A/B/C 안정",
  },
  {
    name: "천안논산 천안방향 논산시점.mp4",
    size: "118.01 MB",
    changed: "공유 생성 13:37:20",
    state: "A/B/C 안정",
  },
  {
    name: "20250916_073358.mp4",
    size: "10.70 GB",
    changed: "공유 생성 13:37:23",
    state: "A/B/C 안정",
  },
  {
    name: "20250905_095953.mp4",
    size: "4.35 MB",
    changed: "공유 생성 13:38:33",
    state: "A/B/C 안정",
  },
];

export const modelRows = [
  { name: "POTHOLE", active: 0.912, candidate: 0.934, duplicates: "2.8%" },
  { name: "ROAD_SURFACE_DAMAGE", active: 0.884, candidate: 0.901, duplicates: "3.4%" },
  { name: "ROAD_DEBRIS", active: 0.806, candidate: 0.848, duplicates: "4.1%" },
  { name: "LANE_MARKING_FADED", active: 0.871, candidate: 0.869, duplicates: "3.0%" },
  { name: "SIGN_DAMAGE", active: 0.834, candidate: 0.803, duplicates: "5.8%" },
  { name: "ROADSIDE_MOWING_REQUIRED", active: 0.902, candidate: 0.914, duplicates: "2.4%" },
];

export const reportOps = [
  {
    id: "OP-01",
    op: "문단 채우기",
    target: "§1 조사 개요",
    value: "차량 고정 촬영 영상과 GPS 기록을 기반으로 노면·시설물·식생·차선 상태를 점검하였다.",
    state: "accepted",
  },
  {
    id: "OP-02",
    op: "표 셀 채우기",
    target: "점검일자",
    value: "2026-07-14",
    state: "accepted",
  },
  {
    id: "OP-03",
    op: "표 행 추가",
    target: "유형별 집계",
    value: "낙하물 1 · 포트홀 3 · 식생 가림 1 · 차선 1",
    state: "pending",
  },
  {
    id: "OP-04",
    op: "이미지 삽입",
    target: "사진 영역 #1",
    value: "비식별화 근거 이미지 · INC-0423",
    state: "pending",
  },
  {
    id: "OP-05",
    op: "지도 삽입",
    target: "경로 지도 영역",
    value: "trip_002 경로와 확정 사건 9건",
    state: "pending",
  },
];

export const exportArtifacts = [
  ["raw_detections.csv", "1,284행", "AI 원시값", "412 KB"],
  ["reviewed_incidents.csv", "9행", "사용자 최종값", "18 KB"],
  ["label_questions.csv", "147행", "질문·정책", "54 KB"],
  ["annotations.csv", "214행", "학습 라벨", "76 KB"],
  ["ocr_observations.csv", "4행", "OCR 원문·검증", "6 KB"],
  ["route_points.csv", "8,942행", "WGS84", "1.2 MB"],
  ["maintenance_actions.csv", "9행", "제안·확정", "9 KB"],
  ["report_payload.json", "1건", "보고서 입력", "28 KB"],
  ["manifest.json", "1건", "provenance·SHA-256", "12 KB"],
];

export const routeSegments: [number, number][][] = [
  [
    [127.938, 36.787],
    [127.9321, 36.783],
    [127.9256, 36.7772],
    [127.9179, 36.7709],
  ],
  [
    [127.9088, 36.7658],
    [127.901, 36.7595],
    [127.8943, 36.754],
    [127.8849, 36.7482],
  ],
];
