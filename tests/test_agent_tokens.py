"""E4 토큰 집계: 손계산 사용량, 거절, 취소, CSV 및 재시작을 확인한다."""

import asyncio
import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from server.mock_llm import STATE, create_app
from server.rate_limit import TokenBucket


def rows(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


class AgentTokenTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_agents_retry_actual_tokens_csv_and_restart(self):
        now = 0.0
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(bucket=TokenBucket(2, 1, lambda: now), log_dir=directory,
                             output_min=4, output_max=4, service_delay=0,
                             base_ttft=0, input_token_seconds=0, output_token_seconds=0)
            async with TestClient(TestServer(app)) as http:
                async def send(agent, identifier, inputs, workload=None):
                    return await http.post("/generate", json={
                        "agent_id": agent, "request_id": identifier,
                        "workload_id": workload or identifier,
                        "input_tokens": inputs, "max_output_tokens": 20})

                responses = await asyncio.gather(send("A", "1", 10), send("B", "2", 30))
                bodies = [await r.json() for r in responses]
                self.assertEqual([r.status for r in responses], [200, 200])
                self.assertEqual((await send("A", "3", 7, "retry")).status, 429)
                self.assertEqual((await send("A", "4", 7, "retry")).status, 429)
                self.assertEqual((await send("C", "5", 10001)).status, 422)
                now = 1.0
                response = await send("A", "6", 7, "retry")
                self.assertEqual(response.status, 200)
                bodies.append(await response.json())
                stats = await (await http.get("/stats")).json()
                self.assertEqual(stats["agent_stats"], {
                    "A": dict(completed=2, rate_limited=2,
                              input_tokens_consumed=17, output_tokens_consumed=8),
                    "B": dict(completed=1, rate_limited=0,
                              input_tokens_consumed=30, output_tokens_consumed=4),
                    "C": dict(completed=0, rate_limited=0,
                              input_tokens_consumed=0, output_tokens_consumed=0),
                })
                for kind in ("input", "output"):
                    field = kind + "_tokens_consumed"
                    self.assertEqual(stats[field], sum(b[kind + "_tokens"] for b in bodies))
                    self.assertEqual(stats[field], sum(a[field] for a in stats["agent_stats"].values()))

            path = Path(directory) / f"agent_stats_{stats['run_id']}.csv"
            latest = {r["agent_id"]: r for r in rows(path)}
            for agent, counters in stats["agent_stats"].items():
                self.assertEqual(latest[agent]["run_id"], stats["run_id"])
                for key, expected in counters.items():
                    self.assertEqual(int(latest[agent][key]), expected)
            async with TestClient(TestServer(create_app(log_dir=directory))) as fresh:
                reset = await (await fresh.get("/stats")).json()
                self.assertNotEqual(reset["run_id"], stats["run_id"])
                self.assertEqual(reset["agent_stats"], {})
                self.assertEqual((reset["input_tokens_consumed"], reset["output_tokens_consumed"]), (0, 0))

    async def test_cancelled_partial_output_is_logged_without_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(log_dir=directory, output_min=4, output_max=4,
                             output_token_seconds=60, service_delay=0, base_ttft=0)
            path = Path(directory) / f"agent_stats_{app[STATE].run_id}.csv"
            decode = asyncio.Event()
            calls = 0

            async def processing(_):
                nonlocal calls
                calls += 1
                if calls == 2:
                    decode.set()
                    await asyncio.Future()

            async with TestClient(TestServer(app, handler_cancellation=True)) as http:
                with patch("server.mock_llm.simulate_processing", new=processing):
                    task = asyncio.create_task(http.post("/generate", json={
                        "agent_id": "A", "request_id": "cancel", "input_tokens": 9,
                        "max_output_tokens": 20}))
                    try:
                        await asyncio.wait_for(decode.wait(), 3)
                        async with asyncio.timeout(3):
                            while not rows(path):
                                await asyncio.sleep(.01)
                        self.assertEqual(rows(path)[-1]["output_tokens_consumed"], "0")
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    async with asyncio.timeout(3):
                        while app[STATE].cancelled != 1:
                            await asyncio.sleep(.001)
                    stats = await (await http.get("/stats")).json()
                    self.assertEqual(stats["agent_stats"]["A"], dict(
                        completed=0, rate_limited=0, input_tokens_consumed=9, output_tokens_consumed=1))
            self.assertGreaterEqual(len(rows(path)), 2)
            final = rows(path)[-1]
            self.assertEqual((final["completed"], final["input_tokens_consumed"],
                              final["output_tokens_consumed"]), ("0", "9", "1"))
