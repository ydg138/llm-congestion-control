import asyncio
import unittest

from aiohttp import ClientTimeout
from aiohttp.test_utils import AioHTTPTestCase

from server.mock_llm import create_app
from server.rate_limit import TokenBucket


class BucketTests(unittest.TestCase):
    def test_refill_and_capacity(self):
        now = [0.0]
        bucket = TokenBucket(2, 1, lambda: now[0])
        self.assertTrue(bucket.consume().allowed)
        self.assertTrue(bucket.consume().allowed)
        self.assertEqual(bucket.consume().retry_after_seconds, 1)
        now[0] = 0.5
        decision = bucket.consume()
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.remaining, 0.5)
        self.assertEqual(decision.retry_after_seconds, 0.5)
        now[0] = 1
        self.assertTrue(bucket.consume().allowed)
        now[0] = 100
        self.assertEqual(bucket.consume().remaining, 1)

    def test_invalid_configuration(self):
        for capacity, rate in [(0, 1), (1, 0), (1, float("nan")), (1, float("inf")),
                               (1, 5e-324), (1, True), (10**10, 1)]:
            with self.assertRaises(ValueError):
                TokenBucket(capacity, rate)

    def test_backwards_clock_does_not_double_refill(self):
        now = [10.0]
        bucket = TokenBucket(1, 1, lambda: now[0])
        bucket.consume()
        now[0] = 9
        self.assertFalse(bucket.consume().allowed)
        now[0] = 10
        self.assertFalse(bucket.consume().allowed)

    def test_invalid_pool_configuration(self):
        for args in [{"slots": 0}, {"queue_size": -1}, {"service_delay": float("nan")},
                     {"service_delay": -1}, {"slots": True}]:
            with self.assertRaises(ValueError):
                create_app(**args)


class HttpTests(AioHTTPTestCase):
    async def get_client(self, server):
        from aiohttp.test_utils import TestClient
        return TestClient(server, timeout=ClientTimeout(total=5))

    async def get_application(self):
        self.now = 0.0
        return create_app(bucket=TokenBucket(5, 1, lambda: self.now))

    async def send(self, number):
        return await self.client.post("/generate", json={
            "request_id": str(number), "agent_id": f"agent-{number % 2}",
            "input_tokens": 100})

    async def test_shared_limit_concurrent_requests_and_recovery(self):
        responses = await asyncio.gather(*(self.send(i) for i in range(20)))
        self.assertEqual(sum(r.status == 200 for r in responses), 5)
        self.assertEqual(sum(r.status == 429 for r in responses), 15)
        rejected = next(r for r in responses if r.status == 429)
        self.assertEqual(rejected.headers["Retry-After"], "1")
        self.assertEqual((await rejected.json())["error"], "rate_limit_exceeded")
        self.now = 1.0
        self.assertEqual((await self.send(21)).status, 200)
        self.assertEqual((await self.send(22)).status, 429)
        result = await (await self.client.get("/stats")).json()
        self.assertEqual(result["accepted"], 6)
        self.assertEqual(result["rate_limited"], 16)

    async def test_invalid_requests_do_not_consume_quota(self):
        for payload in [[], {}, {"request_id": "x", "agent_id": "a", "input_tokens": True}]:
            response = await self.client.post("/generate", json=payload)
            self.assertEqual(response.status, 400)
        response = await self.client.post("/generate", data="{", headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 400)
        response = await self.client.post("/generate", data="text")
        self.assertEqual(response.status, 415)
        self.assertEqual((await self.send(0)).headers["X-RateLimit-Remaining"], "4")

    async def test_health(self):
        response = await self.client.get("/health")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["status"], "ok")

    async def test_all_http_errors_are_json(self):
        for path, expected in [("/missing", 404), ("/generate", 405)]:
            response = await self.client.get(path)
            self.assertEqual(response.status, expected)
            self.assertIn("error", await response.json())
            self.assertIn("X-Run-Id", response.headers)
        response = await self.client.post("/generate", data=b"x" * 70000,
                                          headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 413)
        self.assertEqual((await response.json())["error"], "payload_too_large")

    async def test_malformed_json_and_charset_are_client_errors(self):
        for raw, content_type in [
            (b"{}", "application/json; charset=not-a-codec"),
            (b"\xff", "application/json"),
            (b"1" * 5000, "application/json"),
            (b"[" * 2000 + b"]" * 2000, "application/json"),
        ]:
            response = await self.client.post("/generate", data=raw,
                                              headers={"Content-Type": content_type})
            self.assertEqual(response.status, 400)
            self.assertIn("error", await response.json())

    async def test_success_contract_and_identifiers(self):
        response = await self.send(1)
        data = await response.json()
        self.assertEqual(data["request_id"], "1")
        self.assertEqual(data["agent_id"], "agent-1")
        self.assertEqual(data["run_id"], response.headers["X-Run-Id"])
        self.assertEqual(response.headers["X-API-Version"], "2.0")
        self.assertTrue(16 <= data["output_tokens"] <= 64)
        self.assertGreaterEqual(data["server_elapsed_seconds"], data["service_seconds"])

    async def test_p2_async_adapter_preserves_429(self):
        from aiohttp import ClientSession
        from examples.async_client import generate
        async with ClientSession(raise_for_status=True) as session:
            results = [await generate(session, str(self.server.make_url("")), "p2", str(i),
                                      workload_id="shared-work", max_output_tokens=32)
                       for i in range(6)]
        self.assertEqual([r["status"] for r in results], [200] * 5 + [429])
        self.assertEqual(results[-1]["headers"]["Retry-After"], "1")
        self.assertEqual(len({r["body"]["output_tokens"] for r in results[:5]}), 1)
        self.assertTrue(all(r["body"]["workload_id"] == "shared-work" for r in results))
