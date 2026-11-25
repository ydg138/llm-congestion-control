import asyncio
import unittest
from unittest.mock import patch

from aiohttp import ClientTimeout
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

from server.mock_llm import STATE, create_app
from server.rate_limit import TokenBucket


class QueueTests(AioHTTPTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.gate = asyncio.Event()

        async def process(_delay):
            await self.gate.wait()
        replacement = patch("server.mock_llm.simulate_processing", side_effect=process)
        replacement.start()
        self.addCleanup(replacement.stop)

    async def get_application(self):
        return create_app(bucket=TokenBucket(100, 1, lambda: 0), slots=1,
                          queue_size=1, service_delay=0.15)

    async def get_server(self, app):
        return TestServer(app, handler_cancellation=True)

    async def get_client(self, server):
        return TestClient(server, timeout=ClientTimeout(total=5))

    async def send(self, identifier):
        return await self.client.post("/generate", json={"request_id": identifier, "agent_id": "a", "input_tokens": 7})

    async def wait_state(self, active, queued):
        async def wait():
            while self.app[STATE].active != active or len(self.app[STATE].waiting) != queued:
                await asyncio.sleep(0.001)
        await asyncio.wait_for(wait(), timeout=3)

    async def test_bounded_queue_overload_and_release(self):
        first = asyncio.create_task(self.send("first"))
        await self.wait_state(1, 0)
        second = asyncio.create_task(self.send("second"))
        await self.wait_state(1, 1)
        third = await self.send("third")
        self.assertEqual(third.status, 529)
        self.assertEqual((await third.json())["error"], "server_overloaded")
        self.gate.set()
        a, b = await asyncio.gather(first, second)
        self.assertEqual([a.status, b.status], [200, 200])
        self.assertGreater((await b.json())["queue_wait_seconds"], 0)
        state = self.app[STATE]
        self.assertEqual((state.active, len(state.waiting), state.completed, state.overloaded), (0, 0, 2, 1))
        final = await self.send("after")
        self.assertEqual(final.headers["X-RateLimit-Remaining"], "97")

    async def test_cancel_queued_request_frees_queue(self):
        first = asyncio.create_task(self.send("first"))
        await self.wait_state(1, 0)
        queued = asyncio.create_task(self.send("queued"))
        await self.wait_state(1, 1)
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        await self.wait_state(1, 0)
        self.gate.set()
        self.assertEqual((await first).status, 200)
        await self.wait_state(0, 0)
        self.assertEqual(self.app[STATE].cancelled, 1)
        self.assertEqual(self.app[STATE].agent_stats["a"]["input_tokens_consumed"], 14)
        self.assertEqual(self.app[STATE].agent_stats["a"]["output_tokens_consumed"], self.app[STATE].output_consumed)

    async def test_cancel_active_request_promotes_waiter(self):
        first = asyncio.create_task(self.send("first"))
        await self.wait_state(1, 0)
        queued = asyncio.create_task(self.send("queued"))
        await self.wait_state(1, 1)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        # TCP 연결 종료가 서버에 전달되어 대기 요청이 승격될 때까지 기다림.
        await self.wait_state(1, 0)
        self.gate.set()
        self.assertEqual((await queued).status, 200)
        await self.wait_state(0, 0)
        self.assertEqual(self.app[STATE].cancelled, 1)

    async def test_cancel_both_requests_does_not_leak_slots(self):
        first = asyncio.create_task(self.send("first"))
        await self.wait_state(1, 0)
        queued = asyncio.create_task(self.send("queued"))
        await self.wait_state(1, 1)
        first.cancel()
        queued.cancel()
        await asyncio.gather(first, queued, return_exceptions=True)
        await self.wait_state(0, 0)
        self.gate.set()
        self.assertEqual((await self.send("after")).status, 200)


class NoQueueTests(QueueTests):
    # 대기열이 없는 설정은 별도 테스트만 실행.
    async def get_application(self):
        return create_app(capacity=100, slots=1, queue_size=0, service_delay=0.15)

    async def test_no_waiting_space(self):
        first = asyncio.create_task(self.send("first"))
        await self.wait_state(1, 0)
        self.assertEqual((await self.send("second")).status, 529)
        self.gate.set()
        self.assertEqual((await first).status, 200)

    test_bounded_queue_overload_and_release = None
    test_cancel_queued_request_frees_queue = None
    test_cancel_active_request_promotes_waiter = None
    test_cancel_both_requests_does_not_leak_slots = None
