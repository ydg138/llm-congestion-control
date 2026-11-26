"""실행 결과 상태(runner.status)와 stats JSON 저장을 손으로 정한 응답으로 확인한다.

run_experiment는 자기 이벤트 루프(asyncio.run)를 만들므로, 가짜 서버는 표준 라이브러리
http.server를 스레드로 띄운다. 명령줄 인자의 기본값도 확인한다.
"""

import csv
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from runner import e4_check
from runner.agent import AgentSpec
from runner.experiments import E1, E1_SERVER, E3, E6, Condition
from runner.request_log import RequestLog
from runner.run import REPO, Backend, check, parse_args, run_experiment, run_once, save_stats, server_launch
from runner.server_proc import ServerTimes, free_port, server_command
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


def test_server_dir_defaults_to_this_repo():
    args = parse_args(["--exp", "minimum"])                 # --server-dir 없이
    repo = Path(__file__).resolve().parents[1]              # tests/의 상위 = 저장소 폴더
    assert args.server_dir == repo
    assert (args.server_dir / "server" / "mock_llm.py").is_file()   # 서버 코드가 실제로 여기 있다


def test_server_dir_can_still_be_given(tmp_path):
    assert parse_args(["--exp", "minimum", "--server-dir", str(tmp_path)]).server_dir == tmp_path


def test_runner_saved_even_if_server_is_gone(tmp_path):
    path = tmp_path / "stats.json"
    outcome = {"status": "aborted", "reason": "안전장치: 30초 동안 완료 없음"}
    assert save_stats(f"http://127.0.0.1:{free_port()}", path, outcome) is None
    assert json.loads(path.read_text(encoding="utf-8"))["runner"] == outcome


# ---------- --backend ollama, --base-url (E6) ----------

def test_mock_backend_command_is_unchanged(tmp_path):
    module, extra, times = server_launch(E1[0], Backend())
    cmd = server_command(Path("py.exe"), 8000, tmp_path, None, module, extra)
    assert cmd == ["py.exe", "-u", "-m", "server.mock_llm", "--port", "8000", "--log-dir", str(tmp_path.resolve())]
    assert times == ServerTimes()                                   # 준비 대기도 지금과 같음 (15초)


def test_ollama_backend_starts_relay_with_fixed_num_ctx(tmp_path):
    config = tmp_path / "c.json"
    module, extra, times = server_launch(E3[0], Backend("ollama"))
    cmd = server_command(Path("py.exe"), 8001, tmp_path, config, module, extra)
    assert cmd[2:4] == ["-m", "runner.real_adapter"] and cmd[cmd.index("--config") + 1] == str(config.resolve())
    # light 에이전트: 150 + 64 + 256 = 470 → 최소 4096
    assert extra == ["--model", "llama3.1:8b", "--ollama-url", "http://127.0.0.1:11434", "--num-ctx", "4096"]
    assert times.ready_seconds == 120                               # 워밍업(모델 적재)까지 기다림
    assert server_launch(E3[0], Backend("ollama", num_ctx=8192))[1][-1] == "8192"   # 직접 주면 그 값


def test_base_url_only_for_a_single_run(tmp_path):
    attach = ["--base-url", "http://127.0.0.1:1"]
    with pytest.raises(SystemExit):
        parse_args(["--exp", "E1", *attach])                        # 실행 30개 → 대조가 맞지 않음
    assert parse_args(["--exp", "E1", "--controller", "C2", "--seed", "1", *attach]).base_url == attach[1]
    for extra in (["--backend", "ollama"], ["--server-dir", str(tmp_path)]):
        with pytest.raises(SystemExit):                             # 서버를 띄우지 않으므로 함께 쓸 수 없음
            parse_args(["--exp", "E3", "--seed", "1", *attach, *extra])


def test_ollama_backend_needs_relay_code(tmp_path):
    with pytest.raises(SystemExit):
        parse_args(["--exp", "E3", "--backend", "ollama", "--server-dir", str(tmp_path)])
    assert parse_args(["--exp", "E3", "--backend", "ollama"]).backend == "ollama"   # 이 저장소에는 있다


def test_attach_runs_on_existing_server_without_starting_one(server, tmp_path):
    cond = Condition("T", "C2", (AgentSpec("a1", "light", "C2"),), 3, E1_SERVER)
    # 서버 폴더가 없어도 된다: 서버를 띄우지 않는다는 뜻
    row = run_once(cond, 1, tmp_path, tmp_path / "없는 폴더", Limits(), attach_url=url(server))
    assert (row["run_id"], row["status"]) == ("fake", "completed")
    assert (tmp_path / row["csv"]).is_file()
    assert (tmp_path / row["csv"].replace(".csv", "_stats.json")).is_file()


# ---------- E6 ----------

def test_e6_relay_num_ctx_is_4096():
    # heavy_in: 1500 + 64 + 256 = 1820 → 최소 4096 (긴 입력 측정의 16384와 다르다)
    assert server_launch(E6[0], Backend("ollama"))[1][-1] == "4096"


@pytest.mark.skipif(not (REPO / ".venv" / "Scripts" / "python.exe").is_file(),
                    reason="서버를 띄울 .venv가 없음 (Windows)")
def test_e6_one_run_on_mock_backend(tmp_path):
    """--backend mock(기본)으로 E6-input C2 한 건. 노트북 리허설과 같은 경로 (서버를 실제 프로세스로 띄움)."""
    cond = replace(E6[0], tasks=5)                          # --tasks 5와 같은 빠른 확인
    assert (cond.exp, cond.label) == ("E6-input", "C2")
    row = run_once(cond, 1, tmp_path, REPO, Limits(max_seconds=60))
    assert row["status"] == "completed", row["reason"]
    run = {"exp": cond.exp, "controller": cond.label, "seed": 1, **row, "check": check(tmp_path / row["csv"])}
    assert run["check"] == "OK"
    # e4_check가 E6 결과 폴더에서도 돈다. 줄 앞부분만 본다 (PR #25가 뒤에 열을 붙임)
    assert e4_check.check_run(tmp_path, run).startswith("E6-input C2 seed1: completed·대조 OK | 같이 돈 구간")
