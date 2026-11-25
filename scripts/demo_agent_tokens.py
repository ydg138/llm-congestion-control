"""두 C2 에이전트의 요청 로그·통계·서버 CSV를 대조한다. E4 성능 실험은 아니다.

python -m scripts.demo_agent_tokens --serve --port 8088
"""

import argparse
import asyncio
import csv
import html
import json
import time
from pathlib import Path

from aiohttp import ClientSession, web

from client.factory import make_controller
from runner.agent import Agent, AgentSpec, Progress
from runner.check_run import compare, read_run
from runner.request_log import RequestLog, log_path
from runner.workload import ClassConfig, make_tasks
from server.mock_llm import STATE, create_app


def read_csv(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


async def run(port, serve):
    app = create_app(capacity=3, refill_rate=20, slots=4, queue_size=8,
                     input_capacity=100000, output_capacity=10000,
                     output_min=4, output_max=4, service_delay=.03,
                     base_ttft=0, input_token_seconds=0, output_token_seconds=.001)
    run_id = app[STATE].run_id
    directory = Path("results") / f"agent_tokens_{run_id}"
    directory.mkdir(parents=True)
    # 데모도 운영 서버와 동일한 CSV 수명주기를 사용한다.
    from server.experiment_log import record_experiment
    app.cleanup_ctx.append(lambda app: record_experiment(app[STATE], directory / "server"))
    page = {"text": "검증 진행 중입니다. 잠시 후 새로고침하세요."}

    async def show(_):
        return web.Response(text=page["text"], content_type="text/html")

    app.router.add_get("/demo", show)
    server = web.AppRunner(app, handler_cancellation=True)
    await server.setup()
    try:
        await web.TCPSite(server, "127.0.0.1", port).start()
        base = f"http://127.0.0.1:{port}"
        async with ClientSession() as session:
            start = await (await session.get(base + "/stats")).json()
            progress = Progress(time.perf_counter())
            path = log_path(directory, "token_check", "C2", 1, run_id)
            log = RequestLog(path, run_id, "token_check", 1)
            classes = {"light": ClassConfig(100, 100, 20),
                       "heavy_in": ClassConfig(1000, 1000, 20)}
            specs = [AgentSpec("A", "light", "C2"), AgentSpec("B", "heavy_in", "C2")]
            try:
                await asyncio.wait_for(asyncio.gather(*[
                    Agent(spec, make_tasks(spec.agent_id, spec.agent_class, 20, 1, classes),
                          make_controller("C2"), base, log, progress).run() for spec in specs]), 30)
            finally:
                log.close()
            stats = await (await session.get(base + "/stats")).json()
        stats["runner"] = {"status": "completed" if progress.completed == 40 else "aborted",
                           "server_elapsed_at_start_seconds": start["elapsed_seconds"]}
        path.with_name(path.stem + "_stats.json").write_text(
            json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        header, requests, saved = read_run(path)
        checks = compare(header, requests, saved)
        checks.extend([(name, stats[name], 0) for name in ("active", "queued", "cancelled")])
        agent_csv = directory / "server" / f"agent_stats_{run_id}.csv"
        # 서버 CSV의 다음 실제 표본을 기다린다(최대 3초).
        async with asyncio.timeout(3):
            while True:
                latest = {r["agent_id"]: r for r in read_csv(agent_csv)}
                if all(a in latest and all(int(latest[a][k]) == v for k, v in counts.items())
                       for a, counts in stats["agent_stats"].items()):
                    break
                await asyncio.sleep(.02)
        for agent, expected_input in (("A", 2000), ("B", 20000)):
            successful = [r for r in requests if r["agent_id"] == agent and r["status"] == "200"]
            for kind, expected in (("input", expected_input), ("output", 80)):
                field = kind + "_tokens_consumed"
                actual = stats["agent_stats"][agent][field]
                checks.extend([(f"{agent} {kind} hand calculation", actual, expected),
                               (f"{agent} {kind} request CSV", sum(int(r[kind + "_tokens"]) for r in successful), actual),
                               (f"{agent} {kind} server CSV", int(latest[agent][field]), actual)])
        for field in ("input_tokens_consumed", "output_tokens_consumed"):
            checks.append((field, sum(a[field] for a in stats["agent_stats"].values()), stats[field]))
        passed = all(got == want for _, got, want in checks)
        report = {"passed": passed, "run_id": run_id, "checks": [
            {"name": name, "actual": sorted(got) if isinstance(got, set) else got,
             "expected": sorted(want) if isinstance(want, set) else want}
            for name, got, want in checks]}
        (directory / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if not passed:
            raise AssertionError(report)
        body = "".join(f"<tr><td>{html.escape(a)}</td><td>{c['completed']}</td>"
                       f"<td>{c['rate_limited']}</td><td>{c['input_tokens_consumed']:,}</td>"
                       f"<td>{c['output_tokens_consumed']:,}</td></tr>" for a, c in stats["agent_stats"].items())
        page["text"] = f'''<!doctype html><html lang="ko"><meta charset="utf-8">
<title>에이전트 토큰 통계 검증</title><style>
body{{font:18px sans-serif;max-width:1000px;margin:60px auto;background:#f5f7fb;color:#17263b}}
table{{border-collapse:collapse;width:100%;background:white}}td,th{{padding:20px;border-bottom:1px solid #ddd;text-align:left}}
.pass{{color:#087342;font-weight:bold}}code{{font-size:15px}}</style>
<h1>에이전트별 실제 토큰 통계</h1><p class="pass">검증 통과 · 요청 CSV = /stats = 서버 CSV</p>
<p>C2 에이전트 2개 · 각각 20건 완료 · 통계 기능 검증용 실행</p>
<table><tr><th>에이전트</th><th>완료</th><th>429</th><th>실제 입력 토큰</th><th>실제 출력 토큰</th></tr>{body}</table>
<p>출력은 요청마다 20토큰을 예약하고 실제로 4토큰을 생성했습니다.<br>
거절된 재시도는 토큰 사용량에 포함되지 않습니다.</p>
<p>대기 0 · 처리 중 0 · 취소 0 · 대조 {len(checks)}항목 통과</p>
<p>실행 ID: <code>{run_id}</code></p><p><a href="/stats">실시간 서버 /stats 보기</a></p>
<p>이 화면은 완료된 검증 결과입니다. E4 공정성 평가 결과가 아닙니다.</p></html>'''
        print(json.dumps({"url": base + "/demo", "directory": str(directory.resolve()), **report}, ensure_ascii=False), flush=True)
        if serve:
            await asyncio.Event().wait()
    finally:
        await server.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--serve", action="store_true", help="검증 후 브라우저 확인용 서버 유지")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.port, args.serve))
    except KeyboardInterrupt:
        pass
