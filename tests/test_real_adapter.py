"""Ollama 중계 서버(runner/real_adapter.py)를 가짜 Ollama에 붙여 확인한다. 실제 Ollama는 쓰지 않는다."""

import asyncio
import csv
import json
import time

import aiohttp
import pytest
from aiohttp import web

from runner import real_adapter
from runner.real_adapter import RELAY_COLUMNS, create_app, load_config, parse_args
from runner.relay_rules import RelayConfig
from runner.server_proc import free_port
from server import mock_llm
from server.config import ServerConfig
from server.model import LLMModel
from tests.test_ollama_client import OK_LINES, fake_ollama, serve
from tests.test_relay_rules import MOCK_200_KEYS, words_of


async def start_relay(ollama_url, log_dir=None, config=ServerConfig(), **relay_changes):
    """중계 서버를 띄운다. runner.setup()(워밍업 포함)이 끝나야 포트가 열린다."""
    app = create_app(config, RelayConfig(ollama_url=ollama_url, **relay_changes), log_dir)
    return await serve(app)


async def get_json(session, url):
    async with session.get(url) as r:
        return await r.json(), r.headers


def test_warmup_before_port_opens_and_stats_match_mock():
    async def call():
        ollama_runner, ollama_url = await serve(fake_ollama(OK_LINES, delay=0.5)[0])   # 워밍업 0.5초
        started = time.perf_counter()
        relay_runner, relay_url = await start_relay(ollama_url, num_ctx=5120)
        setup_seconds = time.perf_counter() - started
        mock_runner, mock_url = await serve(mock_llm.create_app())
        async with aiohttp.ClientSession() as s:
            health, headers = await get_json(s, relay_url + "/health")
            stats, _ = await get_json(s, relay_url + "/stats")
            config, _ = await get_json(s, relay_url + "/config")
            mock_stats, _ = await get_json(s, mock_url + "/stats")
        for runner in (relay_runner, mock_runner, ollama_runner):
            await runner.cleanup()
        return setup_seconds, health, headers, stats, config, mock_stats

    setup_seconds, health, headers, stats, config, mock_stats = asyncio.run(call())
    assert setup_seconds >= 0.5                                     # 워밍업이 끝나야 포트가 열린다
    assert health["run_id"] == headers["X-Run-Id"] == stats["run_id"]
    assert set(mock_stats) <= set(stats)                            # 가짜 서버 /stats 키 전부
    assert stats["queued"] == 0 and stats["backend"] == "ollama"
    assert stats["elapsed_seconds"] < 0.5                           # 시작 시각을 워밍업 뒤에 정함
    assert stats["ollama"]["num_ctx"] == 5120
    assert stats["ollama"]["version"] == {"version": "0.0-test"} and stats["ollama"]["ps_at_start"]["models"]
    assert config["relay"]["num_ctx"] == 5120 and "slots" in config["unused_by_relay"]


def test_server_csv_and_relay_csv_are_created(tmp_path):
    async def call():
        ollama_runner, url = await serve(fake_ollama(OK_LINES)[0])
        relay_runner, _ = await start_relay(url, log_dir=tmp_path)
        run_id = relay_runner.app[mock_llm.STATE].run_id
        await relay_runner.cleanup()
        await ollama_runner.cleanup()
        return run_id

    run_id = asyncio.run(call())
    for name in ("server_state", "agent_stats", "relay_requests"):
        assert (tmp_path / f"{name}_{run_id}.csv").is_file()
    with open(tmp_path / f"relay_requests_{run_id}.csv", encoding="utf-8", newline="") as f:
        assert next(csv.reader(f)) == RELAY_COLUMNS


def test_startup_fails_when_ollama_is_off():
    with pytest.raises(RuntimeError, match="connection_error"):     # 워밍업 실패 → 서버가 뜨지 않음
        asyncio.run(start_relay(f"http://127.0.0.1:{free_port()}"))


def test_cli_config(tmp_path):
    path = tmp_path / "c.json"
    path.write_text('{"capacity": 1}', encoding="utf-8")
    assert load_config(path).capacity == 1 and load_config(None) == ServerConfig()
    args = parse_args(["--num-ctx", "5120", "--model", "m"])
    assert (args.num_ctx, args.model, args.ollama_url) == (5120, "m", "http://127.0.0.1:11434")


# ---------- /generate (3b) ----------

def ollama_for_relay(mode="ok"):
    """워밍업(num_predict=1)에는 늘 정상으로, 실험 요청에는 mode대로 답하는 가짜 Ollama.

    ok: num_predict개 조각 뒤 done / busy: 503 / crash: 조각 2개 뒤 error / hang: 조각 3개 뒤 멈춤
    """
    received, hold = [], asyncio.Event()

    async def generate(request):
        payload = await request.json()
        received.append(payload)
        n = payload["options"]["num_predict"]
        now = "ok" if n == 1 else mode
        if now == "busy":
            return web.json_response({"error": "server busy"}, status=503)
        response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await response.prepare(request)
        for _ in range({"ok": n, "crash": 2, "hang": 3}[now]):
            await response.write(b'{"response":"x","done":false}\n')
        if now == "crash":
            await response.write(b'{"error":"model crashed"}\n')
        elif now == "hang":
            await hold.wait()
        else:
            final = {"done": True, "done_reason": "length", "eval_count": n, "prompt_eval_count": 7,
                     "prompt_eval_duration": 250_000_000, "eval_duration": 500_000_000, "load_duration": 0}
            await response.write(json.dumps(final).encode() + b"\n")
        await response.write_eof()
        return response

    async def info(request):
        return web.json_response({"version": "0.0-test", "models": []})

    app = web.Application()
    app.add_routes([web.post("/api/generate", generate), web.get("/api/version", info), web.get("/api/ps", info)])
    return app, received, hold


async def with_relay(ollama, client, config=ServerConfig(), log_dir=None):
    """가짜 Ollama와 중계 서버를 띄우고 client(session, 중계 서버 주소)를 돌린 뒤 끈다."""
    app, _, hold = ollama
    ollama_runner, ollama_url = await serve(app)
    relay_app = create_app(config, RelayConfig(ollama_url=ollama_url), log_dir)
    relay_runner, relay_url = await serve(relay_app, handler_cancellation=True)   # 연결이 끊기면 핸들러 취소
    try:
        async with aiohttp.ClientSession() as s:
            return await client(s, relay_url)
    finally:
        hold.set()
        await relay_runner.cleanup()
        await ollama_runner.cleanup()


def request_body(request_id, agent="a1", workload="s1-a1-000", inputs=100, maximum=64):
    return {"request_id": request_id, "agent_id": agent, "workload_id": workload,
            "input_tokens": inputs, "max_output_tokens": maximum}


async def post(s, url, payload):
    async with s.post(url + "/generate", json=payload) as r:
        return r.status, await r.json(), r.headers


def read_csv(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def assert_totals_match(stats):
    """같은 /stats에서 서버 전체 입력·출력 합 = 에이전트별 합 (interface.md 4절)."""
    agents = stats["agent_stats"].values()
    assert stats["input_tokens_consumed"] == sum(a["input_tokens_consumed"] for a in agents)
    assert stats["output_tokens_consumed"] == sum(a["output_tokens_consumed"] for a in agents)


def test_success_matches_mock_body_and_token_totals(tmp_path):
    ollama = ollama_for_relay()

    async def client(s, url):
        first = await post(s, url, request_body("r1"))
        await post(s, url, request_body("r2", agent="a2", workload="s1-a2-000", inputs=120))
        return first, (await get_json(s, url + "/stats"))[0]

    (status, body, headers), stats = asyncio.run(with_relay(ollama, client, log_dir=tmp_path))
    target = LLMModel(ServerConfig().model()).plan("s1-a1-000", 100, 64).output_tokens
    assert status == 200 and MOCK_200_KEYS <= set(body)
    assert body["output_tokens"] == body["target_output_tokens"] == target      # 가짜 서버와 같은 목표 길이
    assert "X-RateLimit-Remaining" in headers and headers["X-Run-Id"] == stats["run_id"]
    sent = ollama[1][1]                                                         # [0]은 워밍업
    assert sent["options"]["num_predict"] == target and len(words_of(sent["prompt"])) == 100
    assert stats["input_tokens_consumed"] == 220                               # 100 + 120
    assert_totals_match(stats)
    assert (stats["accepted"], stats["completed"], stats["active"], stats["output_tokens_reserved"]) == (2, 2, 0, 0)
    assert stats["ollama"]["done_reasons"] == {"length": 2}
    rows = read_csv(tmp_path / f"relay_requests_{stats['run_id']}.csv")
    assert [r["status"] for r in rows] == ["200", "200"] and rows[0]["output_tokens"] == str(target)
    last = [r for r in read_csv(tmp_path / f"agent_stats_{stats['run_id']}.csv") if r["agent_id"] == "a1"][-1]
    assert (last["input_tokens_consumed"], last["output_tokens_consumed"]) == ("100", str(target))


def test_second_request_gets_429_when_bucket_is_empty():
    async def client(s, url):
        await post(s, url, request_body("r1"))
        return await post(s, url, request_body("r2")), (await get_json(s, url + "/stats"))[0]

    config = ServerConfig(capacity=1, refill_rate=0.000001)                    # 요청 1개만, 거의 안 채워짐
    (status, body, headers), stats = asyncio.run(with_relay(ollama_for_relay(), client, config=config))
    assert status == 429 and body["error"] == "rate_limit_exceeded" and body["retry_after_seconds"] > 0
    assert int(headers["Retry-After"]) >= 1
    assert stats["rate_limited"] == 1 == stats["agent_stats"]["a1"]["rate_limited"]


def test_ollama_busy_becomes_529_and_is_rolled_back():
    async def client(s, url):
        return await post(s, url, request_body("r1")), (await get_json(s, url + "/stats"))[0]

    (status, body, headers), stats = asyncio.run(with_relay(ollama_for_relay("busy"), client))
    assert status == 529 and body["error"] == "server_overloaded" and "X-RateLimit-Remaining" not in headers
    assert (stats["overloaded"], stats["accepted"], stats["input_tokens_consumed"]) == (1, 0, 0)
    assert stats["output_tokens_reserved"] == 0 and stats["agent_stats"]["a1"]["input_tokens_consumed"] == 0
    assert stats["quotas"]["requests"]["remaining"] == pytest.approx(ServerConfig().capacity)  # 요청 1개도 돌려받음
    assert_totals_match(stats)


def test_request_over_num_ctx_is_422_without_calling_ollama():
    ollama = ollama_for_relay()

    async def client(s, url):
        return await post(s, url, request_body("r1", inputs=4000))             # 4000 + 64 + 256 > 4096

    status, body, _ = asyncio.run(with_relay(ollama, client))
    assert status == 422 and body["error"] == "invalid_generation"
    assert len(ollama[1]) == 1                                                  # 워밍업만 Ollama로 갔다


def test_error_mid_stream_is_502_and_settles_received_chunks():
    async def client(s, url):
        return await post(s, url, request_body("r1")), (await get_json(s, url + "/stats"))[0]

    (status, body, _), stats = asyncio.run(with_relay(ollama_for_relay("crash"), client))
    assert status == 502 and body["backend_error"] == "backend_error"
    assert stats["ollama"]["backend_errors"] == 1 and stats["output_tokens_consumed"] == 2   # 받은 조각 2개
    assert (stats["completed"], stats["active"], stats["output_tokens_reserved"]) == (0, 0, 0)
    assert_totals_match(stats)


def test_client_disconnect_cancels_and_settles_received_chunks():
    async def client(s, url):
        with pytest.raises(TimeoutError):                                       # 실행기 쪽 타임아웃처럼 끊는다
            async with s.post(url + "/generate", json=request_body("r1"),
                              timeout=aiohttp.ClientTimeout(total=0.5)) as r:
                await r.read()
        for _ in range(50):                                                     # 중계 서버가 취소를 처리할 때까지
            stats = (await get_json(s, url + "/stats"))[0]
            if stats["cancelled"]:
                break
            await asyncio.sleep(0.05)
        return stats

    stats = asyncio.run(with_relay(ollama_for_relay("hang"), client))
    assert (stats["cancelled"], stats["active"], stats["output_tokens_reserved"]) == (1, 0, 0)
    assert stats["output_tokens_consumed"] == 3 == stats["agent_stats"]["a1"]["output_tokens_consumed"]
    assert_totals_match(stats)


def test_unexpected_exception_is_settled_as_502_not_cancelled(monkeypatch, tmp_path):
    async def broken(*args, **kwargs):
        raise RuntimeError("코드 버그 흉내")

    monkeypatch.setattr(real_adapter, "stream_generate", broken)               # 워밍업은 그대로 동작

    async def client(s, url):
        async with s.post(url + "/generate", json=request_body("r1")) as r:
            status = r.status
        return status, (await get_json(s, url + "/stats"))[0]

    status, stats = asyncio.run(with_relay(ollama_for_relay(), client, log_dir=tmp_path))
    assert status == 500                                                        # 예외는 숨기지 않는다
    assert (stats["cancelled"], stats["ollama"]["backend_errors"]) == (0, 1)    # 취소가 아니라 502로 정산
    assert (stats["active"], stats["output_tokens_reserved"]) == (0, 0)
    assert_totals_match(stats)
    rows = read_csv(tmp_path / f"relay_requests_{stats['run_id']}.csv")
    assert (rows[0]["status"], rows[0]["error"]) == ("502", "exception RuntimeError")


# ---------- P1 검토 11/26: 검토자의 reviewer_checks.py에서 옮김 ----------

def test_concurrent_quota_and_totals():
    async def client(s, url):
        replies = await asyncio.gather(*[
            post(s, url, request_body(f"r{i}", agent=f"a{i % 2}")) for i in range(20)
        ])
        stats = (await get_json(s, url + "/stats"))[0]
        assert sum(r[0] == 200 for r in replies) == 3
        assert sum(r[0] == 429 for r in replies) == 17
        assert stats["input_tokens_consumed"] == 300
        assert stats["output_tokens_consumed"] == sum(r[1].get("output_tokens", 0) for r in replies)
        assert stats["active"] == stats["output_tokens_reserved"] == 0
        assert_totals_match(stats)
    asyncio.run(with_relay(ollama_for_relay(), client,
                          config=ServerConfig(capacity=3, refill_rate=0.000001)))


@pytest.mark.parametrize("mode,code,inputs,outputs", [("busy",529,0,0),("crash",502,200,4)])
def test_two_agents_error_accounting(mode, code, inputs, outputs):
    async def client(s, url):
        replies = await asyncio.gather(*[
            post(s, url, request_body(f"r{i}", agent=f"a{i}")) for i in range(2)
        ])
        stats = (await get_json(s, url + "/stats"))[0]
        assert all(r[0] == code for r in replies)
        assert stats["input_tokens_consumed"] == inputs
        assert stats["output_tokens_consumed"] == outputs
        assert stats["active"] == stats["output_tokens_reserved"] == 0
        assert_totals_match(stats)
        if code == 529:
            for key in ("requests", "input_tokens", "output_tokens"):
                q = stats["quotas"][key]
                assert q["remaining"] == q["capacity"]
    asyncio.run(with_relay(ollama_for_relay(mode), client))


def test_early_stop_uses_actual_count(monkeypatch):
    async def early(session, cfg, payload, result):
        result.status = 200
        result.chunks = 1
        result.first_chunk = result.finished = time.perf_counter()
        result.final = {"done": True, "done_reason": "stop", "eval_count": 3,
                        "prompt_eval_count": 125}
    monkeypatch.setattr(real_adapter, "stream_generate", early)
    async def client(s, url):
        reply = await post(s, url, request_body("r"))
        stats = (await get_json(s, url + "/stats"))[0]
        assert reply[0] == 200 and reply[1]["output_tokens"] == 3
        assert stats["output_tokens_consumed"] == 3
        assert stats["input_tokens_consumed"] == 100
        assert stats["ollama"]["prompt_eval_count_total"] == 125
        assert_totals_match(stats)
    asyncio.run(with_relay(ollama_for_relay(), client))


def test_invalid_final_must_not_break_accounting(monkeypatch):
    async def malformed(session, cfg, payload, result):
        result.status = 200
        result.chunks = 2
        result.first_chunk = result.finished = time.perf_counter()
        result.final = {"done": True, "eval_count": None}
    monkeypatch.setattr(real_adapter, "stream_generate", malformed)
    async def client(s, url):
        async with s.post(url + "/generate", json=request_body("r")) as response:
            code = response.status
            await response.read()
        stats = (await get_json(s, url + "/stats"))[0]
        assert code == 502
        assert stats["accepted"] == stats["completed"] + stats["cancelled"] + stats["ollama"]["backend_errors"]
    asyncio.run(with_relay(ollama_for_relay(), client))


# ---------- P1 검토 11/26: 마지막 조각의 eval_count 경우별 정산 ----------

MISSING = object()          # eval_count 키가 아예 없음 (Ollama는 0인 값을 응답에서 뺀다)


def inject_final(monkeypatch, final, chunks):
    """stream_generate 대신 조각 chunks개와 마지막 조각 final을 받은 것으로 한다 (워밍업은 그대로)."""
    async def fake(session, cfg, payload, result):
        result.status, result.chunks, result.final = 200, chunks, final
        result.first_chunk = result.finished = time.perf_counter()

    monkeypatch.setattr(real_adapter, "stream_generate", fake)


@pytest.mark.parametrize("eval_count, code, settled", [
    (MISSING, 200, 2),                  # 키 없음: 조각 수 min(2, 64)
    (0, 200, 0), (64, 200, 64),         # 경계: 0 이상 상한(64) 이하는 정상
    (None, 502, 2), ("5", 502, 2), (2.5, 502, 2), (True, 502, 2), (-1, 502, 2), (65, 502, 2),  # 조각 수로 한 번만
], ids=["missing", "zero", "maximum", "null", "string", "float", "bool", "negative", "over_maximum"])
def test_eval_count_cases_are_settled_once(monkeypatch, tmp_path, eval_count, code, settled):
    final = {"done": True, "done_reason": "stop"}
    if eval_count is not MISSING:
        final["eval_count"] = eval_count
    inject_final(monkeypatch, final, chunks=2)

    async def client(s, url):
        return await post(s, url, request_body("r")), (await get_json(s, url + "/stats"))[0]   # 출력 상한 64

    config = ServerConfig(output_refill_rate=0.000001)      # 출력 버킷이 거의 다시 차지 않게 (잔량을 손으로 계산)
    (status, body, _), stats = asyncio.run(with_relay(ollama_for_relay(), client, config=config, log_dir=tmp_path))
    assert status == code
    if code == 200:
        assert body["output_tokens"] == settled
    else:
        assert body["backend_error"] == "invalid_response"
    assert stats["accepted"] == stats["completed"] + stats["cancelled"] + stats["ollama"]["backend_errors"] == 1
    assert stats["active"] == stats["output_tokens_reserved"] == 0
    assert stats["output_tokens_consumed"] == settled
    assert stats["quotas"]["output_tokens"]["remaining"] == pytest.approx(1000 - settled, abs=0.01)  # 안 쓴 예약은 환불
    assert_totals_match(stats)
    row = read_csv(tmp_path / f"relay_requests_{stats['run_id']}.csv")[0]
    assert (row["status"], row["output_tokens"]) == (str(code), str(settled))
    if code == 502:
        assert row["error"] == f"invalid_response eval_count={eval_count!r}"       # message에 받은 값


def test_odd_prompt_eval_count_and_done_reason_do_not_fail_request(monkeypatch):
    inject_final(monkeypatch, {"done": True, "eval_count": 3, "prompt_eval_count": "125",
                               "done_reason": ["stop"]}, chunks=3)

    async def client(s, url):
        return await post(s, url, request_body("r")), (await get_json(s, url + "/stats"))[0]

    (status, body, _), stats = asyncio.run(with_relay(ollama_for_relay(), client))
    assert status == 200 and body["output_tokens"] == 3
    assert stats["ollama"]["prompt_eval_count_total"] == 0         # 0 이상 정수가 아니면 합에 더하지 않는다
    assert stats["ollama"]["done_reasons"] == {'["stop"]': 1}      # 문자열이 아니면 JSON 표기를 키로
    assert (stats["completed"], stats["active"], stats["output_tokens_reserved"]) == (1, 0, 0)
    assert_totals_match(stats)
