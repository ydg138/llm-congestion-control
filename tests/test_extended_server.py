import asyncio
import unittest
from unittest.mock import patch

from aiohttp import ClientTimeout
from aiohttp.test_utils import AioHTTPTestCase, TestClient

from server.config import ServerConfig, validate_scenario
from server.mock_llm import STATE, create_app
from server.rate_limit import MultiQuota, TokenBucket


class ConfigTests(unittest.TestCase):
    def test_scenario_validation(self):
        cfg = ServerConfig()
        for events in [{}, [{"at_seconds": -1, "changes": {"slots": 2}}],
                       [{"at_seconds": 0, "changes": {"unknown": 1}}],
                       [{"at_seconds": 2, "changes": {"slots": 2}},
                        {"at_seconds": 1, "changes": {"slots": 3}}]]:
            with self.assertRaises(ValueError):
                validate_scenario(events, cfg)
        with self.assertRaises(ValueError):
            cfg.changed({"base_ttft": 10**1000})


class ExtendedTests(AioHTTPTestCase):
    async def get_application(self):
        app = create_app(capacity=100, input_capacity=100, output_capacity=100,
                         output_min=4, output_max=4, slots=1, queue_size=3,
                         service_delay=0, base_ttft=0, input_token_seconds=0,
                         output_token_seconds=0, admin_token="test-key")
        app[STATE].quota = MultiQuota(TokenBucket(100, 1, lambda: 0),
                                      TokenBucket(100, 1, lambda: 0),
                                      TokenBucket(100, 1, lambda: 0))
        return app

    async def get_client(self, server):
        return TestClient(server, timeout=ClientTimeout(total=5))

    async def send(self, request_id="r", **values):
        return await self.client.post("/generate", json={"request_id": request_id,
            "agent_id": "a", "input_tokens": 10, "max_output_tokens": 20, **values})

    async def change(self, changes, authorized=True):
        return await self.client.post("/admin/config", json=changes,
            headers={"Authorization": "Bearer test-key"} if authorized else {})

    async def until(self, condition):
        async def wait():
            while not condition():
                await asyncio.sleep(.001)
        await asyncio.wait_for(wait(), 3)

    async def test_actual_output_and_refund(self):
        response = await self.send()
        data = await response.json()
        self.assertEqual(data["output_tokens"], 4)
        self.assertEqual(response.headers["X-InputTokenLimit-Remaining"], "90")
        self.assertEqual(response.headers["X-OutputTokenLimit-Remaining"], "96")
        self.assertGreaterEqual(data["ttft_seconds"], data["queue_wait_seconds"])
        stats = await (await self.client.get("/stats")).json()
        self.assertEqual(stats["output_tokens_reserved"], 0)
        self.assertEqual(stats["output_tokens_consumed"], 4)

    async def test_input_limit_no_partial_debit(self):
        self.assertEqual((await self.send(input_tokens=80)).status, 200)
        response = await self.send(input_tokens=30)
        self.assertEqual(response.status, 429)
        self.assertEqual((await response.json())["limited_by"], ["input_tokens"])
        state = self.app[STATE]
        self.assertEqual(state.quota.requests.snapshot()["remaining"], 99)
        self.assertEqual(state.quota.outputs.snapshot()["remaining"], 96)

    async def test_output_reservations_prevent_oversubscription(self):
        gate = asyncio.Event()
        async def wait(_):
            await gate.wait()
        with patch("server.mock_llm.simulate_processing", side_effect=wait):
            pending = asyncio.create_task(self.send(max_output_tokens=80))
            await self.until(lambda: self.app[STATE].active == 1)
            blocked = await self.send("other", max_output_tokens=30)
            self.assertEqual(blocked.status, 429)
            self.assertIn("output_tokens", (await blocked.json())["limited_by"])
            gate.set()
            self.assertEqual((await pending).status, 200)
        self.assertEqual(self.app[STATE].quota.outputs.snapshot()["remaining"], 96)

    async def test_impossible_request_is_not_retried_forever(self):
        response = await self.send(input_tokens=101)
        self.assertEqual(response.status, 422)
        self.assertNotIn("Retry-After", response.headers)
        self.assertIsNone((await response.json())["retry_after_seconds"])
        self.assertEqual(self.app[STATE].accepted, 0)

    async def test_workload_identity_survives_retry_and_agent_change(self):
        await self.change({"output_min": 1, "output_max": 30})
        one = await (await self.send("attempt1", workload_id="same-work", max_output_tokens=30)).json()
        two = await (await self.send("attempt2", workload_id="same-work", agent_id="b", max_output_tokens=30)).json()
        self.assertEqual(one["output_tokens"], two["output_tokens"])

    async def test_config_auth_and_atomic_validation(self):
        self.assertEqual((await self.change({"slots": 2}, authorized=False)).status, 403)
        self.assertEqual((await self.change({"capacity": 50, "slots": 0})).status, 400)
        self.assertEqual(self.app[STATE].config.capacity, 100)
        await self.change({"capacity": 50, "refill_rate": 2})
        self.assertEqual(self.app[STATE].quota.requests.snapshot()["capacity"], 50)
        cfg = await (await self.client.get("/config")).json()
        self.assertEqual(cfg["revision"], 2)
        self.assertEqual(cfg["changes"][0]["source"], "admin")
        self.assertNotIn("test-key", str(cfg))

    async def test_increase_slots_promotes_without_waiting_for_completion(self):
        gate = asyncio.Event()
        async def wait(_):
            await gate.wait()
        with patch("server.mock_llm.simulate_processing", side_effect=wait):
            first = asyncio.create_task(self.send("one"))
            await self.until(lambda: self.app[STATE].active == 1)
            second = asyncio.create_task(self.send("two"))
            await self.until(lambda: len(self.app[STATE].waiting) == 1)
            await self.change({"slots": 2, "output_min": 7, "output_max": 7})
            self.assertEqual(self.app[STATE].active, 2)
            self.assertEqual(len(self.app[STATE].waiting), 0)
            gate.set()
            responses = await asyncio.gather(first, second)
        for response in responses:
            body = await response.json()
            self.assertEqual(body["output_tokens"], 4)
            self.assertEqual(body["config_revision"], 1)
        self.assertEqual((await (await self.send("three")).json())["output_tokens"], 7)

    async def test_shrinking_slots_drains_existing_work(self):
        await self.change({"slots": 2})
        gate = asyncio.Event()
        async def wait(_):
            await gate.wait()
        with patch("server.mock_llm.simulate_processing", side_effect=wait):
            tasks = [asyncio.create_task(self.send(str(i))) for i in range(3)]
            await self.until(lambda: self.app[STATE].active == 2 and len(self.app[STATE].waiting) == 1)
            await self.change({"slots": 1, "queue_size": 0})
            self.assertEqual(self.app[STATE].active, 2)
            self.assertEqual((await self.send("rejected")).status, 529)
            gate.set()
            self.assertEqual([r.status for r in await asyncio.gather(*tasks)], [200]*3)
        self.assertEqual(self.app[STATE].active, 0)

    async def test_cancel_before_first_token_refunds_output_only(self):
        gate = asyncio.Event()
        async def wait(_):
            await gate.wait()
        with patch("server.mock_llm.simulate_processing", side_effect=wait):
            task = asyncio.create_task(self.send())
            await self.until(lambda: self.app[STATE].active == 1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await self.until(lambda: self.app[STATE].cancelled == 1)
        state = self.app[STATE]
        self.assertEqual(state.output_reserved, 0)
        self.assertEqual(state.quota.outputs.snapshot()["remaining"], 100)
        self.assertEqual(state.quota.inputs.snapshot()["remaining"], 90)
        self.assertEqual(state.quota.requests.snapshot()["remaining"], 99)

    async def test_cancel_after_first_token_charges_generated_tokens(self):
        await self.change({"output_token_seconds": 60})
        decode_started = asyncio.Event()
        gate = asyncio.Event()
        calls = 0
        async def wait(_):
            nonlocal calls
            calls += 1
            if calls == 2:
                decode_started.set()
                await gate.wait()
        with patch("server.mock_llm.simulate_processing", side_effect=wait):
            task = asyncio.create_task(self.send())
            await asyncio.wait_for(decode_started.wait(), 3)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await self.until(lambda: self.app[STATE].cancelled == 1)
        self.assertEqual(self.app[STATE].output_consumed, 1)
        self.assertEqual(self.app[STATE].quota.outputs.snapshot()["remaining"], 99)


class ScenarioTests(AioHTTPTestCase):
    async def get_application(self):
        return create_app(scenario=[{"at_seconds": 0.02, "changes": {"capacity": 2}},
                                    {"at_seconds": 0.04, "changes": {"slots": 3}}])

    async def test_scheduled_changes_and_history(self):
        async def wait():
            while self.app[STATE].scenario_status != "completed":
                await asyncio.sleep(.005)
        await asyncio.wait_for(wait(), 3)
        state = self.app[STATE]
        self.assertEqual(state.config.capacity, 2)
        self.assertEqual(state.config.slots, 3)
        self.assertEqual(state.revision, 3)
        self.assertEqual(len(state.history), 2)
        self.assertTrue(all(x["source"] == "scenario" for x in state.history))
