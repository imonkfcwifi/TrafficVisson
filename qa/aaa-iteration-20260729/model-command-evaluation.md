# 자연어 명령형 Grounding DINO 실제 평가

> 실행일: 2026-07-29  
> 목적: 사용자 자연어 명령을 고정된 허용 유형·프롬프트로 컴파일한 뒤, 특정 완료 분석 작업의 분할 프레임만 비파괴 평가

## 실행 계약

- 사용자 명령: 산출물에 원문을 저장하지 않고 SHA-256만 기록
- 컴파일 유형: `sign_vegetation_occlusion`
- 모델 프롬프트: `road sign obscured by vegetation`
- 대상: 실제 `20250916_073358.mp4` 분석 작업의 후보 7개 전체
- 모델: `IDEA-Research/grounding-dino-tiny`
- 실행 장치: `cuda:0`, NVIDIA RTX A2000 12GB
- 실행 시간: 17.859초
- 점수 의미: `model_score_not_probability`
- 자동 라벨·Incident·모델 승격·sidecar: 각각 0건

## 결과

- 평가 프레임: 7개
- 제안 프레임: 3개
- 박스 후보: 3개
- 연락시트: `12-grounding-command-sign-vegetation-contact-sheet.jpg`
- 원본 평가 디렉터리: `C:\ProgramData\TrafficVisson\ModelEvaluations\grounding-dino-command-sign-vegetation-20260729-job-1ca29d0d-v1`

## 육안 사전검토

3개 후보 모두 `표지판이 식생에 가려진 상태`를 뒷받침하지 않는다. 두 후보는 같은 주차장 장면의 작은 영역을 잘못 잡았고, 나머지 한 후보는 도로 이정표를 찾았지만 식생 가림은 보이지 않는다. 따라서 이 결과는 자동 라벨로 전환하거나 모델 승격 근거로 사용할 수 없고, 어려운 음성 예제로 보존하는 방향이 적절하다.

이 평가는 명령→고정 recipe→실제 GPU 프레임 검지 경로가 동작한다는 실행 증거다. 해당 유형의 정밀도·재현율이나 유지보수 판단 정확도를 입증하지 않는다.
