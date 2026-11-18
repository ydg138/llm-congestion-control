# 인터페이스 (확정본)

팀원끼리 주고받는 약속을 한곳에 모은 문서다. 바꿔야 하면 디스코드 `#개발`에 먼저 알리고 이 문서를 고친다.

| 약속 | 만드는 쪽 → 쓰는 쪽 |
| --- | --- |
| 1. 서버 HTTP API | P1 → P2·P3 |
| 2. 컨트롤러 인터페이스 | P2 → P3 |
| 3. 요청 로그 CSV | P3 → P4 |
| 4. 서버 로그 CSV | P1 → P4 |

## 1. 서버 HTTP API (API 2.0)

P1 서버 API 2.0을 그대로 따른다 (11/11 결정). 상세는 서버 코드의 `docs/API.md`. 아래는 P2·P3가 쓰는 부분만 요약.

```
POST /generate
요청 본문(JSON):
  request_id         문자열    시도마다 새로 (재시도도 새 값)
  agent_id           문자열    어느 에이전트인지 (공정성 계산용)
  workload_id        문자열    작업 번호. 재시도해도 같은 값 → 같은 출력 길이
  input_tokens       정수      입력 토큰 수
  max_output_tokens  정수      출력 상한. 재시도해도 같은 값

응답:
  200  output_tokens, ttft_seconds(대기 포함), queue_wait_seconds, server_elapsed_seconds 등
  429  error=rate_limit_exceeded, limited_by, retry_after_seconds(실수)
  529  error=server_overloaded            (처리 슬롯과 대기열이 모두 참, 한도 헤더 없음)
  422  error=request_exceeds_capacity     (기다려도 불가능 → 혼잡 아님)

남은 한도 헤더 (200·429):
  X-RateLimit-Remaining, X-InputTokenLimit-Remaining, X-OutputTokenLimit-Remaining
  (각각 -Limit도 있음)

GET  /health, /stats, /config            상태·통계·설정 확인 (한도를 쓰지 않음)
POST /admin/config                       실행 중 한도·용량 변경 (MOCK_LLM_ADMIN_TOKEN 필요)
```

꼭 지킬 것:

- **재시도할 때** `request_id`만 새로 만들고, `workload_id`와 `max_output_tokens`는 그대로 둔다. 그래야 같은 작업이 같은 출력 길이를 받는다.
- **기다릴 시간**은 본문의 `retry_after_seconds`(실수)를 쓴다. `Retry-After` 헤더는 1초 이상 정수로 올림돼 있어 쓰지 않는다.
- **실험마다 서버를 재시작**한다. 버킷·통계가 초기화되고 새 `run_id`(응답 헤더 `X-Run-Id`)가 생긴다. 이 값을 요청 로그에 같이 남긴다.
- **출력 토큰**은 서버가 입장할 때 `max_output_tokens`만큼 예약하고, 끝나면 실제량으로 정산한다. "완료 시 차감" 옵션은 C3 설계 때 P1·P2가 논의한다.
- 서버는 텍스트를 만들지 않고 계산한 지연만큼 기다린 뒤 응답한다. `ttft_seconds`는 서버 안에서 계산한 값으로, 실제 스트리밍 TTFT와 다르다.
- 같은 실험의 에이전트는 모두 같은 서버 주소를 쓴다. 각자 서버를 켜면 서로 다른 실험 환경이다.

## 2. 컨트롤러 인터페이스

모든 컨트롤러는 같은 메서드를 가진다. 실행기는 `make_controller("C2")`처럼 이름으로 만들고, 컨트롤러 종류를 몰라도 된다.

```python
class Controller:
    def can_send(self, est_tokens: int = 0) -> bool: ...
    def on_send(self, est_tokens: int = 0) -> int: ...                 # 요청 번호를 돌려줌
    def on_success(self, req_no: int, resp: dict) -> None: ...         # 200
    def on_rate_limited(self, req_no: int, retry_after: float | None) -> None: ...  # 429
    def on_overloaded(self, req_no: int) -> None: ...                  # 529
    def on_error(self, req_no: int) -> None: ...                       # 422·타임아웃·연결 오류
    def wait_before_retry(self, attempt: int) -> float: ...            # 재시도 전 대기 초
    def state(self) -> dict: ...                                       # 로그용
```

| 항목 | 약속 |
| --- | --- |
| `resp` | `{**응답 본문, "headers": dict(응답 헤더)}`. C4는 `ttft_seconds`, C5는 `headers`를 읽는다 |
| `retry_after` | 429 본문의 `retry_after_seconds` (없으면 `None`) |
| `on_error` | 혼잡 신호가 아니므로 윈도는 그대로, `in_flight`만 줄인다 |
| `attempt` | 이번이 몇 번째 재시도인지. 첫 재시도가 1 (C1 백오프용, 나머지는 0초) |
| `state()` | 항상 `cwnd`, `in_flight`, `ssthresh`. C4는 `min_ttft`, `ttft_ewma`, C5는 `limit`, `pause_until`을 더한다 |
| `est_tokens` | C3만 쓴다. 실행기는 `can_send`·`on_send`에 이번 요청의 `input_tokens + max_output_tokens`를 넘긴다. 다른 컨트롤러는 무시한다 |

컨트롤러 이름: `C0-1`, `C0-8`, `C0-32`, `C0-64`, `C1`, `C2`, `C4`, `C5` (C3는 13주차). 설명은 `docs/controllers.md`.

C3가 들어오면 `on_success`의 `resp`에서 실제 `output_tokens`를 읽어 예약을 정산한다. 실행기 쪽 변경은 위 `est_tokens`를 넘기는 것뿐이다. 세부는 C3 설계 때 이 절에 추가한다.

## 3. 요청 로그 CSV

요청 한 번(재시도 포함 각 시도)마다 한 줄.

```
run_id, exp, controller, seed, agent_id, agent_class, task_id, req_no, attempt,
t_send, t_done, status, input_tokens, max_output_tokens, output_tokens, ttft_ms, latency_ms,
retry_after, cwnd, in_flight
```

- `agent_class`: `light` 또는 `heavy` (E4용, 11/17 추가). E1·E3은 모두 `light`.
- `max_output_tokens`: 요청에 넣은 출력 상한. C3의 출력 예상 오차(`max_output_tokens` − `output_tokens`)를 볼 때 쓴다 (11/17 추가).
- `output_tokens`: 200일 때만 값이 있고, 나머지는 빈 칸.
- `cwnd`: C3는 토큰 단위 윈도다. 다른 컨트롤러와 단위가 다르므로 그래프에서 따로 그린다.

- `run_id`에는 서버의 `X-Run-Id`를 쓴다 (서버 로그와 연결용).
- 시각(`t_send`, `t_done`)은 실험 시작부터 지난 초.
- `cwnd`, `in_flight`는 응답을 처리한 직후의 값.
- 파일 이름: `results/<exp>_<controller>_seed<seed>_<run_id>.csv`
- `status`: HTTP 상태 코드. 응답을 받지 못하면 `timeout`(요청 시간 초과) 또는 `conn_error`(연결 실패, 응답을 읽지 못함). 이 둘과 422는 재시도하지 않고 그 작업은 실패로 남는다 (11/17 추가).
- stats JSON: `results/<exp>_<controller>_seed<seed>_<run_id>_stats.json`. 실행이 끝난 뒤 서버 `/stats` 원본 맨 위에 `runner`를 붙인 것 (11/17 추가).
  - `runner.status`: `completed`(모든 작업이 200으로 끝남) 또는 `aborted`(안전장치로 멈춤, 422·`timeout`·`conn_error`로 포기한 작업이 있음, 코드 오류·Ctrl+C)
  - `runner.reason`: `aborted`인 이유. `completed`면 `null`
- **`runner.status`가 `aborted`인 실행은 분석에서 뺀다** (11/17 추가). 멈추는 순간 응답을 기다리던 요청은 요청 로그에 없어 서버 숫자와 맞지 않을 수 있다. 확인은 `python -m runner.check_run <요청 로그 CSV>`.

## 4. 서버 로그 CSV

서버가 0.1초마다 대기열 길이, 처리 중 슬롯 수, 요청·입력·출력 버킷 잔량을 기록하고, `agent_id`별 완료 건수와 429 횟수를 남긴다. 형식은 서버 코드의 문서를 따른다.

**추가 요청 (11/17, E4용)**: `agent_id`별로 **실제로 쓴 입력 토큰 합, 출력 토큰 합**도 남긴다. 토큰 기준 Jain 지수를 서버 쪽 숫자로도 계산해 요청 로그와 대조하기 위해서다. 열 이름과 위치는 P1이 정하고 이 절에 적는다.
