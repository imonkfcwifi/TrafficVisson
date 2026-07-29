# TrafficSuperVision 구현 요구사항 매트릭스

> 감사 기준 시각: 2026-07-29 16:30 KST
> 감사 범위: 루트 계획서, 업로드 보관본, Electron main/preload, renderer, Python bridge, 모델 실행기, 테스트, Windows 패키지 산출물  
> 구현 반영: PipelineJob 복구·멱등성·0건 결과, 승인 사례 원장, sample HWPX 의미 검증, Model Pack provenance, 단일 창 IA·라벨링 근거 보강과 대응 회귀를 현재 작업 트리에 반영함.

## 1. 기준 문서와 판정 원칙

업로드 폴더의 계획서는 스스로 `역사적 v2.0 보관본`이며 현재 구현 기준으로 사용하지 않는다고 명시한다
(`Pretendard와 리퀴드 글래스 디자인/uploads/TRAFFIC_SUPER_VISION_MASTER_PLAN.md:3-6`).
현재 단일 기준은 루트 v2.14이다
(`TRAFFIC_SUPER_VISION_MASTER_PLAN.md:3-9`).
두 문서의 M0~M11 완료 조건과 최종 완료 정의 1~19는 실질적으로 동일하므로, 이 감사는 루트 v2.14의 문구를 판정 기준으로 삼고 업로드본은 요구사항 누락 여부를 대조하는 용도로만 사용했다.

판정 용어는 다음처럼 엄격하게 사용했다.

| 판정 | 의미 |
|---|---|
| **구현됨** | 제품 경로에 연결되어 있고, 해당 완료 조건을 실제 데이터 또는 수용 시험으로 입증함 |
| **부분 구현** | 제품 코드가 존재하지만 완료 조건의 일부가 빠졌거나 실제 수용 증거가 없음 |
| **미구현** | 제품 경로가 없거나 사용자가 실행할 수 있는 기능이 없음 |
| **시뮬레이션/실험** | fixture, 고정값, 순수 정책 함수, 개발자용 평가 스크립트 또는 review-only 실험으로만 존재함 |

심각도는 다음과 같다.

- **P0**: 제품의 핵심 약속, 안전성, 개인정보, 보고서 무결성 또는 현장 배포를 막음
- **P1**: MVP 수용과 운영 신뢰성을 막지만 다른 핵심 흐름은 계속 검증 가능함
- **P2**: 후속 품질·편의 개선

## 2. 총괄 결론

### 2.1 마일스톤 집계

| 구현됨 | 부분 구현 | 미구현 | 시뮬레이션/실험 |
|---:|---:|---:|---:|
| 0 | 11 | 0 | 1 |

M0~M11 중 계획서의 엄격한 완료 조건까지 충족한 마일스톤은 아직 없다. 다만 이것은 “아무것도 없다”는 뜻이 아니다. 보안 셸, 로컬 영상 수집, 실제 후보 프레임과 근거 파일, 질문 답변 감사 기록, DatasetVersion, GPX 가져오기·오프셋·MapLibre 재생 동기화·전체 지점 export provenance, 검증 ZIP, Provider 안전 경계, HWP COM 어댑터, NSIS 설치본은 실제 코드다. 부족한 부분은 제품의 핵심인 **검지 성능 입증, 실제 사용자 라벨, 실제 운영 GPX가 연결된 수용 증거, 학습·승격, 비식별화, HWP 실문서 검증, 서명·클린 PC 수용**이다.

### 2.2 최종 완료 정의 집계

| 구현됨 | 부분 구현 | 미구현 | 시뮬레이션/실험 |
|---:|---:|---:|---:|
| 1 | 15 | 2 | 1 |

19개 최종 조건 중 독립적으로 완료 판정할 수 있는 것은 **18번 Local Model 비활성 Provider 슬롯**뿐이다. GPX 기능 수직 슬라이스는 구현됐지만 실제 사용자 시간대 GPX가 없어 2번·10번을 수용 완료로 승격하지 않았다. 13번 비식별화는 제품 경로가 없고, 17번 전 페이지 HWP/HWPX 렌더 검증은 코드 일부와 COM 등록만으로 실제 sample 48쪽 종단이 입증되지 않았으며, 9번 OCR·위치 검증은 현재 실험 수준이다.

### 2.3 최신 UI·런타임 증거

- 본문 글꼴은 패키지 의존성 `@sun-typeface/suit` 2.0.5(OFL-1.1)의 **SUIT Variable 전체 face**를 renderer entry에서 직접 import하며, 루트 font stack도 `"SUIT Variable"`로 고정한다 (`desktop/src/main.tsx:4-10`; `desktop/src/styles.css:1-8`; `desktop/package-lock.json:1642-1646`).
- alpha.10 최종 Vite 빌드에는 `dist/client/assets/SUIT-Variable-gqD86R3M.woff2` 624,536 bytes가 실제 생성되어 패키지 renderer에 번들됐다. 패키지 런타임 측정도 `document.fonts.status=loaded`, `document.fonts.check(...) = true`, body computed family `"SUIT Variable"`를 확인했다 (`qa/aaa-continuation-20260729/harsh-visual-perf-audit.md:155-162`).
- 1600×900 홈의 위쪽 카드만 남고 아래가 비던 회귀는 실제 후보 증거 쇼케이스로 채웠다. `analysisSummary.candidates`의 앞 3건을 실제 evidence URL로 렌더하고 클릭하면 같은 후보의 라벨링 화면으로 이동한다 (`desktop/src/screens/HomeScreen.tsx:60-72,255-308`). 첫 후보를 크게 두는 비대칭 3열과 남은 공간을 채우는 flex/grid가 적용됐다 (`desktop/src/styles-visual-polish.css:172-220,233-272`). postfix 단계의 1600×900 증거 캡처는 `qa/aaa-continuation-20260729/postfix-audit/10-home-1600x900-100.png`이며, 고정 mock 이미지가 아니라 현재 분석 후보 1~3과 검토 대기열을 한 화면에 표시한다. 이 중간 캡처가 아니라 아래 final-suit-p2 재감사의 최종 원본과 측정값으로 alpha.10 UI 통과를 판정했다.
- SUIT import, 홈 비대칭 계층, 실제 candidate evidence URL, 짧은 화면의 line box·clip 방지 회귀 검사는 renderer 테스트에 포함된다 (`desktop/src/visualPolish.test.mjs:15-49`).
- **PASSED — v2.13 post-fix UI 재감사:** [`qa/visual-reaudit-20260729`](../visual-reaudit-20260729/)의 Home·Labeling·Report 실화면과 참조 결합본 6개는 P0 0건·P1 0건·P2 0건이다. 1280×720 live pixel, 정밀 심각도 상태, 실제 OCR 카드 상태는 P3 증거 공백이며 제품 전체·현장 수용 완료 판정이 아니다.
- **PASSED — v2.14 단일 창 IA 실기동 재검증:** [`qa/ia-confusion-20260729`](../ia-confusion-20260729/)에 변경 전 Electron, 실제 1282×721 개발 Electron과 유일한 디자인 참조 결합본을 보존했다. 화면은 `업무 화면 4/9`, 9개 업무 내비게이션 그룹, 영상→자동 분석→질문 검토→결과 자동 갱신, 현재 검토 모드, 일반 질문의 GPS 없음·경로 미생성, AI 심각도 미제안 근거와 사용자 최종값 분리, 3·4분기 유사 4건과 Y/N/U/E를 문서 전체 스크롤 없이 표시한다.
- `HistoryScreen` 초기 청크는 22.28kB(gzip 7.60kB)로 분리됐다. 지도를 열 때 lazy-load되는 별도 `HistoryMap` 청크 947.96kB(gzip 249.38kB)가 Vite 500kB 경고를 남기므로 저사양 현장 PC의 첫 지도 진입 시간을 측정할 P3 경계다.

### 2.4 실제 NAS PipelineJob와 승인 경계

프로젝트 `11111111-1111-4111-8111-111111111111`의 `PipelineJob` `98282642-e5bd-42a2-98b2-04c8c2c81a6d`는 analysis `7571e9f1-09ff-4236-aeb7-4946fca4ac0a`, 원본 `20250905_095953.mp4`(SHA-256 `020ac3d642e1b02ad1d3834a5fd98b6e24ad38de00b74262bce93e4280eb16a4`)에 대해 `awaiting_review` / `question_queue` 92%, 질문 1건이다. 시간대별 GPS·GPX가 없고 사용자 답변·확정 Annotation·확정 Incident는 모두 0건이다.

모든 durable checkpoint를 검사하는 pre-review projection gate가 검토 완료 전 Annotation·Incident 투영을 막는다. 검토 완료 뒤에도 정확히 같은 완료 analysis/source/question provenance만 확정 투영하고 해당 analysis의 CSV·증거를 생성한다.

v2.14는 여기에 source SHA-256·프로젝트 설정이 같은 재입력의 canonical job alias, 재시작 중 `cancelling`의 `cancelled` 마감, 실제 0건만 허용하는 `zero_detections` 결과·빈 산출물 hash를 추가했다. 모델 미설치·분석 실패·거짓 0건을 빈 성공으로 승격하지 않는다.

### 2.5 반드시 구분해야 하는 “실제”와 “시뮬레이션”

다음 항목은 화면에 값이 보이거나 테스트 fixture가 있어도 실제 기능 완료로 계산하면 안 된다.

1. URL 검사는 원격 메타데이터를 읽지 않고 모든 URL에 `734,003,200 bytes`, `612.4 seconds`를 넣는다
   (`desktop/bridge/app/store.py:294-345`).
2. 구형 fixture history API는 demo ID에만 합성 좌표를 생성한다
   (`desktop/bridge/app/store.py:467-513`).
3. 기본 후보 추출기는 코드 주석대로 도로 결함을 분류하지 않는다
   (`desktop/bridge/app/candidate_extraction.py:1-6`).
4. 번들 Grounding DINO 모델은 `review_only=true`, 자동 라벨·Incident·모델 승격 금지다
   (`desktop/build/command-proposal-runner/model-pack/manifest.json:1`).
5. EasyOCR 결과는 개발자용 비파괴 평가 산출물이며 확정 OCR·GPS 검증 경로가 아니다
   (`desktop/scripts/evaluate_easyocr_candidates.py:1-10`, `TRAFFIC_SUPER_VISION_MASTER_PLAN.md:1623-1625`).
6. domain fixture의 GPX, 승격 정책, CSV 예시는 제품 저장소·학습 API의 대체 증거가 아니다
   (`desktop/src/domain/fixtures.ts:78-86`, `desktop/src/domain/domain.test.mjs:265-355`).
7. Vinoth RDD YOLOv8은 349개 실제 대표 JPEG를 읽어 10프레임·11박스를 제안한 **비파괴 평가**이지만 활성 모델이나 라벨러가 아니다 (`desktop/qa/model-intake/vinoth-rdd-b001687-all349-20260729/EVALUATION.md:16-31`). 육안 검토는 확정 양성 0프레임, 판단 어려움 1프레임, 오탐 9프레임/10박스로 판정했고 (`desktop/qa/model-intake/vinoth-rdd-b001687-all349-20260729/VISUAL_REVIEW.md:3-11,28-35`), 정확한 RDD release·라이선스와 YOLOv8 base-weight 권리가 불명확하다 (`desktop/qa/model-intake/vinothvikas1987-pothole-b001687/trafficvisson-model-pack.json:6-11`). 따라서 `evaluation_only_review_only`, 활성화 거부, 설치본 번들·재배포 차단 상태를 유지한다 (`desktop/qa/model-intake/vinoth-rdd-b001687-all349-20260729/intake-run-summary.json:2-21`).

## 3. M0~M11 구현 매트릭스

| 단계 | 판정 | 심각도 | 실제 구현 증거 | 완료를 막는 차이 |
|---|---|---:|---|---|
| **M0 저장소·계약·Electron 보안 기반** | **부분 구현** | P1 | IPC sender와 payload를 검사한다 (`desktop/electron/main.mjs:128-139`). BrowserWindow는 Node 비활성, context isolation, sandbox, web security를 사용한다 (`desktop/electron/main.mjs:640-648`). bridge는 127.0.0.1 readiness와 실행별 256-bit 토큰을 사용한다 (`desktop/electron/bridge-supervisor.mjs:32-50,134-187`; `desktop/bridge/app/security.py:22-40,118-126`). SQLite ordered migration v4가 있다 (`desktop/bridge/app/project_catalog.py:27,162-193`). | 완료 조건은 실제 입력에서 만들어진 Project·Trip·Incident·Question을 UI에서 재조회하는 것이다 (`TRAFFIC_SUPER_VISION_MASTER_PLAN.md:1549-1559`). 현재 실제 사용자 승인 Incident가 0건이고 신규 queued/running 작업, 정규 Detection, ModelVersion·Report 카탈로그가 완성되지 않았다. |
| **M1 안전한 영상 링크·로컬 파일 수집** | **부분 구현** | P0 | 로컬/UNC MP4·MOV 선택, 안정성 확인, ffprobe, streamed SHA-256과 프로젝트 등록은 제품 경로다 (`desktop/bridge/app/media_intake.py:401-507,587-679,681-995`). 권리 확인 UI도 있다 (`desktop/src/screens/ImportScreen.tsx:617-643`). GPX parser/store와 opaque picker·IPC도 구현됐다 (`desktop/bridge/app/gpx_tracks.py:263-337,486-527,600-708`; `desktop/electron/main.mjs:161-175,415-449`). 완료 분석 Trip에는 History 화면에서 GPX를 실제 연결·교체할 수 있다 (`desktop/src/screens/HistoryScreen.tsx:295-318,534-545`). | URL 공급자 분류·원격 메타데이터·다운로드·재개·저장·프로젝트 등록이 없다. UI도 URL은 검사 전용이라고 표시한다 (`desktop/src/screens/ImportScreen.tsx:361-383,645-651,764-770`). URL 메타데이터는 고정값이다 (`desktop/bridge/app/store.py:321-322`). 영상 가져오기 단계에서 GPX를 함께 선택하는 UX와 링크형 GPX 수집도 후속이다. |
| **M2 영상·GPS·지도 수직 구현** | **부분 구현** | P0 | 기능 수직 슬라이스는 완료했다. GPX 원본을 안전하게 파싱·영속화하고 오프셋과 10초 초과 gap을 적용한다 (`desktop/bridge/app/gpx_tracks.py:263-337,600-708`). History가 get/import/offset API를 호출하고 현재·선택 후보 좌표를 영상 시각으로 보간한다 (`desktop/src/screens/HistoryScreen.tsx:101-134,259-269,295-335,497-545`). MapLibre는 전체 경로, 재생 진행선, 현재 위치, 후보 위치를 분리하고 후보 마커 클릭을 같은 후보 선택으로 되돌린다 (`desktop/src/components/HistoryMap.tsx:31-140,182-372`). | 현재 사용자 운영 영상에는 시간대별 GPX가 없어 “실제 영상 재생에 따라 지도 마커와 진행 경로가 움직임”을 운영 데이터로 수용하지 못했다. 방향 마커, 도로명·주소 검증, 실제 지도 타일도 남아 있다 (`TRAFFIC_SUPER_VISION_MASTER_PLAN.md:1574-1583`). 따라서 코드 완료와 마일스톤 수용 완료를 구분해 M2 전체는 부분 구현으로 유지한다. |
| **M3 질문형 라벨링** | **부분 구현** | P0 | 실제 후보 이미지·클립과 제안 영역을 질문으로 만들고 빠른 판정과 정밀 판정(예 뒤 1/2/3 심각도)을 키보드·클릭으로 저장한다. 낙하물·긴급 질문은 건너뛰기 불가이며 답변은 append-only hash chain과 승인/음성/보류 partition으로 정규화된다. 3·4분기 사례는 프로젝트별 승인 원장에서 문서·인덱스·이미지·셀 결속 해시와 사용자 revision을 확인한 뒤에만 approved reference가 되며, renderer는 약한 참고와 승인 사례 provenance를 각각 fail-closed 검증한다. point-prompt SAM2 refinement의 service·IPC·UI 경로도 존재한다. | 현재 SAM2 런타임 모델을 사용할 수 없고 실제 마스크·추적 E2E가 미완료다. CVAT/YOLO 내보내기, 실제 비전문 사용자 50건 시험이 없으며 실제 NAS 질문도 아직 답변 0건이다. 승인 reference는 Annotation·Incident가 아니며 실제 학습 실행도 아직 없다. |
| **M4 핵심 도로 검지** | **부분 구현** | P0 | FFmpeg 장면·선명도 기반 실제 프레임/클립 추출과 격리 ONNX 실행기는 존재한다. 고정 Grounding DINO Q4 pack은 실제 이미지에서 review-only 박스를 만들 수 있다 (`desktop/bridge/app/vision_inference.py:1-14,844-873`; `desktop/build/command-proposal-runner/model-pack/manifest.json:1`). 별도 Vinoth RDD intake도 저장된 349개 대표 JPEG 전부를 비파괴 평가해 10프레임·11박스를 산출했다 (`desktop/qa/model-intake/vinoth-rdd-b001687-all349-20260729/EVALUATION.md:16-31`). | 기본 후보 추출은 결함 분류기가 아니다 (`desktop/bridge/app/candidate_extraction.py:1-6`). Vinoth 결과의 육안 판정도 확정 양성 0, 판단 어려움 1프레임, 오탐 9프레임/10박스였고 터널 조명·차선·색깔유도선·영상 가장자리를 손상으로 오인했다 (`desktop/qa/model-intake/vinoth-rdd-b001687-all349-20260729/VISUAL_REVIEW.md:3-11,17-35`). RDD·base-weight 권리도 확정되지 않아 활성화·번들·재배포를 거부하고 평가 전용으로만 보존한다 (`desktop/qa/model-intake/vinothvikas1987-pothole-b001687/trafficvisson-model-pack.json:6-11`; `desktop/qa/model-intake/vinoth-rdd-b001687-all349-20260729/VISUAL_REVIEW.md:45-52`). 포트홀·노면 파손·낙하물의 고정 한국 도로 검증 세트, dense recall, 추적/사건 병합과 실제 영상→질문→Incident→CSV 종단은 미완료다. |
| **M5 전체 유지보수 유형** | **부분 구현** | P0 | 자연어 명령을 allowlisted issue type과 고정 prompt recipe로 컴파일하는 순수 정책 모듈이 있고, 포트홀·파손·낙하물·중앙분리대·식생·삭초·차선·색깔유도선·이정표·터널 identity의 계약 일부가 있다 (`desktop/bridge/app/auto_labeling.py:1-7,29-41,187-230`; `desktop/bridge/app/candidate_labels.py:37-58`). | 표준 12종 중 `SIGN_DAMAGE`, `TUNNEL_NAME_OCCLUSION`이 production allowed type에 없고 색깔유도선 명명도 기준 계약과 불일치한다 (`TRAFFIC_SUPER_VISION_MASTER_PLAN.md:371-382`; `desktop/bridge/app/candidate_labels.py:39-58`). 관계 판정, 심각도·조치 검증, 다중 유형 Incident가 성능으로 입증되지 않았다. |
| **M6 OCR·위치 검증** | **시뮬레이션/실험** | P0 | generic model-pack worker에는 EasyOCR Korean/English adapter가 있고 개발자용 CPU 평가·테스트가 있다 (`desktop/bridge/app/vision_inference.py:29,77`; `desktop/bridge/app/vision_worker.py:416-521`; `desktop/scripts/evaluate_easyocr_candidates.py:1-10`). | 배포 모델팩은 Grounding DINO일 뿐 OCR팩이 아니다. 실제 12프레임 제한 평가에서 참고 가능한 한글 후보는 `도척` 1건, 터널명 0건이었고 결과는 별도 평가 파일에만 남았다. 질문, 확정 시설명, 시설물 기준 인덱스, GPS·도로·방향 검증과 연결되지 않았다 (`TRAFFIC_SUPER_VISION_MASTER_PLAN.md:1623-1625`). |
| **M7 프로젝트 모델 관리** | **부분 구현** | P0 | 최신 답변에서 immutable DatasetVersion을 만들고 프로젝트 경계, analysis-job 단위 train/validation split, 해시·재사용을 보장한다 (`desktop/bridge/app/dataset_versions.py:1-10,437-559`). ModelScreen은 실제 label stats와 dataset API를 읽고 사용자가 버튼을 눌러야 snapshot을 만든다 (`desktop/src/screens/ModelScreen.tsx:30-43,210-259`). | GPU 학습 API, ModelVersion 저장, 유형별 고정 검증 세트, 성능 비교, 후보 승격·거부·롤백이 없다. `새 모델 학습` 버튼은 의도적으로 비활성이다 (`desktop/src/screens/ModelScreen.tsx:278-284,328-336`). 프로젝트별 “모델” 격리는 아직 데이터셋 격리뿐이다. |
| **M8 GPS 증거 히스토리** | **부분 구현** | P0 | 실제 후보/Incident 목록, 영상 timecode, 타임라인, Review↔History 선택을 프로젝트·작업 범위로 연결했다. GPX가 있으면 MapLibre 전체 경로·재생 진행선·현재 위치·후보 위치를 만들고, 재생 시각 변경 시 source를 갱신하며 지도 후보 클릭도 동일 후보로 이동한다 (`desktop/src/components/HistoryMap.tsx:56-140,215-372`; `desktop/src/screens/HistoryScreen.tsx:637-648,675-721`). GPX가 없거나 gap·범위 밖이면 위치를 만들지 않는다 (`desktop/src/components/HistoryMap.tsx:403-458`; `desktop/src/gpsTracks.test.mjs:88-105`). | 실제 운영 데이터에는 승인 Incident와 시간대별 GPX가 동시에 존재하지 않아 촬영자가 아닌 검토자가 실제 사건을 지도에서 선택하는 수용 종단은 미입증이다. 주소·도로명·진행 방향과 공유용 비식별화 근거도 남아 있어 M8 완료가 아니다 (`TRAFFIC_SUPER_VISION_MASTER_PLAN.md:1644-1653`). |
| **M9 CSV·비식별화·Trip Package** | **부분 구현** | P0 | 7개 CSV, report payload, manifest, 승인 Incident의 JPEG/MP4를 hash·magic·size 검증 ZIP으로 만드는 실제 export가 있다 (`desktop/bridge/app/artifact_export.py:184-211,872-992`). 선택 분석 작업과 Incident revision/provenance도 제한한다. GPX 연결 시 표시용 최대 8,000점 샘플과 분리해 최대 50,000점의 검증된 전체 route를 export snapshot으로 읽고, `route_points.csv`와 manifest에 track/source/record SHA-256, revision, offset, gap·범위 통계를 기록한다 (`desktop/bridge/app/gpx_tracks.py:700-904`; `desktop/bridge/app/artifact_export.py:445-550`; `desktop/bridge/tests/test_gpx_tracks.py:260-287`; `desktop/bridge/tests/test_analysis_jobs.py:1481-1595`). | 얼굴·번호판 blur 및 공유용 파생본이 없고 Review UI도 `공유본 없음`으로 막혀 있다 (`desktop/src/screens/ReviewScreen.tsx:580-590`). 현재 운영 데이터의 실제 승인 Incident와 GPX가 0건이어서 실제 Trip Package 사용자 종단은 아직 없다. route export 완료를 비식별화 완료로 해석하지 않는다. |
| **M10 Codex·Claude와 HWP/HWPX** | **부분 구현** | P0 | Codex/Claude 공식 CLI의 allowlisted 상태·로그인 명령을 `shell=False`로 실행한다. 보고서 입력·모델 출력 스키마와 incident/target 참조를 검증하고, 파생 산출물 생성 전 서버가 exact completed analysis/source/question provenance와 review completion을 다시 확인한다. 3·4분기 sample 272개 사진의 정확한 페이지 쌍·좌우 병합 캡션·노선·점검 의견·전체 `■` 셀을 재결속하고 승인 reference와 현재 expected value가 일치하는 의미 계획만 COM 전에 허용하는 validator가 있다. HWP COM은 현재 PC에 등록돼 있으며 전용 STA 경로가 존재한다. | 의미 validator는 아직 실제 report/COM writer 제품 경로와 연결되지 않았다. COM 등록은 실문서 성공 증거가 아니며 sample 양식 실제 적용, 영구 복구 journal, 저장 후 재열기와 48쪽 전체 렌더가 아직 수용되지 않았다. |
| **M11 설치본·현장 수용** | **부분 구현** | P0 | `package:win`은 테스트, renderer/bridge/runner build, frozen bridge test, NSIS, release manifest를 묶는다. 현재 source gate는 renderer 156/156, Electron 80/80, bridge 640 passed·2 skipped, typecheck/build passed다. release 검증은 model/version/source revision/license/SHA/GPU 계약이 빠진 runtime을 ready로 인정하지 않고 오래된 packaged runner trust manifest를 manifest 생성 전에 거부한다. 실제 1282×721 단일 창 UI 재검증도 통과했다. | 현재 source trust chain으로 새 설치본을 다시 만들지 않았고 alpha.10 역사 설치본은 `NotSigned`, `development_only_not_distribution_ready`다. 별도 `HistoryMap` 947.96kB(gzip 249.38kB)의 첫 지도 진입 성능, clean Windows 설치/업그레이드/rollback/uninstall, 검증된 도로·OCR·SAM2 runtime, sample HWP 48쪽 종단과 Hancom clean-PC gate가 미해결이다. |

## 4. 최종 완료 정의 1~19 매트릭스

| # | 완료 조건 | 판정 | 심각도 | 근거와 남은 일 |
|---:|---|---|---:|---|
| 1 | 로컬 영상과 지원 등급 링크 영상을 안전하게 가져온다 | **부분 구현** | P0 | 로컬/UNC는 실제 구현. URL은 보안 형식 검사와 권리 UI만 있고 원격 metadata/download/resume/register가 없다 (`desktop/src/screens/ImportScreen.tsx:361-383,645-651`; `desktop/bridge/app/store.py:321-376`). |
| 2 | GPX를 영상과 동기화하고 누락 상태를 구분한다 | **부분 구현** | P0 | parser·gap·offset·영속 store·opaque IPC뿐 아니라 History의 연결·교체·±1초 보정, 영상 시각 보간과 gap/범위 밖 `null`까지 제품 경로에 연결됐다 (`desktop/src/screens/HistoryScreen.tsx:101-134,259-269,295-335,497-545`; `desktop/src/gpsTracks.test.mjs:88-105`). 기능 구현은 완료했지만 실제 사용자 영상과 시간대 GPX의 운영 수용 증거가 없어 엄격한 완료 판정만 보류한다. |
| 3 | AI가 이미지·영역·클립·GPS가 있는 질문을 만든다 | **부분 구현** | P0 | 실제 이미지·클립·review-only 영역 질문은 있고, 같은 후보 시각의 GPX 좌표는 History 증거에서 보간·표시된다. SAM2 point refinement service·IPC·UI도 존재한다. 그러나 현재 runtime 모델을 사용할 수 없어 실제 mask·추적 E2E는 미완료이고, GPS 없는 입력에는 위치를 만들지 않는다. |
| 4 | 예·아니오·판단 어려움·영역 수정으로 라벨을 확정한다 | **부분 구현** | P1 | 빠른/정밀 판정과 감사 저장, 정밀 모드의 예 뒤 1/2/3 심각도 입력이 동작한다. 영역 수정은 point-prompt 경로를 사용하지만 runtime SAM2가 없어 실제 자동 마스크 수용과 50건 사용자 시험은 남아 있다. |
| 5 | 질문·답변·라벨·영상·프레임·사건·모델이 추적된다 | **부분 구현** | P0 | Question/Answer/Annotation/Incident와 분석 작업 provenance는 상당 부분 영속화됨. ModelVersion과 실제 학습 lineage가 없고 실제 사용자 Incident가 0건이다 (`desktop/bridge/app/project_catalog.py:397-603`; `TRAFFIC_SUPER_VISION_MASTER_PLAN.md:1557-1559`). |
| 6 | 프로젝트별 데이터셋과 모델을 격리한다 | **부분 구현** | P0 | DatasetVersion의 프로젝트 경계·foreign-key 테스트는 있다 (`desktop/bridge/app/dataset_versions.py:459-470`; `desktop/bridge/tests/test_project_catalog_v3.py:306-390`). 학습·ModelVersion이 없어 모델 격리/복구는 입증 불가다. |
| 7 | 승인 없이 재학습·승격되지 않는다 | **부분 구현** | P1 | 현재 runner는 자동 라벨·Incident·훈련·승격을 명시적으로 금지하고 학습 버튼도 비활성이다 (`desktop/build/command-proposal-runner/model-pack/manifest.json:1`; `desktop/src/screens/ModelScreen.tsx:278-284`). 그러나 학습·승격 기능 자체가 없어 실제 승인 gate를 종단 검증한 것은 아니다. |
| 8 | 정의된 도로·시설물·식생·차선·낙하물 유형을 처리한다 | **부분 구현** | P0 | 계약·명령 recipe와 zero-shot proposal은 일부 유형을 다룬다. `SIGN_DAMAGE`, `TUNNEL_NAME_OCCLUSION` 계약 누락과 성능 검증 부재가 있다 (`TRAFFIC_SUPER_VISION_MASTER_PLAN.md:371-382`; `desktop/bridge/app/candidate_labels.py:39-58`). |
| 9 | 이정표·터널명을 OCR하고 GPS 검증 상태를 표시한다 | **시뮬레이션/실험** | P0 | EasyOCR adapter와 제한평가만 있고 제품 질문/Incident/GPS 검증 UI가 없다 (`desktop/scripts/evaluate_easyocr_candidates.py:1-10`; `TRAFFIC_SUPER_VISION_MASTER_PLAN.md:1623-1625`). |
| 10 | 경로가 재생 방향으로 채워지고 모든 화면이 같은 사건으로 이동한다 | **부분 구현** | P0 | MapLibre 전체 경로와 별도로 `videoTimeSeconds`까지의 진행선·현재점·후보점을 갱신하고 지도 후보 클릭을 같은 candidate 선택으로 연결한다 (`desktop/src/components/HistoryMap.tsx:56-140,215-372`). 영상·타임라인·목록·Review 선택도 같은 project/job candidate key를 공유한다. 실제 GPX+승인 Incident 운영 자료에서 “같은 사건” 종단을 아직 수용하지 못해 부분 구현으로 유지한다. |
| 11 | 촬영자가 아니어도 증거 카드만으로 사건을 이해한다 | **부분 구현** | P1 | 실제 프레임·클립·유형·시각과 GPX가 있을 때 경로·좌표를 Review/History에서 볼 수 있다. 검증 OCR, 주소·도로 방향, 공유 비식별화 근거와 실제 확정 조치가 한 카드에 완성되지 않았다 (`desktop/src/screens/HistoryScreen.tsx:390-464`; `desktop/src/screens/ReviewScreen.tsx:539-590`). |
| 12 | 원시·최종 CSV, 라벨, 증거, Trip Package를 내보낸다 | **부분 구현** | P1 | CSV 7종+JSON+manifest+승인 evidence ZIP은 실제 구현됐다. GPX 연결 시 renderer 8,000점 샘플과 무관하게 최대 50,000점 전체 route를 `route_points.csv`에 넣고 source/record hash·offset·revision provenance를 보존하며 tamper를 거부한다 (`desktop/bridge/app/gpx_tracks.py:700-904`; `desktop/bridge/tests/test_gpx_tracks.py:260-314`; `desktop/bridge/tests/test_analysis_jobs.py:1481-1608`). 다만 실제 승인 Incident가 있는 운영 패키지와 공유 비식별화 파생본 종단은 없다. |
| 13 | 공유 자료 얼굴·번호판을 비식별화한다 | **미구현** | P0 | 제품 blur/redaction pipeline이 없고 공유 버튼도 비활성이다 (`desktop/src/screens/ReviewScreen.tsx:588-590`). |
| 14 | Codex·Claude CLI 상태를 확인하고 최소 데이터로 보고서 초안을 만든다 | **부분 구현** | P1 | allowlisted status/login, 명시 동의, bounded redacted payload, structured draft/plan 코드가 있다. 서버는 자동 파생 산출물 전에 review completion과 exact completed analysis/source/question provenance를 재검증한다. 다만 실제 사용자 승인 Incident와 공식 CLI를 사용한 보고서 수용 종단은 아직 없다. |
| 15 | 편집 계획이 스키마·문서 대상 검사를 통과해야만 한/글에 적용된다 | **부분 구현** | P0 | model output은 supplied incident/target 집합으로 제한되고 COM operation allowlist도 있다 (`desktop/bridge/app/report_generation.py:795-835`; `desktop/bridge/app/main.py:1817-1846`). 그러나 document snapshot/target summary가 실제 선택 문서 hash·HWPML과 서버 측으로 결속되지 않고 preview와 apply 사이의 일회성 승인 token이 없다. |
| 16 | 기본·사용자 양식에서 미리보기·수락·거절·롤백한다 | **부분 구현** | P0 | 사용자 문서 picker와 HWPML preview, apply, sibling save, in-memory rollback 경로가 있다 (`desktop/bridge/app/main.py:1848-1946`; `desktop/bridge/app/hwp_com.py:476-617`). 기본 양식 파일이 없고, production accept/reject audit와 재시작 후 rollback이 없으며 사진·지도·표가 거부된다. |
| 17 | 최종 HWP/HWPX가 재읽기·전 페이지 렌더 검증을 통과한다 | **미구현** | P0 | 저장 후 reopen/hash/HWPML readback은 있으나 페이지 이미지 렌더, 표 잘림, 겹침, overflow, 사진 누락 판정기는 없다 (`desktop/bridge/app/hwp_com.py:501-565`; `TRAFFIC_SUPER_VISION_MASTER_PLAN.md:1659-1668`). |
| 18 | Local Model 비활성 슬롯과 확장 가능한 Provider 계약이 있다 | **구현됨** | — | `local` status는 `coming_soon`, 설치 안 됨, capability 없음으로 고정되고 Provider 목록/UI 계약을 공유한다 (`desktop/bridge/app/provider_status.py:238-247`; `desktop/bridge/app/main.py:1564-1604`). |
| 19 | 실제 영상 E2E와 Windows 설치본 검증을 통과한다 | **부분 구현** | P0 | 로컬 영상→후보→질문→review-only proposal→CSV/ZIP, GPX 제품 경로와 unpacked 실행은 각각 확인됐다. 그러나 하나의 실제 운영 입력에서 사용자 승인 Incident+시간대 GPX+GPU 학습·승격+비식별화+HWP 실문서까지 잇지 못했고, 링크 다운로드·서명·clean-PC 설치도 빠져 있다 (`TRAFFIC_SUPER_VISION_MASTER_PLAN.md:1677-1686`; `desktop/release/release-manifest.json:118-126`). |

## 5. 테스트 증거

2026-07-29 현재 source gate에서 TypeScript와 Vite production build가 통과했다. 최신 핵심 자동 회귀는 renderer **156/156**, Electron **80/80**, Python bridge **640 passed·2 skipped**다. 아래 domain·release·Sites 및 alpha.10 패키징 수치는 현재 소스 테스트와 역사 패키지 증거를 구분한다.

| 실행 | 결과 |
|---|---|
| `npm run typecheck` | **passed** |
| `npm run test:domain` | **10 passed** |
| `npm run test:renderer-state` | **156 passed** |
| `npm run test:electron` | **80 passed** |
| `npm run test:bridge` | **640 passed, 2 skipped** |
| `npm run test:release-manifest` | **9 passed** |
| `npm run test:sites` | **4 passed** |
| Vite production build | **passed** |
| frozen bridge smoke | **passed** |
| alpha.10 NSIS package·manifest | **passed** |

최신 단일 창 IA 재검증은 [`qa/ia-confusion-20260729`](../ia-confusion-20260729/)에서 실제 개발 Electron 1282×721과 참조 결합본으로 통과했다. `HistoryScreen` 초기 청크는 22.28kB(gzip 7.60kB)이며 별도 `HistoryMap` 947.96kB(gzip 249.38kB)의 첫 지도 진입 성능은 P3 경계다. UI 통과를 전체 제품·현장 수용 완료로 승격하지 않는다.

GPX 회귀는 parser/store의 안전성·durability·offset뿐 아니라 renderer의 project/provenance·gap 보간, MapLibre의 정직한 empty state, Electron opaque handle/IPC, full export와 tamper 거부까지 포함한다 (`desktop/src/gpsTracks.test.mjs:88-123`; `desktop/src/honestyScreens.test.mjs:146-155`; `desktop/electron/tests/gpx-ipc.test.mjs:18-43`; `desktop/bridge/tests/test_gpx_tracks.py:105-176,260-314`; `desktop/bridge/tests/test_analysis_jobs.py:1481-1608`).

그 밖에 저장소에는 다음 대표 테스트가 존재한다.

- Electron 보안/CSP/evidence URL: `desktop/electron/tests/security.test.mjs:21-125`
- bridge loopback/minimal env/fail-closed: `desktop/electron/tests/bridge-supervisor.test.mjs:16-72`
- 실제 로컬 파일→분석·근거·프로젝트 격리: `desktop/bridge/tests/test_analysis_jobs.py:354-722`
- 완료 분석 재시작 복구·tamper 거부: `desktop/bridge/tests/test_analysis_persistence.py:12-60`
- Incident 투영·revision·프로젝트 격리: `desktop/bridge/tests/test_project_catalog_v4.py:62-368`
- DatasetVersion hash·split·프로젝트 경계: `desktop/bridge/tests/test_project_catalog_v3.py:228-461`
- 완전 CSV/JSON/근거 ZIP·tamper 거부: `desktop/bridge/tests/test_artifact_export.py:147-428`
- Provider allowlist·timeout·로그인 소유권: `desktop/bridge/tests/test_provider_status.py:35-316`
- 보고서 최소 payload·악성 출력 거부: `desktop/bridge/tests/test_report_generation.py:116-417`
- HWP fake adapter preview/apply/rollback·unsafe COM 거부: `desktop/bridge/tests/test_hwp_com.py:164-429`

전체 회귀 통과는 코드 계약의 회귀 방지를 입증하지만, 모델 검지 정확도나 실제 사용자 GPX·한/글·클린 PC 수용을 대신하지 않는다. 특히 마지막 두 종류는 fake/injected adapter가 중심이므로 현재 PC의 실제 한/글 COM 종단 증거로 승격해서는 안 된다.

## 6. P0/P1/P2 우선순위

### P0 — 제품 주장 전에 반드시 닫을 것

1. **검지 성능 수직 슬라이스**: 포트홀·노면 파손·낙하물의 dense inference, 추적/사건 병합, 대표 증거, 고정 검증 세트, per-class precision/recall.
2. **표준 유형 계약 통합**: 12종 canonical code, 다중 유형/관계 판정, 심각도·조치, `SIGN_DAMAGE`·`TUNNEL_NAME_OCCLUSION` 누락 제거.
3. **OCR 위치 검증**: 한국어 OCR pack을 제품 분석에 연결하고 raw OCR/검증명/불일치 상태를 분리하며 GPX·도로·방향으로 검증.
4. **모델 생명주기**: GPU 학습, 자원 preflight, 고정 validation, 비교, 승격 거부, rollback, ModelVersion provenance.
5. **GPS 운영 수용**: 구현된 GPX 연결·오프셋·MapLibre 진행선·현재 위치·후보 좌표·전체 export를 시간대가 있는 실제 운영 GPX에서 재생·지도 클릭·ZIP 재현까지 수용하고 도로명·진행 방향을 검증.
6. **비식별화**: 얼굴·번호판 detection/blur, 원본과 파생본 hash·provenance, 공유 패키지에서 원본 제외.
7. **HWP 무결성**: persistent Incident 조회, 실제 HWP snapshot binding, preview token, 사진·지도·표, 재시작 복구, 전 페이지 렌더/overflow 검사.
8. **현장 배포**: Authenticode, dependency lock/SBOM/license, clean-PC 설치·업그레이드·제거, Hancom Office 실제 E2E.

### P1 — 첫 현장 파일럿 전에 닫을 것

1. URL provider adapter, 실 metadata, resume/download/provenance/DRM 거부.
2. 구현된 빠른/정밀 판정과 프로젝트 질문 정책의 50건 비전문 사용자 usability.
3. CVAT/COCO/YOLO annotation export와 import round-trip.
4. 실제 승인 Incident가 있는 Trip Package 및 촬영자 외 검토자 시험.
5. Provider disconnect/usage 표기와 실제 공식 CLI 수용 시험.

### P2 — 핵심 성능 입증 후

1. Local Provider 실제 구현.
2. 고급 지도 스타일·오프라인 타일 캐시.
3. 보고서 편집 이력 비교와 장기 보관 정책 고도화.

## 7. 현실적인 다음 구현 배치

사용자가 처음부터 “검지가 우선”이라고 정의했으므로, 다음 배치는 기능 수를 넓히기보다 **실제 영상에서 신뢰할 수 있는 검지 3종을 끝까지 증명하는 배치**가 적절하다.

### Batch A — 핵심 검지·질문·Incident 수직 슬라이스

범위는 서로 다른 문제 구조를 대표하는 세 유형으로 제한한다.

1. `ROAD_DEBRIS`: 단일 객체 검지와 긴급 강제 질문
2. `POTHOLE` 또는 `ROAD_SURFACE_DAMAGE`: 노면 영역 검지
3. `SIGN_VEGETATION_OCCLUSION`: 표지판과 식생의 관계 판정

구현 순서는 다음과 같다.

1. 실제 업로드 영상에서 1~2 fps dense frame index를 만들고 scene 후보 32개 제한과 분리한다.
2. Grounding DINO는 객체 proposal 전용으로 사용하고, 노면/식생 관계는 별도 detector·segmenter 또는 검증된 rule head로 분리한다.
3. 연속 프레임을 IoU·시간·camera motion으로 한 사건으로 병합한다.
4. 사건마다 대표 JPEG, 전후 프레임, 2~5초 clip, 모델/프롬프트/threshold provenance를 만든다.
5. 기존 질문 UI에 review-only proposal을 넣고 사용자의 네 답변으로만 Annotation·Incident를 생성한다.
6. 최소 30개 validation 표본/유형과 충분한 negative를 고정하고, threshold와 합격 기준을 시험 전에 문서화한다.
7. 같은 실제 Incident가 `raw_detections.csv`와 `reviewed_incidents.csv`에서 answer revision까지 추적되는지 E2E로 확인한다.

이 배치의 완료 조건은 “박스가 보인다”가 아니다.

- 세 유형 모두 고정 validation set에서 사전 정의한 precision/recall 기준을 통과한다.
- 같은 실제 사건이 연속 프레임 때문에 여러 질문으로 중복되지 않는다.
- 사용자 승인 전 Incident/Annotation은 0건이다.
- 승인 후 앱 재시작, Review/History 재조회, CSV/ZIP provenance가 동일 incident ID를 유지한다.
- 실패·오탐·모델 미사용 상태가 성공처럼 표시되지 않는다.

GPX의 backend/opaque IPC뿐 아니라 History 연결·오프셋, MapLibre 경로·진행선·현재점·후보점, 지도 클릭 동기화와 전체 `route_points.csv`/manifest provenance까지 이미 연결됐고 최신 전체 회귀를 통과했다. 따라서 이를 다시 구현 배치로 잡지 않는다. Batch A와 별도의 짧은 수용 트랙에서 시간대가 있는 실제 운영 GPX만 연결해 지도 재생·gap·전체 export 증거를 남긴다. 모델 검지 성능 실패와 GPS 운영자료 부재를 서로 섞어 판정하지 않는다.

## 8. 최종 판정

현재 TrafficSuperVision은 **보안 경계와 데이터 provenance를 신중하게 만든 alpha 설치형 프로토타입**이다. 실제 영상 파일을 읽고 근거 후보를 만들며 사람이 답하고 내보내는 기반, GPX를 연결해 영상과 MapLibre 경로를 동기화하고 전체 지점을 provenance와 함께 내보내는 제품 경로가 있다. 그러나 “도로 파손·삭초·낙하물·OCR을 검증된 성능으로 검지하고, 프로젝트 모델과 비식별화와 HWP 보고서까지 완성한 통합 프로그램”이라는 최종 제품 정의에는 아직 도달하지 않았다.

따라서 현재 상태를 대외적으로 표현할 때는 다음 문구가 정확하다.

> SUIT Variable을 실제 번들한 단일 창 Windows alpha. 실제 영상 장면 후보와 근거 쇼케이스, review-only 로컬 AI 제안, 사용자 확인형 라벨 저장, GPX 연결·오프셋·MapLibre 동기화·전체 경로 provenance export, 검증 CSV/근거 ZIP, Provider·HWP 안전 경계를 갖췄다. 핵심 도로 검지 성능, 실제 운영 GPX 수용, 프로젝트 모델 학습, 비식별화, HWP 실문서 종단과 현장 배포 검증은 진행 중.
