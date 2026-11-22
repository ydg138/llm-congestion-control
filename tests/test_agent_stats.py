import asyncio
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from server.mock_llm import create_app
from server.rate_limit import TokenBucket


class AgentStatsTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_cancelled_and_overloaded_attempts_have_no_completions(self):
        app = create_app(slots=1, queue_size=0)
        async with TestClient(TestServer(app, handler_cancellation=True)) as client:
            started = asyncio.Event()
            cancelled = asyncio.Event()

            async def block(_delay):
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()

            with patch("server.mock_llm.simulate_processing", new=block):
                active = asyncio.create_task(client.post("/generate", json={"agent_id": "a", "request_id": "1", "input_tokens": 7}))
                await asyncio.wait_for(started.wait(), 3)
                overload = await client.post("/generate", json={"agent_id": "b", "request_id": "2", "input_tokens": 5})
                self.assertEqual(overload.status, 529)
                active.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await active
                await asyncio.wait_for(cancelled.wait(), 3)

            stats = await (await client.get("/stats")).json()
            self.assertEqual((stats["cancelled"], stats["overloaded"]), (1, 1))
            self.assertEqual(stats["agent_stats"], {
                "a": {"completed": 0, "rate_limited": 0, "input_tokens_consumed": 7, "output_tokens_consumed": 0},
                "b": {"completed": 0, "rate_limited": 0, "input_tokens_consumed": 0, "output_tokens_consumed": 0},
            })

    async def test_invalid_unicode_id_rejected_before_counting_or_logging(self):
        async with TestClient(TestServer(create_app())) as client:
            for field in ("agent_id", "request_id", "workload_id"):
                with self.subTest(field=field):
                    payload = {"agent_id": "a", "request_id": "1", field: "\ud800"}
                    response = await client.post("/generate", json=payload)
                    self.assertEqual(response.status, 400)
                    self.assertEqual(await response.json(), {"error": "invalid_field", "field": field})
            stats = await (await client.get("/stats")).json()
            self.assertEqual(stats["agent_stats"], {})

    async def test_retries_count_by_agent_and_new_run_starts_empty(self):
        now = 0.0

        def app():
            return create_app(bucket=TokenBucket(1, 1, lambda: now), service_delay=0,
                              output_min=1, output_max=1, output_token_seconds=0,
                              input_capacity=1)

        async with TestClient(TestServer(app())) as client:
            async def attempt(agent_id, request_id, workload_id, inputs=0):
                response = await client.post("/generate", json={
                    "agent_id": agent_id, "request_id": request_id,
                    "workload_id": workload_id, "input_tokens": inputs})
                return response.status

            self.assertEqual(await attempt("b", "1", "first"), 200)
            self.assertEqual(await attempt("a", "2", "retried"), 429)
            self.assertEqual(await attempt("a", "3", "retried"), 429)
            self.assertEqual(await attempt("c", "4", "too-large", inputs=2), 422)
            now = 1.0
            self.assertEqual(await attempt("a", "5", "retried"), 200)
            response = await client.get("/stats")
            stats = await response.json()
            self.assertEqual(stats["agent_stats"], {
                "a": {"completed": 1, "rate_limited": 2, "input_tokens_consumed": 0, "output_tokens_consumed": 1},
                "b": {"completed": 1, "rate_limited": 0, "input_tokens_consumed": 0, "output_tokens_consumed": 1},
                "c": {"completed": 0, "rate_limited": 0, "input_tokens_consumed": 0, "output_tokens_consumed": 0},
            })
            self.assertEqual((stats["completed"], stats["rate_limited"]), (2, 2))
            run_id = response.headers["X-Run-Id"]

        async with TestClient(TestServer(app())) as fresh:
            response = await fresh.get("/stats")
            self.assertNotEqual(response.headers["X-Run-Id"], run_id)
            self.assertEqual((await response.json())["agent_stats"], {})
