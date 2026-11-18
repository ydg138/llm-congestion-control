"""컨트롤러 C0·C1·C2·C4·C5를 손으로 계산한 값으로 확인한다."""

import pytest

from client.controllers import (
    AIMDConfig, AIMDWindow, BackoffConfig, DelayBased, DelayConfig, ExplicitAIMD, FixedBackoff,
    FixedConfig, FixedWindow,
)
from client.factory import NAMES, make_controller


def send_all(c):
    """윈도가 허락하는 만큼 보내고 요청 번호 목록을 돌려준다."""
    nos = []
    while c.can_send():
        nos.append(c.on_send())
    return nos


# ---------- C0 ----------

def test_c0_window_4_sends_only_4():
    c = FixedWindow(FixedConfig(window=4))
    assert send_all(c) == [1, 2, 3, 4]
    c.on_rate_limited(1, 0.1)               # 429가 와도 윈도는 그대로
    assert c.cwnd == 4
    assert send_all(c) == [5]               # 하나 빠진 자리만큼 다시 보냄
    assert c.wait_before_retry(1) == 0.0    # 즉시 재시도


# ---------- C2 ----------

def test_c2_slow_start_doubles_each_round():
    c = AIMDWindow(AIMDConfig(init_cwnd=1, ssthresh=32))
    seen = []
    for _ in range(4):
        seen.append(c.cwnd)
        for no in send_all(c):
            c.on_success(no, {})
    assert seen == [1, 2, 4, 8]             # 바퀴마다 2배
    assert c.cwnd == 16


def test_c2_congestion_avoidance_plus_one_per_round():
    # 4 → 4.25 → 4.4853 → 4.7082 → 4.9206 (매번 +1/cwnd)
    c = AIMDWindow(AIMDConfig(init_cwnd=4, ssthresh=4))
    for no in send_all(c):
        c.on_success(no, {})
    assert c.cwnd == pytest.approx(4.9206, abs=1e-4)


def test_c2_429_halves():
    c = AIMDWindow(AIMDConfig(init_cwnd=8, ssthresh=32))
    nos = send_all(c)
    c.on_rate_limited(nos[0], 0.1)
    assert c.cwnd == 4 and c.ssthresh == 4


def test_c2_same_round_429_counted_once():
    c = AIMDWindow(AIMDConfig(init_cwnd=8, ssthresh=32))
    nos = send_all(c)                       # 요청 1~8
    for no in nos:                          # 8개 모두 429
        c.on_rate_limited(no, 0.1)
    assert c.cwnd == 4                      # 한 번만 줄어 4 (8 → 4, 그 뒤는 무시)
    assert c.in_flight == 0
    new = c.on_send()                       # 줄인 뒤 보낸 요청 9
    c.on_rate_limited(new, 0.1)
    assert c.cwnd == 2                      # 새 바퀴의 429는 다시 반영


def test_c2_529_halves_like_429():
    c = AIMDWindow(AIMDConfig(init_cwnd=8, ssthresh=32))
    nos = send_all(c)
    c.on_overloaded(nos[0])
    assert c.cwnd == 4


def test_c2_never_below_1_and_capped_at_max():
    c = AIMDWindow(AIMDConfig(init_cwnd=1, ssthresh=32))
    c.on_rate_limited(c.on_send())
    assert c.cwnd == 1.0                    # 1 / 2 = 0.5 이지만 최소 1
    c = AIMDWindow(AIMDConfig(init_cwnd=10, ssthresh=100, max_window=10))
    c.on_success(c.on_send())
    assert c.cwnd == 10                     # 상한


def test_error_releases_without_changing_window():
    c = AIMDWindow(AIMDConfig(init_cwnd=4, ssthresh=32))
    no = c.on_send()
    c.on_error(no)                          # 422·타임아웃은 혼잡 신호가 아님
    assert c.cwnd == 4 and c.in_flight == 0


# ---------- C1 ----------

def test_c1_backoff_doubles_and_stops_at_cap():
    c = FixedBackoff(BackoffConfig(window=4, base=1.0, cap=8.0, jitter=0.0))
    assert [c.wait_before_retry(a) for a in range(1, 6)] == [1, 2, 4, 8, 8]
    assert c.wait_before_retry(0) == 0.0


def test_c1_jitter_within_range_and_same_seed_same_values():
    cfg = BackoffConfig(base=1.0, cap=8.0, jitter=0.1, seed=7)
    a = FixedBackoff(cfg).wait_before_retry(3)
    b = FixedBackoff(cfg).wait_before_retry(3)
    assert a == b                           # 같은 시드면 같은 지터
    assert 3.6 <= a <= 4.4                  # 4초 ± 10%


# ---------- C4 ----------

def ok(ttft):
    """서버 200 응답 본문 중 C4가 읽는 부분."""
    return {"ttft_seconds": ttft}


def test_c4_ewma():
    c = DelayBased()
    c.on_success(c.on_send(), ok(0.1))
    assert c.ttft_ewma == 0.1 and c.min_ttft == 0.1      # 첫 값은 그대로
    c.on_success(c.on_send(), ok(0.2))
    assert c.ttft_ewma == pytest.approx(0.1125)           # 0.1 + 0.125 × (0.2 - 0.1)
    assert c.min_ttft == 0.1


def test_c4_no_congestion_plus_one_per_round():
    c = DelayBased(DelayConfig(init_cwnd=4))
    for no in send_all(c):                                # 요청 1~4, TTFT 그대로
        c.on_success(no, ok(0.1))
    assert c.cwnd == 5                                    # 4 × 1.0 + 1, 한 바퀴에 한 번만


def test_c4_congestion_reduces_by_gradient():
    c = DelayBased(DelayConfig(init_cwnd=8))
    c.min_ttft, c.ttft_ewma = 0.1, 0.2                    # TTFT가 기준의 2배
    c.on_success(c.on_send(), ok(0.2))                    # EWMA는 0.2 그대로
    assert c.cwnd == pytest.approx(5)                     # 8 × 0.5 + 1


def test_c4_gradient_clamped_at_half():
    c = DelayBased(DelayConfig(init_cwnd=8))
    c.min_ttft, c.ttft_ewma = 0.1, 0.5                    # 계산상 0.2
    c.on_success(c.on_send(), ok(0.5))
    assert c.cwnd == pytest.approx(5)                     # 0.5로 막혀 8 × 0.5 + 1


def test_c4_429_halves_once_per_round():
    c = DelayBased(DelayConfig(init_cwnd=8))
    for no in send_all(c):                                # 요청 1~8 모두 429
        c.on_rate_limited(no, 0.1)
    assert c.cwnd == 4


def test_c4_ignores_response_without_ttft():
    c = DelayBased(DelayConfig(init_cwnd=4))
    c.on_success(c.on_send(), {})
    assert c.cwnd == 4 and c.ttft_ewma is None and c.in_flight == 0


def test_c4_state_has_ttft():
    c = DelayBased()
    c.on_success(c.on_send(), ok(0.1))
    s = c.state()
    assert s["min_ttft"] == 0.1 and s["ttft_ewma"] == 0.1


# ---------- C5 ----------

class FakeClock:
    """테스트에서 시각을 직접 정하는 시계."""
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def test_c5_pauses_for_retry_after():
    clock = FakeClock(10.0)
    c = ExplicitAIMD(AIMDConfig(init_cwnd=8, ssthresh=32), clock=clock)
    for no in send_all(c):                                # 8개 모두 429, 10.0 + 0.5 = 10.5까지 쉼
        c.on_rate_limited(no, 0.5)
    assert c.cwnd == 4 and c.in_flight == 0               # C2와 같이 절반 (같은 바퀴는 한 번)
    clock.now = 10.3
    assert not c.can_send()
    clock.now = 10.5
    assert c.can_send()


def test_c5_no_retry_after_no_pause():
    c = ExplicitAIMD(AIMDConfig(init_cwnd=2, ssthresh=32), clock=FakeClock(10.0))
    for no in send_all(c):                                # 요청 1·2 모두 retry_after 없는 429
        c.on_rate_limited(no, None)
    assert c.cwnd == 1 and c.pause_until == 0.0
    assert c.can_send()                                   # 쉬지 않고 바로 보냄 (C2와 같음)


def test_c5_same_round_pause_extends_but_halves_once():
    clock = FakeClock(10.0)
    c = ExplicitAIMD(AIMDConfig(init_cwnd=8, ssthresh=32), clock=clock)
    nos = send_all(c)
    c.on_rate_limited(nos[0], 0.5)
    c.on_rate_limited(nos[1], 0.8)
    assert c.cwnd == 4                                    # 한 번만 절반
    assert c.pause_until == pytest.approx(10.8)           # 더 늦은 쪽까지 쉼


def test_c5_remaining_header_caps_window():
    c = ExplicitAIMD(AIMDConfig(init_cwnd=8, ssthresh=4), clock=FakeClock(0.0))
    nos = [c.on_send() for _ in range(4)]                 # 4개 보냄
    c.on_success(nos[0], {"headers": {"X-RateLimit-Remaining": "2"}})
    assert c.limit == 5                                   # 기다리는 3 + 남은 2
    assert send_all(c) == [5, 6]                          # cwnd는 8이지만 5개까지만
    assert c.in_flight == 5 and not c.can_send()


def test_c5_zero_remaining_still_sends_one_when_idle():
    c = ExplicitAIMD(AIMDConfig(init_cwnd=4, ssthresh=32), clock=FakeClock(0.0))
    c.on_success(c.on_send(), {"headers": {"x-ratelimit-remaining": "0"}})
    assert c.limit == 0 and c.in_flight == 0
    assert c.can_send()                                   # 멈춤 방지: 1개는 보냄
    c.on_send()
    assert not c.can_send()


def test_c5_without_headers_acts_like_c2():
    c = ExplicitAIMD(AIMDConfig(init_cwnd=1, ssthresh=32), clock=FakeClock(0.0))
    for _ in range(3):
        for no in send_all(c):
            c.on_success(no, {})
    assert c.cwnd == 8 and c.limit is None                # 1 → 2 → 4 → 8


# ---------- factory ----------

def test_factory_names():
    for name in NAMES:
        assert make_controller(name) is not None
    assert isinstance(make_controller("C0-32"), FixedWindow)
    assert make_controller("C0-32").cwnd == 32
    assert make_controller("C1", seed=5).cfg.seed == 5
    assert make_controller("C2", ssthresh=16).ssthresh == 16
    with pytest.raises(ValueError):
        make_controller("C9")


def test_on_send_accepts_token_args_for_all_controllers():
    """실행기는 모든 컨트롤러에 est_tokens·input_tokens를 똑같이 넘긴다. C0~C5 모두 받아야 한다."""
    from client.factory import NAMES, make_controller
    for name in NAMES:
        c = make_controller(name)
        assert c.can_send(164, input_tokens=100)
        assert c.on_send(164, input_tokens=100) == 1     # 요청 번호는 그대로 1부터
        assert c.in_flight == 1                           # 요청 수 윈도는 토큰과 무관하게 1 증가
