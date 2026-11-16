import asyncio
import json
import unittest

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from scripts.measurements import compare, percentile, summarize
from scripts.probe_ollama import probe_one


class MeasurementMathTests(unittest.TestCase):
    def row(self, value=2, kind="client_first_response_chunk", status=200, error=""):
        return {"backend": "ollama", "concurrency": 2, "status": status, "error": error,
                "total_seconds": value, "ttft_seconds": value / 2, "ttft_kind": kind}

    def test_percentile_and_failures(self):
        self.assertEqual(percentile([1, 2, 3, 4, 5]), 4.8)
        self.assertIsNone(percentile([]))
        result = summarize([self.row(2), self.row(4), self.row(100, status=429)], min_samples=2)["2"]
        self.assertEqual(result["successes"], 2)
        self.assertEqual(result["failures"], 1)
        self.assertEqual(result["metrics"]["total_seconds"]["mean"], 3)
        self.assertTrue(result["sufficient_samples"])

    def test_different_ttft_sources_are_not_compared(self):
        result = compare([self.row(2)], [self.row(3, kind="mock_server_ttft")], min_samples=1)
        metrics = result["matched_concurrency"]["2"]["metrics"]
        self.assertEqual(metrics["total_seconds"]["signed_relative_mean_error"], 0.5)
        self.assertIsNone(metrics["ttft_seconds"]["signed_relative_mean_error"])
        self.assertFalse(metrics["ttft_seconds"]["comparable_measurement"])

    def test_insufficient_or_invalid_measurements(self):
        result = compare([self.row(2)], [self.row(3)], min_samples=5)
        self.assertIsNone(result["matched_concurrency"]["2"]["metrics"]["total_seconds"]["signed_relative_mean_error"])
        rows = [self.row(float("nan")), self.row(float("inf")), self.row(-1)]
        self.assertEqual(summarize(rows)["2"]["metrics"]["total_seconds"]["n"], 0)
        self.assertEqual(compare([], [self.row()])["unmatched_mock"], ["2"])


class ProbeTests(AioHTTPTestCase):
    async def get_application(self):
        self.mode = "normal"
        self.last_payload = None

        async def ollama(request):
            self.last_payload = await request.json()
            if self.mode == "http_error":
                return web.json_response({"error": "private content should not be logged"}, status=429)
            response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
            await response.prepare(request)
            if self.mode == "malformed":
                await response.write(b"invalid\n")
            elif self.mode == "backend_error":
                await response.write(b'{"error":"private details"}\n')
            elif self.mode == "incomplete":
                await response.write(b'{"response":"hello","done":false}\n')
            else:
                await response.write(b'{"thinking":"reasoning","done":false}\n')
                # Intentionally split one JSON object across TCP writes.
                await response.write(b'{"response":')
                await asyncio.sleep(0)
                await response.write(b'"hello","done":false}\n')
                await response.write(json.dumps({"done": True, "response": "",
                                                "prompt_eval_count": 12, "eval_count": 8,
                                                "prompt_eval_duration": 250000000,
                                                "eval_duration": 500000000}).encode() + b"\n")
            await response.write_eof()
            return response

        async def mock(request):
            self.last_payload = await request.json()
            return web.json_response({"input_tokens": 100, "output_tokens": 20,
                                      "ttft_seconds": 0.12, "prefill_seconds": 0.1,
                                      "decode_seconds": 0.2})

        app = web.Application()
        app.router.add_post("/api/generate", ollama)
        app.router.add_post("/generate", mock)
        return app

    async def call(self, backend="ollama"):
        return await probe_one(self.client.session, backend=backend,
                               base_url=str(self.server.make_url("")), model="fixture",
                               prompt="test", concurrency=2, repetition=0,
                               request_id="req", workload_id="work", timeout=3)

    async def test_streaming_fields_and_ns_conversion(self):
        row = await self.call()
        self.assertEqual(row["status"], 200)
        self.assertEqual(row["error"], "")
        self.assertEqual(row["input_tokens"], 12)
        self.assertEqual(row["output_tokens"], 8)
        self.assertEqual(row["server_prefill_seconds"], 0.25)
        self.assertEqual(row["server_decode_seconds"], 0.5)
        self.assertGreaterEqual(row["total_seconds"], row["ttft_seconds"])
        self.assertTrue(self.last_payload["stream"])
        self.assertEqual(self.last_payload["options"]["num_predict"], 64)

    async def test_error_responses_do_not_log_content(self):
        for mode, expected in [("http_error", "http_429"), ("malformed", "invalid_or_oversized_response"),
                               ("incomplete", "incomplete_stream"), ("backend_error", "backend_error")]:
            self.mode = mode
            row = await self.call()
            self.assertEqual(row["error"], expected)
            self.assertNotIn("private", json.dumps(row))

    async def test_mock_fields_and_workload_identity(self):
        row = await self.call("mock")
        self.assertEqual(row["ttft_kind"], "mock_server_ttft")
        self.assertEqual(row["ttft_seconds"], 0.12)
        self.assertEqual(row["server_decode_seconds"], 0.2)
        self.assertEqual(self.last_payload["workload_id"], "work")
        self.assertEqual(self.last_payload["max_output_tokens"], 64)
