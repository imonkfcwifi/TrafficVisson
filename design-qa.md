# TrafficVisson 데스크톱 디자인 QA

> 평가일: 2026-07-29  
> 평가 범위: Windows Electron 제품 셸과 9개 핵심 화면의 1280×720 상태  
> 참조 이미지: `Pretendard와 리퀴드 글래스 디자인/uploads/pasted-1785143633633-0.png`  
> 기능·정보구조 참고본: `Pretendard와 리퀴드 글래스 디자인/uploads/TrafficVisson Desktop.dc.html`  
> 구현 화면: `desktop/src`  
> 현재 기능 기준: Master v2.10 · alpha.10 · SQLite v4 · `trafficvisson-artifact-export-v3`  
> 디자인 QA 결과: alpha.9 검증과 postfix 감사는 역사 증거로 보존한다. alpha.10 기능·패키지 회귀를 통과했고, [`qa/aaa-continuation-20260729/final-suit-p2-audit.md`](qa/aaa-continuation-20260729/final-suit-p2-audit.md)의 최종 UI 시각 감사도 **PASSED — P0 0건 / P1 0건 / P2 0건**으로 판정했다. 이는 제품 전체·현장 수용 완료 판정은 아니다  
> 제품 상태: development alpha / 기능 완성 전

## 1. 평가 기준

참조 이미지의 금융 도메인 문구나 차트 형태를 복제하지 않고 다음 시각 원칙을 TrafficVisson 업무 흐름에 적용했다.

- 밝은 청회색 바탕과 절제된 저채도 유리 패널
- 좌측 아이콘 레일, 상단 제품·프로젝트 컨텍스트, 한 화면형 작업 공간
- 핵심 상태에만 쓰는 청색 그라데이션과 검은색 pill CTA
- 둥근 카드, 얇은 경계, 낮은 그림자, 넓은 여백
- 전체 문서 스크롤을 만들지 않고 긴 목록·문서만 해당 패널이 소유하는 구조
- 브라우저 페이지가 아니라 네이티브 caption overlay를 가진 독립 Electron 창

참조는 1200×900이고 구현 검증 창은 외곽 경계를 포함해 1282×721, 렌더러 목표는 1280×720이다. 화면 상태는 실제 로컬 bridge가 복구한 `20250916_073358.mp4`, 후보 7건, 촬영 시작 좌표만 있는 프로젝트다.

## 2. 결합 비교

- 수정 전 결합본: `qa/aaa-iteration-20260728/00-reference-vs-home-before.png`
- 최종 결합본: `qa/aaa-iteration-20260728/29-reference-vs-home-rerun.png`
- 최종 홈: `qa/aaa-iteration-20260728/20-home-final-rerun.png`
- 라벨링 수정 전 참조 결합본: `qa/aaa-iteration-20260729/00-reference-vs-labeling-before.png`
- 라벨링 수정 전 실화면: `qa/aaa-iteration-20260729/03-labeling-before.jpg`
- 라벨링 수정 후 개발 실화면: `qa/aaa-iteration-20260729/10-labeling-after-dev.png`
- 라벨링 수정 전·후 결합본: `qa/aaa-iteration-20260729/11-reference-vs-labeling-before-after.png`
- alpha.9 재감사 전 결합본: `qa/ui-iteration-20260729/03-reference-vs-current.png`
- alpha.9 최종 결합본: `qa/ui-iteration-20260729/15-reference-vs-final.png`
- alpha.9 실제 Electron 9개 화면: `qa/ui-iteration-20260729/06-home-final-1280x720.png`부터 `14-export-final-1280x720.png`

alpha.8 당시의 “최종” 판정은 냉정하지 못했다. 전체 문서 스크롤과 기능 노출은 확인했지만, 컨셉 이미지와 나란히 놓았을 때 네 개의 동등한 KPI 카드, 과도한 빈 패널, 8~10px 한글, 짧은 line-height, 한국어와 mono 혼용, 배지와 라벨링 하단 진행 영역의 잘림이 남아 있었다. alpha.9에서 앱에 번들한 `SUIT Variable 2.0.5`(OFL-1.1), 비대칭 주 카드, 얕은 유리 표면, 최소 가독 크기와 한 화면 작업대 기준을 다시 적용했다. TrafficVisson은 참조 화면을 다시 감싼 웹페이지가 아니라 창 전체를 제품 셸로 사용한다.

## 3. 발견 사항과 수정 결과

| 심각도 | 발견 사항 | 수정 | 사후 증거 |
|---|---|---|---|
| P0 | 보고서 화면 높이가 뷰포트를 98px 초과해 하단 작업이 가려짐 | Provider·편집 계획·문서 미리보기를 고정 작업대에 재배치하고 문서만 내부 스크롤 허용 | `27-report-final-rerun.png` |
| P0 | 루트 `.app-canvas`가 숨은 스크롤 컨테이너여서 포커스 이동 시 타이틀바가 밀릴 가능성 | 루트를 `overflow: clip`과 layout/paint/size containment로 고정 | `accessibilityShell.test.mjs`, 9개 최종 캡처 |
| P1 | 홈·내보내기의 흰 글자가 밝은 그라데이션에서 약한 대비 | 그라데이션을 어둡게 조정하고 상태 문구를 흰색으로 고정 | `20-home-final-rerun.png` |
| P1 | UNC 경로가 앞부분만 보여 실제 대상 폴더를 빠르게 알기 어려움 | `대상 · 새 폴더`를 항상 노출하고 전체 경로는 title에 보존 | `21-import-final-rerun.png` |
| P1 | 빈 사건 검토·히스토리 화면에서 화면 제목 계층이 사라짐 | 빈 상태에서도 공통 화면 헤더 유지 | `honestyScreens.test.mjs` |
| P1 | 검토 표의 영구 UUID가 줄바꿈되어 OCR·조치 행이 내부 스크롤 아래로 밀림 | 고정 표 레이아웃, 한 줄 말줄임, 전체 값 title, 27px 행으로 8개 필드를 한 화면에 표시 | `24-review-final-rerun.png` |
| P1 | 히스토리 증거 세부정보에서 7개 필드 중 2개만 즉시 보임 | 세부정보를 2열 compact 정의 목록으로 바꿔 모든 필드를 한 화면에 배치 | `26-history-final-rerun.png` |
| P1 | 완료된 복구 분석이 `작업 시작 전`으로 보임 | 완료 요약이 있으면 `브리지 단계 · 완료`로 표시 | `23-analysis-final-rerun.png` |
| P1 | 설치되지 않은 Provider에 로그인 CTA가 노출되고 영어 오류가 섞임 | `CLI 설치 필요` 비활성 상태와 한국어 bridge 메시지로 정리 | `27-report-final-rerun.png` |
| P2 | 내보내기 목록에 3px짜리 불필요한 내부 스크롤바가 생김 | compact 행 높이를 조정해 9개 계약 산출물을 한 번에 표시 | `28-export-final-rerun.png` |
| P2 | 첫 화면 뒤 다른 화면의 최초 전환이 지연됨 | 공유 lazy loader, 첫 페인트 뒤 idle prefetch, nav hover/focus prefetch 적용 | `productShell.test.mjs` |
| P2 | 34px 슬롯에 1254px·1.5MB 앱 아이콘을 사용 | 96×96·23KB PNG, 명시 크기, 비동기 디코딩 적용 | `20-home-final-rerun.png` |
| P1 | 증거 ZIP 계약과 완료 결과가 구조화 CSV만 내보내는 것처럼 보임 | active·approved·exact-job Incident의 JPEG·MP4 포함, 전체 원본 영상 제외, 사건·파일·용량을 명시 | `30-export-evidence-contract-final.png`, `31-export-completed-final.jpg` |
| P0 | 1280×720 라벨링 화면에서 세로 스크롤 소유자가 겹치고 네 개 판단 응답과 건너뛰기 결정이 첫 화면 아래에 숨음 | 유형 선택기를 한 줄로 압축하고 AI 명령·컴파일 계획·실행 결과를 인라인 작업대로 통합했다. `예`·`아니오`·`판단 어려움`·`영역 수정`과 건너뛰기를 첫 화면에 고정하고 `candidate-media-stack`만 경계가 분명한 내부 스크롤을 소유하게 했다. | `qa/aaa-iteration-20260729/03-labeling-before.jpg`, `qa/aaa-iteration-20260729/10-labeling-after-dev.png`, `qa/aaa-iteration-20260729/11-reference-vs-labeling-before-after.png` |
| P1 | 모델 화면에 실제 로컬 상태와 위험도를 구분하는 모델·데이터 출처 목록이 없어 설치·검증·거절 상태를 한눈에 확인하기 어려움 | 모델 관리 화면 오른쪽에 읽기 전용 출처 카탈로그를 넣고 한 화면 구조를 유지했다. 문서 스크롤 없이 길이가 달라지는 출처 표만 하나의 내부 스크롤을 소유하며, 상단 검증 모델과 하단 거절 모델·데이터셋 상태를 분리해 확인한다. | `qa/aaa-iteration-20260729/15-alpha8-packaged-model-sources-1280x720.jpg`, `qa/aaa-iteration-20260729/16-alpha8-packaged-model-rejected-dataset-1280x720.jpg` |
| P1 | 홈의 네 KPI 카드가 모두 같은 비중이고 아래 패널은 빈 공간이 커 컨셉의 우선순위가 사라짐 | 첫 KPI를 넓은 주 카드로 만들고 보조 카드를 축소했다. 희소한 하단 콘텐츠는 화면 전체를 억지로 채우지 않도록 높이를 제한했다. | `qa/ui-iteration-20260729/15-reference-vs-final.png` |
| P1 | 8~10px 글자, 1.0~1.15 line-height, 한글 mono 혼용과 `overflow:hidden`이 제목·배지·세부정보를 잘라냄 | `SUIT Variable 2.0.5`를 앱에 번들하고 본문 11~12px 이상, 질문 16px, line-height 1.3~1.4, 배지 최소 높이 24px를 적용했다. 필수 문장은 여러 줄을 허용하고 가변 목록만 내부 스크롤로 남겼다. | `qa/ui-iteration-20260729/08-label-final-1280x720.png`, `qa/ui-iteration-20260729/10-review-final-1280x720.png` |
| P1 | 가져오기 소스 탭이 줄바꿈되고 라벨링 하단 진행 패널이 상태 바에 잘림 | 소스 탭을 열 구조로 바꾸고 줄바꿈을 금지했다. 라벨링 네 응답 버튼을 46px로 고정하고 진행 패널 하단을 `.screen` 하단 680px 안에 맞췄다. | `qa/ui-iteration-20260729/07-import-final-1280x720.png`, `qa/ui-iteration-20260729/08-label-final-1280x720.png` |

## 4. 9개 화면 증거

1. 홈: `qa/aaa-iteration-20260728/20-home-final-rerun.png`
2. 영상 가져오기: `qa/aaa-iteration-20260728/21-import-final-rerun.png`
3. 질문형 라벨링: `qa/aaa-iteration-20260728/22-labeling-final-rerun.png`
4. 후보 추출: `qa/aaa-iteration-20260728/23-analysis-final-rerun.png`
5. 사건 검토: `qa/aaa-iteration-20260728/24-review-final-rerun.png`
6. 모델 관리: `qa/aaa-iteration-20260728/25-model-final-rerun.png`
7. 영상·후보 히스토리: `qa/aaa-iteration-20260728/26-history-final-rerun.png`
8. HWP/HWPX 보고서: `qa/aaa-iteration-20260728/27-report-final-rerun.png`
9. 내보내기: `qa/aaa-iteration-20260728/28-export-final-rerun.png`

증거 내보내기 추가 상태:

- 계약 상태: `qa/aaa-iteration-20260728/30-export-evidence-contract-final.png`
- 완료 상태: `qa/aaa-iteration-20260728/31-export-completed-final.jpg`

alpha.8 재감사·패키지 실기동 증거:

- 명령형 Grounding DINO 평가 연락시트: `qa/aaa-iteration-20260729/12-grounding-command-sign-vegetation-contact-sheet.jpg`
- 패키지 홈: `qa/aaa-iteration-20260729/13-alpha8-packaged-home-1280x720.jpg`
- 패키지 라벨링·명령 작업대: `qa/aaa-iteration-20260729/14-alpha8-packaged-labeling-command-1280x720.jpg`
- 패키지 모델 출처 표 상단: `qa/aaa-iteration-20260729/15-alpha8-packaged-model-sources-1280x720.jpg`
- 패키지 모델 출처 표 하단·거절 모델·데이터셋: `qa/aaa-iteration-20260729/16-alpha8-packaged-model-rejected-dataset-1280x720.jpg`

모든 패키지 캡처는 같은 실제 Electron 창과 1280×720 목표 상태에서 생성했다. 화면 전체 스크롤은 없으며, 라벨링에서는 `candidate-media-stack`, 모델 관리에서는 출처 표처럼 길이가 입력에 따라 실제로 달라지는 영역만 하나의 경계가 분명한 내부 스크롤을 소유한다.

위 alpha.8 캡처는 패키지 역사 증거이며 현재 시각 기준이 아니다. 다음 alpha.9 Electron 1280×720 캡처도 당시 완료된 역사 증거이며, 현재 alpha.10 최종 UI 시각 판정은 §9의 PASSED 감사 결과에서 별도로 관리한다.

1. 홈: `qa/ui-iteration-20260729/06-home-final-1280x720.png`
2. 영상 가져오기: `qa/ui-iteration-20260729/07-import-final-1280x720.png`
3. 질문형 라벨링: `qa/ui-iteration-20260729/08-label-final-1280x720.png`
4. 후보 추출: `qa/ui-iteration-20260729/09-analysis-final-1280x720.png`
5. 사건 검토: `qa/ui-iteration-20260729/10-review-final-1280x720.png`
6. GPS 히스토리: `qa/ui-iteration-20260729/11-history-final-1280x720.png`
7. 모델 관리: `qa/ui-iteration-20260729/12-model-final-1280x720.png`
8. HWP/HWPX 보고서: `qa/ui-iteration-20260729/13-report-final-1280x720.png`
9. 내보내기: `qa/ui-iteration-20260729/14-export-final-1280x720.png`

모든 alpha.9 최종 화면에서 document client/scroll 크기는 `1280×720`, 각 `.screen`은 `1180×612`로 일치했다. 문서와 screen 자체 overflow는 0이고, 사건·분석 작업 목록처럼 길이가 실제로 변하는 데이터 영역만 내부 스크롤을 소유한다.

## 5. 기능 정직성

디자인 통과는 제품 전체 기능 완료를 뜻하지 않는다.

- 장면·선명도 후보는 도로 결함 확정 결과가 아니다.
- 고정 번들 모델이 지원하지 않거나 manifest·artifact 검증을 통과하지 못한 유형은 자연어 명령만으로 동적 검지가 되지 않는다.
- 실제 GPU 명령 평가에서 `표지판을 가린 수풀을 찾아줘`를 고정 `sign_vegetation_occlusion` 레시피로 컴파일해 한 완료 작업의 후보 7건을 실행했고, 3개 프레임에서 3개 박스를 얻었다. 시각 검토 결과 3건 모두 오탐이므로 자동 라벨이 아니라 review-only 어려운 음성 예제로 다뤄야 한다.
- alpha.9 UI의 `명령 실행`은 사용자 동의 뒤 고정 해시의 번들 ONNX Grounding DINO 실행기를 실제 호출하고, 검증된 결과만 review-only 질문 sidecar로 가져온다. 새 추론·검증 캐시·실행 불가 상태를 별도로 표시한다.
- alpha.9 소스 종단은 CPU fallback으로 7개 프레임을 약 23.844초에 평가해 review-only 제안 7건을 만들었고 자동 라벨·Incident·학습·모델 승격은 0건이었다.
- 이 모델은 표지판·식생 같은 객체 후보를 찾는 검토 보조다. `표지판이 식생에 가려졌다`, `삭초가 필요하다` 같은 관계·조치 판정을 검증한 전용 모델이 아니므로 제안 수를 검지 정확도로 해석하지 않는다.
- alpha.8 설치 패키지에는 ML 런타임과 모델 가중치가 없었다. alpha.9 설치 패키지는 고정 ONNX 실행기와 Q4 모델팩을 포함하며 `win-unpacked` 앱에서 AI 제안 0건이던 작업에 신규 review-only 제안 7건을 생성했다.
- 모델·데이터 출처 카탈로그는 ProgramData의 허용된 고정 경로와 고정 SHA-256을 읽기 전용으로 확인한다. 자동 다운로드·실행·프로젝트 학습 혼합·모델 승격을 수행하지 않는다.
- 별도 Vinoth RDD YOLOv8s intake는 349개 대표 JPEG에서 10프레임·11박스를 제안했으나 사람 확인 결과 확정 양성 0프레임, 판단 어려움 1프레임, 오탐 9프레임·10박스였다. 정확한 RDD 출처·라이선스와 YOLOv8 base-weight 권리도 불명확하므로 활성화·설치본 번들·재배포를 거부하고 평가 전용으로만 보존한다.
- 승인 전 후보는 Incident로 표시하지 않는다.
- 영상 컨테이너 메타데이터 좌표는 촬영 시작점으로만 표시하며 후보 위치나 경로로 보간하지 않는다. 별도로 연결한 GPX는 import·offset·영상 시각 보간·MapLibre 경로/진행선/현재점/후보점·전체 경로 export까지 구현됐지만 시간대가 있는 실제 운영 GPX 수용은 아직 없다.
- HWP/HWPX 문서 영역은 COM 미등록 환경에서 정적 구성 스케치이며 실제 저장 성공으로 표시하지 않는다.
- URL 입력은 검사 전용이고 다운로드·프로젝트 등록은 미구현으로 표시한다.
- 승인 근거 JPEG/MP4는 정확히 선택한 작업의 active·approved Incident에 한해 ZIP에 포함하고 전체 원본 영상은 제외한다.
- 얼굴·번호판 비식별화 공유 파생본은 별도 미구현 기능 게이트다.

## 6. 회귀 검증

alpha.8의 마지막 완결 패키지 회귀 결과:

- domain: 10 passed
- renderer state: 76 passed
- Electron: 48 passed
- Python bridge: 362 passed, 2 skipped
- Sites worker: 4 passed
- TypeScript: passed
- Vite production build: passed

alpha.9 역사 검증은 517 passed, 2 skipped, 실패 0이다. 세부 결과는 domain 10, renderer 81, Electron 51, Python bridge 368 passed·2 skipped, release manifest 3, Sites 4이며 TypeScript와 Vite production build도 통과했다. 당시 실제 소스 Electron 1280×720 9개 화면의 document/screen overflow 0과 필수 조작 노출을 확인했고, 최종 `win-unpacked`에서도 SUIT Variable 로드와 자연어 명령→번들 CPU ONNX→신규 review-only 제안 7건을 확인했다.

현재 alpha.10 소스·패키지 회귀는 **546 passed, 2 skipped, 실패 0**이다. 세부 결과는 domain 10, renderer 91, Electron 56, Python bridge 382 passed·2 skipped, release manifest 3, Sites 4이며 TypeScript, Vite production build, Windows NSIS 패키징과 frozen bridge 스모크도 통과했다. SUIT Variable 2.0.5 전체 face가 번들되고 Vite가 `SUIT-Variable-gqD86R3M.woff2`를 생성한 것도 확인했다.

코드·패키지 회귀와 final-suit-p2 UI 시각 감사는 통과했다. 그러나 이번 시각 감사는 지정 viewport의 핵심 UI 가시성·도달성 판정이며 제품 전체 기능이나 현장 수용 완료 판정이 아니다. 제품은 계속 development alpha이고, 출하 준비도는 코드 서명, 클린 PC 설치·업그레이드·제거, 실제 운영 GPX 수용, 검증된 도로 결함 모델, 비식별화 공유 파생본, HWP COM 실기 검증과 저사양 현장 PC 성능 측정이 완료될 때 별도로 판정한다.

## 7. Windows alpha.8 산출물 - 역사 기록

- 설치본: `desktop/release/TrafficVisson-Setup-0.1.0-alpha.8-x64.exe`
- 크기: 328,170,964 bytes
- SHA-256: `ce1d5c09202959bb1da63826b5b9bd3d94dbbea11d802972c794866956e8e16d`
- 당시 manifest 생성 시각: `2026-07-28T17:25:47.361Z` (`2026-07-29 02:25:47.361+09:00`). 현재 `desktop/release/release-manifest.json`은 최신 빌드로 교체되므로 alpha.8 역사 증거 포인터로 사용하지 않는다
- checksum: 8개 항목 전부 일치
- unpacked runtime: 실제 Electron 창, frozen bridge, 실제 프로젝트 `20250916_073358.mp4`, 후보 7건 복구 확인
- unpacked runtime 캡처: `qa/aaa-iteration-20260729/13-alpha8-packaged-home-1280x720.jpg`
- 자연어 선라벨 UI 캡처: `qa/aaa-iteration-20260729/14-alpha8-packaged-labeling-command-1280x720.jpg`
- 모델 출처 카탈로그 캡처: `qa/aaa-iteration-20260729/15-alpha8-packaged-model-sources-1280x720.jpg`, `qa/aaa-iteration-20260729/16-alpha8-packaged-model-rejected-dataset-1280x720.jpg`
- 별도 명령형 GPU 검증: 후보 7건 → proposal 프레임 3건·박스 3건, 수동 검토상 3건 모두 오탐, 자동 라벨·Incident·모델 승격 0건
- 서명·배포 상태: `NotSigned`, `development_only_not_distribution_ready`

## 8. Windows alpha.9 산출물 - 역사 기록

- 버전: `0.1.0-alpha.9`
- 포함 정의: Electron 앱, frozen bridge, FFmpeg·FFprobe, 고정 ONNX 명령형 실행기, Grounding DINO Tiny Q4 모델팩, tokenizer·설정·Apache-2.0 라이선스·Third-Party Notices
- runner manifest SHA-256: `65f6cc1f6941b25936c10b8387a7867d6249e25b5453a1d2cb8567e28ba547f3`
- 모델 출처: `onnx-community/grounding-dino-tiny-ONNX`, revision `11778337bb87ff84af72a64928c86616d86c495e`
- 선택 모델 SHA-256: `2ed98a91b75f00d325c7581bee83b92b5b98460981772edf5ed844370ac25c11`
- installer: `desktop/release/TrafficVisson-Setup-0.1.0-alpha.9-x64.exe`
- 크기: 491,356,877 bytes
- SHA-256: `c4d00ec0d122df534621ae40e18fe8b6d39ef85517284d7684ebb46e0c99ec2b`
- release manifest: `2026-07-28T19:03:29.146Z` (`2026-07-29 04:03:29.146+09:00`), 배포 artifact 11개·manifest 포함 checksum 12개
- packaged 종단: SUIT Variable 로드, 1280×720 document/screen overflow 0, 실제 프로젝트·51개 완료 분석 작업 복구
- 로컬 AI 종단: `20250820_133356.mp4`의 기존 AI 제안 0건 작업에서 7프레임을 CPU fallback 약 14.856초에 평가해 `local_model_generated/generated` review-only 제안 7건 생성
- 증거: `qa/aaa-iteration-20260729/17-alpha9-suit-packaged-local-ai-result-1280x720.png`
- 승인 경계: 자동 라벨·Incident·학습·모델 승격 0건
- 배포 상태: `NotSigned`, `development_only_not_distribution_ready`

## 9. Windows alpha.10 현재 산출물과 final-suit-p2 UI 감사 결과

- 버전: `0.1.0-alpha.10`
- installer: `desktop/release/TrafficVisson-Setup-0.1.0-alpha.10-x64.exe`
- 크기: 491,689,854 bytes
- SHA-256: `b0b0a79f952fd614f8c23a449073bfeea108af82e164c5a59f9c1d84d408f945`
- 배포 상태: `NotSigned`, `development_only_not_distribution_ready`
- 전체 회귀: 546 passed, 2 skipped. Domain 10, renderer 91, Electron 56, Python bridge 382 passed·2 skipped, release manifest 3, Sites 4
- 빌드 게이트: TypeScript, Vite production build, Windows NSIS 패키징, frozen bridge 스모크 passed
- 글꼴: 앱 번들 `SUIT Variable 2.0.5`; Vite 산출물 `SUIT-Variable-gqD86R3M.woff2` 624,536 bytes
- GPX 경계: import·offset·interpolation·MapLibre 경로/진행선/현재점/후보점·전체 경로 export 구현. 시간대가 있는 실제 운영 GPX 수용은 미실시
- 모델 경계: Vinoth RDD 349 JPEG 평가의 10프레임·11박스 중 확정 양성 0, 판단 어려움 1프레임, 오탐 9프레임·10박스. 활성화·설치본 번들·재배포 거부
- **PASSED — final-suit-p2 UI 시각 감사:** [`최종 감사 보고서`](qa/aaa-continuation-20260729/final-suit-p2-audit.md)는 P0 0건·P1 0건·P2 0건이다. 실제 Electron의 1280×720 DPR 1.0, 1282×722 DPR 1.25, 1600×900 DPR 1.0에서 라벨링 안전 문구·정책값 한 글자 고아행·11.5px 증거 메타데이터와 검지 오버레이·History 사건 목록 및 근거 상세의 마지막 항목 도달성을 확인했다
- **최종 패키지 실기동:** [`패키지 홈`](qa/aaa-continuation-20260729/final-suit-p2-audit/15-packaged-home-1600x900-final.png)과 [`패키지 히스토리`](qa/aaa-continuation-20260729/final-suit-p2-audit/16-packaged-history-1600x900-final.png)에서 SUIT 실제 로드, document scroll 0, History 두 내부 스크롤의 마지막 항목 도달성을 다시 확인했다
- **P3 성능 경계:** lazy-load되는 `HistoryScreen` 청크는 약 969.24kB로 Vite 500kB 경고가 남는다. 기능·가시성 결함으로 판정하지 않았지만 저사양 현장 PC에서 첫 History 진입 시간을 별도 측정해야 한다
- 범위 경계: 위 PASSED는 이번 final-suit UI 감사만의 판정이다. `NotSigned`, `development_only_not_distribution_ready`, 실제 운영 GPX·검지 성능·HWP COM·클린 PC 수용 경계는 그대로다
