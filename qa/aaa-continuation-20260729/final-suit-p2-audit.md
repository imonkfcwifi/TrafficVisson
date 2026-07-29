# TrafficVisson Final SUIT P2 Audit

감사 일시: 2026-07-29  
감사 대상: Electron 소스 빌드 `desktop@0.1.0-alpha.10`  
최종 판정: **PASSED — P0 0건 / P1 0건 / P2 0건**

최종 통합 수정본은 1280×720 DPR 1.0, 최소 창의 DPR 1.25 경로, 1600×900 DPR 1.0에서 문서 스크롤 없이 핵심 작업을 유지한다. 이전 감사의 P2 세 건인 라벨링 안전 문구 잘림, 1600 히스토리 eyebrow 잘림, 증거 메타데이터의 8~10px 마이크로타입은 모두 폐쇄됐다.

감사 중 새로 발견한 라벨링 정책값 한 글자 고아행과 1600 히스토리 하단 근거 영역의 접근 불가 문제도 최종 빌드에서 수정·재측정했다. 최종 히스토리는 사건 목록과 근거 상세만 각각 독립 스크롤을 소유하며, 두 영역 모두 마지막 데이터까지 도달한다.

## 감사 방법

- 최신 소스에서 `npm run build:desktop`을 반복 실행하고, 각 수정 뒤 격리된 임시 사용자 프로필로 실제 Electron을 새로 실행했다.
- 실행 창 제목은 `TrafficVisson · 로컬 AI 도로점검`, 렌더러 URL은 `trafficvisson://app/index.html`이었다.
- 실제 창을 1280×720 DPR 1.0, 1600×900 DPR 1.0으로 설정했다.
- 별도 Electron 프로세스에 `--force-device-scale-factor=1.25`를 적용했다. Windows/Electron 최소 창 반올림으로 CSS viewport는 1282×722, DPR은 1.25였다.
- DOM의 client/scroll 크기, Range glyph 경계, 계산 글꼴, 답변 버튼 경계, 문서 스크롤, 내부 스크롤의 최대 위치와 마지막 항목 도달성을 측정했다.
- 각 측정 뒤 실제 스크린샷을 원본 크기로 육안 확인했다.
- 감사 담당자는 제품 소스를 수정하지 않았다. 감사에서 발견한 문제는 메인 구현 작업자가 수정했고, 감사 담당자는 새 빌드를 다시 실행해 폐쇄 여부만 판정했다.

## 최종 증거

| 화면 | 환경 | 결과 | 증거 |
|---|---|---|---|
| 홈 | 1600×900 / DPR 1.0 | 후보 이미지 3장, 후보 큐 7건, 입력 provenance가 한 화면에 표시 | [13 최종 홈](final-suit-p2-audit/13-home-1600x900-dpr1-final.png) |
| 질문형 라벨링 | 1280×720 / DPR 1.0 | AI 명령 패널, 안전 문구, 질문, 답변 4개, 건너뛰기 표시 | [11 최종 라벨링](final-suit-p2-audit/11-labeling-open-1280x720-dpr1-final.png) |
| 질문형 라벨링 | 1282×722 / DPR 1.25 | 동일 핵심 영역 표시, 문서 스크롤 0 | [12 최종 라벨링 125%](final-suit-p2-audit/12-labeling-open-min1280x720-dpr1.25-final.png) |
| GPS 히스토리 | 1600×900 / DPR 1.0 | 영상·지도 상태·타임라인·사건 목록·근거 상세 동시 표시 | [10 최종 히스토리](final-suit-p2-audit/10-history-1600x900-dpr1-postfix2.png) |
| 레퍼런스 비교 | A/B/C 익명 시트 | 현재 라벨링·원본 레퍼런스·현재 홈을 동일 규격으로 비교 | [14 블라인드 비교](final-suit-p2-audit/14-blind-reference-comparison.png) |
| 최종 Windows 패키지 홈 | 1600×900 / DPR 1.0 | SUIT 실제 로드, edge-to-edge 앱 셸, document scroll 0 | [15 패키지 홈](final-suit-p2-audit/15-packaged-home-1600x900-final.png) |
| 최종 Windows 패키지 히스토리 | 1600×900 / DPR 1.0 | 소스 감사와 동일한 네 행·두 내부 스크롤 계약 | [16 패키지 히스토리](final-suit-p2-audit/16-packaged-history-1600x900-final.png) |

초기 결함과 중간 수정 실패도 삭제하지 않고 감사 추적 증거로 보존했다.

- [02 초기 라벨링](final-suit-p2-audit/02-labeling-open-1280x720-dpr1.png): `정책 · 전수 검토`의 마지막 글자 고아행.
- [06 초기 히스토리](final-suit-p2-audit/06-history-1600x900-dpr1.png): 하단 카드가 screen 밖으로 연장.
- [09 1차 히스토리 수정](final-suit-p2-audit/09-history-1600x900-dpr1-postfix.png): 내부 스크롤 높이가 0px로 붕괴.
- [10 2차 히스토리 수정](final-suit-p2-audit/10-history-1600x900-dpr1-postfix2.png): 명시적 grid 행과 도달 가능한 내부 스크롤로 폐쇄.

## 이전 P2 세 건 재측정

### P2-1 라벨링 안전 설명 — 폐쇄

1280×720 DPR 1.0:

- 강제 검토 설명: client `382×15`, scroll `382×15`.
- 실제 텍스트 Range: `381.86×14`, 요소 경계 안에 완전히 포함.
- 실험 모델 설명: client `406×43`, scroll `406×43`.
- 실제 텍스트 Range는 3줄 `398.47×42.59`, 요소 경계 안에 완전히 포함.
- 계산 글꼴은 두 설명 모두 `"SUIT Variable", SUIT, system-ui, sans-serif`, `11px`.
- `예`, `아니오`, `판단 어려움`, `영역 수정`은 각각 약 `105.72×46`.
- 건너뛰기는 `437.88×30`, 하단 `612.33px`; screen 하단은 `680px`.
- 문서 client/scroll은 모두 `720px`, document scrollTop은 0.

DPR 1.25:

- 강제 검토 설명 client/scroll `382×15`.
- 실험 모델 설명 client/scroll `407×43`.
- 실험 모델 Range의 glyph ascent가 계산 line box 위로 약 0.8px 나가지만 부모 overflow는 visible이고 client/scroll이 같으며, 원본 캡처에서 첫 줄·마지막 줄이 모두 온전하다. 실제 텍스트 잘림으로 판정하지 않는다.
- 답변 4개는 각각 약 `106.05×46`, 건너뛰기 하단 `611.55px`; screen 하단 `681.60px`.
- document client/scroll은 모두 `722px`, scrollTop은 0.

따라서 안전 경계 문장과 자동 승인 금지 문장이 최소 창에서도 전부 읽힌다.

### P2-2 1600 히스토리 헤더 — 폐쇄

- `.screen`: `y=68`, `height=792`, `bottom=860`.
- `.screen-header`: `y=68`, `height=65.016`.
- 왼쪽 텍스트 묶음: `y=68`, `height=65.016`.
- `.eyebrow`: `y=68`, `height=15.516`.
- screen 대비 eyebrow 상단 clip: `0px`.
- 문서 scrollTop은 0이며 titlebar는 `(0, 0, 1600, 56)`, 브랜드는 `(16, 11.5, 198, 32)`로 정상이다.

이전의 약 6.5px 상단 잘림은 재현되지 않는다.

### P2-3 증거 메타데이터 8~10px — 폐쇄

| 정보 | 최종 계산 글꼴 | line-height | client/scroll | 잘림 |
|---|---|---:|---:|---|
| 히스토리 `C-001` | SUIT Variable 11.5px, 650 | 14.95px | 45×19 / 45×19 | 없음 |
| 사건 시간 `00:00:00` | JetBrains Mono Variable 11.5px | 15.525px | 64×16 / 64×16 | 없음 |
| 사건 해시·장면 점수 | SUIT Variable 11.5px | 15.525px | 382×16 / 382×16 | 없음 |
| 근거 상세 점수 | SUIT Variable 11.5px | 15.525px | 397×16 / 397×16 | 없음 |
| 홈 후보 시간·장면 | SUIT Variable 11.5px | 15.525px | 동일한 client/scroll | 없음 |
| 라벨링 후보 UUID 행 | SUIT Variable 11.5px | 15.525px | 500×16 / 540×16 | 의도적 한 줄 말줄임 |
| 검지 오버레이 코드·점수 | JetBrains Mono Variable 11.5px, 700 | 14.95px | 235×23 / 235×23 | 없음 |

긴 후보 UUID는 작업 헤더를 밀어내지 않도록 명시적인 ellipsis를 사용한다. 유형·시간·상태는 같은 화면의 질문 및 오버레이에 별도로 표시되므로 증거 정보 손실로 판정하지 않는다.

검지 오버레이는 1280 DPR 1.0과 DPR 1.25 모두 Range가 자체 span 안에 포함된다. DPR 1.25에서 이미지 경계는 `(98.8, 368.55)–(777.59, 563.38)`, 오버레이 라벨은 `(471.33, 484.77)–(706.06, 507.72)`, 검지 박스는 `(471.93, 509.33)–(516.18, 528.11)`로 라벨과 박스가 모두 이미지 안에 있다.

## 감사 중 발견한 추가 결함의 폐쇄

### 질문 정책값 한 글자 고아행

초기 1280 캡처에서는 `정책 · 전수 검토`의 마지막 `토`가 단독 둘째 줄로 내려갔다. 최종 빌드는 question metadata의 직접 자식을 항목 단위로 묶는다.

- 정책 항목: `72.67×15.52`, client/scroll `73×16`, `white-space: nowrap`.
- 계산 글꼴: SUIT Variable `11.5px / 15.525px`.
- DPR 1.0과 DPR 1.25 모두 한 줄이며 screen 오른쪽 경계 안에 있다.
- 유형 badge와 모델 점수 항목도 각각 `nowrap`이고, 육안 캡처에서 한글 고아행은 없다.

### 1600 히스토리 하단 접근성

1차 수정은 하단을 0px로 붕괴시켜 통과시키지 않았다. 최종 2차 수정에서 히스토리 자체가 네 행 grid를 소유하고, 하단 카드의 사건 목록과 근거 상세가 각각 스크롤을 소유한다.

| 영역 | rect | clientHeight / scrollHeight | 결과 |
|---|---|---:|---|
| screen | `y=68, h=792, bottom=860` | 792 / 792 | 문서·screen overflow 없음 |
| history top | `y=143.016, h=369.359, bottom=512.375` | 369 / 369 | 영상·지도 상태 표시 |
| timeline | `y=522.375, h=106, bottom=628.375` | 104 / 104 | 7개 후보 표시 |
| history bottom | `y=638.375, h=221.625, bottom=860` | 222 / 222 | screen 안에 정확히 종료 |
| incident panel | `y=638.375, h=221.625, bottom=860` | 220 / 220 | panel 자체 잘림 없음 |
| incident list | `y=699.828, h=145.172, bottom=845` | 145 / 387 | max scrollTop 242 |
| evidence panel | `y=638.375, h=221.625, bottom=860` | 220 / 220 | panel 자체 잘림 없음 |
| evidence details | `y=703.375, h=141.625, bottom=845` | 142 / 256 | max scrollTop 114 |

도달성 검증:

- 사건 목록을 최대로 스크롤했을 때 실제 scrollTop이 `242=maxTop`이었고, 마지막 `장면 후보 7`의 하단은 `844.469px`로 scrollport 하단 안에 있었다.
- 근거 상세를 최대로 스크롤했을 때 실제 scrollTop이 `114=maxTop`이었고, 마지막 provenance DL의 하단은 `844.984px`로 scrollport 하단 안에 있었다.

따라서 하단 데이터는 단순히 잘라 숨긴 것이 아니라 사용자 입력으로 끝까지 접근할 수 있다.

## 블라인드 레퍼런스 비교

비교 시트의 매핑은 다음과 같다.

- A: 최종 질문형 라벨링.
- B: 제공된 유일한 리퀴드 글래스 레퍼런스.
- C: 최종 프로젝트 홈.

매핑을 모르는 별도 리뷰어의 전체 순위는 **B > C > A**였다. B는 반투명 표면과 부드러운 깊이의 순수한 리퀴드 글래스 일관성이 가장 강하고, C는 실제 도로 이미지와 작업 큐의 우선순위가 분명해 운영 화면의 계층과 한 화면 활용이 가장 강하다고 평가했다. A는 영상 선택→근거 검사→사용자 답변이라는 작업 순서는 가장 분명하지만, 제어 띠와 중첩 카드가 많아 셋 중 가장 조밀하다고 평가했다.

A와 C는 B의 옅은 청회색 바탕, 둥근 반투명 카드, 절제된 파란 주 강조, 얇은 경계와 부드러운 그림자를 도로 점검 업무에 맞게 변형한 화면으로 보인다. 단순한 금융 대시보드 복제가 아니라, 같은 시각 언어를 데이터 밀도가 높은 데스크톱 검수 도구에 적용한 결과다.

블라인드 리뷰어는 축소된 비교 시트만 보고 A의 보조 설명 밀도를 P1, A의 중첩 표면과 C의 반복 메타데이터를 P2 의견으로 제시했다. 최종 등급에는 다음 원본 크기 재검증을 함께 적용했다.

- 비교 시트에서 A와 C는 각각 폭 600px로 축소돼 실제 CSS viewport 1280~1600px의 보조 글자가 절반 이하로 보인다. 따라서 축소 시트의 판독성만으로 운영 화면의 접근 가능 여부를 확정하지 않았다.
- A 원본에서 핵심 답변은 높이 46px, 건너뛰기는 높이 30px이고 모두 screen 안에 있다. 질문 제목·유형 선택·4개 답변은 보조 설명보다 명확히 크며 겹치지 않는다.
- A의 안전 설명은 원본에서 client/scroll이 같고 3줄 전체가 보인다. 자동 승인 금지라는 핵심 의미가 잘리지 않는다.
- C에서 사건 구분에 필요한 시간·장면 값은 최종 11.5px/15.525px로, 이전 8~10px보다 상향됐고 client/scroll 잘림이 없다.

이에 따라 블라인드 의견은 중요한 향후 표면 단순화·여백 정제 항목으로 보존하되, 원본 크기에서 핵심 판단을 막거나 잘못 읽게 하는 현재 P1/P2로 승격하지 않았다. 현재 캡처에서 기능을 가리거나 의미를 끊는 겹침, 한 글자 고아행, 잘린 안전 문구는 없다.

별도 리뷰어도 원본 11·12·13번 캡처와 정량값으로 재검토한 뒤 초기 P1 의견을 공식 철회했다. 최종 독립 판정은 `P0 없음 / P1 없음 / 반드시 고칠 P2 없음`이다. B보다 약한 깊이 단계, A의 약한 타이포 초점 대비, 좁은 여백 리듬은 작업 완료나 판독성을 해치지 않는 P3 시각 광택 차이로 분류했다.

## P0 / P1 / P2 최종 판정

| 등급 | 건수 | 판정 | 근거 |
|---|---:|---|---|
| P0 | 0 | PASS | 앱 실행·렌더러·핵심 화면을 막는 결함 없음 |
| P1 | 0 | PASS | 답변, 건너뛰기, 히스토리 하단 사건·근거가 모두 표시되고 끝까지 접근 가능 |
| P2 | 0 | PASS | 이전 P2 3건과 감사 중 발견한 고아행·하단 레이아웃을 모두 재캡처로 폐쇄 |

엄격한 기준인 `P0=0`, `P1=0`, `P2=0`을 만족하므로 최종 디자인 감사는 **PASSED**다.

## 자동 검증

- `npm run build:desktop`: PASS.
- `npm run typecheck`: PASS.
- `npm run test:renderer-state`: **91/91 PASS**.
- 최종 build asset: `index-PP8mOAO7.css`, `HistoryScreen-ChaP_9dc.js`.

## 최종 Windows 패키지 실기동

- 설치본: `desktop/release/TrafficVisson-Setup-0.1.0-alpha.10-x64.exe`
- 크기: `491,689,854 bytes`
- SHA-256: `b0b0a79f952fd614f8c23a449073bfeea108af82e164c5a59f9c1d84d408f945`
- manifest와 직접 해시·크기가 일치했다.
- 최종 `win-unpacked/TrafficVisson.exe`를 별도로 실행해 `trafficvisson://app/index.html`, 제품 제목, 실제 프로젝트 홈 복구, `"SUIT Variable"` 로드와 `1600×900` document `clientHeight=scrollHeight=900`, `scrollTop=0`을 확인했다.
- 패키지 히스토리도 screen `bottom=860`, history bottom `bottom=860`, 사건 목록 `145/387px`, 근거 상세 `142/256px`로 소스 감사와 동일했다. 각각 최대 scrollTop `242/114`에서 마지막 항목 하단 `844.469/844.984px`가 scrollport 하단 `845px` 안에 도달했다.
- 이 실기동은 패키지 렌더링 회귀 확인이다. 설치본은 계속 `NotSigned`, `development_only_not_distribution_ready`이며 클린 PC 설치 수용을 대체하지 않는다.

빌드의 `HistoryScreen` 청크는 약 969.24kB로 500kB 경고가 남는다. 화면은 lazy-load되고 이번 감사에서 기능·가시성 결함은 없으므로 P0/P1/P2에 포함하지 않고 성능 P3로 둔다. 저사양 현장 PC에서 첫 히스토리 진입 시간을 별도 성능 예산으로 측정하는 것이 좋다.

## 범위 경계

- DPR 1.25는 Windows 전역 배율 설정을 바꾼 것이 아니라 Electron 프로세스 단위 강제 배율 경로다.
- 수동 스크린리더 낭독, Windows 고대비 모드, 200% 이상 확대, 장시간 GPU 재생 성능은 이번 집중 재감사 범위가 아니다.
- GPX가 없는 현재 프로젝트는 촬영 시작 좌표와 정직한 빈 지도를 표시한다. 이는 UI 결함이 아니라 현재 입력 데이터 상태다.

최종 결론: **TrafficVisson은 브라우저 문서를 창 안에 다시 넣은 화면이 아니라, 한 화면 중심의 실제 Electron 데스크톱 프로그램으로서 최종 SUIT 시각 감사 기준을 통과했다.**
