"""HTTP와 독립적인 토큰 버킷 및 3종 한도. 시간 단위는 초.

서버는 MultiQuota의 reserve/settle/reconfigure를 같은 lock으로 보호해야 합니다.
"""

import math
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Decision:
    allowed: bool
    remaining: float
    retry_after_seconds: float | None


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    retry_after_seconds: float | None
    limited_by: list[str]
    impossible: bool = False


def validate_cost(value: int, name: str = "cost") -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10**9:
        raise ValueError(f"{name}는 0~1,000,000,000 정수여야 합니다")


def validate_config(capacity: int, refill_rate: float) -> None:
    if isinstance(capacity, bool) or not isinstance(capacity, int) or not 1 <= capacity <= 10**9:
        raise ValueError("capacity는 1~1,000,000,000 정수여야 합니다")
    if (isinstance(refill_rate, bool) or not isinstance(refill_rate, (int, float))
            or not 1e-6 <= refill_rate <= 1e9 or not math.isfinite(refill_rate)):
        raise ValueError("refill_rate는 0.000001~1,000,000,000 유한수여야 합니다")


class TokenBucket:
    def __init__(self, capacity: int, refill_rate: float,
                 clock: Callable[[], float] = time.monotonic):
        validate_config(capacity, refill_rate)
        self.capacity = capacity
        self.refill_rate = refill_rate
        self._clock = clock
        self._tokens = float(capacity)
        self._updated_at = clock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated_at)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate)
        self._updated_at = max(now, self._updated_at)
    def preview(self, cost: int = 1) -> Decision:
        """현재 잔량으로 비용을 감당할 수 있는지 확인. 영구 불가능은 대기 시간 None."""
        validate_cost(cost)
        self._refill()
        if cost > self.capacity:
            return Decision(False, self._tokens, None)
        if self._tokens >= cost:
            return Decision(True, self._tokens, 0.0)
        return Decision(False, self._tokens, (cost - self._tokens) / self.refill_rate)

    def consume(self, cost: int = 1) -> Decision:
        decision = self.preview(cost)
        if decision.allowed:
            self._tokens -= cost
            return Decision(True, self._tokens, 0.0)
        return decision

    def refund(self, amount: int) -> None:
        validate_cost(amount, "amount")
        self._refill()
        self._tokens = min(self.capacity, self._tokens + amount)

    def reconfigure(self, capacity: int | None = None,
                    refill_rate: float | None = None) -> None:
        """기존 속도로 현재까지 충전 후 설정 변경. 증설만으로 잔량이 늘지는 않음."""
        next_capacity = self.capacity if capacity is None else capacity
        next_rate = self.refill_rate if refill_rate is None else refill_rate
        validate_config(next_capacity, next_rate)
        self._refill()
        self.capacity = next_capacity
        self.refill_rate = next_rate
        self._tokens = min(self._tokens, self.capacity)

    def snapshot(self) -> dict:
        self._refill()
        return {"capacity": self.capacity, "refill_rate_per_second": self.refill_rate,
                "remaining": self._tokens}


class MultiQuota:
    """요청 1개·입력 실제량·출력 상한을 함께 검사하고 전부 가능한 경우만 차감."""

    def __init__(self, requests: TokenBucket, inputs: TokenBucket, outputs: TokenBucket):
        self.requests = requests
        self.inputs = inputs
        self.outputs = outputs
        self.buckets = {"requests": requests, "input_tokens": inputs, "output_tokens": outputs}

    def reserve(self, input_tokens: int, max_output_tokens: int) -> QuotaDecision:
        validate_cost(input_tokens, "input_tokens")
        validate_cost(max_output_tokens, "max_output_tokens")
        costs = {"requests": 1, "input_tokens": input_tokens, "output_tokens": max_output_tokens}
        decisions = {name: bucket.preview(costs[name]) for name, bucket in self.buckets.items()}
        limited = [name for name, decision in decisions.items() if not decision.allowed]
        if limited:
            impossible = any(decisions[name].retry_after_seconds is None for name in limited)
            retry = None if impossible else max(decisions[name].retry_after_seconds for name in limited)
            return QuotaDecision(False, retry, limited, impossible)
        for name, bucket in self.buckets.items():
            bucket.consume(costs[name])
        return QuotaDecision(True, 0.0, [], False)

    def settle_output(self, reserved: int, actual: int) -> None:
        """수락 1회당 한 번 호출. 미사용 출력 예약량 환불; 요청/입력은 환불하지 않음."""
        validate_cost(reserved, "reserved")
        validate_cost(actual, "actual")
        if actual > reserved:
            raise ValueError("actual은 reserved를 초과할 수 없습니다")
        self.outputs.refund(reserved - actual)

    def snapshot(self) -> dict:
        return {name: bucket.snapshot() for name, bucket in self.buckets.items()}
