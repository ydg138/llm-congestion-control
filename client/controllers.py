"""컨트롤러 C0~C5: 에이전트가 요청을 언제, 몇 개 보낼지 정한다.

순수 로직만 둔다 (asyncio·HTTP 없음). 실행기(runner/agent.py)가 응답을 받을 때마다
아래 메서드를 불러 준다.

    can_send(est_tokens, input_tokens)  -> 지금 하나 더 보내도 되나
    on_send(est_tokens, input_tokens)   -> 요청 번호(req_no)를 돌려줌
    (est_tokens = 입력 + 출력 상한, input_tokens = 입력. 토큰 인자는 C3만 쓴다)
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
import time
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


@dataclass(frozen=True)
class DelayConfig:
    init_cwnd: float = 4.0      # 처음 윈도
    alpha: float = 0.125        # TTFT EWMA 가중치 (TCP RTT 추정과 같은 1/8)
    headroom: float = 1.0       # 한 바퀴마다 더하는 여유 (안 붐비면 +1)
    min_gradient: float = 0.5   # 한 번에 최대 절반까지만 줄임
    max_window: float = 128.0   # 윈도 상한


@dataclass(frozen=True)
class TokenConfig:
    init_cwnd: float = 2000.0       # 처음 토큰 윈도
    ssthresh: float = 1_000_000.0   # 크게 두고 첫 손실로 찾게 한다 (RFC 5681)
    mss: float = 500.0              # 혼잡 회피에서 한 바퀴에 늘리는 토큰 (모든 에이전트 같은 값)
    min_cwnd: float = 500.0         # 줄여도 이 아래로는 안 감
    max_cwnd: float = 10_000_000.0  # 토큰 윈도 상한
    alpha: float = 0.125            # 출력 길이 EWMA 가중치 (TCP RTT 추정과 같은 1/8)
    estimate: str = "max"           # 예약량: "max"(입력 + 출력 상한) 또는 "ewma"(입력 + 예상 출력)


# ---------- 공통 뼈대 ----------

class BaseController:
    """요청 번호와 in_flight만 관리한다. 윈도 규칙은 자식 클래스가 정한다."""

    def __init__(self, cwnd: float):
        self.cwnd = cwnd        # 지금 동시에 보낼 수 있는 요청 수
        self.ssthresh = None    # AIMD 계열만 쓴다
        self.in_flight = 0      # 응답을 기다리는 요청 수
        self.sent = 0           # 지금까지 발급한 마지막 요청 번호

    def can_send(self, est_tokens: int = 0, input_tokens: int = 0) -> bool:
        return self.in_flight < int(self.cwnd)

    def on_send(self, est_tokens: int = 0, input_tokens: int = 0) -> int:
        """est_tokens·input_tokens는 C3(토큰 윈도)만 쓴다. 나머지는 받기만 하고 무시한다."""
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


# ---------- C3: 토큰 가중 AIMD (docs/c3_design.md) ----------

class TokenAIMD(BaseController):
    """윈도를 요청 수가 아니라 토큰으로 잡는 AIMD.

    보낼 때 토큰을 예약하고, 응답이 오면 정산한다. 혼잡 회피에서는 요청 크기·개수와 상관없이
    한 바퀴에 mss 토큰씩 늘린다. 그래서 크기가 다른 에이전트도 같은 토큰 몫으로 모인다.
    한 바퀴 = 바퀴를 시작할 때까지 보낸 요청이 돌아오는 동안 (C4의 round_end와 같은 방식).
    cwnd·in_flight·ssthresh는 모두 토큰 단위다.
    """

    def __init__(self, cfg: TokenConfig | None = None):
        self.cfg = cfg or TokenConfig()
        super().__init__(cwnd=float(self.cfg.init_cwnd))
        self.ssthresh = float(self.cfg.ssthresh)
        self.reserved = {}          # 요청 번호 → 예약한 토큰
        self.recover_until = 0      # 이 번호까지는 이미 줄인 바퀴로 본다
        self.round_end = 0          # 이 번호까지 보낸 요청은 이미 늘린 바퀴로 본다
        self.est_output = None      # 출력 길이 EWMA (C3-ewma, 첫 응답 전에는 None)

    def _reserve(self, est_tokens: int, input_tokens: int) -> float:
        """이번 요청에 예약할 토큰. 출력 길이는 끝나야 알므로 상한이나 예상값으로 잡는다."""
        if self.cfg.estimate == "ewma" and self.est_output is not None:
            return input_tokens + self.est_output
        return float(est_tokens)    # 입력 + 출력 상한 (서버가 실제로 꺼내는 양과 같음)

    def can_send(self, est_tokens: int = 0, input_tokens: int = 0) -> bool:
        if not self.reserved:       # 예약 중인 게 없으면 큰 요청도 하나는 보낸다
            return True
        return self.in_flight + self._reserve(est_tokens, input_tokens) <= self.cwnd

    def on_send(self, est_tokens: int = 0, input_tokens: int = 0) -> int:
        self.sent += 1
        amount = self._reserve(est_tokens, input_tokens)
        self.reserved[self.sent] = amount
        self.in_flight += amount    # 보냈지만 응답이 안 온 토큰
        return self.sent

    def _settle(self, req_no: int) -> None:
        """응답이 오면(종류와 상관없이) 예약했던 토큰을 돌려받는다."""
        self.in_flight = max(0.0, self.in_flight - self.reserved.pop(req_no, 0.0))

    def on_success(self, req_no: int, resp: dict | None = None) -> None:
        reserved = self.reserved.get(req_no, 0.0)
        self._settle(req_no)
        resp = resp or {}
        output = resp.get("output_tokens")
        if output is None:          # 응답에 토큰 수가 없으면 예약한 만큼 쓴 것으로 본다
            cost = reserved
        else:
            cost = resp.get("input_tokens", 0) + output    # 실제 쓴 토큰 (ACK된 바이트)
            # 출력 길이 예상: 4주차 RTT 추정과 같은 EWMA
            if self.est_output is None:
                self.est_output = float(output)
            else:
                self.est_output += self.cfg.alpha * (output - self.est_output)
        if self.cwnd < self.ssthresh:
            self.cwnd += min(cost, self.cwnd)   # 슬로 스타트: 실제 쓴 토큰만큼, 한 번에 최대 2배
        elif req_no > self.round_end:           # 이번 바퀴에서 처음 돌아온 성공
            self.cwnd += self.cfg.mss           # 혼잡 회피: 한 바퀴에 +mss 토큰 (덧셈 증가, 요청 크기와 무관)
            self.round_end = self.sent          # 지금까지 보낸 요청이 다 돌아와야 다음 바퀴
        self.cwnd = min(self.cwnd, self.cfg.max_cwnd)

    def on_rate_limited(self, req_no: int, retry_after: float | None = None) -> None:
        self._settle(req_no)
        self._decrease(req_no)

    def on_overloaded(self, req_no: int) -> None:
        self._settle(req_no)
        self._decrease(req_no)      # 529도 손실로 보고 절반

    def on_error(self, req_no: int) -> None:
        self._settle(req_no)        # 혼잡이 아니므로 윈도는 그대로

    def _decrease(self, req_no: int) -> None:
        if req_no <= self.recover_until:    # 같은 바퀴의 손실은 한 번만 반영
            return
        self.ssthresh = max(self.cwnd / 2, self.cfg.min_cwnd)
        self.cwnd = self.ssthresh           # 곱셈 감소: 절반으로
        self.recover_until = self.sent      # 지금까지 보낸 요청은 같은 바퀴
        self.round_end = self.sent          # 줄인 바퀴에서는 늘리지 않는다

    def state(self) -> dict:
        s = super().state()
        s.update(est_output=self.est_output, in_flight_reqs=len(self.reserved))
        return s


# ---------- C4: 지연 기반 (TCP Vegas, 넷플릭스 gradient) ----------

class DelayBased(BaseController):
    """TTFT가 늘기 시작하면 429·529가 오기 전에 미리 윈도를 줄인다.

    TTFT에는 서버 대기열에서 기다린 시간이 들어 있어 붐비면 늘어난다.
    전체 응답 시간은 출력이 길어도 늘어나므로 혼잡 신호로 쓰지 않는다.
    """

    def __init__(self, cfg: DelayConfig | None = None):
        self.cfg = cfg or DelayConfig()
        super().__init__(cwnd=float(self.cfg.init_cwnd))
        self.min_ttft = None    # 지금까지 본 가장 작은 TTFT = 안 붐빌 때 (Vegas의 BaseRTT)
        self.ttft_ewma = None   # 최근 TTFT의 지수 이동 평균
        self.round_end = 0      # 이 번호까지는 이미 조정한 바퀴로 본다
        self.recover_until = 0  # 이 번호까지는 이미 줄인 바퀴로 본다 (429·529용)

    def on_success(self, req_no: int, resp: dict | None = None) -> None:
        self._release()
        ttft = (resp or {}).get("ttft_seconds")
        if ttft is None:
            return
        self._update_ttft(ttft)
        if req_no <= self.round_end:        # 윈도 조정은 한 바퀴에 한 번
            return
        gradient = self.min_ttft / self.ttft_ewma if self.ttft_ewma > 0 else 1.0
        gradient = max(self.cfg.min_gradient, min(1.0, gradient))
        # 넷플릭스 gradient: 안 붐비면(1.0) +headroom, TTFT가 늘어난 만큼 줄임
        self.cwnd = self.cwnd * gradient + self.cfg.headroom
        self.cwnd = max(1.0, min(self.cwnd, self.cfg.max_window))
        self.round_end = self.sent          # 지금까지 보낸 요청은 같은 바퀴

    def _update_ttft(self, ttft: float) -> None:
        if self.ttft_ewma is None:
            self.min_ttft = self.ttft_ewma = ttft
            return
        self.min_ttft = min(self.min_ttft, ttft)
        a = self.cfg.alpha
        self.ttft_ewma = (1 - a) * self.ttft_ewma + a * ttft   # EWMA: 4주차 RTT 추정과 같은 방식

    def on_rate_limited(self, req_no: int, retry_after: float | None = None) -> None:
        self._release()
        self._decrease(req_no)              # 지연 신호가 놓친 경우의 안전장치 (Vegas도 손실에 반응)

    def on_overloaded(self, req_no: int) -> None:
        self._release()
        self._decrease(req_no)

    def _decrease(self, req_no: int) -> None:
        if req_no <= self.recover_until:    # 같은 바퀴의 손실은 한 번만 반영
            return
        self.cwnd = max(self.cwnd / 2, 1.0) # 곱셈 감소: 절반으로
        self.recover_until = self.sent
        self.round_end = self.sent          # 이번 바퀴에는 지연으로 다시 조정하지 않음

    def state(self) -> dict:
        s = super().state()
        s.update(min_ttft=self.min_ttft, ttft_ewma=self.ttft_ewma)   # E5 그래프용
        return s


# ---------- C5: AIMD + 명시적 신호 (ECN과 비교) ----------

def _header(resp: dict | None, name: str):
    """resp["headers"]에서 대소문자 구분 없이 헤더 값을 찾는다. 없으면 None."""
    headers = (resp or {}).get("headers") or {}
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


class ExplicitAIMD(AIMDWindow):
    """C2를 그대로 쓰고, 서버가 알려 주는 두 가지 신호를 더한다.

    - 429의 retry_after: 그 시간 동안 새 요청을 아예 보내지 않는다
    - 200의 X-RateLimit-Remaining: 남은 요청 수만큼만 더 보낸다

    실행기는 on_success에 resp = {**본문, "headers": 응답 헤더}를 넘긴다.
    시간은 clock()으로 읽는다. 테스트에서는 가짜 시계를 넣는다.
    """

    def __init__(self, cfg: AIMDConfig | None = None, clock=time.monotonic):
        super().__init__(cfg)
        self.clock = clock
        self.pause_until = 0.0  # 이 시각 전에는 보내지 않는다 (retry-after)
        self.limit = None       # 남은 한도로 정한 동시 요청 상한 (None이면 상한 없음)

    def can_send(self, est_tokens: int = 0, input_tokens: int = 0) -> bool:
        if self.clock() < self.pause_until:         # 서버가 기다리라고 한 시간
            return False
        if self.limit is not None and self.in_flight > 0 and self.in_flight >= self.limit:
            return False                            # 남은 한도만큼 이미 보냄 (기다리는 요청이 없으면 1개는 허용)
        return super().can_send(est_tokens, input_tokens)

    def on_success(self, req_no: int, resp: dict | None = None) -> None:
        super().on_success(req_no, resp)            # C2와 같이 cwnd를 늘림
        remaining = _header(resp, "X-RateLimit-Remaining")
        if remaining is not None:
            # ECN처럼 서버가 알려 준 여유: 지금 기다리는 요청 + 남은 요청 수까지만
            self.limit = self.in_flight + int(float(remaining))

    def on_rate_limited(self, req_no: int, retry_after: float | None = None) -> None:
        super().on_rate_limited(req_no, retry_after)    # C2와 같이 절반 (같은 바퀴는 한 번)
        if retry_after:
            self.pause_until = max(self.pause_until, self.clock() + retry_after)

    def state(self) -> dict:
        s = super().state()
        s.update(limit=self.limit, pause_until=self.pause_until)
        return s
