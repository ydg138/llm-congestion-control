"""실행 결과 상태(runner.status)와 stats JSON 저장을 손으로 정한 응답으로 확인한다.

run_experiment는 자기 이벤트 루프(asyncio.run)를 만들므로, 가짜 서버는 표준 라이브러리
http.server를 스레드로 띄운다.
"""

import csv
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from runner.agent import AgentSpec
from runner.request_log import RequestLog
from runner.run import run_experiment, save_stats
from runner.server_proc import free_port
from runner.watchdog import Limits


class FakeHandler(BaseHTTPRequestHandler):
    """POST /generate에는 server.status로, GET /stats에는 server.stats로 답한다."""

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        code = self.server.status
        body = {"output_tokens": 1, "ttft_seconds": 0.001} if code == 200 else {"retry_after_seconds": 0.0}
        self._reply(code, body)

    def do_GET(self):
        self._reply(200, self.server.stats)

    def _reply(self, code, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass                                                # 테스트 출력에 접속 기록을 남기지 않음


@pytest.fixture
def server():
    s = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    s.status = 200
    s.stats = {"run_id": "fake", "completed": 3}            # elapsed_seconds가 없는 서버
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s
    s.shutdown()
    s.server_close()


def url(s):
    return f"http://127.0.0.1:{s.server_address[1]}"


def run(s, tmp_path, limits=Limits(), agents=(AgentSpec("a1", "light", "C2"),)):
    """기본은 C2 에이전트 1개, 에이전트당 작업 3개."""
    log = RequestLog(tmp_path / "log.csv", "fake", "T", 1)
    try:
        return run_experiment(agents, 3, 1, url(s), "fake", log, limits)
    finally:
        log.close()


def test_all_tasks_succeed_is_completed(server, tmp_path):
    assert run(server, tmp_path) == {"status": "completed", "reason": None,
                                     "server_elapsed_at_start_seconds": None}   # 서버에 값이 없으면 None


def test_server_elapsed_at_start_is_recorded(server, tmp_path):
    server.stats = {"run_id": "fake", "elapsed_seconds": 3.25, "completed": 3}
    outcome = run(server, tmp_path)
    assert outcome["server_elapsed_at_start_seconds"] == 3.25
    save_stats(url(server), tmp_path / "stats.json", outcome)
    doc = json.loads((tmp_path / "stats.json").read_text(encoding="utf-8"))
    assert doc["runner"]["server_elapsed_at_start_seconds"] == 3.25


def test_run_id_mismatch_raises(server, tmp_path):
    server.stats = {"run_id": "other-server", "elapsed_seconds": 1.0}
    with pytest.raises(RuntimeError, match="run_id"):
        run(server, tmp_path)


def test_safety_stop_is_aborted(server, tmp_path):
    server.status = 429                                     # 계속 429 → 완료가 없어 안전장치가 멈춤
    outcome = run(server, tmp_path, Limits(stall_seconds=0.3, check_seconds=0.05))
    assert outcome["status"] == "aborted"
    assert "완료 없음" in outcome["reason"]


def test_failed_tasks_make_run_aborted(server, tmp_path):
    server.status = 422                                     # 기다려도 안 되는 요청 → 재시도 없이 실패
    outcome = run(server, tmp_path)
    assert (outcome["status"], outcome["reason"]) == ("aborted", "작업 0/3만 성공")


def test_two_agents_late_join_both_complete(server, tmp_path):
    agents = (AgentSpec("a1", "light", "C2", 0.0), AgentSpec("a2", "light", "C2", 0.3))   # a2는 0.3초 뒤
    assert run(server, tmp_path, agents=agents)["status"] == "completed"
    with open(tmp_path / "log.csv", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    ok = sorted(r["task_id"] for r in rows if r["status"] == "200")
    assert ok == [f"s1-{a}-00{i}" for a in ("a1", "a2") for i in range(3)]   # 2 에이전트 × 3 작업
    first = {a: min(float(r["t_send"]) for r in rows if r["agent_id"] == a) for a in ("a1", "a2")}
    assert first["a1"] < 0.3 <= first["a2"]


def test_stats_json_has_runner_first(server, tmp_path):
    path = tmp_path / "stats.json"
    outcome = {"status": "completed", "reason": None}
    assert save_stats(url(server), path, outcome)["completed"] == 3
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert list(doc)[0] == "runner"                         # 맨 위
    assert doc["runner"] == outcome and doc["completed"] == 3


def test_runner_saved_even_if_server_is_gone(tmp_path):
    path = tmp_path / "stats.json"
    outcome = {"status": "aborted", "reason": "안전장치: 30초 동안 완료 없음"}
    assert save_stats(f"http://127.0.0.1:{free_port()}", path, outcome) is None
    assert json.loads(path.read_text(encoding="utf-8"))["runner"] == outcome
