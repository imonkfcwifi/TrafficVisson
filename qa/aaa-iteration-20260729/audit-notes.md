# TrafficVisson 1280×720·자연어 로컬 AI 재감사 메모

> 상태: alpha.8 패키지 역사 보존, alpha.9 소스·NSIS·unpacked UI·ONNX 실행기 종단 검증  
> 실제 소스 Electron renderer: 1280×720  
> 시각 기준: `Pretendard와 리퀴드 글래스 디자인/uploads/pasted-1785143633633-0.png`  
> alpha.9 NSIS·unpacked 패키지 검증: passed, 외부 배포 게이트는 미통과

## alpha.8 역사 기록

alpha.8에서는 질문형 라벨링의 중첩 스크롤과 모델 출처 표시를 수정했고
다음 패키지 증거를 남겼다.

- 홈: `13-alpha8-packaged-home-1280x720.jpg`
- 열린 AI 명령 라벨링: `14-alpha8-packaged-labeling-command-1280x720.jpg`
- 모델 소스 상단: `15-alpha8-packaged-model-sources-1280x720.jpg`
- 거부 모델·데이터셋 하단: `16-alpha8-packaged-model-rejected-dataset-1280x720.jpg`
- 설치본: `desktop/release/TrafficVisson-Setup-0.1.0-alpha.8-x64.exe`
- 크기: 328,170,964 bytes
- SHA-256:
  `ce1d5c09202959bb1da63826b5b9bd3d94dbbea11d802972c794866956e8e16d`
- 회귀: domain 10, renderer 76, Electron 48, Python bridge 362
  passed·2 skipped, Sites worker 4
- 배포 상태: `NotSigned`, `development_only_not_distribution_ready`

alpha.8의 “한 화면 통과” 판정은 기능 노출과 document overflow에
치우쳤다. 컨셉 이미지와 냉정하게 다시 비교하면 다음 문제가 남아 있었다.

1. 네 개의 같은 크기 KPI 카드가 화면 위계를 평평하게 만들었다.
2. 큰 빈 패널이 실제 업무 밀도보다 강해 MVP처럼 보였다.
3. 8~10px 한글, 1.0~1.15 line-height와 mono 혼용이 가독성을 떨어뜨렸다.
4. `overflow:hidden`과 한 줄 말줄임이 라벨링 진행 상태, 분석 요약,
   검토 근거의 필수 문장을 잘랐다.
5. 가져오기 소스 탭의 `네트워크 폴더`, `영상 링크`가 줄바꿈됐다.

## alpha.9 UI 정정

alpha.9에서는 다음을 적용했다.

- 앱 번들 `SUIT Variable 2.0.5`를 전체 UI 글꼴로 적용했다. 라이선스는
  OFL-1.1이며 참조 폴더명의 `Pretendard`는 경로명으로만 유지한다.
- 홈 첫 카드를 넓은 주 KPI로 만들고 나머지를 보조 카드로 축소했다.
- 반경 18px, 옅은 경계, 얕은 그림자와 저채도 유리 표면을 통일했다.
- 본문을 11~12px 이상, 핵심 질문을 16px, line-height를 1.3~1.4로
  확보하고 배지 최소 높이를 24px로 잡았다.
- 문서와 `.screen` 전체 스크롤은 없애고 실제로 길이가 변하는 목록만
  내부 스크롤을 갖게 했다.
- 라벨링 설명, 네 응답 버튼과 진행 패널을 같은 화면 안에 유지했다.

실제 소스 Electron 9개 화면 증거는
`qa/ui-iteration-20260729/06-home-final-1280x720.png`부터
`14-export-final-1280x720.png`까지이며, 컨셉 결합 비교는
`qa/ui-iteration-20260729/15-reference-vs-final.png`다.

수치 확인:

- 모든 화면에서 document client/scroll 크기 `1280×720` 일치
- 모든 `.screen` 크기 `1180×612`, 자체 문서 overflow 0
- 라벨링 네 응답 버튼 각각 46px, 하단 진행 패널 bottom 680px
- 분석 요약 초과 높이 제거
- 사건·분석 작업 목록만 의도된 내부 스크롤 소유

## alpha.9 자연어 로컬 AI

- runner manifest SHA-256:
  `65f6cc1f6941b25936c10b8387a7867d6249e25b5453a1d2cb8567e28ba547f3`
- 모델: `onnx-community/grounding-dino-tiny-ONNX`
- revision: `11778337bb87ff84af72a64928c86616d86c495e`
- 선택 파일: `onnx/model_q4.onnx`, 227,229,745 bytes
- 모델 SHA-256:
  `2ed98a91b75f00d325c7581bee83b92b5b98460981772edf5ed844370ac25c11`
- 라이선스: Apache-2.0
- 실행 정책: DML 우선, 현재 개발 PC는 CPU fallback
- 소스 종단: 7프레임, review-only 제안 7건, 약 23.844초
- 자동 라벨 0건, Incident 0건, 학습 반영 0건, 모델 승격 0건

이 결과는 앱이 사용자의 자연어 명령을 고정 recipe로 바꾸고 로컬 모델로
실제 프레임을 평가해 질문 후보를 만든다는 증거다. 표지판·식생 객체를
찾는 것과 식생 가림·삭초 필요 관계를 맞게 판정하는 것은 별개이므로,
7개 제안을 정확한 유지보수 사건이나 성능 통과로 해석하지 않는다.

## alpha.9 패키지 확인

- 전체 `npm run test:all`: 517 passed, 2 skipped, 실패 0
- installer: `desktop/release/TrafficVisson-Setup-0.1.0-alpha.9-x64.exe`
- 크기: 491,356,877 bytes
- SHA-256:
  `c4d00ec0d122df534621ae40e18fe8b6d39ef85517284d7684ebb46e0c99ec2b`
- release manifest: `2026-07-28T19:03:29.146Z`, 배포 artifact 11개,
  manifest 포함 checksum 12개
- `win-unpacked`: 실제 프로젝트·51개 완료 분석 작업 복구, SUIT Variable
  로드, 1280×720 document/screen overflow 0
- 패키지 AI: 기존 AI 제안 0건 작업의 7프레임을 CPU fallback 약
  14.856초에 평가해 신규 review-only 제안 7건 표시
- 패키지 증거:
  `17-alpha9-suit-packaged-local-ai-result-1280x720.png`
- 자동 라벨·Incident·학습·승격: 0건

남은 출하 게이트는 Authenticode 코드 서명, Python이 없는 클린 PC,
설치·업그레이드·제거, DML 환경, 검증된 한국 도로 정답 세트다.
