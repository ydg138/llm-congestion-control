"""E6 Ollama 중계 서버: 우리 API 2.0(/generate)을 받아 한도를 검사하고, 통과한 요청만 Ollama로 생성한다.

    python -m runner.real_adapter --port 8001 --log-dir results/server --config C.json --num-ctx 4096

- 한도·통계·헤더는 가짜 서버(server/mock_llm.py)의 State, 미들웨어, 핸들러를 그대로 쓴다 (server/는 고치지 않음)
- 실행기와 컨트롤러는 그대로다. 접속 주소만 이 서버로 바뀐다
- 서버 CSV: 가짜 서버와 같은 두 파일 + Ollama로 보낸 요청마다 한 줄인 relay_requests_<run_id>.csv
- 응답: 200(생성 완료), 429·422(한도), 529(Ollama 대기열이 가득 참 → 받지 않은 요청으로 되돌림),
  502 backend_error(그 밖의 Ollama 오류·타임아웃. 실행기는 혼잡이 아닌 오류로 보고 그 작업을 포기)
"""

import argparse
import asyncio
import csv
import json
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import aiohttp
from aiohttp import web

from runner.ollama_client import StreamResult, make_session, server_info, stream_generate, warmup
from runner.relay_rules import (
    RelayConfig, Timing, is_count, make_prompt, ollama_payload, reason_key, seconds, settled_tokens,
    success_body,
)
from server import mock_llm
from server.config import ServerConfig
from server.experiment_log import record_experiment
from server.model import LLMModel
from server.rate_limit import MultiQuota, TokenBucket

RELAY_COLUMNS = ["run_id", "elapsed_seconds", "agent_id", "workload_id", "status", "error", "input_tokens",
                 "prompt_eval_count", "target_output_tokens", "output_tokens", "done_reason", "ttft_seconds",
                 "prefill_seconds", "decode_seconds", "load_seconds", "active_at_start"]
# ServerConfig 중 중계 서버가 쓰지 않거나, 가짜 서버 모델의 예측값(model_*_seconds)에만 쓰는 설정
UNUSED = {"slots": "쓰지 않음 (동시 처리는 Ollama가 정함)", "queue_size": "쓰지 않음",
          "service_delay, base_ttft, input_token_seconds, output_token_seconds": "보정용 예측값에만 씀"}


@dataclass
class RelayState:
    """중계 서버에만 있는 값. 한도·통계는 가짜 서버와 같은 State(app[mock_llm.STATE])에 둔다."""
    cfg: RelayConfig
    session: aiohttp.ClientSession | None = None
    info: dict = field(default_factory=dict)        # Ollama /api/version, /api/ps 원본 (워밍업 뒤)
    backend_errors: int = 0                         # 502로 바꾼 Ollama 오류 수
    done_reasons: dict = field(default_factory=dict)    # done_reason별 건수 (length, stop)
    prompt_eval_total: int = 0                      # Ollama가 센 입력 토큰 합
    eval_total: int = 0                             # Ollama가 센 생성 토큰 합
    load_max: float = 0.0                           # 요청 중 가장 긴 모델 적재 시간 (0에 가까워야 함)
    log: csv.DictWriter | None = None               # relay_requests CSV
    log_file: object = None


RELAY = web.AppKey("relay", RelayState)


async def ollama_ctx(app):
    """시작: 세션 → 워밍업 → server_info → 시작 시각. 끝: 세션 닫기.

    Ollama가 꺼져 있으면 워밍업에서 예외가 나 서버가 바로 끝난다 (run.py의 wait_ready가 콘솔 로그와 함께 알림).
    aiohttp는 이 시작 단계를 마친 뒤에 포트를 연다 (run_app: runner.setup → site.start).
    """
    relay = app[RELAY]
    relay.session = make_session(relay.cfg)
    try:
        await warmup(relay.session, relay.cfg)
        relay.info = await server_info(relay.session, relay.cfg)  # 워밍업 뒤라 /api/ps에 올라간 모델이 보인다
        # 워밍업 시간은 서버 CSV·/stats의 elapsed_seconds 시간축에 넣지 않는다
        app[mock_llm.STATE].started_at = time.perf_counter()
        yield
    finally:
        await relay.session.close()


async def relay_log_ctx(app, log_dir):
    """relay_requests_<run_id>.csv를 연다. 행은 /generate가 한 줄씩 쓰고 바로 flush한다."""
    relay, state = app[RELAY], app[mock_llm.STATE]
    path = Path(log_dir) / f"relay_requests_{state.run_id}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as f:
        relay.log, relay.log_file = csv.DictWriter(f, fieldnames=RELAY_COLUMNS), f
        relay.log.writeheader()
        f.flush()
        yield
        relay.log = None


async def health(request):
    state = request.app[mock_llm.STATE]
    return web.json_response({"status": "ok", "run_id": state.run_id, "api_version": mock_llm.API_VERSION,
                              "backend": "ollama",
                              "capabilities": ["request_rate_limit", "input_output_quotas", "ollama_generation"]})


async def stats(request):
    """가짜 서버 /stats와 같은 키(같은 State에서 만듦) + backend, ollama 묶음."""
    body = json.loads((await mock_llm.stats(request)).text)
    relay = request.app[RELAY]
    body.update(backend="ollama", ollama={
        "model": relay.cfg.model, "num_ctx": relay.cfg.num_ctx, "version": relay.info.get("version"),
        "ps_at_start": relay.info.get("ps"), "backend_errors": relay.backend_errors,
        "done_reasons": dict(relay.done_reasons), "prompt_eval_count_total": relay.prompt_eval_total,
        "eval_count_total": relay.eval_total, "load_seconds_max": relay.load_max})
    return web.json_response(body)


async def get_config(request):
    """가짜 서버 /config + 중계 서버 설정과 쓰지 않는 설정 안내."""
    body = json.loads((await mock_llm.get_config(request)).text)
    body.update(backend="ollama", relay=asdict(request.app[RELAY].cfg), unused_by_relay=UNUSED)
    return web.json_response(body)


@dataclass(frozen=True)
class Req:
    """검증을 마친 /generate 요청."""
    request_id: str
    agent_id: str
    workload_id: str
    inputs: int                         # 입력 토큰 (요청값, 한도에서 빼는 값)
    maximum: int                        # 출력 상한 = 출력 예약량


def parse_request(payload: dict, config: ServerConfig) -> tuple[Req | None, str | None]:
    """가짜 서버와 같은 본문 검증. 틀리면 (None, 틀린 필드 이름)."""
    for name in ("request_id", "agent_id"):
        if not mock_llm.valid_identifier(payload.get(name)):
            return None, name
    workload_id = payload.get("workload_id", payload["request_id"])
    if not mock_llm.valid_identifier(workload_id):
        return None, "workload_id"
    inputs = payload.get("input_tokens", 0)
    if not mock_llm.integer(inputs):
        return None, "input_tokens"
    maximum = payload.get("max_output_tokens", config.output_max)
    if not mock_llm.integer(maximum, 1, 10**6):
        return None, "max_output_tokens"
    return Req(payload["request_id"], payload["agent_id"], workload_id, inputs, maximum), None


def admit(state, relay: RelayState, req: Req, base: dict):
    """계획·문맥 검사와 한도 예약 (lock 안에서 부른다). 통과하면 (None, plan), 거절이면 (응답, None).

    plan은 같은 요청에 대한 가짜 서버의 계획: 목표 출력 길이(output_tokens)와 보정용 예측 시간.
    """
    counts = state.agent_stats.setdefault(req.agent_id, {      # 첫 요청 때 등록 (가짜 서버와 같은 네 칸)
        "completed": 0, "rate_limited": 0, "input_tokens_consumed": 0, "output_tokens_consumed": 0})
    try:
        plan = LLMModel(state.config.model()).plan(req.workload_id, req.inputs, req.maximum)
        if req.inputs + req.maximum + relay.cfg.context_margin > relay.cfg.num_ctx:
            raise ValueError(f"입력 + 출력 상한 + 여유가 num_ctx {relay.cfg.num_ctx}를 넘음")
    except ValueError as exc:
        return web.json_response(dict(base, error="invalid_generation", message=str(exc)), status=422), None
    decision = state.quota.reserve(req.inputs, req.maximum)    # 요청 1·입력·출력 상한을 함께 검사해 예약
    if not decision.allowed:
        return reject(state, counts, base, decision), None
    state.accepted += 1
    state.input_consumed += req.inputs
    counts["input_tokens_consumed"] += req.inputs
    state.output_reserved += req.maximum
    state.active += 1                                           # Ollama에 가 있는 요청 수
    return None, plan


def reject(state, counts: dict, base: dict, decision):
    """한도 부족: 429(기다리면 됨) 또는 422(기다려도 안 됨). 가짜 서버와 같은 본문·헤더."""
    headers = mock_llm.quota_headers(state)
    if decision.impossible:
        return web.json_response(dict(base, error="request_exceeds_capacity", limited_by=decision.limited_by,
                                      retry_after_seconds=None), status=422, headers=headers)
    state.rejected += 1
    counts["rate_limited"] += 1
    headers["Retry-After"] = str(max(1, math.ceil(decision.retry_after_seconds)))
    return web.json_response(dict(base, error="rate_limit_exceeded", limited_by=decision.limited_by,
                                  retry_after_seconds=decision.retry_after_seconds), status=429, headers=headers)


def outcome(result: StreamResult, maximum: int) -> int:
    """Ollama 결과 → 우리 상태 코드 (정산으로 상태를 바꾸기 전에 부른다).

    마지막 조각이 있으면 200. 단 eval_count가 있는데 0~maximum 정수가 아니면 502 invalid_response
    (키가 아예 없으면 정상: Ollama는 0인 값을 응답에서 뺀다).
    마지막 조각이 없으면 503(Ollama 대기열이 가득 참)만 529, 그 밖의 오류는 502.
    """
    if result.final is None:
        return 529 if result.status == 503 else 502
    if "eval_count" in result.final and not is_count(result.final["eval_count"], maximum):
        result.error = "invalid_response"
        result.message = f"eval_count={result.final['eval_count']!r}"[:200]
        return 502
    return 200


def settle(state, relay: RelayState, req: Req, result: StreamResult, status) -> int:
    """정산 (lock 안에서 부른다). 정산한 생성량을 돌려준다.

    status: 200, 529, 502(Ollama 오류·타임아웃·이상한 응답·예상 못 한 예외), "cancelled"(클라이언트가 끊음)
    생성량 계산을 먼저 끝내고 그다음에 상태를 바꾼다 (중간에 예외가 나 반만 정산되는 일이 없게).
    """
    counts = state.agent_stats[req.agent_id]
    final = result.final or {}
    # 200은 Ollama가 센 eval_count, 502·취소는 그때까지 받은 조각 수만큼 생성한 것으로 본다
    generated = 0 if status == 529 else settled_tokens(status, final, result.chunks, req.maximum)
    state.active -= 1
    state.output_reserved -= req.maximum
    if status == 529:           # 받지 않은 요청으로 되돌린다 (가짜 서버는 한도 검사 전에 529를 낸다)
        state.quota.requests.refund(1)
        state.quota.inputs.refund(req.inputs)
        state.quota.settle_output(req.maximum, 0)
        state.accepted -= 1
        state.input_consumed -= req.inputs
        counts["input_tokens_consumed"] -= req.inputs
        state.overloaded += 1
        return 0
    state.quota.settle_output(req.maximum, generated)          # 예약했지만 쓰지 않은 출력 토큰을 돌려준다
    state.output_consumed += generated
    counts["output_tokens_consumed"] += generated
    if status == 200:
        state.completed += 1
        counts["completed"] += 1
        reason = reason_key(final.get("done_reason"))
        relay.done_reasons[reason] = relay.done_reasons.get(reason, 0) + 1
        prompt_eval, evals = final.get("prompt_eval_count"), final.get("eval_count")
        relay.prompt_eval_total += prompt_eval if is_count(prompt_eval) else 0     # 0 이상 정수일 때만 (이상해도 실패 아님)
        relay.eval_total += evals if is_count(evals) else 0
        relay.load_max = max(relay.load_max, seconds(final.get("load_duration")) or 0.0)
    elif status == 502:
        relay.backend_errors += 1
    else:
        state.cancelled += 1
    return generated


def log_relay(state, relay: RelayState, req: Req, plan, result: StreamResult, status, generated: int,
              arrived: float, active_at_start: int) -> None:
    """Ollama로 보낸 요청 하나 = relay_requests CSV 한 줄 (429·422는 Ollama에 가지 않아 남기지 않음)."""
    if relay.log is None:
        return
    final = result.final or {}
    relay.log.writerow({
        "run_id": state.run_id, "elapsed_seconds": round(time.perf_counter() - state.started_at, 6),
        "agent_id": req.agent_id, "workload_id": req.workload_id, "status": status,
        "error": f"{result.error} {result.message}".strip(), "input_tokens": req.inputs,
        "prompt_eval_count": final.get("prompt_eval_count"), "target_output_tokens": plan.output_tokens,
        "output_tokens": generated, "done_reason": final.get("done_reason"),
        "ttft_seconds": None if result.first_chunk is None else result.first_chunk - arrived,
        "prefill_seconds": seconds(final.get("prompt_eval_duration")),
        "decode_seconds": seconds(final.get("eval_duration")),
        "load_seconds": seconds(final.get("load_duration")), "active_at_start": active_at_start})
    relay.log_file.flush()


async def generate(request):
    """가짜 서버의 generate와 같은 순서: 검증(400) → 계획·문맥(422) → 한도(429·422) → Ollama → 정산 → 응답."""
    state, relay = request.app[mock_llm.STATE], request.app[RELAY]
    req, bad = parse_request(await mock_llm.read_json(request), state.config)
    if bad:
        return web.json_response({"error": "invalid_field", "field": bad}, status=400)
    arrived = time.perf_counter()
    base = {"request_id": req.request_id, "agent_id": req.agent_id, "workload_id": req.workload_id,
            "run_id": state.run_id, "config_revision": state.revision}
    async with state.lock:                                      # 예약과 정산은 같은 lock 안에서
        rejected, plan = admit(state, relay, req, base)
        if rejected is not None:
            return rejected
        active_at_start = state.active
    prompt = make_prompt(req.workload_id, req.inputs, relay.cfg.prompt_overhead_tokens)
    # 목표 출력 길이 = 가짜 서버와 같은 계산 → 출력 길이 분포가 같아 차이는 지연에서만 난다
    payload = ollama_payload(relay.cfg, prompt, plan.output_tokens, state.config.seed)
    result, status = StreamResult(), 502        # 결과를 정하지 못하고 끝나면 502(backend_error)로 정산
    try:
        await stream_generate(relay.session, relay.cfg, payload, result)    # lock 밖: 다른 요청을 막지 않음
        status = outcome(result, req.maximum)
    except asyncio.CancelledError:              # 클라이언트가 끊음 (handler_cancellation=True)
        status = "cancelled"
        raise
    except Exception as exc:                    # 코드 버그 등: 취소가 아니므로 502로 정산하고 예외는 그대로 올린다
        result.error = f"exception {type(exc).__name__}"
        raise
    finally:                                    # 어떻게 끝나든 받은 조각만큼 정산한다
        async with state.lock:
            generated = settle(state, relay, req, result, status)
            headers = mock_llm.quota_headers(state)             # 200은 정산 뒤의 남은 한도
        log_relay(state, relay, req, plan, result, status, generated, arrived, active_at_start)
    if status == 529:
        return web.json_response(dict(base, error="server_overloaded"), status=529)     # 한도 헤더 없음
    if status == 502:
        return web.json_response(dict(base, error="backend_error", backend_error=result.error), status=502)
    timing = Timing(arrived, result.first_chunk or result.finished, result.finished)
    body = success_body(base, req.inputs, req.maximum, plan.output_tokens, result.final, generated,
                        timing, plan, state.config.service_delay)
    return web.json_response(body, headers=headers)


def create_app(config: ServerConfig, relay_cfg: RelayConfig, log_dir: Path | None = None) -> web.Application:
    """시작 순서는 cleanup_ctx에 넣은 순서: Ollama 준비 → 서버 CSV(record_experiment) → 중계 서버 요청 CSV."""
    config.validate()
    quota = MultiQuota(TokenBucket(config.capacity, config.refill_rate),
                       TokenBucket(config.input_capacity, config.input_refill_rate),
                       TokenBucket(config.output_capacity, config.output_refill_rate))
    app = web.Application(client_max_size=64 * 1024, middlewares=[mock_llm.json_errors])  # X-Run-Id 등 헤더
    app[mock_llm.STATE] = mock_llm.State(config, quota)
    app[RELAY] = RelayState(relay_cfg)
    app.cleanup_ctx.append(ollama_ctx)
    if log_dir is not None:
        app.cleanup_ctx.append(lambda app: record_experiment(app[mock_llm.STATE], log_dir))
        app.cleanup_ctx.append(lambda app: relay_log_ctx(app, log_dir))
    app.add_routes([web.get("/health", health), web.get("/stats", stats), web.get("/config", get_config),
                    web.post("/generate", generate)])
    return app


def load_config(path: Path | None) -> ServerConfig:
    """ServerConfig JSON (한도·출력 길이 범위). 없으면 기본값."""
    base = ServerConfig()
    return base.changed(json.loads(path.read_text(encoding="utf-8"))) if path else base.validate()


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ollama 중계 서버: API 2.0을 받아 한도 검사 뒤 Ollama로 생성")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8001)
    p.add_argument("--config", type=Path, help="ServerConfig JSON (생략하면 기본값)")
    p.add_argument("--log-dir", type=Path, default=Path("results/server"))
    p.add_argument("--model", default=RelayConfig.model)
    p.add_argument("--ollama-url", default=RelayConfig.ollama_url)
    p.add_argument("--num-ctx", type=int, default=RelayConfig.num_ctx, help="실행 내내 고정")
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    relay_cfg = RelayConfig(ollama_url=args.ollama_url, model=args.model, num_ctx=args.num_ctx)
    app = create_app(load_config(args.config), relay_cfg, args.log_dir)
    web.run_app(app, host=args.host, port=args.port, handler_cancellation=True)   # 연결이 끊기면 핸들러 취소


if __name__ == "__main__":
    main()
