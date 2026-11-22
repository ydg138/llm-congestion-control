"""Ollama 중계 서버(runner/real_adapter.py)를 가짜 Ollama에 붙여 확인한다. 실제 Ollama는 쓰지 않는다."""

import asyncio
import csv
import time

import aiohttp
import pytest

from runner.real_adapter import RELAY_COLUMNS, create_app, load_config, parse_args
from runner.relay_rules import RelayConfig
from runner.server_proc import free_port
from server import mock_llm
from server.config import ServerConfig
from tests.test_ollama_client import OK_LINES, fake_ollama, serve


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
