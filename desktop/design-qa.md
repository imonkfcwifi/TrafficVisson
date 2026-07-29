# [역사 스냅샷] TrafficSuperVision Desktop alpha.6 디자인·동작 QA

> **주의 — 이 문서는 2026-07-28 alpha.6 검증 결과를 보존한 역사 자료이며 현재 구현 기준으로 사용하지 않는다.**  
> **현재 제품·구현 단일 기준:** 루트 [`TRAFFIC_SUPER_VISION_MASTER_PLAN.md`](../TRAFFIC_SUPER_VISION_MASTER_PLAN.md) v2.13
> **현재 디자인·동작 QA:** 루트 [`design-qa.md`](../design-qa.md)  
> **현재 UI 감사 포인터:** 루트 `design-qa.md` §10과 [`qa/visual-reaudit-20260729`](../qa/visual-reaudit-20260729/)는 post-fix Home·Labeling·Report를 P0 0건·P1 0건·P2 0건으로 판정한다. 1280×720 live pixel·정밀 심각도·실제 OCR 상태는 P3 증거 공백이며 제품 전체 완료 판정이 아니다.
> **현재 성능·회귀:** `HistoryScreen` 초기 22.28kB(gzip 7.60kB), 별도 `HistoryMap` 947.96kB(gzip 249.38kB) P3 경계. renderer 150/150, Electron 80/80, Python bridge 614 passed·2 skipped, typecheck/build passed. 아래 alpha.6 본문은 역사 기록으로 보존한다.

검증일: 2026-07-28  
검증 범위: 2026-07-28 당시 소스와 alpha.6 `win-unpacked` Electron의 1280×720 원스크린 UI, 자연어 AI 선라벨 명령 compile/run, SQLite v3 라벨·DatasetVersion, 실제 bridge 모델 관리 화면, 외부 source registry 계약, 자동 회귀·패키징 게이트  
패키징 경계: alpha.6 NSIS와 unpacked 실행 검증까지 완료했으나 서명·클린 PC 설치·업그레이드·제거 검증은 아님

> 이 문서의 `passed`는 2026-07-28 alpha.6 범위의 UI·계약·저장소 회귀가 당시 통과했다는 뜻이다. 현재 소스 상태나 실제 도로 결함 검지 성능, 자동 승인 라벨, GPU 학습, GPS 경로, 한/글 COM 종단 또는 상용 배포 준비 완료를 뜻하지 않는다.

## 결과 요약

| 검증 영역 | 결과 | 확인한 범위 |
|---|---|---|
| 1280×720 제품 셸 | passed | Windows 네이티브 caption과 고정 좌측 내비게이션을 유지하고, 홈·질문 라벨링·모델 관리 핵심 조작을 전체 페이지 스크롤 없이 표시 |
| 자연어 명령 compile | passed | `표지판을 가린 수풀을 찾아줘`를 고정 `sign_vegetation_occlusion` recipe로 해석하고 고정 탐색 기준·기능 요구·경고를 표시 |
| 선라벨 run | passed, 결과 0건 | 현재 분석 범위의 persisted proposal만 필터링했다. 설치 모델이 만든 해당 유형 proposal이 없어 `model_unavailable`, 일치 0, 자동 수락 0, 학습 반영 0으로 종료하고 빈 감사 기록을 저장 |
| 라벨·DatasetVersion | passed | SQLite v3 정규 라벨 통계와 불변 DatasetVersion API 계약, 실제 bridge 기반 ModelScreen을 확인. 현재 답변 0건이라 스냅샷·학습 버튼 비활성 |
| 외부 source registry | passed, 계약 범위 | 고정 allowlist digest·manifest·로컬 artifact inventory·라이선스가 일치한 출처만 `verified_inactive`로 등록. 기본 allowlist·자동 다운로드·자동 활성화 없음 |
| 자동 회귀 | passed | domain 10, renderer 66, Electron 40, Python bridge 340 passed와 2 skipped, Sites worker 4 |
| alpha.6 패키지 | passed, 개발용 | NSIS 생성, release manifest·SHA-256 대조, `win-unpacked` 1280×720 홈·자연어 recipe compile·ModelScreen 실행 확인. `NotSigned`, `development_only_not_distribution_ready` |

어떤 단계도 proposal을 사용자 승인 라벨, annotation, incident, maintenance action 또는 학습 데이터로 자동 승격하지 않았다.

## 실기동 시각 증거

증거 디렉터리: `qa/auto-label-alpha6-20260728`

- `00-reference-vs-home.jpg`: 사용자 참고 이미지와 현재 홈 화면의 합성 비교
- `01-home-1280x720.jpg`: 실제 Electron 홈 화면
- `02-auto-label-compiled-1280x720.jpg`: 자연어 명령을 `sign_vegetation_occlusion` recipe로 컴파일한 상태
- `03-auto-label-run-result-1280x720.jpg`: 설치 모델 proposal 없음, 일치 0건, 빈 실행 감사 저장 결과
- `04-model-registry-1280x720.jpg`: 실제 bridge 통계 0건, DatasetVersion 생성 전, 스냅샷·학습 비활성 상태
- `05-packaged-auto-label-compiled-1280x720.jpg`: alpha.6 `win-unpacked` 앱에서 같은 자연어 명령을 고정 recipe로 컴파일한 상태
- `06-packaged-model-registry-1280x720.jpg`: alpha.6 `win-unpacked` 앱의 실제 bridge 모델 관리 화면

참고 이미지의 타이포그래피 위계를 SUIT Variable로 통일하고, 차분한 청회색 작업면, 얕은 유리 질감, 제한된 청색 강조, 검은 주요 CTA, 둥근 패널 계층을 유지했다. 중첩 웹페이지형 외곽 프레임이나 브라우저 메뉴는 사용하지 않는다. 긴 가변 데이터는 해당 패널 안에서만 제한적으로 스크롤하며 화면 전체 문서 스크롤에 의존하지 않는다.

## 자연어 AI 선라벨 명령

실제 UI와 API는 다음 경계를 지킨다.

1. 사용자 명령을 정규화해 지원 사건 유형 한 개로만 해석한다.
2. 모호하거나 미등록인 명령은 명시적 유형 선택 없이는 거부한다.
3. 원문 명령을 모델 프롬프트에 삽입하지 않는다.
4. 버전 고정 positive/negative prompt와 요구 기능을 가진 recipe ID를 만든다.
5. 실행 직전에 같은 입력으로 recipe를 다시 계산해 변경 여부를 검사한다.
6. 현재 작업 또는 프로젝트의 완료 작업에 이미 저장된 최신 유효 proposal만 검사한다.
7. run 자체는 Grounding DINO·OCR·검지 모델을 새로 실행하거나 파일을 내려받지 않는다.
8. 결과 유무와 관계없이 프로젝트별 불변 JSON 감사 기록을 원자적으로 저장한다.
9. 일치 항목이 있으면 정렬된 첫 항목의 작업·후보·유형·검토 상태·결정 해시만 `first_match`로 반환한다.

`assisted_auto` 요청도 검증 세트 기반 클래스별 임계값이 없는 현재 상태에서는 `proposal_only`로 적용된다. 실행 기록과 match는 `review_only=true`, `include_in_training=false`이고 `training_labels_created=0`이다.

실기동 명령 `표지판을 가린 수풀을 찾아줘`는 고정 `sign_vegetation_occlusion` recipe로 정상 컴파일됐다. 그러나 선택한 영상 범위에는 설치 모델이 만든 해당 유형 persisted proposal이 없었으므로 `first_match=null`이었다. 이는 명령 해석과 감사 저장 경로의 검증이며 식생 가림 검지 성공 사례가 아니다.

## SQLite v3 라벨·DatasetVersion

현재 구현은 JSONL 답변 감사 기록을 원본 권위로 유지하고 다음 SQLite 테이블로 멱등 투영한다.

- `label_questions`
- `label_answers`
- `annotations`
- `dataset_versions`
- `dataset_memberships`

답변 revision chain, 감사 해시, 완료 분석 작업 소유권, 프로젝트 경계를 확인한다. 현재 답변은 승인·음성·보류·제외로 분리되고 승인·음성만 학습 포함 상태가 된다.

DatasetVersion은 각 질문의 최신 답변 revision을 고정한다. 같은 분석 작업의 표본은 `analysis_job_hash_v1` 규칙으로 train과 validation 사이에 분리되지 않는다. 같은 구성의 SHA-256은 기존 버전을 재사용하고 답변 revision이 바뀌면 새 버전을 만든다. SQLite trigger는 저장된 DatasetVersion과 membership의 UPDATE·DELETE를 차단한다.

ModelScreen은 localStorage 검토 이력이 아니라 다음 실제 bridge API를 사용한다.

- `GET /projects/{project_id}/labels/stats`
- `GET /projects/{project_id}/datasets`
- `POST /projects/{project_id}/datasets`

화면은 승인·음성·보류·제외·검증 수치와 최신 DatasetVersion을 표시한다. 원시 프로젝트 UUID를 사용자에게 노출하지 않고 선택 프로젝트만 집계한다는 격리 문구를 사용한다. `current_answer_count > 0`일 때만 사용자의 클릭으로 `userConfirmed=true` 스냅샷 요청을 보낼 수 있다. 현재 실데이터는 답변 0건, DatasetVersion 0건이므로 스냅샷과 학습 버튼이 비활성이다.

GPU 학습 실행, 모델 성능 비교, 후보 모델 승격·거부·롤백은 아직 연결되지 않았다.

## 외부 source registry

`source_registry`는 로컬 모델팩과 공개 라벨 데이터셋의 출처 계약을 검증하는 순수 모듈이다. 다음 조건이 모두 맞아야 출처를 `verified_inactive`로 등록한다.

- source manifest의 정규 SHA-256이 별도로 전달된 고정 allowlist에 정확히 포함됨
- allowlist 자체가 호출자가 지정한 digest와 일치함
- 관측된 로컬 파일의 상대 경로·크기·SHA-256이 manifest inventory와 정확히 일치함
- 모델 runtime·weight 또는 데이터 이미지·annotation에 필요한 라이선스 범위와 권리가 명시됨
- 고정 revision, HTTPS 메타데이터 URL, 허용된 artifact 종류와 경로를 사용함

이 모듈은 경로를 직접 읽지 않고 네트워크에 접속하지 않으며 다운로드·압축 해제·모델 로드·라벨 import를 수행하지 않는다. 제품에 기본 allowlist도 없다. 등록 상태의 안전 플래그는 자동 다운로드·자동 활성화·remote code·프로젝트 학습 포함·외부 라벨의 사용자 승인 전환을 모두 금지한다.

이는 외부 소스를 안전하게 즉시 사용할 수 있다는 뜻이 아니라, 향후 명시적 프로젝트 import 전에 검증 가능한 비활성 등록 계약이 마련됐다는 뜻이다.

## 화면·상호작용 QA

- 고정 좌측 내비게이션과 Windows caption 버튼이 같은 제품 창 안에 유지됨
- 홈 핵심 상태와 다음 행동이 1280×720 한 화면에 표시됨
- 질문 라벨링의 AI 선라벨 명령 영역이 기존 질문 카드 위에 압축 배치됨
- 명령 입력, 유형, 범위, 처리 방식, `명령 해석`, `실행 확인`이 실제 조작 가능함
- recipe 유형·탐색 기준·필요 기능·안전 경고를 같은 패널에서 확인 가능함
- run 결과가 성공 건수처럼 과장되지 않고 `AI 모델을 사용할 수 없음`, 자동 수락 0, 학습 반영 0으로 표시됨
- ModelScreen의 라벨 통계·DatasetVersion·학습 게이트가 한 화면에 표시됨
- 답변이 없을 때 스냅샷과 학습 CTA가 비활성이고 이유를 설명함
- 긴 콘텐츠는 소유 패널 내부에서만 bounded scroll을 사용함
- renderer에서 직접 파일·쉘·COM 권한을 사용하지 않음

## 자동 회귀 결과

| 테스트 묶음 | 결과 |
|---|---:|
| Domain | 10 passed |
| Renderer state | 66 passed |
| Electron | 40 passed |
| Python bridge | 340 passed, 2 skipped |
| Sites worker | 4 passed |

새 회귀에는 다음 계약이 포함된다.

- 자연어 명령의 결정적 단일 유형 매핑과 고정 prompt recipe
- 변경된 recipe·다른 프로젝트·모호한 명령 거부
- persisted proposal의 provenance·evidence 검증
- 검증 임계값이 없는 자동 수락 차단과 proposal-only 하향
- 결과 0건을 포함한 run 감사 저장·해시·프로젝트 격리
- `first_match` 응답 계약
- SQLite v2에서 v3으로의 무손실 migration
- JSONL 답변의 멱등 projection과 revision chain
- 승인·음성·보류·제외 partition
- DatasetVersion 해시 재사용, 새 revision 버전 생성, 작업 단위 split
- DatasetVersion·membership 불변 trigger와 프로젝트 간 혼합 차단
- ModelScreen의 실제 bridge stats/list/create와 사용자 확인 플래그
- source manifest·pinned allowlist·artifact inventory·라이선스·비활성 안전 플래그

## 실제 데이터 경계

기존 읽기 전용 실제 데이터 QA에서 확인한 범위는 완료 영상 51개, 장면·선명도 후보 349건, JPEG/H.264 근거 698개, 최신 유효 review-only sidecar 제안 43건이다. 기존 추출은 영상당 최대 8프레임의 희소 표본이므로 dense 결함 recall 결과가 아니다.

현재 사용자 답변, 확정 annotation, 확정 incident, maintenance action, DatasetVersion, 자동 승인 라벨, 모델 승격은 모두 0건이다. alpha.6 실기동은 기존 영상을 새로 dense 분석하거나 정답 라벨을 만들지 않았다.

## 미완료·차단 조건

- 자연어 run에서 동적 Grounding DINO 또는 다른 검지 모델 실행
- 검증된 한국 도로 dense 정밀도·재현율
- GPU 모델 학습, 고정 검증 성능 비교, 승격·롤백
- GPX 또는 시간대별 GPS 경로 동기화·진행선 재생
- 이정표·터널명 OCR의 GPS·도로·방향 검증
- point-prompt 자동 분할
- 비식별화 공유 이미지·증거 미디어 패키지
- HWP/HWPX COM 실문서 열기·부분 편집·저장·재열기·전 페이지 렌더 검증
- 클린 PC 설치·업그레이드·제거
- Authenticode, 라이선스 고지, SBOM, 상용 배포 승인

alpha.6 당시 개발 PC의 한/글 COM 사전 점검은 `HWPFrame.HwpObject` 미등록 상태였다. 현재 PC에는 COM이 등록되어 있지만, 어느 경우에도 registry-only preflight를 실문서 HWP/HWPX 종단 통과로 해석하지 않는다.

## 패키지 경계

이번 alpha.6 QA는 2026-07-28 당시 소스, 새 NSIS 생성, 당시 release manifest·checksum, frozen bridge, 실제 `win-unpacked` Electron 실행까지 검증했다. 자연어 명령 compile과 ModelScreen을 패키지 앱에서 1280×720으로 다시 확인했으며 확정 라벨이나 DatasetVersion은 만들지 않았다.

- 설치본: `desktop/release/TrafficSuperVision-Setup-0.1.0-alpha.6-x64.exe`
- 크기: `329,614,000 bytes`
- SHA-256: `4af550503b430755865451ccf974d73a091d3823b8fcb3d2f5d82a8e4af4f3c2`
- 당시 검증 근거: 이 문서에 보존한 설치본명·크기·SHA-256과 날짜가 고정된 `qa/auto-label-alpha6-20260728` 실기동 캡처
- 증거 포인터 경계: 현재 `desktop/release/release-manifest.json`과 `SHA256SUMS.txt`는 최신 릴리스가 덮어쓰는 가변 파일이므로 alpha.6 증거로 인용하지 않는다
- 상태: `NotSigned`, `development_only_not_distribution_ready`

당시 release manifest는 installer·blockmap·unpacked 실행 파일·frozen bridge·FFmpeg 도구뿐 아니라 standalone renderer `win-unpacked/resources/app.asar`도 직접 해시했다. 당시 `SHA256SUMS.txt`는 이 7개 artifact와 manifest 자체를 합친 8개 항목을 포함했다.

서명·타임스탬프, 클린 Windows 설치·업그레이드·롤백·제거, FFmpeg 재배포 검토, EULA·Third-Party Notices·SBOM, 검증된 도로 결함/OCR 모델팩, 한컴오피스 클린 PC 검증은 여전히 배포 차단 조건이다.

final result: passed (scoped alpha.6 source, package build, and unpacked runtime regression)
