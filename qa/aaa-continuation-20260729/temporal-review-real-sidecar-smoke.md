# 실제 review-only 제안 시간축 병합 스모크

검증 시각: 2026-07-29 KST

## 입력

- 프로젝트: `11111111-1111-4111-8111-111111111111`
- 완료 분석 작업: `1ca29d0d-7dd7-4024-9f98-6d0ffa1d6984`
- 실제 저장 제안 실행: `a35b81bb-58e0-445b-9351-960a7c27bd62`
- 분석 후보 프레임: 7개
- Grounding DINO frame detection: 12개
- 제안 유형: `sign_vegetation_occlusion`

## 결과

- `group_temporal_incident_proposals`가 12개 frame detection을 11개
  `awaiting_user_review` 사건 후보로 계산했다.
- 같은 프레임에서 공간적으로 겹친 2개 박스가 하나의 검토 후보로
  병합됐다.
- 모든 결과가 `review_only=true`이고
  `automatic_incident_creation_allowed=false`를 유지했다.
- 입력 분석 기록, 질문 답변, Incident, Annotation, DatasetVersion,
  ModelVersion은 읽거나 변경하지 않았다.

## 판정

시간·공간 병합기가 실제 저장된 모델 제안 형식을 처리하고 중복 박스
하나를 줄이는 계산 경로는 동작한다. 다만 이 입력은 약 11.8분 간격의
희소 장면 후보이므로, 연속 주행 프레임에서 같은 실제 사건이 반복되는
상황의 중복 제거 성능을 입증하지 않는다. 그 검증에는 1~5 fps 밀집
프레임 인덱스와 사용자 확정 사건이 필요하다.
