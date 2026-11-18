import asyncio
import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from server.mock_llm import create_app


def read_rows(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


class ExperimentLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_stats_elapsed_seconds_matches_csv_and_resets_on_restart(self):
        with tempfile.TemporaryDirectory() as directory, patch("time.perf_counter", return_value=100.0) as clock:
            async with TestClient(TestServer(create_app(log_dir=directory))) as http:
                clock.return_value = 100.25
                first = await (await http.get("/stats")).json()
                clock.return_value = 106.5
                second = await (await http.get("/stats")).json()
                self.assertEqual(first["elapsed_seconds"], 0.25)
                self.assertEqual(second["elapsed_seconds"], 6.5)
                self.assertEqual(first["run_id"], second["run_id"])

            samples = read_rows(Path(directory) / f"server_state_{first['run_id']}.csv")
            self.assertEqual(float(samples[-1]["elapsed_seconds"]), second["elapsed_seconds"])

            clock.return_value = 110.0
            async with TestClient(TestServer(create_app(log_dir=directory))) as http:
                clock.return_value = 110.125
                restarted = await (await http.get("/stats")).json()
                self.assertNotEqual(restarted["run_id"], first["run_id"])
                self.assertEqual(restarted["elapsed_seconds"], 0.125)

    async def test_live_queue_and_final_agent_counts_survive_restart(self):
        def client(directory):
            return TestClient(TestServer(create_app(
                log_dir=directory, capacity=2, refill_rate=.001,
                slots=1, queue_size=1, output_min=1, output_max=1,
                input_capacity=10, input_refill_rate=.001,
                output_capacity=10, output_refill_rate=.001,
            )))

        async def send(http, identifier, agent):
            return await http.post("/generate", json={
                "request_id": identifier, "agent_id": agent,
                "input_tokens": 2, "max_output_tokens": 1,
            })

        with tempfile.TemporaryDirectory() as directory:
            async with client(directory) as http:
                first_id = (await http.get("/health")).headers["X-Run-Id"]
                samples = Path(directory) / f"server_state_{first_id}.csv"
                agents = Path(directory) / f"agent_stats_{first_id}.csv"
                release = asyncio.Event()

                async def hold(_):
                    await release.wait()

                with patch("server.mock_llm.simulate_processing", side_effect=hold):
                    requests = [asyncio.create_task(send(http, agent, agent)) for agent in ("A", "B")]
                    try:
                        async with asyncio.timeout(4):
                            while True:
                                queued = [row for row in read_rows(samples) if row["queued"] == "1"]
                                if queued:
                                    break
                                await asyncio.sleep(.02)
                        self.assertEqual(queued[-1]["active"], "1")
                        self.assertLess(float(queued[-1]["requests_remaining"]), 1)
                        self.assertLess(float(queued[-1]["input_tokens_remaining"]), 7)
                        self.assertLess(float(queued[-1]["output_tokens_remaining"]), 9)
                        self.assertEqual((await send(http, "overload", "A")).status, 529)
                    finally:
                        release.set()
                        responses = await asyncio.gather(*requests)
                self.assertEqual([response.status for response in responses], [200, 200])
                self.assertEqual((await send(http, "retry", "A")).status, 429)

            # Even a stop immediately after the last response must persist the final counters.
            latest = {row["agent_id"]: (row["completed"], row["rate_limited"])
                      for row in read_rows(agents)}
            self.assertEqual(latest, {"A": ("1", "1"), "B": ("1", "0")})
            state_rows = read_rows(samples)
            self.assertTrue(all(row["run_id"] == first_id for row in state_rows + read_rows(agents)))
            self.assertEqual((state_rows[-1]["queued"], state_rows[-1]["active"]), ("0", "0"))
            async with client(directory) as http:
                next_id = (await http.get("/health")).headers["X-Run-Id"]
            self.assertNotEqual(next_id, first_id)
            self.assertEqual(read_rows(samples), state_rows)
            self.assertEqual(read_rows(Path(directory) / f"agent_stats_{next_id}.csv"), [])
            self.assertEqual(float(read_rows(Path(directory) /
                                            f"server_state_{next_id}.csv")[0]["requests_remaining"]), 2)
