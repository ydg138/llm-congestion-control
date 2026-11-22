"""Ollama 통신(runner/ollama_client.py)을 가짜 Ollama로 확인한다. 실제 Ollama는 쓰지 않는다."""

import asyncio
import json

import pytest
from aiohttp import web

from runner.ollama_client import StreamResult, make_session, server_info, stream_generate, warmup
from runner.relay_rules import RelayConfig, ollama_payload
from runner.server_proc import free_port

FINAL = {"done": True, "done_reason": "length", "response": "", "eval_count": 2, "prompt_eval_count": 12,
         "prompt_eval_duration": 250_000_000, "eval_duration": 500_000_000}
OK_LINES = [b'{"response":"a","done":false}\n',
            b'{"response":', b'"b","done":false}\n',           # 조각 하나가 두 번에 나뉘어 옴
            b'{"response":"","done":false}\n',                  # 빈 조각은 세지 않음
            json.dumps(FINAL).encode() + b"\n"]


def fake_ollama(lines, status=200, delay=0.0, hold=None):
    """lines를 NDJSON으로 보내는 가짜 Ollama. hold(asyncio.Event)를 주면 다 보낸 뒤 그 이벤트를 기다린다."""
    received = []

    async def generate(request):
        received.append(await request.json())
        if status != 200:
            return web.json_response({"error": "server busy"}, status=status)
        response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await response.prepare(request)
        await asyncio.sleep(delay)
        for line in lines:
            await response.write(line)
            await asyncio.sleep(0)
        if hold is not None:
            await hold.wait()
        await response.write_eof()
        return response

    async def version(request):
        return web.json_response({"version": "0.0-test"})

    async def ps(request):
        return web.json_response({"models": [{"name": "llama3.1:8b", "size_vram": 1}]})

    app = web.Application()
    app.add_routes([web.post("/api/generate", generate), web.get("/api/version", version),
                    web.get("/api/ps", ps)])
    return app, received


async def serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner, f"http://127.0.0.1:{runner.addresses[0][1]}"


async def generate_once(app, **cfg_changes):
    runner, url = await serve(app)
    cfg = RelayConfig(ollama_url=url, **cfg_changes)
    result = StreamResult()
    try:
        async with make_session(cfg) as session:
            await stream_generate(session, cfg, ollama_payload(cfg, "p", num_predict=40, seed=42), result)
    finally:
        await runner.cleanup()
    return result


def test_stream_counts_nonempty_chunks_and_keeps_final():
    app, received = fake_ollama(OK_LINES)
    result = asyncio.run(generate_once(app))
    assert (result.status, result.error, result.chunks) == (200, "", 2)       # "a", "b" (빈 조각 제외)
    assert result.final["eval_count"] == 2 and result.first_chunk <= result.finished
    assert received[0]["options"] == {"num_ctx": 4096, "num_predict": 40, "seed": 42}


def test_long_last_line_is_parsed():
    final = dict(FINAL, context=list(range(40000)))                           # 마지막 조각에 큰 context 배열
    line = json.dumps(final).encode() + b"\n"
    assert len(line) > 200_000
    result = asyncio.run(generate_once(fake_ollama([b'{"response":"a"}\n', line])[0]))
    assert result.error == "" and result.final["eval_count"] == 2


@pytest.mark.parametrize("lines, status, changes, error", [
    ([], 503, {}, "http_503"),                                                # Ollama 대기열이 가득 참
    ([b'{"response":"a"}\n', b'{"error":"model crashed"}\n'], 200, {}, "backend_error"),
    ([b'{"response":"a"}\n'], 200, {}, "incomplete_stream"),                  # done 없이 끝남
    ([b"not json\n"], 200, {}, "invalid_response"),
    ([b'{"response":"' + b"x" * 4000 + b'"}\n'], 200, {"read_bufsize": 1024}, "invalid_response"),  # 한 줄 > 2 KiB
])
def test_errors_are_classified(lines, status, changes, error):
    app, _ = fake_ollama(lines, status=status)
    result = asyncio.run(generate_once(app, **changes))
    assert result.error == error
    if status == 503:
        assert "server busy" in result.message


def test_timeout():
    app, _ = fake_ollama(OK_LINES, delay=1.0)
    assert asyncio.run(generate_once(app, ollama_timeout_seconds=0.2)).error == "timeout"


def test_connection_error_when_ollama_is_off():
    async def call():
        cfg = RelayConfig(ollama_url=f"http://127.0.0.1:{free_port()}")      # 아무도 받지 않는 포트
        result = StreamResult()
        async with make_session(cfg) as session:
            await stream_generate(session, cfg, ollama_payload(cfg, "p", 1, 0), result)
        return result
    assert asyncio.run(call()).error == "connection_error"


def test_cancel_keeps_chunks_received_so_far():
    async def call():
        hold = asyncio.Event()
        app, _ = fake_ollama([b'{"response":"a"}\n'] * 3, hold=hold)
        runner, url = await serve(app)
        cfg = RelayConfig(ollama_url=url)
        result = StreamResult()
        async with make_session(cfg) as session:
            task = asyncio.create_task(stream_generate(session, cfg, ollama_payload(cfg, "p", 40, 0), result))
            while result.chunks < 3:                                          # 조각 3개를 받을 때까지
                await asyncio.sleep(0.01)
            task.cancel()                                                     # 클라이언트가 끊은 것과 같음
            await asyncio.gather(task, return_exceptions=True)
        hold.set()
        await runner.cleanup()
        return result, task
    result, task = asyncio.run(asyncio.wait_for(call(), 5))
    assert task.cancelled() and result.chunks == 3 and result.final is None


def test_warmup_uses_same_num_ctx_and_fails_loudly():
    app, received = fake_ollama(OK_LINES)

    async def call(app, cfg_changes):
        runner, url = await serve(app)
        cfg = RelayConfig(ollama_url=url, **cfg_changes)
        try:
            async with make_session(cfg) as session:
                return await warmup(session, cfg), await server_info(session, cfg)
        finally:
            await runner.cleanup()

    _, info = asyncio.run(call(app, {"num_ctx": 5120}))
    assert received[0]["options"]["num_ctx"] == 5120 and received[0]["options"]["num_predict"] == 1
    assert info == {"version": {"version": "0.0-test"}, "ps": {"models": [{"name": "llama3.1:8b", "size_vram": 1}]}}
    with pytest.raises(RuntimeError, match="http_503"):
        asyncio.run(call(fake_ollama([], status=503)[0], {}))
