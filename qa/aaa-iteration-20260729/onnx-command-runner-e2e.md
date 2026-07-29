# TrafficVisson alpha.9 ONNX 자연어 명령 실행기 E2E

> 실행일: 2026-07-29  
> 범위: 소스 Electron→frozen 대상 bridge 계약→고정 ONNX 실행기→review-only 질문 제안  
> 패키지 상태: 소스·NSIS·unpacked 종단 통과, 외부 배포는 차단

## 목적

사용자가 자연어로 원하는 도로 문제를 지시했을 때 앱이 저장된 과거
proposal만 검색하는 데서 끝나지 않고, 현재 완료 작업의 실제 후보
프레임을 로컬 모델로 평가해 사용자가 클릭해서 검토할 제안을 만드는지
확인한다.

이 시험은 자동 라벨링 정확도 시험이 아니다. 통과 조건은 모델 제안을
사용자 승인 전 상태로만 가져오고 라벨·Incident·학습 데이터를 만들지
않는 것이다.

## 고정 실행 구성

| 항목 | 값 |
|---|---|
| runner ID | `trafficvisson.command-proposal.onnx-grounding-dino` |
| runner version | `0.1.0` |
| protocol | `trafficvisson.command-proposal-request.v1` |
| runner manifest SHA-256 | `65f6cc1f6941b25936c10b8387a7867d6249e25b5453a1d2cb8567e28ba547f3` |
| model pack | `trafficvisson-onnx-grounding-dino-tiny` `0.2.0-review` |
| upstream | `onnx-community/grounding-dino-tiny-ONNX` |
| revision | `11778337bb87ff84af72a64928c86616d86c495e` |
| selected model | `onnx/model_q4.onnx` |
| model size | 227,229,745 bytes |
| model SHA-256 | `2ed98a91b75f00d325c7581bee83b92b5b98460981772edf5ed844370ac25c11` |
| license | Apache-2.0 |
| input | 800×800 |
| thresholds | box 0.25, text 0.25 |
| maximum predictions | 프레임당 32 |
| score semantics | `model_score_not_probability` |

실행기 EXE에는 ONNX Runtime, tokenizer, NumPy와 Pillow가 동봉된다.
ONNX Runtime이 `DmlExecutionProvider`를 제공하면 DML을 우선하고,
그렇지 않으면 `CPUExecutionProvider`로 실행한다. 이번 실측은 CPU
fallback이다.

## 시험 범위

- 프로젝트:
  `11111111-1111-4111-8111-111111111111`
- 완료 분석 작업:
  `1ca29d0d-7dd7-4024-9f98-6d0ffa1d6984`
- 영상: `20250916_073358.mp4`
- 후보 프레임: 7개
- 입력 명령 계열: 사용자가 요청한 도로 문제를 고정 issue type과
  versioned prompt recipe로 컴파일
- 실행 동의: 사용자가 앱에서 명시

renderer는 프로젝트·작업·명령·동의만 전달했다. 실행 파일, 모델 경로,
프로세스 인수, timeout, provider와 threshold는 renderer가 선택하지
않았다.

## 종단 흐름

1. bridge가 명령을 NFKC 정규화하고 허용 issue type 하나와 고정
   positive/negative prompt로 컴파일했다.
2. 원문 대신 `command_sha256`, `prompt_sha256`, recipe ID와 구조화
   prompt를 준비했다.
3. bridge에 고정된 runner manifest SHA-256을 확인했다.
4. runner EXE, model manifest, Q4 ONNX, tokenizer, 설정, 라이선스와
   고지 파일의 크기·SHA-256을 확인했다.
5. 정확한 완료 작업의 후보 JPEG 7개를 path·size·SHA-256으로 해석했다.
6. bounded JSON을 stdin으로 전달하고 실행기는 오프라인 ONNX 추론을
   수행했다.
7. bridge가 bounded stdout JSON의 프로젝트·작업·recipe·candidate
   집합·박스·점수를 다시 검증했다.
8. 검증된 결과만 review-only 질문 sidecar로 저장하고 UI의 첫 제안으로
   이동할 수 있게 했다.

## 실측 결과

| 측정 | 결과 |
|---|---:|
| 평가 프레임 | 7 |
| review-only 제안 | 7 |
| 실행 시간 | 약 23.844초 |
| 실행 provider | CPU fallback |
| `proposal_origin` | `local_model_generated` |
| `proposal_generation_status` | `generated` |
| 자동 수락 | 0 |
| 자동 라벨 | 0 |
| Incident | 0 |
| 학습 반영 | 0 |
| 모델 승격 | 0 |

결과는 사용자가 질문 카드에서 `예`, `아니오`, `판단 어려움`,
`영역 수정` 중 하나를 선택하기 전까지 승인 annotation이나 유지보수
사건이 아니다. 같은 recipe·후보·manifest·이미지 해시의 유효 결과는
이후 `cached_existing/cached`로 재사용할 수 있고, 실행기 누락 또는
무결성 실패는 `none/unavailable`로 구분한다.

## 판정

소스 E2E는 통과했다. alpha.9 앱은 자연어 명령을 받아 실제 로컬 ONNX
추론을 실행하고 질문형 라벨링 후보로 가져오는 기능을 구현했다.

다만 Grounding DINO가 표지판이나 식생 객체를 찾았다는 사실만으로
`식생이 표지판을 가렸다`, `삭초가 필요하다`, `도로가 파손됐다`를
확정할 수 없다. 이번 7개 결과에는 한국 도로 정답 검증 세트가 없고,
유형별 정밀도·재현율이나 객체 간 관계·유지보수 조치 성능을 측정하지
않았다. 따라서 이 기능은 현재 라벨링 검토 범위를 줄이는 보조 기능이다.

## 패키지 E2E

| 측정 | 결과 |
|---|---:|
| installer | `desktop/release/TrafficVisson-Setup-0.1.0-alpha.9-x64.exe` |
| installer 크기 | 491,356,877 bytes |
| installer SHA-256 | `c4d00ec0d122df534621ae40e18fe8b6d39ef85517284d7684ebb46e0c99ec2b` |
| manifest 생성 | `2026-07-28T19:03:29.146Z` |
| 배포 artifact / checksum | 11 / 12 |
| Authenticode | `NotSigned` |
| 시험 작업 | `e27b78d0-d0c5-4520-b85e-46b91912a852` |
| 영상 | `20250820_133356.mp4` |
| 실행 전 저장 AI 제안 | 0 |
| 평가 프레임 | 7 |
| 신규 review-only 제안 | 7 |
| 패키지 warm CPU 실행 시간 | 약 14.856초 |
| `proposal_origin/status` | `local_model_generated/generated` |
| 자동 라벨·Incident·학습·승격 | 각각 0 |

`win-unpacked/TrafficVisson.exe`는 실제 프로젝트와 51개 완료 작업을
복구하고, 앱에 동봉한 runner·model manifest·Q4 ONNX 무결성 체인을
통과한 뒤 새 제안을 만들었다. 결과 화면은
`17-alpha9-suit-packaged-local-ai-result-1280x720.png`에 보존했다.
화면은 `SUIT Variable 2.0.5`를 앱 번들에서 로드했고 document와
`.screen`의 1280×720 overflow는 0이었다.

이 시험은 개발 스크립트를 직접 호출한 것이 아니라 packaged Electron
main→frozen bridge→`resources/command-proposal-runner` 고정 경로를
사용했다. 다만 Python이 아예 없는 클린 PC, DML 사용 가능 PC, 설치·업그레이드·
제거 종단은 아직 검증하지 않았다.
