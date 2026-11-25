"""실제 프로세스로 설정 파일·시나리오·CSV/비교 CLI 연결을 검증."""

import csv
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import urlopen
from urllib.error import URLError

from scripts.smoke_test import start_server_process

ROOT = Path(__file__).resolve().parents[1]


class CommandLineTests(unittest.TestCase):
    def test_config_scenario_probe_and_comparison_pipeline(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryFile() as log:
            directory = Path(directory)
            scenario = directory / "scenario.json"
            scenario.write_text(json.dumps([
                {"at_seconds": .05, "changes": {"slots": 2}},
                {"at_seconds": .1, "changes": {"slots": 4}},
            ]), encoding="utf-8")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            base_url = f"http://127.0.0.1:{port}"
            server = start_server_process(["--port", str(port),
                "--config", str(ROOT / "examples/config.json"), "--scenario", str(scenario),
                "--log-dir", str(directory / "server-logs")], log)
            try:
                deadline = time.monotonic() + 10
                while True:
                    if server.poll() is not None:
                        self.fail("server exited during startup")
                    try:
                        with urlopen(base_url + "/config", timeout=1) as response:
                            config = json.load(response)
                        if config["scenario_status"] == "completed":
                            break
                    except (URLError, OSError):
                        pass
                    if time.monotonic() > deadline:
                        self.fail("scenario did not complete")
                    time.sleep(.02)
                self.assertEqual(config["revision"], 3)
                self.assertEqual(config["config"]["output_capacity"], 2000)
                measured = directory / "mock.csv"
                probe = subprocess.run([sys.executable, str(ROOT / "scripts/probe_ollama.py"),
                    "--backend", "mock", "--base-url", base_url, "--concurrency", "1", "2",
                    "--repetitions", "1", "--output", str(measured)],
                    cwd=directory, capture_output=True, timeout=20)
                self.assertEqual(probe.returncode, 0, probe.stderr.decode(errors="replace"))
                with measured.open(encoding="utf-8", newline="") as stream:
                    reader = csv.DictReader(stream)
                    fields = reader.fieldnames
                    rows = list(reader)
                self.assertEqual(len(rows), 3)
                self.assertTrue(all(row["status"] == "200" and not row["error"] for row in rows))
                self.assertTrue(all(row["config_revision"] == "3" for row in rows))
                self.assertTrue(all(int(row["output_tokens"]) > 0 for row in rows))
                meta = json.loads(measured.with_suffix(".csv.meta.json").read_text(encoding="utf-8"))
                self.assertEqual(meta["mock_config_at_end"]["revision"], 3)
                # 비교 CLI의 형식 검증용 합성 입력. 실제 LLM 측정 데이터가 아님.
                fixture = directory / "synthetic-real.csv"
                with fixture.open("w", encoding="utf-8", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(dict(row, backend="ollama", ttft_kind="client_first_response_chunk")
                                     for row in rows)
                output = directory / "comparison.json"
                comparison = subprocess.run([sys.executable, str(ROOT / "scripts/compare_measurements.py"),
                    "--real", str(fixture), "--mock", str(measured), "--output", str(output),
                    "--min-samples", "1"], cwd=directory, capture_output=True, timeout=10)
                self.assertEqual(comparison.returncode, 0, comparison.stderr.decode(errors="replace"))
                data = json.loads(output.read_text(encoding="utf-8"))
                self.assertIsNone(data["matched_concurrency"]["1"]["metrics"]["ttft_seconds"][
                                  "signed_relative_mean_error"])
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
