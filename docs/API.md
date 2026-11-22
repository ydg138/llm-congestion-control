# API 2.0 — P1 실험 서버

프로젝트 자체 계약이며 특정 상용 LLM API 호환 규약이 아닙니다. API 1.1에서 2.0으로 변경: output_tokens가 실제 모의 출력량으로 바뀌고 3종 한도·출력 예약·422·지연 모델·동적 설정이 추가되었습니다. 기존 필수 요청 필드는 유지합니다.

## 공통

UTF-8 JSON, 본문 64 KiB 미만. `/`만 HTML. 앱이 처리한 응답에 `X-API-Version: 2.0`, `X-Run-Id`, `Cache-Control: no-store`. HTTP 프로토콜 단계 실패/연결 종료까지 JSON을 보장하지 않습니다. 요청의 모든 시간 단위는 초. 입력/출력 토큰은 텍스트를 토크나이즈한 값이 아니라 실험용 정수입니다.

## POST /generate

```json
{"request_id":"attempt-1","agent_id":"agent-a","workload_id":"job-1","input_tokens":100,"max_output_tokens":64}
```

| 필드 | 규칙 |
|---|---|
| request_id, agent_id | 필수. 공백만 아닌 1~128자 문자열 |
| workload_id | 선택. 같은 문자열 규칙. 기본 request_id |
| input_tokens | 선택. 정수 0~1,000,000,000. 기본 0 |
| max_output_tokens | 선택. 정수 1~1,000,000. 기본 현재 설정 output_max |

불리언은 정수가 아닙니다. 알 수 없는 필드는 무시합니다. ID 중복 제거는 하지 않으므로 재시도도 별도 요청입니다. 동일 seed·workload_id·출력 범위·max_output_tokens로 동일 길이를 얻습니다. agent_id와 도착 순서는 길이에 영향을 주지 않습니다. 컨트롤러는 `/config`의 모델 설정을 이용해 미래 출력량을 미리 계산하지 않아야 실험의 '출력 길이 미지' 조건이 유지됩니다.

처리 순서: 입력/모델 검증 → 큐 공간 검사(529) → 요청1·입력량·최대출력량 전체 검사 → 전체 예약 → 슬롯/FIFO 큐 → prefill/decode → 출력 정산 → 200. 일부 한도만 차감하지 않습니다. 초과 제약이 여러 개면 limited_by에 모두 포함됩니다. 큐 과부하는 한도 부족보다 우선합니다.

### 한도 및 정산

요청/입력/출력 한도는 독립 토큰 버킷입니다. 충전 속도는 초당이며 용량은 버스트 상한. 출력은 클라이언트가 지정한 최대량을 예약하므로 실제량을 미리 아는 서버도 그 정보로 입장 판단을 유리하게 하지 않습니다. 완료 시 `예약량-실제출력량` 반환(현재 용량으로 cap). 예약 중에도 시간에 따른 버킷 충전은 계속됩니다. 이는 과제의 **상한 예약 모델**이며 특정 제공자의 과금/한도 정책을 주장하지 않습니다.

대기·prefill 중 취소: 출력 예약 전액 반환. decode 중 취소: 실제 첫 토큰 발생 시점 이후 모델 간격으로 부분 사용량 계산, 나머지 반환. 요청/입력은 수락 때 소비한 것으로 유지합니다. 완료된 뒤 클라이언트가 받지 못해도 완료 사용량은 유지합니다. 설정 축소 중 정산도 새 용량으로 cap합니다.

### 200 응답

기존 request_id/agent_id/run_id/output/input_tokens에 다음을 포함합니다.

| 필드 | 의미 |
|---|---|
| workload_id | 작업 식별자 |
| config_revision | 요청 수락 당시 모델 설정 버전 |
| output | 항상 mock response, 실제 생성 텍스트 없음 |
| output_tokens | 1~max_output_tokens의 모의 실제량 |
| max_output_tokens | 예약한 최대 출력량 |
| queue_wait_seconds | 입력 검증 후 처리 시작까지 |
| prefill_seconds | 처리 시작~첫 모의 토큰(고정 오버헤드 포함) |
| decode_seconds | 첫 토큰~마지막 토큰 |
| ttft_seconds | 입력 검증 후 첫 모의 토큰까지(큐+prefill) |
| service_seconds | prefill+decode의 실제 벽시계 경과 |
| server_elapsed_seconds | queue_wait+service |
| model_prefill_seconds | service_delay + base_ttft + input_tokens × input_token_seconds |
| model_decode_seconds | (output_tokens-1) × output_token_seconds |
| token_interval_seconds | 설정된 토큰 사이 간격 |

모든 결과는 완료 시 한 번에 반환됩니다. TTFT는 서버 내부 시뮬레이션 신호이며 클라이언트가 첫 스트림 조각을 받은 시간이 아닙니다. OS 스케줄링으로 측정값과 모델값이 다를 수 있습니다. 시간은 body 파싱/전송 구간을 제외합니다. 수락된 요청은 설정이 바뀌어도 수락 당시 모델을 사용합니다.

### 오류

| 상태 | error | 처리 |
|---|---|---|
| 400 | invalid_json / invalid_field | 본문·필드 수정 |
| 403 | admin_access_denied | 관리 API 키가 없거나 불일치 |
| 404 / 405 / 413 / 415 | not_found / method_not_allowed / payload_too_large / expected_application_json | HTTP 요청 수정 |
| 422 | request_exceeds_capacity | 입력량 또는 최대 출력량이 현재 버킷 최대 용량 초과. 설정/요청 변경 필요 |
| 422 | invalid_generation | 모델상 처리 시간이 3600초 초과 등 |
| 429 | rate_limit_exceeded | 현재 잔량 부족. 팀 컨트롤러에는 본문의 retry_after_seconds(초)를 전달 |
| 529 | server_overloaded | 처리 슬롯과 대기열 모두 가득 참 |

429/422의 한도 오류에는 limited_by(`requests`, `input_tokens`, `output_tokens`), retry_after_seconds를 포함합니다. 422의 대기 시간은 null, Retry-After 헤더 없음. 429의 값은 모든 부족량을 채우는 데 필요한 최대 시간이며 정수 초 올림 헤더도 제공합니다. 출력 예약 반환으로 더 일찍 가능해지거나 경쟁 요청 때문에 더 늦어질 수 있습니다.

200/429/한도422 응답 헤더:
- `X-RateLimit-Limit`, `X-RateLimit-Remaining`: 요청 버킷
- `X-InputTokenLimit-Limit`, `X-InputTokenLimit-Remaining`: 입력 버킷
- `X-OutputTokenLimit-Limit`, `X-OutputTokenLimit-Remaining`: 출력 버킷

Remaining은 소수 잔량의 내림값. **200은 완료·정산 후**, 거절은 검사 직후 snapshot입니다(API1.1의 수락 당시 snapshot과 다름). 529는 한도 검사를 하지 않아 이 헤더가 없습니다. 변경 중이면 헤더는 최신 한도지만 본문 config_revision은 수락 당시 모델 버전입니다.

## GET /health, /stats, /config

- `/health`: status, run_id, api_version, capabilities.
- `/stats`: accepted/rate_limited/overloaded/completed/cancelled, active/queued 및 기본 설정. 추가 input_tokens_consumed/output_tokens_consumed/output_tokens_reserved, 3종 quotas snapshot, config_revision, scenario_status. 정상 종료 후 accepted = completed + cancelled. 진행 중에는 active+queued도 포함. 입력·422 오류는 수락/거절 통계에 포함하지 않음.
- `/config`: 전체 설정, revision, 변경 내역(실제 elapsed_seconds·source·changes), 시나리오 상태, 관리 변경 활성 여부. 관리 토큰은 공개하지 않음. 조회는 요청 한도를 소비하지 않음.

`/stats`의 `agent_stats`는 에이전트 ID를 키로 사용합니다. 예를 들어 `{"agent-a":{"completed":1,"rate_limited":2,"input_tokens_consumed":100,"output_tokens_consumed":30}}`는 완료 1건과 HTTP 429 응답 2건입니다. 한 작업의 재시도도 각각 집계합니다. 422, 529, 처리 중 취소는 두 수에 포함하지 않습니다.

### 에이전트별 실제 토큰 사용량

`input_tokens_consumed`는 요청 수락 시 입력 전량을 누적합니다. 수락 후 대기·prefill 중 취소해도 유지합니다. `output_tokens_consumed`는 완료·취소 시 실제 모의 생성량을 누적합니다. 예약한 max_output_tokens는 포함하지 않습니다. 대기·prefill 취소 시 출력은 0이며 decode 취소 시 부분 생성량을 포함합니다. 이는 위 한도 정산과 서버 전체 통계와 동일한 기준입니다.

수락 전 400·422·429·529는 토큰 사용량 0입니다. 429 재시도는 rate_limited만 증가합니다. 같은 workload_id도 다시 수락되면 사용량이 다시 발생합니다. 완료 후 응답 유실 시에도 서버 사용량은 유지됩니다.

에이전트별 합은 동시 조회한 서버 전체 입력·출력 합과 같습니다. 정상 완료·취소/유실 없는 실행에서는 성공(200) 요청 로그의 input_tokens/output_tokens 합과 비교합니다. 취소·진행 중 요청이 있으면 성공 로그와 직접 같다고 볼 수 없습니다. 서버 재시작 시 전부 초기화합니다.

에이전트 CSV에는 기존 5열 뒤에 두 토큰 열을 추가합니다. 토큰만 바뀌어도 다음 표본에 기록하고, 에이전트별 마지막 행을 사용합니다(누계 행 전체를 합산하지 않음). 과거 CSV에는 새 열이 없으므로 분석기는 열 존재 여부를 확인해야 합니다.

### 서버 경과 시간

`GET /stats`의 최상위 `elapsed_seconds`는 현재 `run_id`의 서버 시작 후 경과한 초입니다. `time.perf_counter() - state.started_at`을 소수점 6자리로 반올림하며, 아래 두 서버 CSV의 `elapsed_seconds`와 같은 시작 시각과 단조 시계를 사용합니다. 서로 다른 시점의 조회·표본은 그만큼 다른 값을 가집니다. 서버를 재시작하면 새 `run_id`와 함께 경과 시간도 처음부터 계산합니다. 첫 조회까지 시간이 지나므로 첫 값은 0보다 클 수 있습니다.

P3 실행기는 실험 시작 직전에 `/stats`를 조회하고 `run_id`가 이번 서버와 일치하는지 확인합니다. 이때 읽은 `elapsed_seconds`를 종료 시 `/stats`와 구분해 결과 JSON에 저장합니다. 저장 위치는 예를 들어 `runner.server_elapsed_at_start_seconds`로 정할 수 있습니다. 요청 로그의 `t_send`·`t_done`에 이 시작값을 더하면 서버 CSV 시간축에서의 근삿값을 얻습니다. 조회 왕복 시간과 실험 시작까지의 간격 때문에 오차가 생길 수 있으므로, 조회 직후 실험을 시작합니다.

시작값이 3.25초이고 `t_send`가 0.40초인 요청은 서버 CSV의 약 3.65초 구간과 비교합니다. 종료 통계의 최상위 `elapsed_seconds`는 종료 시점의 경과 시간으로 저장합니다. `/generate`의 `server_elapsed_seconds`는 요청 한 건의 대기·처리 시간을 나타냅니다.

### 실험 CSV

일반 실행은 현재 작업 폴더의 `results/server/`에 아래 두 파일을 생성합니다. CLI에서는 `--log-dir`, Python에서 앱을 직접 생성할 때는 `create_app(log_dir=...)`로 경로를 지정합니다.

| 파일 | CSV 열 |
|---|---|
| `server_state_<run_id>.csv` | `run_id,elapsed_seconds,queued,active,requests_remaining,input_tokens_remaining,output_tokens_remaining` |
| `agent_stats_<run_id>.csv` | `run_id,elapsed_seconds,agent_id,completed,rate_limited,input_tokens_consumed,output_tokens_consumed` |

서버 상태는 시작 시, 약 0.1초 간격, 정상 종료 시에 기록합니다. 각 행은 실제 관측 시점의 값이며, 짧은 대기나 혼잡은 표본 사이에 끝날 수 있습니다. 에이전트별 누계는 요청 처리 중 갱신하고, 표본 시점에 직전 기록과 다른 에이전트만 CSV에 추가합니다. 최종 값은 같은 실행·에이전트의 마지막 행입니다. 프로세스를 강제 종료하면 마지막 표본 이후 변경이 파일에 남지 않을 수 있습니다.

## POST /admin/config

`MOCK_LLM_ADMIN_TOKEN` 환경변수를 서버 시작 전에 설정하고 `Authorization: Bearer <token>` 사용. 미설정이면 비활성. JSON 본문은 변경할 설정들의 객체(예: `{"slots":2,"refill_rate":5}`). 모든 값을 검증한 뒤 한 번에 적용. 오류는 400 invalid_config. 기존 요청/통계/run_id는 유지하며 revision 증가.

버킷: 이전 속도로 변경 시점까지 충전 → 잔량 보존 → 새 용량 상한 적용. 용량을 늘린다고 잔량을 즉시 채우지 않음. 슬롯 증가: 즉시 대기 요청 승격. 슬롯 감소: 실행 중 작업을 중단하지 않고 자연 종료를 기다림. 큐 축소: 기존 대기 작업 유지, 추가 입장 제한. 기존 active/queued가 새 상한보다 일시적으로 클 수 있음. 모델 변경: 새 요청부터 적용. 이벤트 기록은 최대 2000개, 초과하면 새 실험 필요.

## 시작 설정·시나리오

기본값은 `ServerConfig`, 실행 예시는 `examples/config.json`. CLI가 JSON 설정보다 우선합니다.

| 설정 | 기본값 | 범위 |
|---|---:|---|
| capacity / refill_rate | 5 / 1 | 용량 정수1..1e9, 속도 유한수1e-6..1e9 |
| input_capacity / input_refill_rate | 10000 / 1000 | 동일 |
| output_capacity / output_refill_rate | 1000 / 100 | 동일 |
| slots / queue_size | 2 / 16 | 정수1..10000 / 0..10000 |
| service_delay | .05 | 0..3600초 |
| seed | 42 | 정수0..2^32-1 |
| output_min / output_max | 16 / 64 | 정수1..1e6, min<=max |
| base_ttft / input_token_seconds / output_token_seconds | .01 / .0001 / .001 | 유한수0..60초 |

모델 전체 요청 시간이 3600초를 넘으면 그 요청은 422입니다. `output_max`가 출력 버킷 용량보다 크면 기본 max_output_tokens 요청은 422이므로 설정 또는 요청 상한을 맞추세요.

`--scenario`는 `[{"at_seconds":5,"changes":{"slots":1}}]` 형식. 최대1000개, 오름차순0..86400초, 서버 시작 시각 기준. 미리 전체 시나리오를 검증합니다. 수동 변경과 충돌해 실행 시 실패하면 scenario_status에 failed 이유를 기록하고 후속 이벤트를 중단합니다. 실제 적용 시각은 event loop 지연을 포함합니다. 재시작하면 시드/설정 파일을 다시 읽고 버킷과 통계 초기화, 새 run_id 생성.

단일 프로세스가 실험 상태를 소유합니다. create_app()을 직접 실행하는 경우 AppRunner/web.run_app에 `handler_cancellation=True`를 지정해야 연결 종료 시 취소 정책이 적용됩니다.


### 에이전트 토큰 통계 재현 확인

최신 main 기준 프로젝트 가상환경에서 실행합니다.

```sh
python -m scripts.demo_agent_tokens --serve --port 8088
```

브라우저에서 `http://127.0.0.1:8088/demo`를 열면 두 C2 에이전트의 완료 결과가 보이고 `/stats`로 이동할 수 있습니다. `--serve`를 빼면 대조 후 정상 종료합니다. 포트가 사용 중이면 `--port`로 다른 포트를 지정합니다. 종료는 Ctrl+C입니다. 데모 화면은 이 스크립트에서만 제공하며 일반 서버의 API에는 추가하지 않습니다.

입력 100토큰(A), 1,000토큰(B)의 요청을 각각 20건 보냅니다. 출력은 상한 20을 예약하고 실제 4토큰씩 생성하므로 A는 입력 2,000·출력 80, B는 입력 20,000·출력 80이 기대값입니다. 요청 로그는 기존 P3 RequestLog/Agent와 C2를 사용하며 `results/agent_tokens_<run_id>/`에 요청 CSV, stats JSON, 서버 CSV, verification.json을 저장합니다. 429 수는 실행 환경에 따라 달라집니다. 집계 기능 확인용이며 E4 성능·공정성 결론을 내리는 설정은 아닙니다.
