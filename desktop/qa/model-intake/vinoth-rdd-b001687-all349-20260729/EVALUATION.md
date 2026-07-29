# TrafficVisson 기존 후보 로컬 모델 비파괴 평가

생성 시각(UTC): `2026-07-28T19:58:18.006871Z`

## 해석 제한

이 결과는 사용자 검토를 위한 실험적 후보입니다. ONNX detector의 model_score 값은 현재 TrafficVisson 한국 도로 고정 검증 세트에서 보정되지 않은 모델 출력 점수이며, 실제 결함 확률·정확도·확정 판정이 아닙니다. 표시 박스도 사용자가 확인하기 전에는 정답 영역이 아닙니다. 자동 라벨, 유지보수 사건 확정, 보고서 확정 입력, 모델 승격에 사용하면 안 됩니다.

- 사용자 확정 라벨: `0건`
- 자동 생성 라벨: `0건`
- 자동 생성 유지보수 사건: `0건`
- 모델 자동 승격: `아니오`
- 검토 전용 sidecar 기록: `요청하지 않음`
- 정밀도·재현율: 사용자 확정 정답이 없으므로 산출하지 않음

## 실행 범위

- 무결성 검증 완료 분석 레코드: `51건`
- 평가한 대표 JPEG: `349건`
- 검토 제안이 생성된 JPEG: `10건`
- 내보낸 모델 제안 점수 행: `11건`
- 워커가 보고한 후보별 추론 시간 합계: `50923ms`
- 건너뛴 레코드: `0건`
- 제한 때문에 건너뛴 후보: `0건`

## 모델

- Pack: `vinothvikas1987-rdd-yolov8s-eval` `b001687.review-only`
- Engine: `onnx_yolo_v8`
- Manifest SHA-256: `0cdba1051a59836170786d42c78b02f78bf480a23bf9aeb91ca5712e4e37a0b7`
- Calibration: `model_score`

## 추론 상태

| 상태 | 후보 수 |
| --- | ---: |
| `classified` | 10 |
| `no_detection` | 339 |

## 최상위 검토 제안 분포

| 유형 | 후보 수 |
| --- | ---: |
| `pothole` | 2 |
| `road_surface_damage` | 8 |

이 분포는 모델 성능 지표가 아니다. 동일 영상에서 연속 추출된 장면이 포함될 수 있고, 정답 라벨이 없는 검토 대기열의 구성만 보여준다.

## 산출물

- `predictions.json`: 출처 해시와 전체 실험 제안
- `candidate_scores.csv`: Excel 호환 UTF-8 CSV
- `sidecar_job_payloads.json`: `ExperimentalProposalStore.record_run`에 전달 가능한 job별 검토 전용 payload
- `manifest.json`: 산출물 SHA-256
- `review-contact-sheet-*.jpg`: 상위 상대점수 순 검토 연락시트 (10건)

## 다음 단계

이 CSV와 연락시트는 질문형 라벨링 대기열을 구성하는 참고자료다. 사용자가 각 이미지를 `예`, `아니오`, `판단 어려움`, `영역 수정` 중 하나로 확인하기 전에는 학습 데이터나 보고서 근거로 사용할 수 없다.
