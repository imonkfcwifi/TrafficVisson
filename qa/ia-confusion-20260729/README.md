# TrafficSuperVision 단일 창 정보 구조 재검증

검증일: 2026-07-29

## 판정

TrafficSuperVision은 설정 화면 하나나 웹 SaaS를 Electron 창 안에 감싼 구조가 아니다. 하나의 Windows Electron 창 안에서 9개 업무 화면을 전환한다. 기존 캡처는 그 구조와 자동 파이프라인을 충분히 설명하지 못해 설정형 관리 콘솔처럼 읽혔다.

이번 수정은 다음을 실제 renderer와 개발 Electron에 반영했다.

- 논리적인 9개 화면 순서와 `업무 화면 4 / 9`
- 좌측 `자동 점검`, `결과 확인`, `관리` 그룹
- PipelineJob의 고정 판정 프로필을 편집 토글이 아닌 `현재 검토 모드`로 표시
- `영상 → 자동 분석 → 질문 검토 → 결과 자동 갱신`의 지속 흐름
- 모든 질문의 정직한 GPS 상태와 경로 생성 불가 설명
- AI 추천 심각도·추천 근거와 사용자 최종 심각도 분리
- 약한 3·4분기 참고와 프로젝트별 사용자 승인 사례 구분
- 1280×720 핵심 글자 11px 이상과 SUIT 유지

## 시각 증거

1. `01-labeling-electron-current.jpg`
   - IA 보강 전 개발 Electron
   - 판정 모드가 설정 토글처럼 보이고 화면 전체 여정이 약함
2. `02-labeling-electron-clarified-1280x720.png`
   - 보강 후 실제 개발 Electron
   - Windows 테두리 반올림으로 캡처는 1282×721
   - 문서 전체 스크롤 없이 현재 질문, 근거, 유사 사례, GPS, AI 심각도 경계와 Y/N/U/E가 보임
3. `03-reference-vs-clarified-1280x720.png`
   - 유일한 디자인 참조와 보강 후 Electron을 같은 높이로 결합한 비교

## 자동 회귀

- renderer: 156 passed
- Electron: 80 passed
- Python bridge: 640 passed, 2 skipped
- TypeScript: passed
- Vite desktop build: passed

## 보존한 승인 경계

- 실제 NAS 질문에 대한 사용자 답변: 0
- 확정 Annotation: 0
- 확정 Incident: 0
- 시간대별 GPS/GPX가 없으므로 실제 이동 경로 생성 안 함

## 남은 수용 공백

- 사용자가 실제 질문에 답한 뒤 Incident·확정 CSV·증거·보고서 초안 자동 갱신
- 시간대별 실제 GPX가 있는 운영 영상의 경로 재생
- 결정적 sample HWPX writer, 한/글 COM 적용, 재열기와 48쪽 전체 렌더
- 검증된 SAM2·한국어 OCR·도로 전용 모델팩과 프로젝트 모델 학습·승격
- 현재 소스 trust chain을 반영한 새 서명 설치본 및 clean Windows 수용
