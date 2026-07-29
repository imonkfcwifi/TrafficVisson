# 실제 프레임 비식별화 후보 검지 스모크

작성일: 2026-07-29  
상태: **검토 전용 후보 검지**. 자동 비식별화 성능이나 공유 안전성을 입증하지 않는다.

## 입력

- NAS 원본: `20250905_095953.mp4`
- 원본 SHA-256: `020ac3d642e1b02ad1d3834a5fd98b6e24ad38de00b74262bce93e4280eb16a4`
- 밀집 인덱스: 2 FPS, 6 frames, 원본 읽기 전용
- 평가 프레임: `frame-00000003.jpg`, 영상 시각 1.5초
- 프레임 SHA-256: `804eefa5d86324465b8c5fec0989582a07ed4fb339ea3d032d89cb8455218af0`
- 프레임 크기: 1440×1200
- 모델팩: pinned Grounding DINO Tiny ONNX Q4, review-only
- positive prompts: `vehicle license plate`, `human face`
- negative prompts: `road surface`, `traffic signal`, `vehicle body`

번호판 문자열 자체는 감사 문서에 기록하지 않았다.

## 결과

모델은 `vehicle license plate` 후보 1개를 반환했다.

| 항목 | 값 |
|---|---|
| model score | `0.76767147` |
| normalized box | `x=0.27107824`, `y=0.66734357`, `w=0.11062273`, `h=0.02666697` |
| 대략적 pixel box | `x=390`, `y=801`, `w=159`, `h=32` |
| face proposal | 0 |
| 자동 승인·자동 blur | 0 |

육안상 이 박스는 전방 차량 번호판 위치와 일치한다. 이 단일 양성은 모델이 적어도 한 실제 장면에서 번호판 검토 후보를 만들 수 있다는 연결 증거일 뿐이다. 얼굴이 보이지 않는 후면 장면이므로 얼굴 검지 성능 증거는 아니다.

## 안전 판정

- 원본 영상과 원본 프레임은 변경하지 않았다.
- 모델 출력은 `issue_type=other`, `review_only=true`, `automatic_labeling_allowed=false` 경계에서만 실행했다.
- 현재 제품에는 확정된 얼굴·번호판 검증 세트, 사용자 영역 수정, 결정적 blur 출력, 공유 전 누락 차단이 없다.
- 따라서 원본/검지 표시 이미지는 내부 검토용으로만 취급하고, 보고서·ZIP·외부 공유에는 사용하면 안 된다.

## 다음 수직 슬라이스

1. `privacy_face`·`privacy_license_plate`를 유지보수 유형과 분리된 전용 검토 계약으로 정의한다.
2. 고정 prompt와 pinned model-pack으로 후보 박스를 만든다.
3. 사용자가 `예/아니오/영역 수정/추가 영역`으로 확정한다.
4. 원본을 덮어쓰지 않는 결정적 blur 사본과 SHA-256 provenance를 만든다.
5. 공유 export와 HWP 사진 삽입은 `redaction_status=confirmed` 사본만 허용한다.
6. 한국 도로 영상의 얼굴·번호판 고정 검증 세트로 누락률을 측정하고, 미검토 근거가 한 장이라도 있으면 공유를 차단한다.
