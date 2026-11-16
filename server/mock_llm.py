"""P1 LLM 실험 서버. python -m server.mock_llm --help"""

import argparse
import asyncio
import contextlib
import hmac
import json
import math
import os
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from aiohttp import web

from .config import ServerConfig, validate_scenario
from .experiment_log import record_experiment
from .model import LLMModel
from .rate_limit import MultiQuota, TokenBucket

API_VERSION = "2.0"


@dataclass
class State:
    config: ServerConfig
    quota: MultiQuota
    admin_token: str | None = None
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    started_at: float = field(default_factory=time.perf_counter)
    revision: int = 1
    accepted: int = 0
    rejected: int = 0
    overloaded: int = 0
    completed: int = 0
    agent_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    cancelled: int = 0
    active: int = 0
    waiting: deque = field(default_factory=deque)
    input_consumed: int = 0
    output_consumed: int = 0
    output_reserved: int = 0
    history: list = field(default_factory=list)
    scenario_status: str = "idle"


STATE = web.AppKey("state", State)


@web.middleware
async def json_errors(request, handler):
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        body = {"error": {400: "invalid_json", 404: "not_found", 405: "method_not_allowed",
                          413: "payload_too_large", 415: "expected_application_json"}.get(
                              exc.status, "http_error"), "message": exc.reason}
        response = web.json_response(body, status=exc.status)
        if "Allow" in exc.headers:
            response.headers["Allow"] = exc.headers["Allow"]
    response.headers["X-API-Version"] = API_VERSION
    response.headers["X-Run-Id"] = request.app[STATE].run_id
    response.headers["Cache-Control"] = "no-store"
    return response


def reject_constant(value):
    raise ValueError("nonfinite JSON number: " + value)


async def read_json(request):
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(reason="Content-Type must be application/json")
    if request.charset and request.charset.lower() not in ("utf-8", "utf8"):
        raise web.HTTPBadRequest(reason="JSON charset must be UTF-8")
    try:
        value = json.loads((await request.read()).decode("utf-8"), parse_constant=reject_constant)
    except (ValueError, UnicodeError, LookupError, RecursionError):
        raise web.HTTPBadRequest(reason="Invalid UTF-8 JSON") from None
    if not isinstance(value, dict):
        raise web.HTTPBadRequest(reason="JSON must be an object")
    return value


def integer(value, minimum=0, maximum=10**9):
    return not isinstance(value, bool) and isinstance(value, int) and minimum <= value <= maximum


async def index(request):
    return web.Response(content_type="text/html", text="""<!doctype html>
<html lang="ko"><meta charset="utf-8"><title>P1 LLM 실험 서버</title>
<h1>P1 LLM 실험 서버 · API 2.0</h1><p>요청·입력·출력 한도, 대기열, 출력 길이와 지연 모델</p>
<ul><li><a href="/health">상태</a></li><li><a href="/stats">통계</a></li>
<li><a href="/config">실험 설정과 변경 기록</a></li></ul>
<p>실험 요청은 POST /generate로 JSON을 보내세요. 실제 LLM을 호출하지 않는 시뮬레이터입니다.</p></html>""")


async def health(request):
    state = request.app[STATE]
    return web.json_response({"status": "ok", "run_id": state.run_id, "api_version": API_VERSION,
                              "capabilities": ["request_rate_limit", "input_output_quotas",
                                               "bounded_queue", "deterministic_output",
                                               "llm_delay_model", "runtime_config", "scenario"]})


async def stats(request):
    state = request.app[STATE]
    async with state.lock:
        return web.json_response({
            "run_id": state.run_id, "config_revision": state.revision,
            "capacity": state.config.capacity, "refill_rate_per_second": state.config.refill_rate,
            "accepted": state.accepted, "rate_limited": state.rejected,
            "completed": state.completed, "overloaded": state.overloaded,
            "agent_stats": state.agent_stats,
            "cancelled": state.cancelled, "active": state.active, "queued": len(state.waiting),
            "slots": state.config.slots, "queue_size": state.config.queue_size,
            "service_delay_seconds": state.config.service_delay,
            "input_tokens_consumed": state.input_consumed, "output_tokens_consumed": state.output_consumed,
            "output_tokens_reserved": state.output_reserved, "quotas": state.quota.snapshot(),
            "scenario_status": state.scenario_status,
        })


async def get_config(request):
    state = request.app[STATE]
    async with state.lock:
        return web.json_response({"run_id": state.run_id, "revision": state.revision,
                                  "config": asdict(state.config), "changes": list(state.history),
                                  "scenario_status": state.scenario_status,
                                  "runtime_updates_enabled": bool(state.admin_token)})


def promote(state):
    """lock을 소유한 상태에서 호출. 축소 중에는 기존 요청이 끝날 때까지 기다림."""
    while state.waiting and state.active < state.config.slots:
        ready = state.waiting.popleft()
        if not ready.cancelled():
            state.active += 1
            ready.set_result(None)


def apply_changes(state, changes, source):
    updated = state.config.changed(changes)
    if len(state.history) >= 2000:
        raise ValueError("configuration history limit reached; start a new run")
    for name, capacity, rate in [
        ("requests", updated.capacity, updated.refill_rate),
        ("input_tokens", updated.input_capacity, updated.input_refill_rate),
        ("output_tokens", updated.output_capacity, updated.output_refill_rate),
    ]:
        state.quota.buckets[name].reconfigure(capacity=capacity, refill_rate=rate)
    state.config = updated
    state.revision += 1
    state.history.append({"revision": state.revision, "elapsed_seconds": time.perf_counter() - state.started_at,
                          "source": source, "changes": changes})
    promote(state)


async def update_config(request):
    state = request.app[STATE]
    authorization = request.headers.get("Authorization", "")
    if not state.admin_token or not hmac.compare_digest(
            authorization.encode(), ("Bearer " + state.admin_token).encode()):
        return web.json_response({"error": "admin_access_denied"}, status=403)
    changes = await read_json(request)
    async with state.lock:
        try:
            apply_changes(state, changes, "admin")
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": "invalid_config", "message": str(exc)}, status=400)
        return web.json_response({"revision": state.revision, "config": asdict(state.config)})


async def simulate_processing(delay):
    await asyncio.sleep(delay)


def quota_headers(state):
    snapshots = state.quota.snapshot()
    headers = {}
    for name, prefix in [("requests", "X-RateLimit"), ("input_tokens", "X-InputTokenLimit"),
                         ("output_tokens", "X-OutputTokenLimit")]:
        item = snapshots[name]
        headers[prefix + "-Limit"] = str(item["capacity"])
        headers[prefix + "-Remaining"] = str(math.floor(item["remaining"]))
    return headers


def valid_identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


async def generate(request):
    payload = await read_json(request)
    for name in ("request_id", "agent_id"):
        if not valid_identifier(payload.get(name)):
            return web.json_response({"error": "invalid_field", "field": name}, status=400)
    workload_id = payload.get("workload_id", payload["request_id"])
    if not valid_identifier(workload_id):
        return web.json_response({"error": "invalid_field", "field": "workload_id"}, status=400)
    inputs = payload.get("input_tokens", 0)
    if not integer(inputs):
        return web.json_response({"error": "invalid_field", "field": "input_tokens"}, status=400)
    state = request.app[STATE]
    arrived = time.perf_counter()
    async with state.lock:
        config = state.config
        maximum = payload.get("max_output_tokens", config.output_max)
        if not integer(maximum, 1, 10**6):
            return web.json_response({"error": "invalid_field", "field": "max_output_tokens"}, status=400)
        body = {"request_id": payload["request_id"], "agent_id": payload["agent_id"],
                "workload_id": workload_id, "run_id": state.run_id, "config_revision": state.revision}
        # E3에서 완료 건수가 0인 참여자도 관찰할 수 있도록 첫 요청 때 등록한다.
        counts = state.agent_stats.setdefault(payload["agent_id"], {"completed": 0, "rate_limited": 0})
        try:
            plan = LLMModel(config.model()).plan(workload_id, inputs, maximum)
            if plan.total_seconds + config.service_delay > 3600:
                raise ValueError("modeled request duration exceeds 3600 seconds")
        except ValueError as exc:
            return web.json_response(dict(body, error="invalid_generation", message=str(exc)), status=422)
        if state.active >= config.slots and len(state.waiting) >= config.queue_size:
            state.overloaded += 1
            return web.json_response(dict(body, error="server_overloaded"), status=529)
        decision = state.quota.reserve(inputs, maximum)
        if not decision.allowed:
            status = 422 if decision.impossible else 429
            if status == 429:
                state.rejected += 1
                counts["rate_limited"] += 1
            headers = quota_headers(state)
            if not decision.impossible:
                headers["Retry-After"] = str(max(1, math.ceil(decision.retry_after_seconds)))
            return web.json_response(dict(body, error="request_exceeds_capacity" if decision.impossible
                                          else "rate_limit_exceeded", limited_by=decision.limited_by,
                                          retry_after_seconds=decision.retry_after_seconds), status=status, headers=headers)
        state.accepted += 1
        state.input_consumed += inputs
        state.output_reserved += maximum
        ready = asyncio.get_running_loop().create_future()
        if state.active < config.slots:
            state.active += 1
            ready.set_result(None)
        else:
            state.waiting.append(ready)

    started = None
    first_token = None
    generated = 0
    succeeded = False
    try:
        await ready
        started = time.perf_counter()
        await simulate_processing(config.service_delay + plan.prefill_seconds)
        first_token = time.perf_counter()
        await simulate_processing(plan.decode_seconds)
        finished = time.perf_counter()
        generated = plan.output_tokens
        succeeded = True
        body.update(output="mock response", input_tokens=inputs, output_tokens=generated,
                    max_output_tokens=maximum, queue_wait_seconds=started-arrived,
                    prefill_seconds=first_token-started, decode_seconds=finished-first_token,
                    ttft_seconds=first_token-arrived, service_seconds=finished-started,
                    server_elapsed_seconds=finished-arrived,
                    model_prefill_seconds=config.service_delay + plan.prefill_seconds,
                    model_decode_seconds=plan.decode_seconds,
                    token_interval_seconds=plan.token_interval_seconds)
    except asyncio.CancelledError:
        if first_token is not None:
            # 실제 첫 토큰 시점부터의 경과량으로 부분 생성량 계산. prefill 중 취소는 0.
            elapsed = plan.prefill_seconds + max(0.0, time.perf_counter() - first_token)
            generated = plan.generated_tokens(elapsed)
        raise
    finally:
        async with state.lock:
            state.quota.settle_output(maximum, generated)
            state.output_reserved -= maximum
            state.output_consumed += generated
            if succeeded:
                state.completed += 1
                counts["completed"] += 1
            else:
                state.cancelled += 1
            if ready in state.waiting:
                state.waiting.remove(ready)
            elif not ready.cancelled():
                state.active -= 1
            promote(state)
    async with state.lock:
        return web.json_response(body, headers=quota_headers(state))


def create_app(capacity=5, refill_rate=1.0, bucket=None, *, slots=2, queue_size=16,
               service_delay=0.05, config=None, admin_token=None, scenario=None, log_dir=None, **options):
    config = config or ServerConfig(capacity=capacity, refill_rate=refill_rate, slots=slots,
                                    queue_size=queue_size, service_delay=service_delay, **options)
    config.validate()
    if bucket is not None:
        config = replace(config, capacity=bucket.capacity, refill_rate=bucket.refill_rate)
    quota = MultiQuota(bucket if bucket is not None else TokenBucket(config.capacity, config.refill_rate),
                       TokenBucket(config.input_capacity, config.input_refill_rate),
                       TokenBucket(config.output_capacity, config.output_refill_rate))
    events = validate_scenario([] if scenario is None else scenario, config)
    app = web.Application(client_max_size=64*1024, middlewares=[json_errors])
    state = State(config, quota, admin_token=admin_token)
    app[STATE] = state

    async def scenario_lifecycle(app):
        state.started_at = time.perf_counter()

        async def run():
            state.scenario_status = "running"
            try:
                for event in events:
                    await asyncio.sleep(max(0, event["at_seconds"] - (time.perf_counter()-state.started_at)))
                    async with state.lock:
                        apply_changes(state, event["changes"], "scenario")
                state.scenario_status = "completed"
            except ValueError as exc:
                state.scenario_status = "failed: " + str(exc)

        task = asyncio.create_task(run()) if events else None
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app.cleanup_ctx.append(scenario_lifecycle)
    if log_dir is not None:
        app.cleanup_ctx.append(lambda app: record_experiment(app[STATE], log_dir))
    app.add_routes([web.get("/", index), web.get("/health", health), web.get("/stats", stats),
                    web.get("/config", get_config), web.post("/admin/config", update_config),
                    web.post("/generate", generate)])
    return app


def main():
    parser = argparse.ArgumentParser(description="P1 LLM quota and latency simulator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--config", type=Path, help="JSON config file")
    parser.add_argument("--scenario", type=Path, help="JSON timed changes")
    parser.add_argument("--log-dir", type=Path, default=Path("results/server"),
                        help="directory for per-run server and agent CSV logs (default: results/server)")
    for name, value in asdict(ServerConfig()).items():
        parser.add_argument("--"+name.replace("_", "-"), type=type(value), default=None)
    args = parser.parse_args()
    try:
        if not 1 <= args.port <= 65535:
            raise ValueError("port must be 1..65535")
        base = ServerConfig()
        if args.config:
            base = base.changed(json.loads(args.config.read_text(encoding="utf-8")))
        overrides = {name: getattr(args, name) for name in asdict(base) if getattr(args, name) is not None}
        if overrides:
            base = base.changed(overrides)
        events = json.loads(args.scenario.read_text(encoding="utf-8")) if args.scenario else []
        app = create_app(config=base, admin_token=os.environ.get("MOCK_LLM_ADMIN_TOKEN"),
                         scenario=events, log_dir=args.log_dir)
    except (ValueError, TypeError, OSError) as exc:
        parser.error(str(exc))
    web.run_app(app, host=args.host, port=args.port, handler_cancellation=True)


if __name__ == "__main__":
    main()
