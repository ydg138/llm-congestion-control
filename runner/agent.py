"""에이전트 하나: 작업 목록을 컨트롤러가 허락하는 만큼 서버에 보낸다.

동작 순서 (TCP 송신 측과 같다)
1. 윈도에 여유가 있으면(can_send) 줄 맨 앞 작업을 보낸다
2. 응답이 오면 종류에 맞는 컨트롤러 메서드를 부른다 (report)
3. 429·529면 같은 작업을 줄 맨 앞에 다시 넣는다 (재전송을 새 작업보다 먼저)
4. 모든 작업이 끝날 때까지 반복
"""

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import NamedTuple

import aiohttp


class AgentSpec(NamedTuple):
    """에이전트 한 줄 설정. 튜플 (agent_id, agent_class, controller, start_delay)로 써도 된다."""
    agent_id: str
    agent_class: str            # "light" 또는 "heavy_in" (runner/workload.py의 AGENT_CLASSES)
    controller: str             # make_controller에 넘길 이름 (예: "C2")
    start_delay: float = 0.0    # 실험 시작 후 몇 초 뒤에 합류하나 (E3)


@dataclass(frozen=True)
class AgentConfig:
    poll_seconds: float = 0.005     # can_send를 다시 확인하는 간격
    timeout_seconds: float = 60.0   # 요청 하나를 기다리는 최대 시간


class Progress:
    """모든 에이전트가 함께 쓰는 진행 기록. 안전장치(watchdog)가 읽는다. 시각은 perf_counter 값."""

    def __init__(self, now: float):
        self.started = now          # 실험 시작 시각 (요청 로그 t_send·t_done의 기준)
        self.last_success = now     # 마지막 200을 받은 시각
        self.active = 0             # 시작했고 아직 끝나지 않은 에이전트 수
        self.busy_since = now       # active가 0에서 1이 된 시각
        self.completed = 0          # 200을 받은 작업 수 (모든 에이전트 합)

    def agent_started(self, now: float) -> None:
        if self.active == 0:
            self.busy_since = now
        self.active += 1

    def agent_finished(self) -> None:
        self.active -= 1

    def on_success(self, now: float) -> None:
        self.last_success = now
        self.completed += 1


def report(controller, req_no: int, status, resp: dict) -> bool:
    """응답 종류에 맞는 컨트롤러 메서드를 부른다. 다시 보내야 하면 True."""
    if status == 200:
        controller.on_success(req_no, resp)                                  # ACK
        return False
    if status == 429:
        controller.on_rate_limited(req_no, resp.get("retry_after_seconds"))  # 손실: 내 한도 초과
        return True
    if status == 529:
        controller.on_overloaded(req_no)                                     # 손실: 서버 전체 과부하
        return True
    controller.on_error(req_no)     # 422·타임아웃·연결 오류: 혼잡이 아니고, 다시 보내도 소용없음
    return False


async def post_generate(session, url: str, body: dict) -> tuple:
    """POST /generate. (상태, {**본문, "headers": 응답 헤더})를 돌려준다.

    응답을 못 받으면 ("timeout" 또는 "conn_error", {}).
    """
    try:
        async with session.post(url, json=body) as r:
            data = await r.json(content_type=None)
            return r.status, {**data, "headers": dict(r.headers)}
    except TimeoutError:
        return "timeout", {}
    except (aiohttp.ClientError, ValueError):
        return "conn_error", {}


class Agent:
    """에이전트 하나. Agent(...).run()으로 돌린다."""

    def __init__(self, spec: AgentSpec, tasks, controller, base_url: str, log, progress: Progress,
                 cfg: AgentConfig | None = None):
        self.spec = spec
        self.controller = controller
        self.url = base_url + "/generate"
        self.log = log
        self.progress = progress
        self.cfg = cfg or AgentConfig()
        self.pending = deque((task, 0) for task in tasks)   # 보낼 차례인 (작업, 재시도 횟수)
        self.running = set()                                # 응답 대기 또는 재시도 대기 중인 태스크
        self.errors = []                                    # 하위 태스크에서 난 예외 (코드 버그)

    async def run(self) -> None:
        await asyncio.sleep(self.spec.start_delay)          # 늦은 합류 (E3)
        self.progress.agent_started(time.perf_counter())
        connector = aiohttp.TCPConnector(limit=0)           # 연결 수 제한 없음: 동시 요청 수는 윈도만 정한다
        timeout = aiohttp.ClientTimeout(total=self.cfg.timeout_seconds)
        try:
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                try:
                    await self._send_loop(session)
                finally:
                    children = list(self.running)           # 취소돼도 보낸 요청·재시도 대기를 남기지 않는다
                    for t in children:
                        t.cancel()
                    await asyncio.gather(*children, return_exceptions=True)
        finally:
            self.progress.agent_finished()

    async def _send_loop(self, session) -> None:
        while self.pending or self.running:
            if self.errors:
                raise self.errors[0]
            if self.pending and self.controller.can_send(self.pending[0][0].est_tokens):
                task, attempt = self.pending.popleft()
                # on_send를 여기서 바로 불러야 in_flight가 곧바로 늘어 윈도보다 많이 보내지 않는다
                # 토큰 인자는 모든 컨트롤러에 똑같이 넘긴다 (C3만 쓰고 나머지는 무시)
                req_no = self.controller.on_send(task.est_tokens, input_tokens=task.input_tokens)
                t = asyncio.create_task(self._attempt(session, task, attempt, req_no))
                self.running.add(t)
                t.add_done_callback(self._finished)
            else:
                await asyncio.sleep(self.cfg.poll_seconds)  # C5의 can_send는 시간만 지나도 참이 되므로 폴링
        if self.errors:                                     # 마지막 작업에서 난 예외는 루프가 끝난 뒤에야 보인다
            raise self.errors[0]

    def _finished(self, t: asyncio.Task) -> None:
        self.running.discard(t)
        if not t.cancelled() and t.exception() is not None:
            self.errors.append(t.exception())

    async def _attempt(self, session, task, attempt: int, req_no: int) -> None:
        """요청 한 번: 보내고, 컨트롤러에 알리고, 로그를 쓰고, 필요하면 다시 줄에 넣는다."""
        body = {
            "request_id": f"{task.task_id}-a{attempt}",     # 시도마다 새 값
            "agent_id": self.spec.agent_id,
            "workload_id": task.task_id,                     # 재시도해도 같은 값 → 같은 출력 길이
            "input_tokens": task.input_tokens,
            "max_output_tokens": task.max_output_tokens,     # 재시도해도 같은 값
        }
        t_send = time.perf_counter()
        status, resp = await post_generate(session, self.url, body)
        t_done = time.perf_counter()
        # 여기부터 줄에 다시 넣을 때까지 await가 없다 → 송신 루프가 끼어들어 새 작업을 먼저 보내지 못한다
        retry = report(self.controller, req_no, status, resp)
        if status == 200:
            self.progress.on_success(t_done)
        self._write_log(task, attempt, req_no, t_send, t_done, status, resp)
        if not retry:
            return
        wait = self.controller.wait_before_retry(attempt + 1)   # attempt+1 = 이번이 몇 번째 재시도인지
        if wait > 0:
            await asyncio.sleep(wait)                           # 백오프 (C1)
        self.pending.appendleft((task, attempt + 1))            # 재전송은 새 작업보다 먼저

    def _write_log(self, task, attempt, req_no, t_send, t_done, status, resp) -> None:
        state = self.controller.state()                         # 응답을 처리한 직후의 윈도
        ok = status == 200
        ttft = resp.get("ttft_seconds") if ok else None
        started = self.progress.started
        self.log.write({
            "controller": self.spec.controller, "agent_id": self.spec.agent_id,
            "agent_class": self.spec.agent_class, "task_id": task.task_id,
            "req_no": req_no, "attempt": attempt,
            "t_send": round(t_send - started, 4), "t_done": round(t_done - started, 4),
            "status": status,
            "input_tokens": task.input_tokens, "max_output_tokens": task.max_output_tokens,
            "output_tokens": resp.get("output_tokens") if ok else None,
            "ttft_ms": round(ttft * 1000, 2) if ttft is not None else None,
            "latency_ms": round((t_done - t_send) * 1000, 2),
            "retry_after": resp.get("retry_after_seconds") if status == 429 else None,
            "cwnd": round(state["cwnd"], 3), "in_flight": state["in_flight"],
        })
