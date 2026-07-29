# Grounding DINO 명령형 런너 보안·가져오기 계약

> 검토일: 2026-07-29  
> 범위: Windows 설치본이 자연어 명령을 로컬 Grounding DINO 검지와
> 질문형 라벨링 후보로 연결하는 경계  
> 현재 상태: alpha.9 소스 구현·실행기 무결성 검증·CPU·NSIS·unpacked
> 패키지 종단 통과, 클린 PC·Authenticode는 대기  
> 결론: 고정 해시의 번들 ONNX 실행기가 실제 프레임을 평가하지만, 결과는
> 자동 라벨이 아니라 **검토 전용 제안**으로만 수용한다.

## 현재 확인된 사실

- alpha.8의 `evaluate_grounding_dino_candidates.py` 평가는 프로젝트·완료
  작업·후보 이미지·모델 팩을 검증한 별도 운영자용 GPU 역사 경로다.
  후보 7개를 RTX A2000에서 17.859초에 처리해 3개 박스를 냈지만 육안상
  3개 모두 해당 명령의 오탐이었다. alpha.8 설치본에는 ML 런타임과
  가중치가 없었다.
- alpha.9는 이 외부 Python 경로 대신 PyInstaller one-file
  `trafficvisson-command-proposal-runner.exe`에 ONNX Runtime, tokenizer,
  NumPy와 Pillow를 동봉한다. 사용자 Python·`PATH`·Hugging Face 캐시를
  찾지 않는다.
- 모델은 `onnx-community/grounding-dino-tiny-ONNX` revision
  `11778337bb87ff84af72a64928c86616d86c495e`의
  `onnx/model_q4.onnx`만 사용한다. 파일 크기는 227,229,745 bytes,
  SHA-256은
  `2ed98a91b75f00d325c7581bee83b92b5b98460981772edf5ed844370ac25c11`,
  라이선스는 Apache-2.0이다.
- runner manifest SHA-256
  `65f6cc1f6941b25936c10b8387a7867d6249e25b5453a1d2cb8567e28ba547f3`
  를 bridge 빌드와 release 검증 스크립트에 고정했다. manifest는 실행
  파일 SHA-256과 model manifest SHA-256을 선언하고, model manifest는
  Q4 ONNX·tokenizer·설정·라이선스·Third-Party Notices의 크기와 해시를
  전부 고정한다.
- alpha.9 소스 앱 종단은 현재 개발 PC의 `CPUExecutionProvider` fallback으로
  후보 프레임 7개를 약 23.844초에 처리해 review-only 질문 제안 7건을
  만들었다. 자동 라벨·Incident·학습 데이터·모델 승격은 모두 0건이다.
- 실행기는 `DmlExecutionProvider`가 실제 런타임에 있으면 이를 우선하고
  CPU를 fallback으로 사용한다. 현재 결과는 DML 성능 증거가 아니다.

## 신뢰 경계

renderer는 다음 값만 보낼 수 있다.

- 프로젝트 ID
- 완료 분석 작업 ID
- 자연어 명령
- 사용자의 명시적 실행 동의

renderer는 실행 파일, Python 경로, 모델 경로, 출력 경로, 인자 배열,
timeout, 환경 변수, 임계값을 선택할 수 없어야 한다.

bridge가 자연어를 허용된 한 가지 도로 문제 유형과 고정 프롬프트로
컴파일한다. 외부 런너에는 자연어 원문 대신 다음 구조화 값만 전달한다.

- `recipe_id`
- `command_sha256`
- `prompt_sha256`
- `issue_type`
- 허용 목록에서 가져온 고정 positive/negative prompt
- 현재 완료 작업의 후보 ID와 bridge가 해석한 JPEG 절대 경로·크기·SHA-256

## 고정 런너 설치 계약

패키지 Electron은 다음 고정 위치를 bridge에 전달한다.

`<Electron resources>\command-proposal-runner`

개발 Electron은 저장소 안의
`desktop\build\command-proposal-runner`만 사용한다. Electron이 명시한
경로가 없는 bridge 단독 실행에는
`%PROGRAMDATA%\TrafficVisson\CommandProposalRunner\grounding-dino-v1`
fallback이 있지만, 동일한 bridge 내장 manifest SHA-256을 통과해야 한다.
renderer나 사용자가 이 경로를 선택할 수 없다.

필수 파일은 다음과 같다.

- `runner-manifest.json`
- `trafficvisson-command-proposal-runner.exe`
- `model-pack\manifest.json`
- `model-pack` manifest에 열거된 모델 파일

현재 `runner-manifest.json`은 다음을 고정한다.

- schema, runner ID/version, protocol version
- 실행 파일 크기와 SHA-256
- 지원하는 모델 팩 ID `trafficvisson-onnx-grounding-dino-tiny`,
  version `0.2.0-review`, manifest SHA-256
- `transformers_grounding_dino` provider
- 평가 ID와 producer/version

실행기·bridge 코드의 고정 계약은 다음을 추가로 강제한다.

- 허용 issue type 11종
- 최대 후보 512개, 프레임당 박스 32개
- 요청 2 MiB, 응답 8 MiB, stderr 1 MiB, 후보 이미지 24 MiB
- bridge wall time 540초
- 입력 크기 800×800, box/text threshold 0.25

`model-pack\manifest.json`은 다음 승인 경계를 고정한다.

- `review_only=true`
- `automatic_labeling_allowed=false`
- `incident_creation_allowed=false`
- `model_promotion_allowed=false`
- `runner_sidecar_write_allowed=false`
- `bridge_review_proposal_import_allowed=true`
- SPDX 라이선스와 SBOM/고지 파일 해시

manifest가 자기 실행 파일의 해시를 선언하는 것만으로는 신뢰할 수 없다.
alpha.9는 허용 runner manifest SHA-256을 frozen bridge 소스에 내장하고,
패키징 전 `verify:command-runner`와 패키징 후 release manifest schema v2가
같은 체인을 다시 확인한다. Authenticode 서명과 설치 디렉터리 ACL의 클린
PC 검증은 아직 별도 배포 게이트다. 사용자 Python, `PATH`에서 찾은 실행
파일, 임의 개발 워크스페이스 경로는 패키지 제품 런너로 인정하지 않는다.

## 프로세스 실행 계약

- 절대 경로 실행 파일 하나만 `shell=False`로 실행한다.
- 명령 원문과 모델/실행 경로를 command line 인자로 넘기지 않는다.
- bounded JSON을 stdin으로 전달하고 bounded JSON 하나만 stdout으로 받는다.
- 환경 변수는 Windows 필수 값과 오프라인 플래그만 허용한다.
- `PYTHONPATH`, 사용자 site-packages, proxy, Hugging Face token은 전달하지
  않는다.
- Windows Job Object로 자식 전체에 kill-on-close, wall time, 메모리 한도를
  적용한다.
- stdout/stderr를 무제한 `PIPE`에 모으지 않는다. 임시 파일 또는 제한된
  수집기로 상한을 강제한다.
- cwd는 검증된 런너 디렉터리로 고정한다.
- 현재 bridge는 프로세스 시작 직전에 runner manifest, 실행 파일,
  model manifest와 모든 모델 artifact의 크기·SHA-256을 확인하고,
  실행기 자체가 모델 로드 직전에 model artifact inventory를 다시
  확인한다. 프로세스 종료 뒤 bridge의 재해시는 추가 방어 목표로 남긴다.
- 네트워크 비사용 환경 변수는 보조 통제다. 배포 런너 자체가 핀된
  실행 파일이고 모델 로딩이 오프라인임을 테스트해야 한다.

## 응답 검증과 검토 큐 가져오기

응답은 중복 JSON key와 NaN/Infinity를 거부하고 정확한 필드만 허용한다.
다음 값은 요청 및 현재 bridge 데이터와 모두 일치해야 한다.

- project ID, analysis job ID, recipe ID
- runner manifest SHA-256, model manifest SHA-256
- 완료 작업 record SHA-256
- 후보 ID 집합
- 대표 JPEG SHA-256
- issue type
- `model_score_not_probability`

각 박스는 유한 숫자이며 `x`, `y`, `width`, `height`가 0~1 이미지 안에
완전히 포함되어야 한다. 프레임당 32개를 넘길 수 없고 점수 내림차순이어야
한다.

런너는 라벨 DB, Incident, 보고서, 모델 버전, sidecar에 직접 쓸 수 없다.
검증을 통과한 `classified` 결과만 bridge가
`ExperimentalProposalStore`와 동등한 결정적 저장기를 통해
`review_only=true`, `automatic_labeling_allowed=false`로 기록한다.

`no_suggestion`을 현재의 후보별 최신 sidecar 저장소에 기록하면 이전
모델이나 다른 명령의 유효 제안을 가릴 수 있다. 따라서 다음 중 하나가
필수다.

- `no_suggestion`은 sidecar에 기록하지 않고 실행 audit에만 남긴다.
- 또는 recipe/evaluation별 별도 command-run 저장소를 만들고 조회 시
  해당 recipe로 명시적으로 필터링한다.

Grounding DINO provenance provider는 `onnx_yolo_v8`로 위장하지 않고
호환 식별자 `transformers_grounding_dino`로 별도 기록한다. 실제 alpha.9
실행 엔진은 PyTorch·Transformers가 아니라 ONNX Runtime이며 runner ID
`trafficvisson.command-proposal.onnx-grounding-dino`와 모델 pack ID로
이를 구분한다.

## 모델 팩 버전 경계

alpha.8의 Transformers Grounding DINO `0.1.0` 팩과 평가 산출물은
sidecar 생성을 금지한 역사 증거로 유지하며 기존 manifest를 덮어쓰지
않는다.

alpha.9는 별도 ONNX 팩 `0.2.0-review`를 만들고 다음 의미를 분리했다.

- 런너 자신의 sidecar 쓰기: 금지
- 검증된 bridge의 검토 큐 가져오기: 허용
- 자동 라벨·Incident·보고서 확정·모델 승격: 금지

기존 GPU 평가의 3개 오탐은 자동 승인 대상이 아니며 사용자 답변이 있기
전까지 학습 음성 라벨도 아니다. alpha.9의 7개 소스 실행 제안 역시
review-only 후보일 뿐 정답 검증 세트로 간주하지 않는다.

## P0 통과 테스트

1. runner manifest가 없거나 해시/서명이 허용 목록과 다르면
   `model_unavailable`이며 프로세스를 시작하지 않는다.
2. 사용자 쓰기 가능한 가짜 ProgramData 런너와 자기 일치 manifest가 있어도
   실행되지 않는다.
3. 실행 파일·model manifest·가중치·tokenizer 중 한 바이트를 바꾸면
   실행 전 실패한다.
4. symlink, junction, reparse point, hardlink와 중첩 디렉터리 탈출을
   모두 거부한다.
5. renderer가 path, executable, args, timeout, threshold를 IPC payload에
   넣으면 계약 검증에서 거부한다.
6. raw 자연어는 프로세스 command line, manifest, 결과 파일, 로그에 남지
   않고 SHA-256만 남는다.
7. 응답의 중복 key, NaN, bool 점수, 범위 밖 박스, 중복/누락 후보,
   다른 project/job/recipe/issue type을 모두 거부한다.
8. timeout 또는 crash 시 자식 프로세스가 남지 않고 라벨·Incident·sidecar
   수가 0이다.
9. 성공 시에도 자동 라벨, Incident, 모델 승격, 보고서 확정 입력은 0이며
   검토 카드만 생성된다.
10. `no_suggestion` 실행 뒤 기존 다른 모델/recipe의 제안이 사라지지 않는다.
11. 동일 recipe 재실행은 idempotent하고 다른 recipe 결과와 충돌하지 않는다.
12. 고정 3개 오탐은 자동 승인되지 않고 사용자의 `아니오` 답변 뒤에만
    어려운 음성 라벨로 전환된다.
13. alpha.9 clean PC 설치본은 외부 Python·ML 설치 없이 동봉 ONNX
    실행기와 모델팩을 찾고, 누락 시 기능을 가장하지 않고 무결성 실패를
    설명한다.
14. alpha.9 unpacked·설치본에서는 앱 명령 한 번으로 CPU 또는 DML 실행,
    검증된 후보 카드 표시까지 종단 검증한다. 사용자가 답변하지 않은
    상태의 라벨·Incident·학습 데이터는 0이어야 한다.
15. release manifest schema v2가 runner EXE, runner manifest, model
    manifest, Q4 ONNX를 포함한 11개 배포 artifact와 manifest 자체를
    포함한 checksum 12개를 검증한다.

## 출하 판단

- 개발 PC의 기존 Python과 평가 스크립트를 직접 호출하는 방식: 출하 불가
- 기존 alpha.8 `0.1.0` 평가 결과를 검토 큐 sidecar로 가져오는 방식:
  출하 불가
- bridge-pinned manifest를 가진 alpha.9 frozen ONNX 런너와
  `0.2.0-review` 모델 팩: 소스 구현·CPU 종단 통과
- alpha.9 NSIS·unpacked 무결성·신규 review-only 제안 7건: 통과
- 클린 PC 실행·DML 환경·Authenticode: 배포 검증 대기
- 현재 오탐 성능에서 자동 승인·자동 라벨: 불가
