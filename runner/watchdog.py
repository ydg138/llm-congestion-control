"""실행이 끝나지 않고 멈춰 있는 것을 막는 안전장치.

- 전체 시간 상한(max_seconds): 실행이 이 시간을 넘으면 멈춘다
- 정지 감지(stall_seconds): 돌고 있는 에이전트가 있는데 이 시간 동안 200이 하나도 없으면 멈춘다
  돌고 있는 에이전트가 없는 구간(E3에서 늦은 합류 전이고 나머지는 다 끝남)은 세지 않는다
"""

import asyncio
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    max_seconds: float = 600.0      # 실행 전체 시간 상한
    stall_seconds: float = 30.0     # 이 시간 동안 완료(200)가 없으면 멈춘 것으로 봄
    check_seconds: float = 0.5      # 확인 간격


class RunAborted(Exception):
    """안전장치가 실행을 멈췄다. 메시지에 이유가 들어 있다."""


def check_limits(progress, now: float, limits: Limits) -> str | None:
    """상한을 넘었으면 이유를, 아니면 None을 돌려준다. progress는 runner.agent.Progress."""
    if now - progress.started > limits.max_seconds:
        return f"전체 시간 상한 {limits.max_seconds:g}초를 넘음"
    if progress.active == 0:
        return None                 # 돌고 있는 에이전트가 없으면 정지가 아니다
    # 마지막 성공 이후, 또는 늦게 합류한 에이전트가 시작한 이후부터 센다
    quiet = now - max(progress.last_success, progress.busy_since)
    if quiet > limits.stall_seconds:
        return f"{limits.stall_seconds:g}초 동안 완료 없음"
    return None


async def watch(progress, limits: Limits) -> None:
    """check_seconds마다 확인하다가 상한을 넘으면 RunAborted를 낸다. 문제가 없으면 끝나지 않는다."""
    while True:
        await asyncio.sleep(limits.check_seconds)
        reason = check_limits(progress, time.perf_counter(), limits)
        if reason:
            raise RunAborted(reason)
