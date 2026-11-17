"""명령줄 실행기: 서버를 새로 띄우고, 에이전트를 돌리고, 요청 로그 CSV와 /stats JSON을 남긴다.

예:
    python -m runner.run --exp E1 --controller C2 --seed 1 --tasks 100 ^
        --server-dir ..\\CN_Server --server-config ..\\CN_Server\\examples\\config.json

결과 (results/ 아래):
    <exp>_<controller>_seed<seed>_<run_id>.csv          요청 로그 (docs/interface.md 3절)
    <exp>_<controller>_seed<seed>_<run_id>_stats.json   맨 위 runner(실행 결과 상태) + 서버 /stats
    server/                                             서버 CSV와 서버 콘솔 출력
runner.status가 aborted이거나 /stats를 못 받으면 이유를 stderr에 쓰고 종료 코드 1로 끝난다.
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from client.factory import make_controller
from runner.agent import Agent, AgentSpec, Progress
from runner.request_log import RequestLog, log_path
from runner.server_proc import free_port, get_json, start_server, stop_server, wait_ready
from runner.watchdog import Limits, RunAborted, watch
from runner.workload import make_tasks

RESULTS = Path(__file__).resolve().parents[1] / "results"


def default_agents(controller: str) -> list[AgentSpec]:
    """지금은 light 에이전트 하나. E3·E4는 이 목록만 바꾼다 (다음 단계에서 experiments.py로)."""
    return [AgentSpec("a1", "light", controller, 0.0)]


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


def run_experiment(agents, n_tasks: int, seed: int, base_url: str, log, limits: Limits) -> dict:
    """에이전트를 끝까지 돌리고 실행 결과 상태 {"status", "reason"}를 돌려준다.

    completed: 모든 작업이 200으로 끝남
    aborted: 안전장치로 멈췄거나, 422·타임아웃·연결 오류로 포기한 작업이 있음
    코드 오류·Ctrl+C는 그대로 올린다 (main이 aborted로 기록한다).
    """
    progress = Progress(time.perf_counter())                # 실험 시작 시각
    try:
        asyncio.run(run_agents(agents, n_tasks, seed, base_url, log, limits, progress))
    except RunAborted as e:
        return {"status": "aborted", "reason": f"안전장치: {e}"}
    total = len(agents) * n_tasks
    if progress.completed < total:
        return {"status": "aborted", "reason": f"작업 {progress.completed}/{total}만 성공"}
    return {"status": "completed", "reason": None}


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


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="실험 한 번: 서버를 새로 띄우고 요청 로그와 /stats를 남긴다")
    p.add_argument("--exp", required=True, help="실험 이름 (파일 이름에 들어감, 예: E1)")
    p.add_argument("--controller", required=True, help="컨트롤러 이름 (예: C2, C0-8)")
    p.add_argument("--seed", type=int, default=1, help="작업량 시드")
    p.add_argument("--tasks", type=int, default=100, help="에이전트당 작업 수")
    p.add_argument("--server-dir", type=Path, required=True, help="서버 폴더 (.venv 포함)")
    p.add_argument("--server-config", type=Path, help="서버 설정 JSON (생략하면 서버 기본값)")
    p.add_argument("--max-seconds", type=float, default=600.0, help="전체 시간 상한 (초)")
    p.add_argument("--stall-seconds", type=float, default=30.0, help="이 시간 동안 완료가 없으면 멈춤 (초)")
    args = p.parse_args(argv)
    try:
        make_controller(args.controller)                    # 이름이 틀리면 서버를 띄우기 전에 알린다
    except ValueError as e:
        p.error(str(e))
    if args.server_config is not None and not args.server_config.is_file():
        p.error(f"서버 설정 파일이 없음: {args.server_config}")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    agents = default_agents(args.controller)
    limits = Limits(max_seconds=args.max_seconds, stall_seconds=args.stall_seconds)
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    launch_log = RESULTS / "server" / f"launch_{args.exp}_{args.controller}_seed{args.seed}.log"
    proc = start_server(args.server_dir.resolve(), args.server_config, RESULTS / "server", port, launch_log)
    problems = []
    try:
        run_id = wait_ready(base_url, proc, launch_log)    # 새 서버 = 새 run_id, 버킷·통계 초기화
        csv_path = log_path(RESULTS, args.exp, args.controller, args.seed, run_id)
        stats_path = csv_path.with_name(csv_path.stem + "_stats.json")
        log = RequestLog(csv_path, run_id, args.exp, args.seed)
        try:
            outcome = run_experiment(agents, args.tasks, args.seed, base_url, log, limits)
        except BaseException as e:                          # 코드 오류·Ctrl+C도 aborted로 남기고 그대로 올린다
            outcome = {"status": "aborted", "reason": f"예외: {type(e).__name__}: {e}"}
            raise
        finally:                                            # 어떻게 끝나든 로그를 닫고 /stats를 남긴다
            log.close()
            stats = save_stats(base_url, stats_path, outcome)
    finally:
        stop_server(proc, port)                             # 어떻게 끝나든 서버는 반드시 끈다
    print(f"요청 로그: {csv_path}")
    print(f"서버 통계: {stats_path}")
    if outcome["status"] == "aborted":
        problems.append(outcome["reason"])
    if stats is None:
        problems.append("/stats 저장 실패 (서버가 답하지 않음)")
    elif problems:
        problems.append(f"서버 기준 완료 {stats['completed']}/{len(agents) * args.tasks}")
    if problems:
        print("; ".join(problems), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
