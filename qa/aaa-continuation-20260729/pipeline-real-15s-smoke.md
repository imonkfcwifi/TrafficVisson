# 기본 PipelineJob 실제 15초 영상 스모크

## 판정

`fixture runtime`을 주입하지 않은 기본 `BridgePipelineRuntime`과 실제 MP4 파일로 검증했다. `fixture_mode=false`의 새 프로젝트에서 인증된 Bridge API를 통해 로컬 파일을 등록하고, 최초 1회 프로필을 설정한 뒤 PipelineJob 하나를 시작했다.

현재 구현된 실경로는 `ffprobe 원본 검사 → SHA-256 → GPS preflight → 2fps dense index`까지 정상 완료한다. 설치된 검지 모델팩이 없으므로 다음 단계인 `candidate_rescan_5_10fps`에서 빈 성공으로 넘어가지 않고 `blocked / model_unavailable`로 정직하게 정지한다. 질문 큐·Incident·파생 산출물은 생성되지 않았다.

이 검증은 전체 제품 E2E 통과가 아니다. 후보 구간 5~10fps 재분석, DINO/전용 검지, OCR, SAM2, 병합, 유사 사례, 비전 제안, 질문 큐와 이후 산출물 단계는 모델팩과 실제 stage adapter가 연결될 때까지 미완료다.

## 입력과 실행 조건

| 항목 | 값 |
|---|---|
| 원본 | `desktop/public/assets/real-trip/tunnel-approach-15s.mp4` |
| 원본 크기 | 2,540,481 bytes |
| 원본 SHA-256 | `ffe61150680f6376f59c2f7df50e9ab0b05e37caec43c62971381f956247b4f0` |
| ffprobe 결과 | MP4, H.264, 960×540, 24fps, 15.000초, 영상 스트림 1개 |
| 앱 모드 | `fixture_mode=false`; 기본 `BridgePipelineRuntime` |
| 입력 등록 | Bearer 인증이 적용된 `/media/local-file` 계약 |
| 권리 확인 | `rights_confirmed=true` |
| 네트워크·다운로드 | 없음 |
| 격리 데이터 루트 | `tmp/pipeline-real-15s-ael3f8fo` |
| 기본 양식 | `sample-4q-v1` |
| 기본 양식 SHA-256 | `bbdd61a6ef192a957a5e9480b0c2eedbfe956dd184abd5d76f59474254941539` |
| 판정 프로필 | `learning_assist`, `precision`, 후보 재분석 8fps, Codex 보고서 Provider |
| 요청 모델팩 | `trafficvisson-road-detector-v1`, `trafficvisson-korean-ocr-v1`, `trafficvisson-sam2-v1` |

모델팩 상태는 `model_pack_not_configured`였다. capability snapshot에서 `intake`, `sha256`, `dense_index_2fps`만 `ready`였고 detector/OCR/SAM2/유사 사례/비전 제안은 모두 `missing`이었다. Codex는 선택된 보고서 Provider로만 기록되었고 프레임 검지에 호출되지 않았다.

## PipelineJob 결과

| 단계 | 상태 / 결과 | 시도 | 실측 단계 시간 | 핵심 증거 |
|---|---|---:|---:|---|
| `intake_validation` | `success / success` | 1 | 110.859ms | ffprobe validated, 15.0초, 2,540,481 bytes |
| `sha256` | `success / success` | 1 | 78.276ms | 2,540,481 bytes 해시, 원본 SHA 일치 |
| `metadata_gps_preflight` | `blocked / blocked_gpx_required` | 1 | 21.018ms | timed track 없음, route point 0 |
| `dense_index_2fps` | `success / success` | 1 | 961.261ms | 30프레임, 15.0초, 2fps, `truncated=false` |
| `candidate_rescan_5_10fps` | `blocked / model_unavailable` | 1 | 18.207ms | `candidate_rescan_model_unavailable` |
| 이후 7단계 | `pending` | 0 | - | 업스트림 모델 부재를 우회하지 않음 |

최종 상태는 다음과 같다.

- `status=blocked`
- `current_stage=candidate_rescan_5_10fps`
- `progress_percent=42`
- `outcome_code=model_unavailable`
- `error_code=candidate_rescan_model_unavailable`
- PipelineJob 시작부터 최종 blocked 관측까지 약 1.625초
- 첫 인스턴스 생성부터 fresh-instance 재검증 완료까지 약 1.922초

GPS 단계의 `capture_start_coordinate_used_as_route=false`, `route_point_count=0`, provenance의 `route_status=blocked_gpx_required`를 함께 확인했다. 즉, 시작 좌표나 추정값으로 이동 경로를 조작하지 않았다.

## 동일 provenance와 영속성

| 항목 | 값 |
|---|---|
| Project ID | `3f6c8d87-4126-40a9-96a9-be0f29de903e` |
| Snapshot ID | `485a4438-01df-46a4-8482-37e6aca8c969` |
| PipelineJob ID | `79845fe2-7f23-4d5e-bdae-be0026caeb22` |
| 프로필/프로필 record SHA-256 | `97e808f1bd7eee93b29a9d7790e316336d47fac61ce401dc670e813f6017ac2e` |
| Capability SHA-256 | `a040c3c4639df67903d3bd489170b6320d07a43f89618db975289f048cf9cf0e` |
| 입력+프로필 fingerprint | `a643271765e7dfdd61f8733cc1f0567443135d607a5e588ebcc52fcb68649557` |
| 최종 PipelineJob record SHA-256 | `1eb0bb8b69bd5f9aa2990d8ad61ca7eff783ad2a252181507025b22016c33e2b` |

같은 `snapshot_id + file_name + profile`과 같은 idempotency key로 다시 시작했을 때 `idempotent_replay=true`였고 Job ID, fingerprint, record SHA-256이 모두 같았다.

첫 앱을 완전히 종료하고 같은 격리 데이터 루트로 새 `create_app()`/`PipelineJobService` 인스턴스를 만든 뒤 인증 API로 다시 조회했다. `profiles_loaded=1`, `jobs_loaded=1`, `jobs_requeued=0`, `records_rejected=0`이었고, 같은 Job ID와 record hash로 `blocked / candidate_rescan_5_10fps / model_unavailable` 체크포인트가 그대로 복원됐다. 이미 안전하게 blocked된 Job이므로 재실행되지 않았고 `recovered_after_restart=false`가 맞다.

원시 기계 판독 증거는 `tmp/pipeline-real-15s-ael3f8fo/smoke-evidence.json`에 남겼다. 이 증거 JSON의 SHA-256은 `735e5d79c64d5d33f0ca2985366758cd883fe7193777a3bf7eea570b0df7f3d8`이다. 절대 원본 경로, 인증 토큰, 모델 인증 파일은 기록하지 않았다.

## 스모크에서 발견하고 수정한 복구 버그

첫 실행은 실제 단계 처리까지 성공했지만 fresh-instance GET에서 500을 재현했다. 디스크의 Job JSON과 `.sha256` sidecar는 검증되어 로드됐으나, 이미 blocked된 record는 `_load_records()`가 검증된 digest를 `job.record_sha256`에 복원하지 않았다. API 응답 스키마가 `record_sha256=None`을 거부한 것이 직접 원인이었다.

수정은 이 원인에만 한정했다.

- `_read_verified()`가 검증한 payload와 sidecar digest를 함께 반환한다.
- `_load_records()`가 Job을 파싱한 직후 그 검증 digest를 `record_sha256`에 복원한다.
- blocked Job을 종료·재생성한 서비스에서 다시 읽어 동일 digest와 정확한 checkpoint를 보존하는 회귀 테스트를 추가했다.

수정 후 같은 실제 영상 스모크를 새 격리 루트에서 처음부터 재실행해 fresh GET 200과 동일 record hash를 확인했다.

## 실행 명령과 테스트

워크스페이스 루트:

```powershell
python .\tmp\pipeline_real_15s_smoke.py
```

`desktop\bridge` 디렉터리:

```powershell
python -m pytest .\tests\test_pipeline_jobs.py .\tests\test_pipeline_api.py .\tests\test_media_intake.py .\tests\test_dense_frame_index.py -q
```

결과: `64 passed in 14.16s`.
