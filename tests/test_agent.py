"""에이전트를 작은 가짜 서버에 붙여 재시도 순서와 컨트롤러 호출을 손계산 값으로 확인한다."""

import asyncio
import csv
import time

import pytest
from aiohttp import web

from client.controllers import AIMDConfig, AIMDWindow, FixedConfig, FixedWindow
from runner.agent import Agent, AgentConfig, AgentSpec, Progress, report
from runner.request_log import RequestLog
from runner.workload import Task


class RecordingC2(AIMDWindow):
    """C2 그대로, wait_before_retry에 들어온 attempt만 기록한다."""

    def __init__(self):
        super().__init__(AIMDConfig(init_cwnd=1))
        self.retry_attempts = []

    def wait_before_retry(self, attempt):
        self.retry_attempts.append(attempt)
        return super().wait_before_retry(attempt)


async def start_fake_server(handler):
    app = web.Application()
    app.router.add_post("/generate", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)      # 포트 0: OS가 빈 포트를 준다
    await site.start()
    return runner, f"http://127.0.0.1:{runner.addresses[0][1]}"


def read_rows(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_retry_goes_before_new_task(tmp_path):
    received = []

    async def handler(request):
        received.append(await request.json())
        if len(received) == 1:                      # 첫 요청만 429
            return web.json_response({"error": "rate_limit_exceeded", "retry_after_seconds": 0.25},
                                     status=429)
        return web.json_response({"output_tokens": 10, "ttft_seconds": 0.002})

    tasks = [Task("s1-a1-000", 100, 64), Task("s1-a1-001", 120, 64)]
    controller = RecordingC2()
    progress = Progress(time.perf_counter())
    path = tmp_path / "log.csv"

    async def main():
        runner, url = await start_fake_server(handler)
        log = RequestLog(path, "run-x", "T", 1)
        try:
            # poll_seconds=0: 송신 루프가 늘 준비돼 있어, 재시도 전에 await가 있으면 작업2가 먼저 나간다
            agent = Agent(AgentSpec("a1", "light", "C2"), tasks, controller, url, log, progress,
                          AgentConfig(poll_seconds=0))
            await asyncio.wait_for(agent.run(), 5)
        finally:
            log.close()
            await runner.cleanup()

    asyncio.run(main())

    # 서버가 받은 순서: [작업1, 작업1 재시도, 작업2]
    assert [b["workload_id"] for b in received] == ["s1-a1-000", "s1-a1-000", "s1-a1-001"]
    assert [b["request_id"] for b in received] == ["s1-a1-000-a0", "s1-a1-000-a1", "s1-a1-001-a0"]
    assert received[1]["max_output_tokens"] == received[0]["max_output_tokens"] == 64
    assert controller.retry_attempts == [1]         # 첫 재시도는 attempt=1

    rows = read_rows(path)
    assert [r["status"] for r in rows] == ["429", "200", "200"]
    assert [r["attempt"] for r in rows] == ["0", "1", "0"]
    assert rows[0]["retry_after"] == "0.25" and rows[0]["output_tokens"] == ""
    # 손계산: 429 → ssthresh=max(1/2,1)=1, cwnd=1 / 성공 → 1+1/1=2 / 성공 → 2+1/2=2.5
    assert [r["cwnd"] for r in rows] == ["1.0", "2.0", "2.5"]
    assert rows[1]["ttft_ms"] == "2.0"
    assert progress.active == 0 and progress.last_success > progress.started


class FailOnTaskLog:
    """정해 둔 작업의 로그를 쓸 때 예외를 내는 가짜 로그 (디스크 오류 흉내)."""

    def __init__(self, task_id):
        self.task_id = task_id

    def write(self, row):
        if row["task_id"] == self.task_id:
            raise OSError("로그 쓰기 실패")


def test_error_in_last_task_is_raised():
    async def handler(request):
        return web.json_response({"output_tokens": 1, "ttft_seconds": 0.001})

    tasks = [Task("t0", 10, 8), Task("t1", 10, 8)]
    progress = Progress(time.perf_counter())

    async def main():
        runner, url = await start_fake_server(handler)
        try:
            # C2 init_cwnd=1이라 하나씩 보낸다 → 마지막 작업 t1에서 예외가 나면 줄이 모두 빈 상태
            agent = Agent(AgentSpec("a1", "light", "C2"), tasks, AIMDWindow(AIMDConfig(init_cwnd=1)),
                          url, FailOnTaskLog("t1"), progress)
            await asyncio.wait_for(agent.run(), 5)
        finally:
            await runner.cleanup()

    with pytest.raises(OSError, match="로그 쓰기 실패"):
        asyncio.run(main())
    assert progress.active == 0


class CallLog:
    """report가 어떤 메서드를 불렀는지만 기록하는 가짜 컨트롤러."""

    def __init__(self):
        self.calls = []

    def on_success(self, req_no, resp):
        self.calls.append(("success", req_no))

    def on_rate_limited(self, req_no, retry_after):
        self.calls.append(("rate_limited", req_no, retry_after))

    def on_overloaded(self, req_no):
        self.calls.append(("overloaded", req_no))

    def on_error(self, req_no):
        self.calls.append(("error", req_no))


def test_report_calls_matching_method():
    c = CallLog()
    assert report(c, 1, 200, {}) is False
    assert report(c, 2, 429, {"retry_after_seconds": 0.5}) is True
    assert report(c, 3, 529, {}) is True
    assert report(c, 4, 422, {}) is False           # 기다려도 안 되는 요청은 다시 보내지 않음
    assert report(c, 5, "timeout", {}) is False
    assert c.calls == [("success", 1), ("rate_limited", 2, 0.5), ("overloaded", 3),
                       ("error", 4), ("error", 5)]


def test_cancel_stops_all_child_tasks(tmp_path):
    received = []
    release = asyncio.Event()

    async def handler(request):
        received.append(await request.json())
        await release.wait()                        # 응답을 붙잡아 둔다
        return web.json_response({"output_tokens": 1, "ttft_seconds": 0.001})

    tasks = [Task(f"t{i}", 10, 8) for i in range(6)]
    progress = Progress(time.perf_counter())

    async def main():
        runner, url = await start_fake_server(handler)
        log = RequestLog(tmp_path / "log.csv", "run-x", "T", 1)
        agent = Agent(AgentSpec("a1", "light", "C0-4"), tasks, FixedWindow(FixedConfig(window=4)),
                      url, log, progress)
        run = asyncio.create_task(agent.run())
        while len(received) < 4:                    # 윈도 4개가 모두 나갈 때까지
            await asyncio.sleep(0.01)
        children = list(agent.running)
        run.cancel()                                # 안전장치가 하는 것과 같은 취소
        await asyncio.gather(run, return_exceptions=True)
        release.set()
        log.close()
        await runner.cleanup()
        return children

    children = asyncio.run(asyncio.wait_for(main(), 5))
    assert len(received) == 4                       # 고정 윈도 4개만 보냄
    assert len(children) == 4 and all(t.done() for t in children)
    assert progress.active == 0
    assert read_rows(tmp_path / "log.csv") == []    # 취소된 요청은 로그에 남기지 않음
