"""컨트롤러 C0·C1·C2를 손으로 계산한 값으로 확인한다."""

import pytest

from client.controllers import (
    AIMDConfig, AIMDWindow, BackoffConfig, FixedBackoff, FixedConfig, FixedWindow,
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
