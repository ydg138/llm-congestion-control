"""명령줄 실행기: 실험 조건(runner/experiments.py)을 시드별로 반복해 돌린다.

예:
    python -m runner.run --exp minimum                          # E1·E3 × 시드 1~5 (35회)
    python -m runner.run --exp E1 --controller C2 --seed 1
    python -m runner.run --exp E3 --controller C2 --seed 1 --tasks 10 --backend ollama   # E6 (Ollama를 먼저 켬)

서버는 이 저장소의 server/를 .venv\\Scripts\\python.exe로 띄운다. 다른 서버 폴더는 --server-dir로 준다.
--backend ollama면 가짜 서버 대신 Ollama 중계 서버(runner/real_adapter.py)를 실행마다 새로 띄운다.

명령 한 번의 결과는 results/<exp>_<YYYYmmdd-HHMMSS>/ 폴더 하나에 모인다:
    <exp>_<controller>_seed<seed>_<run_id>.csv          요청 로그 (docs/interface.md 3절)
    <exp>_<controller>_seed<seed>_<run_id>_stats.json   맨 위 runner(실행 결과 상태) + 서버 /stats
    server/                                             서버 CSV와 서버 콘솔 출력
    runs.csv                                            실행 목록: 실행마다 status와 대조 결과
aborted이거나 대조가 맞지 않는 실행이 하나라도 있으면 종료 코드 1로 끝난다.
"""

import argparse
import asyncio
import csv
import json
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

from client.factory import make_controller
from runner.agent import Agent, Progress
from runner.check_run import compare, read_run
from runner.experiments import EXPERIMENTS, SEEDS, Condition, select
from runner.relay_rules import RelayConfig, context_size
from runner.request_log import RequestLog, log_path
from runner.server_proc import ServerTimes, free_port, get_json, start_server, stop_server, wait_ready
from runner.watchdog import Limits, RunAborted, watch
from runner.workload import AGENT_CLASSES, make_tasks

REPO = Path(__file__).resolve().parents[1]                  # 이 저장소 폴더 (server/와 .venv가 있는 곳)
RESULTS = REPO / "results"
RUN_COLUMNS = ["exp", "controller", "seed", "run_id", "status", "reason", "check", "csv"]


async def run_agents(agents, n_tasks: int, seed: int, base_url: str, log, limits: Limits,
                     progress: Progress) -> None:
    """모든 에이전트를 함께 돌린다. 안전장치가 먼저 걸리면 에이전트를 멈추고 RunAborted를 올린다."""
    runs = [Agent(spec, make_tasks(spec.agent_id, spec.agent_class, n_tasks, seed),
                  make_controller(spec.controller, seed=seed * 1000 + i),  # C1 지터가 에이전트끼리 같지 않게
                  base_url, log, progress).run()
            for i, spec in enumerate(agents)]
    work = asyncio.gather(*runs)
    guard = asyncio.create_task(watch(progress, limits))
    done, _ = await asyncio.wait([work, guard], return_when=asyncio.FIRST_COMPLETED)
    work.cancel()                                           # 끝나지 않은 쪽을 멈춘다 (끝난 쪽은 그대로)
    guard.cancel()
    await asyncio.gather(work, guard, return_exceptions=True)   # 에이전트가 하위 요청을 다 정리할 때까지
    if guard in done:
        guard.result()                                      # RunAborted
    work.result()                                           # 에이전트에서 난 예외


def server_elapsed(base_url: str, run_id: str) -> float | None:
    """서버 /stats의 elapsed_seconds (서버 CSV 시간축의 지금). 서버에 그 값이 없으면 None.

    요청 로그의 t_send에 실험 시작 때 읽은 이 값을 더하면 서버 CSV elapsed_seconds의 근삿값이 된다.
    run_id가 다르면 다른 서버에 붙은 것이므로 오류를 낸다.
    """
    stats = get_json(base_url, "/stats")
    if stats.get("run_id") != run_id:
        raise RuntimeError(f"/stats의 run_id가 이번 서버와 다름: {stats.get('run_id')} != {run_id}")
    return stats.get("elapsed_seconds")


def run_experiment(agents, n_tasks: int, seed: int, base_url: str, run_id: str, log,
                   limits: Limits) -> dict:
    """에이전트를 끝까지 돌리고 실행 결과 상태를 돌려준다 (stats JSON의 runner).

    status: completed(모든 작업이 200으로 끝남) 또는 aborted(안전장치로 멈춤, 422·타임아웃·연결 오류로
            포기한 작업이 있음). reason: aborted인 이유.
    server_elapsed_at_start_seconds: 실험 시작 직전에 읽은 서버 경과 시간 (서버 CSV 시간축 맞추기용)
    코드 오류·Ctrl+C는 그대로 올린다 (run_once가 aborted로 기록한다).
    """
    start = server_elapsed(base_url, run_id)                # 읽은 직후 바로 시작해야 오차가 작다
    progress = Progress(time.perf_counter())                # 실험 시작 시각 (요청 로그 t_send의 기준)
    outcome = {"status": "completed", "reason": None, "server_elapsed_at_start_seconds": start}
    try:
        asyncio.run(run_agents(agents, n_tasks, seed, base_url, log, limits, progress))
    except RunAborted as e:
        return {**outcome, "status": "aborted", "reason": f"안전장치: {e}"}
    total = len(agents) * n_tasks
    if progress.completed < total:
        return {**outcome, "status": "aborted", "reason": f"작업 {progress.completed}/{total}만 성공"}
    return outcome


def save_stats(base_url: str, path: Path, outcome: dict) -> dict | None:
    """맨 위에 runner(실행 결과 상태)를 두고 서버 /stats를 이어 붙여 저장한다.

    서버가 답하지 않아도 runner는 저장한다. 돌려주는 값은 /stats (못 받았으면 None).
    """
    try:
        stats = get_json(base_url, "/stats")
    except OSError:
        stats = None
    doc = {"runner": outcome, **(stats if stats is not None else {"error": "/stats 응답 없음"})}
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats


@dataclass(frozen=True)
class Backend:
    """실행마다 띄울 서버. mock: 가짜 서버, ollama: Ollama 중계 서버(runner/real_adapter.py)."""
    name: str = "mock"
    ollama_model: str = RelayConfig.model
    ollama_url: str = RelayConfig.ollama_url
    num_ctx: int | None = None              # None이면 조건마다 context_size로 계산 (실행 내내 고정)
    ready_seconds: float = 120.0            # 중계 서버는 워밍업(모델 적재)을 마쳐야 포트를 연다


def server_launch(cond: Condition, backend: Backend) -> tuple[str, list[str], ServerTimes]:
    """조건 하나에 띄울 서버 모듈, 추가 인자, 준비 대기 시간."""
    if backend.name == "mock":
        return "server.mock_llm", [], ServerTimes()
    num_ctx = backend.num_ctx or context_size(AGENT_CLASSES, cond.agents, RelayConfig())
    args = ["--model", backend.ollama_model, "--ollama-url", backend.ollama_url, "--num-ctx", str(num_ctx)]
    return "runner.real_adapter", args, ServerTimes(ready_seconds=backend.ready_seconds)


def run_on_server(cond: Condition, seed: int, out_dir: Path, base_url: str, run_id: str, limits: Limits) -> dict:
    """준비된 서버에서 조건 하나를 시드 하나로 돌린다. 실행 목록에 들어갈 run_id·status·reason·csv를 돌려준다."""
    csv_path = log_path(out_dir, cond.exp, cond.label, seed, run_id)
    log = RequestLog(csv_path, run_id, cond.exp, seed)
    try:
        outcome = run_experiment(cond.agents, cond.tasks, seed, base_url, run_id, log, limits)
    except BaseException as e:                              # 코드 오류·Ctrl+C도 aborted로 남기고 그대로 올린다
        outcome = {"status": "aborted", "reason": f"예외: {type(e).__name__}: {e}",
                   "server_elapsed_at_start_seconds": None}
        raise
    finally:                                                # 어떻게 끝나든 로그를 닫고 /stats를 남긴다
        log.close()
        save_stats(base_url, csv_path.with_name(csv_path.stem + "_stats.json"), outcome)
    # 실행 목록(runs.csv)에 들어갈 값만 돌려준다. runner의 나머지 값은 stats JSON에 있다
    return {"run_id": run_id, "status": outcome["status"], "reason": outcome["reason"], "csv": csv_path.name}


def run_once(cond: Condition, seed: int, out_dir: Path, server_dir: Path, limits: Limits,
             backend: Backend = Backend(), attach_url: str | None = None) -> dict:
    """서버 시작 → 실행 → stats 저장 → 서버 끄기. 실행마다 새 서버라 새 run_id, 버킷·통계가 초기화된다.

    attach_url을 주면 이미 떠 있는 서버를 띄우지도 끄지도 않고 그대로 쓴다 (초기화되지 않음).
    """
    if attach_url is not None:
        return run_on_server(cond, seed, out_dir, attach_url, get_json(attach_url, "/health")["run_id"], limits)
    module, extra_args, times = server_launch(cond, backend)
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    launch_log = out_dir / "server" / f"launch_{cond.exp}_{cond.label}_seed{seed}.log"
    proc = start_server(server_dir, cond.server_config, out_dir / "server", port, launch_log, module, extra_args)
    try:
        run_id = wait_ready(base_url, proc, launch_log, times)
        return run_on_server(cond, seed, out_dir, base_url, run_id, limits)
    finally:
        stop_server(proc, port)                             # 어떻게 끝나든 서버는 반드시 끈다 (Ollama는 그대로)


def check(csv_path: Path) -> str:
    """요청 로그와 stats JSON을 대조한다 (runner/check_run.py). 맞으면 "OK", 아니면 틀린 항목."""
    failed = [name for name, got, want in compare(*read_run(csv_path)) if got != want]
    return "OK" if not failed else "FAIL: " + ", ".join(failed)


def run_all(plan, out_dir: Path, args, limits: Limits) -> list[dict]:
    """(조건, 시드) 목록을 차례로 돌리고 runs.csv에 한 줄씩 남긴다. 문제 있는 실행 목록을 돌려준다."""
    bad = []
    backend = Backend(args.backend, args.ollama_model, args.ollama_url, args.num_ctx)
    with open(out_dir / "runs.csv", "w", encoding="utf-8", newline="") as f:
        runs = csv.DictWriter(f, fieldnames=RUN_COLUMNS)
        runs.writeheader()
        for i, (cond, seed) in enumerate(plan, 1):
            if args.tasks is not None:
                cond = replace(cond, tasks=args.tasks)      # 빠른 확인용
            if args.server_config is not None:
                cond = replace(cond, server_config=args.server_config)
            row = {"exp": cond.exp, "controller": cond.label, "seed": seed}
            started = time.perf_counter()
            try:
                row.update(run_once(cond, seed, out_dir, args.server_dir.resolve(), limits, backend, args.base_url))
                row["check"] = check(out_dir / row["csv"])
            except Exception as e:                          # 서버 시작 실패 등도 aborted로 적고 다음 실행으로
                row.update(status="aborted", reason=f"예외: {type(e).__name__}: {e}", check="-")
            runs.writerow(row)
            f.flush()                                       # 중간에 멈춰도 그때까지의 목록이 남게
            print(f"[{i}/{len(plan)}] {cond.exp} {cond.label} seed{seed}: {row['status']}, "
                  f"대조 {row['check']}, {time.perf_counter() - started:.1f}초", flush=True)
            if row["status"] != "completed" or row["check"] != "OK":
                bad.append(row)
    return bad


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="실험 조건(runner/experiments.py)을 시드별로 반복해 돌린다")
    p.add_argument("--exp", required=True, choices=sorted(EXPERIMENTS), help="minimum은 E1 + E3")
    p.add_argument("--controller", help="이 컨트롤러 조건만 (예: C2, C0-8)")
    p.add_argument("--seed", type=int, help=f"이 시드만 (생략하면 {SEEDS})")
    p.add_argument("--tasks", type=int, help="에이전트당 작업 수 덮어쓰기 (빠른 확인용)")
    p.add_argument("--server-dir", type=Path, default=REPO,
                   help="서버 폴더 (server/와 .venv 포함). 생략하면 이 저장소")
    p.add_argument("--server-config", type=Path, help="서버 설정 덮어쓰기 (생략하면 조건의 설정)")
    p.add_argument("--max-seconds", type=float, default=600.0, help="실행 하나의 전체 시간 상한 (초)")
    p.add_argument("--stall-seconds", type=float, default=30.0, help="이 시간 동안 완료가 없으면 멈춤 (초)")
    p.add_argument("--backend", choices=["mock", "ollama"], default="mock",
                   help="mock: 가짜 서버, ollama: Ollama 중계 서버 (Ollama를 먼저 켜 둘 것)")
    p.add_argument("--ollama-model", default=RelayConfig.model)
    p.add_argument("--ollama-url", default=RelayConfig.ollama_url)
    p.add_argument("--num-ctx", type=int, help="Ollama 문맥 길이 (생략하면 조건마다 계산, 실행 내내 고정)")
    p.add_argument("--base-url", help="이미 떠 있는 서버 주소. 서버를 띄우지도 끄지도 않아 실행마다 초기화되지 "
                   "않으므로 실행 하나(--controller·--seed 지정)일 때만 쓸 수 있다")
    args = p.parse_args(argv)
    if not select(args.exp, args.controller, args.seed):
        p.error(f"{args.exp}에 컨트롤러 {args.controller} 조건이 없음")
    if args.server_config is not None and not args.server_config.is_file():
        p.error(f"서버 설정 파일이 없음: {args.server_config}")
    if args.base_url is not None:
        if len(select(args.exp, args.controller, args.seed)) != 1:
            p.error("--base-url은 실행 하나일 때만 쓸 수 있음 (--controller와 --seed를 줄 것). "
                    "이미 떠 있는 서버는 실행마다 초기화되지 않아 /stats 대조가 맞지 않는다")
        if args.backend != "mock" or args.server_config is not None or args.server_dir != REPO:
            p.error("--base-url은 --backend ollama, --server-config, --server-dir와 함께 쓸 수 없음 (서버를 띄우지 않음)")
    if args.backend == "ollama" and not (args.server_dir / "runner" / "real_adapter.py").is_file():
        p.error(f"--backend ollama에는 runner/real_adapter.py가 있는 서버 폴더가 필요함: {args.server_dir}")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    plan = select(args.exp, args.controller, args.seed)
    limits = Limits(max_seconds=args.max_seconds, stall_seconds=args.stall_seconds)
    out_dir = RESULTS / f"{args.exp}_{time.strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True)                             # 이미 있으면 오류 (같은 초에 두 번 실행)
    (out_dir / "server").mkdir()
    bad = run_all(plan, out_dir, args, limits)
    print(f"결과 폴더: {out_dir}")
    if bad:
        print(f"문제 있는 실행 {len(bad)}개:", file=sys.stderr)
        for r in bad:
            print(f"  {r['exp']} {r['controller']} seed{r['seed']}: {r['status']} "
                  f"{r.get('reason') or ''} / 대조 {r['check']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
