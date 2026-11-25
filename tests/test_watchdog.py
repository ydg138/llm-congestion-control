"""안전장치를 손으로 정한 시각으로 확인한다. 시각은 실험 시작을 0초로 둔다."""

import asyncio
import time

import pytest

from runner.agent import Progress
from runner.watchdog import Limits, RunAborted, check_limits, watch

LIMITS = Limits(max_seconds=100, stall_seconds=3)


def test_stall_counts_from_last_success():
    p = Progress(0.0)
    p.agent_started(0.0)
    p.on_success(5.0)
    assert check_limits(p, 7.0, LIMITS) is None             # 7 - 5 = 2
    assert "완료 없음" in check_limits(p, 8.5, LIMITS)       # 8.5 - 5 = 3.5 > 3


def test_no_stall_while_late_agent_has_not_started():
    p = Progress(0.0)
    p.agent_started(0.0)
    p.on_success(5.0)
    p.agent_finished()                                      # A는 5초에 다 끝남, B는 아직 시작 전
    assert check_limits(p, 20.0, LIMITS) is None            # 15초 조용해도 돌고 있는 에이전트가 없음
    p.agent_started(20.0)                                   # B가 늦게 합류: 여기서부터 센다
    assert check_limits(p, 22.0, LIMITS) is None            # 22 - 20 = 2
    assert "완료 없음" in check_limits(p, 23.5, LIMITS)      # 23.5 - 20 = 3.5 > 3


def test_joining_while_running_does_not_reset_stall():
    p = Progress(0.0)
    p.agent_started(0.0)
    p.on_success(5.0)
    p.agent_started(7.0)                                    # A가 돌고 있을 때 B 합류 (active 1 → 2)
    assert "완료 없음" in check_limits(p, 8.5, LIMITS)       # 기준은 그대로 5초


def test_total_limit_applies_even_when_idle():
    p = Progress(0.0)                                       # 돌고 있는 에이전트 없음
    assert check_limits(p, 99.0, LIMITS) is None
    assert "전체 시간 상한" in check_limits(p, 100.5, LIMITS)


def test_watch_raises_run_aborted():
    p = Progress(time.perf_counter())
    p.agent_started(p.started)
    limits = Limits(max_seconds=100, stall_seconds=0.05, check_seconds=0.01)
    with pytest.raises(RunAborted, match="완료 없음"):
        asyncio.run(asyncio.wait_for(watch(p, limits), 2))
