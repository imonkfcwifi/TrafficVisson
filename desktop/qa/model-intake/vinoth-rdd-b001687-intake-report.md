# Vinoth RDD YOLOv8 b001687 도입 심사 보고서

작성일: 2026-07-29  
대상: `vinothvikas1987/pothole-detection-yolov8`  
고정 리비전: `b001687443175e43442f63bef4691c3546629def`

## 최종 판정

| 게이트 | 판정 | 이유 |
| --- | --- | --- |
| 로컬 연구용 검토 자료 보존 | 허용 | 원본 349장을 읽기 전용으로 평가했고 라벨·사건·DB를 수정하지 않음 |
| TrafficVisson 활성 모델 승격 | **거부** | 확정 양성 0, 판단 어려움 1, 오탐 9프레임 |
| 자동 라벨 생성 | **금지** | 한국 도로 고정 정답 세트에서 검증되지 않았고 명백한 오탐이 다수임 |
| 설치 프로그램 번들 | **금지** | 학습 데이터와 YOLOv8 기반 가중치 권리가 확인되지 않음 |
| 외부 재배포 | **금지** | RDD 라이선스 출처가 충돌하고 게시자의 재라이선스 권한이 입증되지 않음 |
| 상업 사용 확정 | **아니오** | 현재 manifest가 `commercial_use_confirmed=false`로 차단함 |

이 모델은 **평가 전용 intake 자료**로만 남긴다. 품질 게이트와 권리 게이트가 모두 닫힐 때까지 제품 코드, 자동 라벨, 보고서 확정 입력, 설치 파일에 연결하지 않는다.

## 고정한 파일과 provenance

- Hugging Face 저장소: [vinothvikas1987/pothole-detection-yolov8](https://huggingface.co/vinothvikas1987/pothole-detection-yolov8)
- 고정 커밋: [b001687 — training 폴더 삭제](https://huggingface.co/vinothvikas1987/pothole-detection-yolov8/commit/b001687443175e43442f63bef4691c3546629def)
- `best.onnx`
  - 크기: `44,752,195 bytes`
  - SHA-256: `590a20e8c4a7bcbdb32e8a3b7b3a3c1d57d1fa8a7dde5c83fcec8928a4aa753f`
- 평가 당시 manifest SHA-256:
  - `0cdba1051a59836170786d42c78b02f78bf480a23bf9aeb91ca5712e4e37a0b7`
  - 당시에는 모델 카드의 Apache-2.0 선언을 근거로 `commercial_use_confirmed=true`였음
- 현재 강화 manifest SHA-256:
  - `90187fde6182b2bdb791786d0e479c73da2adeb1746b0b7f5251093a02b61dfd`
  - `commercial_use_confirmed=false`
  - `redistribution_allowed=false`

가중치와 추론 설정은 바뀌지 않았다. 그러나 라이선스 상태가 강화됐으므로 기존 결과의 provenance는 `legacy_pre_hardening_manifest`로 명시했다. 현재 manifest를 사용한 새 추론은 로컬 엔진에서 `The configured model pack is not enabled`로 차단되는 것을 확인했다.

관련 파일:

- [`trafficvisson-model-pack.json`](vinothvikas1987-pothole-b001687/trafficvisson-model-pack.json)
- [`wrapper-evaluator.stderr.log`](vinoth-rdd-b001687-current-manifest-block-proof-20260729/wrapper-evaluator.stderr.log)

## 349장 읽기 전용 평가

### 실행 범위

| 항목 | 결과 |
| --- | ---: |
| 완료된 사용자 bridge 분석 레코드 | 51건 |
| 대표 JPEG | 349장 |
| 건너뛴 레코드 / JPEG | 0 / 0 |
| 보호한 원본 파일 | 453개 |
| 검지가 나온 프레임 | 10장 |
| 전체 제안 박스 | 11개 |
| 검지 없음 | 339장 |
| 워커 추론 시간 합계 | 50,923ms |
| 전체 실행 시간 | 55.672초 |
| 자동 라벨 | 0건 |
| 유지보수 사건 | 0건 |
| 모델 승격 | 없음 |
| sidecar 기록 | 없음 |

평가기에서 보호 대상으로 지정한 분석 JSON, 기존 라벨, SQLite DB, 후보 JPEG를 실행 전후 다시 해시했고 모두 일치했다. 출력은 별도 `desktop/qa/model-intake` 디렉터리에만 생성했다.

### 사람 시각 판정

| 판정 | 프레임 | 박스 |
| --- | ---: | ---: |
| 확정 양성 | 0 | 0 |
| 판단 어려움 / 노면 손상 가능 | 1 | 1 |
| 오탐 | 9 | 10 |

최고 점수 `0.715679` 박스에는 균열 또는 보수 패치 경계로 보이는 흔적이 있으나, 단일 프레임만으로 `거북등 균열`을 확정할 수 없어 `판단 어려움`으로 두었다. 나머지는 터널 천장 조명, 라바콘, 일반 차선, 색깔유도선, 차량 그림자, 영상 가장자리를 균열 또는 포트홀로 잘못 잡았다.

자세한 프레임별 판정:

- [`VISUAL_REVIEW.md`](vinoth-rdd-b001687-all349-20260729/VISUAL_REVIEW.md)
- [`manual-visual-review.csv`](vinoth-rdd-b001687-all349-20260729/manual-visual-review.csv)
- [`all-boxes-contact-sheet-001.jpg`](vinoth-rdd-b001687-all349-20260729/all-boxes-contact-sheet-001.jpg)
- [`intake-run-summary.json`](vinoth-rdd-b001687-all349-20260729/intake-run-summary.json)
- [`intake-review-manifest.json`](vinoth-rdd-b001687-all349-20260729/intake-review-manifest.json)

### 성능 수치로 해석하면 안 되는 이유

이 349장은 사건 유형별 정답이 고정된 검증 세트가 아니다. 검지가 없었던 339장에 실제 손상이 없는지 전수 라벨링하지도 않았다. 따라서 다음 수치는 계산하지 않았다.

- precision
- recall
- F1
- false-positive rate
- 사건 단위 중복률

`10프레임 중 9프레임 오탐`은 이번 제안 대기열의 사람 판정일 뿐 일반화된 오탐률이 아니다. 반대로 339장의 `no_detection`도 정상 판정이나 true negative를 뜻하지 않는다.

`0.4` 임계값은 이번 표본에서 명백한 오탐을 통과시켰다. 그러나 이 미라벨 표본만 보고 임계값을 높이면 평가 자료에 과적합하게 된다. 그래서 threshold, NMS, class mapping은 변경하지 않았다.

## 라이선스와 권리 확인

이 절은 기술적 배포 게이트를 정하기 위한 보수적 검토이며 법률 자문이 아니다.

### 1. 모델 카드의 Apache-2.0만으로는 부족함

고정 모델 카드는 `license: apache-2.0`과 본문 `Apache 2.0`을 선언한다. 동시에 다음 provenance 공백이 있다.

- `datasets: []`로 정확한 데이터셋 식별자가 비어 있음
- 학습 데이터는 `RDD — 26,869 images, Japan/India/Czech Republic`으로만 기재
- RDD2020인지 RDD2022인지, DOI와 국가별 파일 버전을 명시하지 않음
- `YOLOv8s (COCO pretrained)` 기반이라고 기재
- 카드가 링크한 `training/train_improved.py`는 고정 리비전 `b001687`에서 삭제됨

게시자의 Apache-2.0 표시는 게시자가 보유한 권리만 허가할 수 있다. 학습 이미지나 기반 가중치에 대한 권리까지 자동으로 증명하지 않는다.

### 2. RDD 상류 표기가 충돌함

- RoadDamageDetector 저장소의 고정 커밋 `e3be594...` README는 이미지가 [CC BY-SA 4.0](https://github.com/sekilab/RoadDamageDetector/blob/e3be59406501ec95c2c2940a72bc2799bbede035/README.md#license)이라고 기재한다.
- 공식 [RDD2022 Figshare v1](https://figshare.com/articles/dataset/RDD2022_-_The_multi-national_Road_Damage_Dataset_released_through_CRDDC_2022/21431547)은 DOI `10.6084/m9.figshare.21431547.v1`, 라이선스 `CC BY 4.0`으로 표시한다.
- 저장소 코드의 MIT 라이선스와 데이터 이미지의 Creative Commons 라이선스는 별개다.

두 표기가 정확히 같은 이미지 릴리스에 적용되는지, 모델 작성자가 어느 파일을 내려받았는지 모델 카드만으로 알 수 없다. 따라서 현재 단계에서 BY 또는 BY-SA 중 하나를 임의로 선택할 수 없다.

만약 정확한 학습 이미지가 BY-SA이고 공개된 가중치가 적용 법률상 Adapted Material로 판단된다면 attribution과 동일·호환 라이선스 의무가 문제 될 수 있다. [Creative Commons 호환 라이선스 목록](https://creativecommons.org/compatible-licenses/)에는 Apache-2.0이 BY-SA 4.0 호환 라이선스로 올라가 있지 않다.

다만 학습 가중치가 곧바로 데이터 이미지의 파생물이라는 결론도 확정 사실은 아니다. [Creative Commons의 AI 학습 지침](https://creativecommons.org/wp-content/uploads/2025/05/Using-CC-licensed-Works-for-AI-Training.pdf)도 관할과 사실관계에 따라 판단이 달라진다고 설명하며, SA 모델 공개 시 같은 라이선스를 적용하는 접근을 보수적 과잉준수 가능성이 있는 지침으로 제시한다.

### 3. YOLOv8 기반 가중치 권리도 미확인

모델 카드는 COCO pretrained `YOLOv8s`를 기반으로 했다고 한다. Ultralytics 공식 YOLOv8 문서는 모델을 [AGPL-3.0 또는 Enterprise 라이선스](https://github.com/ultralytics/ultralytics/blob/main/docs/en/models/yolov8.md#citations-and-acknowledgments)로 제공한다고 기재한다.

모델 게시자가 다음 중 무엇을 보유했는지 확인되지 않았다.

- 해당 시점의 AGPL-3.0 의무를 충족한 재배포
- Ultralytics Enterprise 권리
- Apache-2.0으로 가중치를 다시 허가할 수 있는 별도 근거

따라서 ONNX 파일을 Electron 설치 프로그램에 넣거나 고객에게 배포하면 안 된다.

## 현재 적용된 안전 장치

- pack version: `b001687.review-only`
- `commercial_use_confirmed=false`
- `redistribution_allowed=false`
- 현재 manifest로 새 inference 시작 불가
- 프로덕션 모델 디렉터리로 복사하지 않음
- Electron 앱의 활성 모델 목록에 등록하지 않음
- 자동 질문 라벨·사건·보고서 payload를 만들지 않음
- 사용자 원본 영상·후보 JPEG·분석 레코드를 수정하지 않음

현재 차단은 오류가 아니라 의도한 안전 상태다. 권리 검증 없이 이 값을 `true`로 되돌리거나 임시 manifest로 우회하면 안 된다.

## 재현과 감사

감사 스크립트:

- [`evaluate_vinoth_rdd_b001687.py`](evaluate_vinoth_rdd_b001687.py)

기존 349장 결과와 원본 해시를 다시 검증하고 모든 박스를 그리는 명령:

```powershell
python desktop\qa\model-intake\evaluate_vinoth_rdd_b001687.py `
  --review-existing desktop\qa\model-intake\vinoth-rdd-b001687-all349-20260729
```

이 명령은 다음을 검증한다.

1. Hugging Face 고정 리비전
2. ONNX 크기와 SHA-256
3. 현재 강화 manifest SHA-256
4. 51개 레코드, 349개 후보, 453개 보호 파일
5. 원본 평가 결과의 side effect 0
6. 원본 후보 JPEG 해시
7. 10프레임 11박스에 대한 수동 판정 coverage

스크립트의 `--output-directory` 모드는 현재 `commercial_use_confirmed=false`를 발견하면 새 추론 전에 실패한다. provenance·base-weight 권리·법률 검토가 완료되어 manifest와 스크립트의 고정 해시를 의도적으로 갱신하기 전에는 재실행하지 않는다.

## 활성화 전 필수 게이트

다음 조건을 모두 충족해야 다시 도입 심사를 시작할 수 있다.

1. 모델 작성자로부터 정확한 RDD 릴리스, 국가별 파일, 다운로드 출처, 학습 스크립트와 데이터 라이선스를 받음
2. RDD 권리자 또는 배포자가 CC BY와 CC BY-SA 표기 차이를 서면으로 설명함
3. YOLOv8s base-weight 사용·재배포 권리를 확인함
4. 법률 검토를 거쳐 NOTICE, attribution, 변경 사실, 재배포 라이선스를 확정함
5. 한국 도로 고정 검증 세트를 프로젝트 데이터와 완전히 분리해 구축함
6. 계획서 최소 기준인 대상 유형별 검증 표본 30건 이상을 확보하고, 실제 배포 판정에는 통계적으로 충분한 표본 수를 별도로 산정함
7. 유형별 사건 recall, precision, 중복 사건, 차선·조명·라바콘·영상 가장자리 hard negative를 평가함
8. 기존 프로젝트 모델 대비 유형별 recall 2%p 이상 하락이나 오탐 증가가 없는지 확인함
9. 사용자 승인 없이 자동 라벨이나 재학습이 시작되지 않는 회귀 테스트를 통과함
10. 후보 모델 상태에서만 제한 운영한 뒤 사람 검토를 거쳐 명시적으로 승격함

현재는 1–8번이 충족되지 않았다. 따라서 **도입 거부**가 최종 결론이다.
