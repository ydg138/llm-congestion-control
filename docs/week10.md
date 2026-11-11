# 10주차: 인터페이스 확정과 가장 작은 연결

## 이번 주 목표

"429를 받으면 윈도가 절반이 되고, 성공하면 늘어나는 것"이 진짜 HTTP 응답으로 한 번 돌아가게 만든다. 기능은 최소한으로, 네 사람의 코드가 서로 맞물리는지 확인하는 주다.

### 완료 기준

- [ ] 가짜 서버가 RPM 토큰 버킷 하나와 처리 슬롯·대기열로 동작하고, 한도를 넘으면 429 + `retry-after`를 준다
- [ ] 컨트롤러 C0(고정)과 C2(AIMD)가 있다
- [ ] 실행기가 에이전트 1개로 작업 100개를 처리하고 요청 로그 CSV를 남긴다
- [ ] 그래프 하나: 시간에 따른 cwnd와 429 발생 지점
- [ ] 테스트: 토큰 버킷 충전, AIMD의 성공·429 처리를 손으로 계산한 값으로 확인
- [ ] 첫 주간 보고서 (`reports/week10.md`)

## 첫 회의 안건 (30분)

1. 역할 확인 (P1 서버 김준수, P2 컨트롤러 양동규, P3 실험 박준영, P4 분석·보고서 김경일)
2. 아래 **인터페이스 초안** 확정 또는 수정 (가장 중요)
3. 모두 Python 3.11+ 설치, `pip install -r requirements.txt`, `pytest -q` 돌아가는지 확인
4. 브랜치·PR 규칙 확인 (`CLAUDE.md`의 Git 절)
5. 실제 API를 쓸지, 쓴다면 어떤 모델과 비용 상한
6. 주간 보고서 담당과 마감 요일

## 인터페이스 초안 (회의에서 확정)

### 1) 가짜 서버 HTTP API (P1이 만들고 P2·P3가 씀)

```
POST /v1/messages
요청 본문(JSON):
  agent_id        문자열    어느 에이전트인지 (공정성 계산용)
  task_id         정수      작업 번호
  input_tokens    정수      입력 토큰 수 (클라이언트가 정함)
  max_tokens      정수      출력 상한
응답:
  200  {"output_tokens": 정수, "ttft_ms": 실수, "total_ms": 실수}
  429  {"error": "rate_limit_error"}       헤더 retry-after: 초(실수)
  529  {"error": "overloaded_error"}
모든 응답 헤더 (C5용, 목표 단계):
  ratelimit-remaining-requests
  ratelimit-remaining-input-tokens
  ratelimit-remaining-output-tokens

POST /admin/config      실행 중 한도·용량 변경 (E2, E5용, 목표 단계)
```

- 서버는 텍스트를 만들지 않는다. 계산한 지연만큼 기다린 뒤 응답한다.
- 10주차에는 RPM 버킷 하나만. ITPM·OTPM은 13주차에 추가.
- 스트리밍(SSE)은 여유 단계. 그때까지 TTFT는 서버가 계산해 응답 본문에 넣는다.
- E6에서 실제 API를 쓸 때는 실제 헤더 이름을 위 이름으로 바꿔 주는 어댑터를 둔다.

### 2) 컨트롤러 인터페이스 (P2가 만들고 P3가 씀)

모든 컨트롤러는 같은 메서드를 가진다. 실행기는 컨트롤러 종류를 몰라도 된다.

```python
class Controller:
    def can_send(self, est_tokens: int = 0) -> bool: ...
    def on_send(self, est_tokens: int = 0) -> int: ...          # 요청 번호를 돌려줌
    def on_success(self, req_no: int, resp: dict) -> None: ...
    def on_rate_limited(self, req_no: int, retry_after: float | None) -> None: ...   # 429
    def on_overloaded(self, req_no: int) -> None: ...            # 529
    def wait_before_retry(self, attempt: int) -> float: ...       # 재시도 전 대기 초 (C1 백오프용, 나머지는 0)
    def state(self) -> dict: ...                                  # 로그용: {"cwnd": ..., "in_flight": ..., "ssthresh": ...}
```

### 3) 요청 로그 CSV (P3가 쓰고 P4가 읽음)

요청 한 번(재시도 포함 각 시도)마다 한 줄.

```
run_id, exp, controller, seed, agent_id, task_id, req_no, attempt,
t_send, t_done, status, input_tokens, output_tokens, ttft_ms, latency_ms,
retry_after, cwnd, in_flight
```

- 시각(`t_send`, `t_done`)은 실험 시작부터 지난 초.
- `cwnd`, `in_flight`는 응답을 처리한 직후의 값.
- 파일 이름: `results/<exp>_<controller>_seed<seed>_<run_id>.csv`

## 담당별 할 일과 Claude Code에 줄 첫 요청

각자 자기 브랜치를 만들고(`git switch -c feat/<이름>-<내용>`), 레포 폴더에서 Claude Code를 실행한 뒤 아래 요청을 붙여넣으면 된다. Claude Code는 `CLAUDE.md`를 자동으로 읽는다.

### P1 서버 (김준수): `server/`

```
docs/plan.md의 2절과 docs/week10.md의 "가짜 서버 HTTP API"를 읽고, 10주차 최소 버전 가짜 서버를 만들어 줘.
- server/token_bucket.py: 토큰 버킷 클래스. 현재 시각을 인자로 받는 순수 로직 (asyncio 없음)
- server/mock_llm.py: aiohttp 서버. POST /v1/messages, RPM 버킷 하나, 처리 슬롯 C개와 대기열(초과 시 대기), 버킷이 비면 429 + retry-after
- 지연 = 대기 + TTFT(기본값 + 입력 토큰 비례) + 출력 토큰 수 × 토큰당 시간. 출력 토큰은 시드 고정 분포에서
- 매개변수는 dataclass 설정으로, 시간 축소(분당 한도를 초 단위로) 가능하게
- tests/test_token_bucket.py: 손으로 계산한 값 테스트
먼저 계획을 보여 주고, 확인받은 뒤에 만들어 줘.
```

### P2 컨트롤러 (양동규): `client/`

```
docs/plan.md의 3절과 docs/week10.md의 "컨트롤러 인터페이스"를 읽고 client/controllers.py를 만들어 줘.
- C0 FixedWindow(고정 동시성, 즉시 재시도)와 C2 AIMDWindow(슬로 스타트 + AIMD, 같은 바퀴의 429는 한 번만 반영)
- plan.md의 C2 참고 구현을 바탕으로 인터페이스 메서드를 맞추고, TCP 개념 주석을 한 줄씩
- asyncio나 HTTP 코드는 넣지 말 것 (순수 로직)
- tests/test_controllers.py: cwnd 변화를 손으로 계산한 값으로 테스트 (슬로 스타트 2배, 혼잡 회피 +1, 429 절반, 같은 바퀴 429 무시)
먼저 계획을 보여 주고, 확인받은 뒤에 만들어 줘.
```

### P3 실험 (박준영): `runner/`

```
docs/plan.md의 4절과 docs/week10.md의 "컨트롤러 인터페이스", "요청 로그 CSV"를 읽고 실행기를 만들어 줘.
- runner/workload.py: 시드 고정으로 작업 N개 생성 (입력 토큰, 최대 출력 토큰)
- runner/agent.py: aiohttp로 가짜 서버에 요청하는 비동기 에이전트. 컨트롤러의 can_send/on_send/on_success/on_rate_limited를 부르고, 429면 컨트롤러가 정한 만큼 기다렸다 재시도
- runner/run.py: 명령줄 옵션(--exp, --controller, --seed, --tasks)으로 실행하고 요청 로그 CSV를 results/에 저장
- 컨트롤러가 아직 없으면 같은 인터페이스의 임시 컨트롤러로 먼저 돌려 볼 것
먼저 계획을 보여 주고, 확인받은 뒤에 만들어 줘.
```

### P4 분석·보고서 (김경일): `analysis/`, `reports/`

```
docs/week10.md의 "요청 로그 CSV" 형식을 읽고 분석 도구를 만들어 줘.
- analysis/metrics.py: CSV를 읽어 처리량(분당 완료 수), 429 비율, 완료 시간 p50·p95, Jain 공정성 지수를 계산하는 함수
- analysis/plot.py: 시간에 따른 cwnd 선 그래프 + 429 발생 지점 표시, matplotlib로 PNG 저장
- 실제 데이터가 나오기 전에는 형식에 맞는 가짜 CSV를 만들어 테스트
- tests/test_metrics.py: Jain 지수를 손으로 계산한 값으로 테스트 (예: [1,1] → 1.0, [1,0] → 0.5)
그리고 reports/week10.md에 주간 보고서 틀(이번 주 한 일, 숫자, 문제, 다음 주 할 일)을 만들어 줘.
먼저 계획을 보여 주고, 확인받은 뒤에 만들어 줘.
```

## 주 후반: 연결 확인 (P1 + P2 + P3)

세 사람의 PR이 합쳐지면 한 사람이 이렇게 돌려 본다.

```
python -m server.mock_llm          # 터미널 1
python -m runner.run --exp E1 --controller C2 --tasks 100 --seed 1    # 터미널 2
python -m analysis.plot results/<방금 생긴 파일>.csv
```

**돌리기 전에 예측을 적는다**: "C2면 cwnd가 처음에 2배씩 오르다 429에서 절반이 되고, 이후 톱니 모양." 그래프가 예측과 다르면 그 차이를 주간 보고서에 쓴다.
