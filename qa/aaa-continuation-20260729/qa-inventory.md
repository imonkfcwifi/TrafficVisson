# TrafficVisson continuation QA inventory

검증 대상은 소스 Electron 앱이며, 브라우저용 HTML 목업은 정보구조 참고본일
뿐 제품 런타임으로 판정하지 않는다. 시각 비교 기준은
`Pretendard와 리퀴드 글래스 디자인/uploads/pasted-1785143633633-0.png`다.

> 현재 기준: 2026-07-29 05:48 KST, 루트 Master v2.10과 alpha.10 최종 소스·패키지.

## 이번 반복에서 서명할 수 있는 사용자 가시 주장

| 주장 | 기능 검증 | 시각 상태 | 기대 증거 |
|---|---|---|---|
| UI 본문은 로컬 SUIT Variable 2.0.5 전체 face를 사용한다 | `document.fonts.check`, computed body font, Vite `SUIT-Variable` WOFF2 asset과 패키지 번들 확인 | 1280×720 홈·라벨링 | 런타임 측정 JSON과 캡처 |
| Electron 창 전체가 하나의 프로그램이며 중첩 웹 shell이 없다 | 실제 Electron main으로 실행, 메뉴·창·renderer 크기 확인 | 최초 실행 창 | 전체 창 캡처 |
| 1280×720 핵심 업무는 문서 스크롤 없이 보인다 | 9개 nav를 정상 클릭하고 document/screen overflow 측정 | 각 화면 기본 상태 | 9개 viewport 캡처와 bounds JSON |
| 라벨링 질문·네 답변·건너뛰기가 항상 접근 가능하다 | 정상 클릭으로 기본/AI 명령 열림/영역 수정 상태 전환 | 1280×720, 1600×900 | 필수 영역 bounding boxes와 캡처 |
| AI 명령 패널은 질문·근거를 가리지 않는다 | 패널 열기/닫기, compile 입력 상태 전환 | 1280×720, 1600×900 | 겹침 검사와 캡처 |
| 분석 화면은 긴 작업 표 하나만 세로 스크롤을 소유한다 | 분석 nav 이동 후 모든 scroll owner 계산 | 1280×720, 1600×900 | scroll owner JSON과 캡처 |
| GPX가 없으면 경로를 꾸며내지 않는다 | 히스토리 nav 이동, 빈 GPS 안내 확인 | 1280×720 | 빈 상태 캡처 |
| 연결된 GPX는 영상 시각과 보간되고 실제 gap을 잇지 않는다 | 격리 fixture에서 GPX import/offset/get 및 renderer 계산 검증 | GPX 연결 히스토리 | API 테스트, route 캡처 |
| GPX 경로와 provenance가 추적 패키지에 포함된다 | 격리 fixture export 후 CSV/manifest hash·scope 검증 | 내보내기 완료 상태 | 자동 테스트와 artifact 검사 |

## 현재 자동 회귀·패키지·모델 경계

- 최종 재생성 alpha.10 설치본 `desktop/release/TrafficVisson-Setup-0.1.0-alpha.10-x64.exe`는 491,689,854 bytes, SHA-256 `b0b0a79f952fd614f8c23a449073bfeea108af82e164c5a59f9c1d84d408f945`다. TypeScript, Vite production build, frozen bridge smoke, NSIS 패키징과 manifest 검증은 통과했지만 `NotSigned`, `development_only_not_distribution_ready`다.
- 최신 자동 회귀는 총 **546 passed, 2 skipped**다: Domain 10, renderer 91, Electron 56, Python bridge 382 passed·2 skipped, release manifest 3, Sites worker 4.
- GPX 가져오기·시간 오프셋·gap 제한 보간·MapLibre 전체 경로/진행선/현재점/후보점·전체 경로 export는 구현됐다. 다만 시간대가 있는 실제 운영 GPX의 현장 수용은 아직 서명하지 않는다.
- Vinoth RDD 평가는 실제 대표 JPEG 349개에서 10프레임·11박스를 제안했다. 육안 판정은 확정 양성 0프레임, 판단 어려움 1프레임, 오탐 9프레임·10박스였으므로 활성화·설치본 번들·재배포를 거부하고 평가 전용으로만 보존한다.

## final-suit-p2 최종 UI 시각 감사 결과

| 상태 | 감사 범위 | 통과 조건 | 증거 위치 |
|---|---|---|---|
| **PASSED — P0 0 / P1 0 / P2 0** | 실제 Electron 1280×720 DPR 1.0, 최소 창 반올림 1282×722 DPR 1.25, 1600×900 DPR 1.0 | 라벨링 안전 문구·정책값 고아행·11.5px 메타데이터/검지 오버레이·History 하단 사건/근거의 끝까지 도달성을 원본 캡처와 bounds/scroll 측정으로 확인 | [`qa/aaa-continuation-20260729/final-suit-p2-audit.md`](final-suit-p2-audit.md) |

최종 보고서는 이전 P2 세 건과 감사 중 발견한 정책값 한 글자 고아행·History 하단 접근성 결함을 모두 재빌드·재측정으로 폐쇄했다. 이 **PASSED**는 이번 final-suit UI 감사만의 판정이며 제품 전체·현장 수용 완료를 뜻하지 않는다.

`HistoryScreen` 청크는 lazy-load되지만 약 969.24kB로 Vite 500kB 경고가 남는다. 기능·가시성 P0/P1/P2가 아니라 저사양 현장 PC의 첫 History 진입 시간을 별도 측정할 P3 성능 경계다.

## 사용자 조작과 상태 전환

- 좌측 9개 navigation: home, import, labeling, analysis, review, model,
  history, report, export.
- 프로젝트 선택기: 현재 프로젝트 유지와 프로젝트 범위 변경.
- 라벨링: 영상 선택, 다음 AI 제안 영상, AI 명령 열기/닫기, 명령 입력,
  유형 선택, 실행 동의, 네 기본 답변, 영역 수정, 건너뛰기.
- 분석: 저장 작업 목록 내부 스크롤과 작업 복구.
- 히스토리: 재생/일시정지, 앞뒤 이동, 배속, 후보 선택, GPX 파일 연결,
  시간 오프셋 ±1초와 초기화.
- 내보내기: 대상 폴더 선택, 추적 패키지 생성, 완료 provenance.

## 탐색 시나리오

1. 1600×900에서 긴 실험 제안 설명과 AI 명령 패널이 동시에 열린 가장
   밀집한 라벨링 상태를 확인한다.
2. 1280×720에서 질문이 두 줄 이상인 상태로 `영역 수정`을 열고 키보드
   방향키를 사용해도 답변과 저장 조작이 잘리지 않는지 확인한다.
3. GPX가 없는 실제 프로젝트에서 경로·현재 차량·후보 좌표가 표시되지
   않는지 확인한다.
4. 10초 초과 GPX gap과 영상 범위 밖 시각에서 좌표 보간이 `null`인지
   단위·API·renderer 세 층에서 확인한다.

## 이번 반복에서 의도적으로 서명하지 않는 항목

- 한국 도로 결함 검지의 실사용 정밀도·재현율.
- 자동 SAM2 마스크 분할.
- GPU 재학습·후보 모델 승격·롤백.
- 한/글 COM이 설치되지 않은 현재 PC의 실문서 종단.
- 얼굴·번호판 자동 비식별화.
- 시간대가 있는 실제 운영 GPX의 지도 재생·동기화·전체 export 현장 수용.
- 저사양 현장 PC의 약 969.24kB History 청크 첫 진입 성능 예산.
- 클린 PC 코드 서명 설치·업그레이드·제거.
