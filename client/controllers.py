"""컨트롤러 C0~C2: 에이전트가 요청을 언제, 몇 개 보낼지 정한다.

순수 로직만 둔다 (asyncio·HTTP 없음). 실행기(runner/agent.py)가 응답을 받을 때마다
아래 메서드를 불러 준다.

    can_send(est_tokens)            -> 지금 하나 더 보내도 되나
    on_send(est_tokens)             -> 요청 번호(req_no)를 돌려줌
    on_success(req_no, resp)        -> 200
    on_rate_limited(req_no, retry_after)  -> 429 (내 한도 초과)
    on_overloaded(req_no)           -> 529 (서버 전체 과부하)
    on_error(req_no)                -> 422·타임아웃·연결 오류 (혼잡 신호 아님)
    wait_before_retry(attempt)      -> 재시도 전에 기다릴 초
    state()                         -> 로그용 {"cwnd", "in_flight", "ssthresh"}

retry_after는 서버 본문의 retry_after_seconds(실수, 초)를 넘겨받는다.
Retry-After 헤더는 1초 이상 정수로 올림돼 있어 쓰지 않는다.
"""

import random
from dataclasses import dataclass


# ---------- 설정 ----------

@dataclass(frozen=True)
class FixedConfig:
    window: int = 8             # 동시에 보낼 요청 수 (고정)


@dataclass(frozen=True)
class BackoffConfig:
    window: int = 8             # 동시에 보낼 요청 수 (고정)
    base: float = 1.0           # 첫 재시도 대기 (초)
    cap: float = 8.0            # 대기 상한 (초)
    jitter: float = 0.1         # 대기를 ±10% 무작위로 흔듦
    seed: int = 0               # 지터 난수 시드


@dataclass(frozen=True)
class AIMDConfig:
    init_cwnd: float = 1.0      # 처음 윈도
    ssthresh: float = 32.0      # 슬로 스타트를 끝낼 기준
    max_window: float = 128.0   # 윈도 상한


# ---------- 공통 뼈대 ----------

class BaseController:
    """요청 번호와 in_flight만 관리한다. 윈도 규칙은 자식 클래스가 정한다."""

    def __init__(self, cwnd: float):
        self.cwnd = cwnd        # 지금 동시에 보낼 수 있는 요청 수
        self.ssthresh = None    # AIMD 계열만 쓴다
        self.in_flight = 0      # 응답을 기다리는 요청 수
        self.sent = 0           # 지금까지 발급한 마지막 요청 번호

    def can_send(self, est_tokens: int = 0) -> bool:
        return self.in_flight < int(self.cwnd)

    def on_send(self, est_tokens: int = 0) -> int:
        self.in_flight += 1
        self.sent += 1
        return self.sent        # 요청 번호를 돌려줘 응답과 짝짓는다

    def _release(self) -> None:
        self.in_flight = max(0, self.in_flight - 1)

    def on_success(self, req_no: int, resp: dict | None = None) -> None:
        self._release()

    def on_rate_limited(self, req_no: int, retry_after: float | None = None) -> None:
        self._release()

    def on_overloaded(self, req_no: int) -> None:
        self._release()

    def on_error(self, req_no: int) -> None:
        self._release()         # 혼잡이 아니므로 윈도는 그대로

    def wait_before_retry(self, attempt: int) -> float:
        return 0.0              # 기본은 즉시 재시도

    def state(self) -> dict:
        return {"cwnd": self.cwnd, "in_flight": self.in_flight, "ssthresh": self.ssthresh}


# ---------- C0: 고정 동시성 + 즉시 재시도 (나쁜 기준선) ----------

class FixedWindow(BaseController):
    """윈도를 바꾸지 않는다. 429·529가 와도 바로 다시 보낸다."""

    def __init__(self, cfg: FixedConfig | None = None):
        self.cfg = cfg or FixedConfig()
        super().__init__(cwnd=self.cfg.window)


# ---------- C1: 고정 동시성 + 지수 백오프·지터 (현업 표준 기준선) ----------

class FixedBackoff(BaseController):
    """동시성은 고정, 실패할 때마다 기다리는 시간만 2배로 늘린다."""

    def __init__(self, cfg: BackoffConfig | None = None):
        self.cfg = cfg or BackoffConfig()
        super().__init__(cwnd=self.cfg.window)
        self.rng = random.Random(self.cfg.seed)   # 같은 시드면 같은 지터

    def wait_before_retry(self, attempt: int) -> float:
        """attempt: 이번이 몇 번째 재시도인지 (첫 재시도 = 1)."""
        if attempt <= 0:
            return 0.0
        exp = min(attempt - 1, 60)                 # 2의 거듭제곱이 너무 커지지 않게
        delay = min(self.cfg.cap, self.cfg.base * 2 ** exp)  # 이진 지수 백오프: 실패마다 2배, 상한에서 멈춤
        j = self.cfg.jitter
        return delay * (1 + self.rng.uniform(-j, j))  # 지터: 여러 에이전트가 같은 순간에 몰리지 않게


# ---------- C2: 슬로 스타트 + AIMD, 요청 수 윈도 (TCP Reno) ----------

class AIMDWindow(BaseController):
    """동시 요청 수를 TCP의 cwnd처럼 조절한다."""

    def __init__(self, cfg: AIMDConfig | None = None):
        self.cfg = cfg or AIMDConfig()
        super().__init__(cwnd=float(self.cfg.init_cwnd))
        self.ssthresh = float(self.cfg.ssthresh)
        self.recover_until = 0  # 이 번호까지는 이미 줄인 바퀴로 본다

    def on_success(self, req_no: int, resp: dict | None = None) -> None:
        self._release()
        if self.cwnd < self.ssthresh:
            self.cwnd += 1                  # 슬로 스타트: 성공마다 +1 → 한 바퀴에 2배
        else:
            self.cwnd += 1 / self.cwnd      # 혼잡 회피: 한 바퀴에 +1 (덧셈 증가)
        self.cwnd = min(self.cwnd, self.cfg.max_window)

    def on_rate_limited(self, req_no: int, retry_after: float | None = None) -> None:
        self._release()
        self._decrease(req_no)              # retry_after는 C2에서 쓰지 않는다 (C5에서 사용)

    def on_overloaded(self, req_no: int) -> None:
        self._release()
        self._decrease(req_no)              # 529도 손실로 보고 429와 같이 절반

    def _decrease(self, req_no: int) -> None:
        if req_no <= self.recover_until:    # 같은 바퀴의 손실은 한 번만 반영
            return
        self.ssthresh = max(self.cwnd / 2, 1.0)
        self.cwnd = self.ssthresh           # 곱셈 감소: 절반으로
        self.recover_until = self.sent      # 지금까지 보낸 요청은 같은 바퀴
