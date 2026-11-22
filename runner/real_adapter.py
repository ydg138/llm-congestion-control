"""E6 Ollama 중계 서버: 우리 API 2.0(/generate)을 받아 한도를 검사하고, 통과한 요청만 Ollama로 생성한다.

    python -m runner.real_adapter --port 8001 --log-dir results/server --config C.json --num-ctx 4096

- 한도·통계·헤더는 가짜 서버(server/mock_llm.py)의 State, 미들웨어, 핸들러를 그대로 쓴다 (server/는 고치지 않음)
- 실행기와 컨트롤러는 그대로다. 접속 주소만 이 서버로 바뀐다
- 서버 CSV: 가짜 서버와 같은 두 파일 + 요청마다 한 줄인 relay_requests_<run_id>.csv
"""

import argparse
import csv
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import aiohttp
from aiohttp import web

from runner.ollama_client import make_session, server_info, warmup
from runner.relay_rules import RelayConfig
from server import mock_llm
from server.config import ServerConfig
from server.experiment_log import record_experiment
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
    app.add_routes([web.get("/health", health), web.get("/stats", stats), web.get("/config", get_config)])
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
