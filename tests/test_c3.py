"""C3 TokenAIMD 테스트. 기대값은 docs/c3_design.md 4절 표를 손으로 계산한 값이다."""

import pytest

from client.controllers import TokenAIMD, TokenConfig
from client.factory import make_controller


def ok(inp, out):
    """200 응답 본문 (필요한 필드만)."""
    return {"input_tokens": inp, "output_tokens": out}


def ca(cwnd=1000.0, **kw):
    """혼잡 회피 구간에서 시작하는 C3 (ssthresh = cwnd)."""
    return TokenAIMD(TokenConfig(init_cwnd=cwnd, ssthresh=cwnd, mss=100.0, **kw))


def test_congestion_avoidance_two_successes_of_500():
    # 1,000 + 100×500/1,000 = 1,050 → 1,050 + 100×500/1,050 ≈ 1,097.62 (한 바퀴에 약 +100)
    c = ca()
    nos = [c.on_send(500, 400), c.on_send(500, 400)]
    for no in nos:
        c.on_success(no, ok(400, 100))
    assert c.cwnd == pytest.approx(1097.62, abs=0.01)


def test_congestion_avoidance_ten_successes_of_100():
    # 같은 1,000토큰을 작은 요청 10개로 써도 한 바퀴에 약 +100: 요청 크기와 무관 (토큰 공정성의 핵심)
    c = ca()
    nos = [c.on_send(100, 50) for _ in range(10)]
    for no in nos:
        c.on_success(no, ok(50, 50))
    assert c.cwnd == pytest.approx(1095.86, abs=0.01)


def test_slow_start_adds_actual_cost():
    # 슬로 스타트: 1,000 + 실제 쓴 300 = 1,300
    c = TokenAIMD(TokenConfig(init_cwnd=1000.0))
    no = c.on_send(400, 200)
    c.on_success(no, ok(200, 100))
    assert c.cwnd == 1300.0


def test_rate_limited_halves_window():
    c = TokenAIMD(TokenConfig(init_cwnd=2000.0))
    c.on_rate_limited(c.on_send(500, 100), retry_after=0.5)
    assert c.cwnd == 1000.0 and c.ssthresh == 1000.0


def test_same_round_losses_halve_once():
    # 2,000에서 보낸 4개가 모두 429 → 한 번만 절반 (1,000). 줄인 뒤 보낸 요청의 429는 새 바퀴 → 500
    c = TokenAIMD(TokenConfig(init_cwnd=2000.0, min_cwnd=100.0))
    nos = [c.on_send(400, 100) for _ in range(4)]
    for no in nos:
        c.on_rate_limited(no)
    assert c.cwnd == 1000.0
    c.on_overloaded(c.on_send(400, 100))     # 529도 손실
    assert c.cwnd == 500.0


def test_big_request_allowed_when_nothing_in_flight():
    # 예약 중인 게 없으면 cwnd 500이어도 1,200짜리 하나는 보낸다
    c = TokenAIMD(TokenConfig(init_cwnd=500.0))
    assert c.can_send(1200, 1000)
    c.on_send(1200, 1000)
    assert c.in_flight == 1200.0
    assert not c.can_send(100, 50)           # 이미 1,200 > 500


def test_window_blocks_when_full():
    # in_flight 1,200 + 400 = 1,600 > 1,500 → 못 보냄. 300이면 1,500 ≤ 1,500 → 보냄
    c = TokenAIMD(TokenConfig(init_cwnd=1500.0))
    c.on_send(1200, 1000)
    assert not c.can_send(400, 300)
    assert c.can_send(300, 200)


def test_every_response_settles_reservation():
    # 200·429·오류 모두 예약량만큼 in_flight가 준다
    c = TokenAIMD(TokenConfig(init_cwnd=5000.0, min_cwnd=100.0))
    a, b, d = c.on_send(300, 200), c.on_send(500, 400), c.on_send(700, 600)
    assert c.in_flight == 1500.0
    c.on_success(a, ok(200, 50))
    assert c.in_flight == 1200.0
    c.on_rate_limited(b)
    assert c.in_flight == 700.0
    c.on_error(d)
    assert c.in_flight == 0.0 and c.state()["in_flight_reqs"] == 0


def test_error_keeps_window():
    c = TokenAIMD(TokenConfig(init_cwnd=2000.0))
    c.on_error(c.on_send(500, 100))
    assert c.cwnd == 2000.0


def test_ewma_output_estimate():
    # 출력 100 → 예상 100 (첫 값), 출력 200 → 100 + 0.125 × (200 − 100) = 112.5
    c = TokenAIMD(TokenConfig(estimate="ewma"))
    c.on_success(c.on_send(164, 100), ok(100, 100))
    c.on_success(c.on_send(164, 100), ok(100, 200))
    assert c.est_output == 112.5


def test_ewma_reserves_input_plus_estimate():
    # 첫 요청은 예상이 없어 상한(est_tokens)으로, 그다음부터 입력 + 예상 출력
    c = TokenAIMD(TokenConfig(estimate="ewma"))
    no = c.on_send(1100, 100)
    assert c.in_flight == 1100.0
    c.on_success(no, ok(100, 112))
    c.on_send(1100, 100)
    assert c.in_flight == 212.0              # 100 + 112


def test_max_reserves_upper_bound_even_after_responses():
    # C3-max는 예상을 갖고 있어도 예약은 항상 입력 + 출력 상한
    c = TokenAIMD(TokenConfig(estimate="max"))
    c.on_success(c.on_send(1100, 100), ok(100, 112))
    c.on_send(1100, 100)
    assert c.in_flight == 1100.0


def test_min_and_max_window():
    c = TokenAIMD(TokenConfig(init_cwnd=600.0, min_cwnd=500.0))
    c.on_rate_limited(c.on_send(100, 50))
    assert c.cwnd == 500.0                   # 300이 아니라 하한 500
    c = TokenAIMD(TokenConfig(init_cwnd=900.0, max_cwnd=1000.0))
    c.on_success(c.on_send(500, 100), ok(100, 400))
    assert c.cwnd == 1000.0                  # 1,400이 아니라 상한 1,000


def test_factory_names():
    assert make_controller("C3").cfg.estimate == "max"
    assert make_controller("C3-ewma").cfg.estimate == "ewma"
    assert make_controller("C3-oracle").cfg.estimate == "max"   # oracle은 실행기가 실제 출력을 넘김
    assert make_controller("C3", mss=200.0).cfg.mss == 200.0
    with pytest.raises(ValueError):
        make_controller("C3-8")
