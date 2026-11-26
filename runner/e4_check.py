"""E4 빠른 확인: 두 에이전트가 같이 돈 구간과, 어느 한도가 먼저 찼는지.

    python -m runner.e4_check results/E4_<시각>

결과 폴더의 runs.csv에 있는 실행마다 한 줄씩 출력한다.
- 같이 돈 구간: 요청 로그에서 에이전트마다 (첫 t_send, 마지막 200의 t_done)을 구해 [max(시작), min(끝)]
- 사용률: 버킷마다 쓴 양 ÷ 받을 수 있었던 양(처음 가득 찬 용량 + 충전 속도 × 서버 경과 시간). stats JSON으로 계산
- 최소 잔량: 서버 CSV(server/server_state_<run_id>.csv)의 0.1초 표본 중 가장 작은 잔량 ÷ 용량
- 구간 처리량(충전 속도 대비): 같이 돈 구간 안에 끝난 200만 세서 초당 성공 요청 수·입력 토큰·출력 토큰
  (전체와 에이전트별)과 그 값 ÷ 그 실행 서버 설정의 충전 속도. 토큰 Jain 지수와 같은 구간이다
두 비율은 분모가 다르다: 사용률은 (용량 + 충전 속도 × 서버 전체 시간), 구간 처리량은 충전 속도(초당)만 쓴다.
의도한 병목 버킷은 사용률이 높고 최소 잔량이 0에 가깝다. 나머지 버킷은 사용률이 낮고 최소 잔량이 100%에 가깝다.
AIMD는 한도에 잠깐 닿으면 윈도를 줄이므로(톱니) 잔량이 0인 표본의 비율보다 이 두 값이 병목을 잘 보여 준다.
429 본문의 limited_by는 요청 로그에 남지 않으므로 서버 쪽 숫자로 본다.
"""

import argparse
import csv
import json
from pathlib import Path

# 버킷마다 (/stats quotas 키, 출력 이름, /stats에서 쓴 양, 서버 CSV 잔량 열)
# 출력은 예약했다가 정산하므로 쓴 양은 실제 생성량이다
BUCKETS = (
    ("requests", "요청", "accepted", "requests_remaining"),
    ("input_tokens", "입력", "input_tokens_consumed", "input_tokens_remaining"),
    ("output_tokens", "출력", "output_tokens_consumed", "output_tokens_remaining"),
)


def read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def overlap_window(rows: list[dict]) -> tuple[float, float]:
    """같이 돈 구간 (시작, 끝). 200을 하나도 못 받은 에이전트는 시작 시각에 끝난 것으로 본다."""
    starts, ends = {}, {}
    for r in rows:
        agent = r["agent_id"]
        starts[agent] = min(starts.get(agent, float("inf")), float(r["t_send"]))
        if r["status"] == "200":
            ends[agent] = max(ends.get(agent, 0.0), float(r["t_done"]))
    first_end = min(ends.get(agent, start) for agent, start in starts.items())   # 빠른 쪽이 끝난 시각
    return max(starts.values()), first_end                                       # 늦게 시작한 쪽부터


def overlap_seconds(rows: list[dict]) -> float:
    """두 에이전트가 같이 돈 시간(초)."""
    start, end = overlap_window(rows)
    return max(0.0, end - start)


def window_rates(rows: list[dict], start: float, end: float) -> dict[str, dict[str, float]]:
    """구간 [start, end] 안에 t_done이 있는 200만 세서 초당 성공 요청 수, 입력·출력 토큰/초.

    키는 "전체"와 에이전트 id (구간 안에 200이 없는 에이전트도 0으로 들어간다). 구간 길이가 0 이하면 모두 0.
    서버는 토큰을 수락할 때 빼지만 여기서는 완료 시각으로 세므로, 구간 양 끝에서 응답 시간(약 1초)만큼 어긋날 수 있다.
    """
    agents = sorted({r["agent_id"] for r in rows})
    totals = {name: {"requests": 0, "input_tokens": 0, "output_tokens": 0} for name in ["전체", *agents]}
    for r in rows:
        if r["status"] != "200" or not start <= float(r["t_done"]) <= end:
            continue
        for name in ("전체", r["agent_id"]):
            totals[name]["requests"] += 1
            totals[name]["input_tokens"] += int(r["input_tokens"] or 0)     # 빈 칸은 0 (중단된 실행 로그 등)
            totals[name]["output_tokens"] += int(r["output_tokens"] or 0)
    length = end - start
    return {name: {key: (count / length if length > 0 else 0.0) for key, count in t.items()}
            for name, t in totals.items()}


def format_rates(rates: dict[str, dict[str, float]], refill: dict[str, float]) -> str:
    """'전체: 요청 0.3/초(60%), 입력 120/초(60%), 출력 15/초(15%) / a1: ...'. %는 값 ÷ 충전 속도.

    구간 시작에 버킷에 쌓여 있던 양(최대 용량)까지 더 쓸 수 있으므로 100%를 조금 넘을 수 있다.
    넘는 양은 최대 용량 ÷ (충전 속도 × 구간 길이). 예: 입력 10000 ÷ (5000 × 30초) ≈ 7%p, 요청 10 ÷ (20 × 30초) ≈ 2%p
    """
    parts = []
    for name, r in rates.items():
        parts.append(f"{name}: 요청 {r['requests']:.1f}/초({r['requests'] / refill['requests']:.0%}), "
                     f"입력 {r['input_tokens']:,.0f}/초({r['input_tokens'] / refill['input_tokens']:.0%}), "
                     f"출력 {r['output_tokens']:,.0f}/초({r['output_tokens'] / refill['output_tokens']:.0%})")
    return " / ".join(parts)


def utilization(used: float, capacity: float, refill_rate: float, elapsed: float) -> float:
    """토큰 버킷 사용률 = 쓴 양 ÷ (처음 가득 찬 용량 + 충전 속도 × 경과 시간). 1에 가까우면 한도를 다 씀."""
    return used / (capacity + refill_rate * elapsed)


def min_remaining(samples: list[dict], column: str, capacity: float) -> float:
    """표본 중 가장 작은 잔량 ÷ 용량. 0에 가까우면 바닥까지 간 적이 있다. 표본이 없으면 1 (한 번도 안 씀)."""
    if not samples:
        return 1.0
    return min(float(s[column]) for s in samples) / capacity


def check_run(folder: Path, run: dict) -> str:
    """runs.csv 한 줄 → 보고 한 줄."""
    name = f"{run['exp']} {run['controller']} seed{run['seed']}"
    log = folder / run["csv"] if run.get("csv") else None
    state = folder / "server" / f"server_state_{run['run_id']}.csv"
    stats_path = log.with_name(log.stem + "_stats.json") if log else None
    if log is None or not (log.is_file() and state.is_file() and stats_path.is_file()):
        return f"{name}: 요청 로그·stats JSON·서버 CSV 중 없는 파일이 있음 ({run['status']})"
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    if "quotas" not in stats:                               # 실행 끝에 서버가 /stats에 답하지 않음
        return f"{name}: stats JSON에 서버 /stats가 없음 ({run['status']})"
    samples = read_csv(state)
    use, low = [], []
    for key, label, used, column in BUCKETS:
        quota = stats["quotas"][key]
        rate = utilization(stats[used], quota["capacity"], quota["refill_rate_per_second"], stats["elapsed_seconds"])
        use.append(f"{label} {rate:.0%}")
        low.append(f"{label} {min_remaining(samples, column, quota['capacity']):.0%}")
    rows = read_csv(log)
    start, end = overlap_window(rows)
    refill = {key: stats["quotas"][key]["refill_rate_per_second"] for key, _, _, _ in BUCKETS}
    return (f"{name}: {run['status']}·대조 {run['check']} | 같이 돈 구간 {overlap_seconds(rows):.1f}초"
            f" | 사용률 {' · '.join(use)} | 최소 잔량 {' · '.join(low)}"
            f" | 구간 처리량(충전 속도 대비) {format_rates(window_rates(rows, start, end), refill)}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="E4 빠른 확인: 같이 돈 구간과 먼저 찬 한도")
    p.add_argument("folder", type=Path, help="결과 폴더 (results/E4_<시각>)")
    args = p.parse_args(argv)
    for run in read_csv(args.folder / "runs.csv"):
        print(check_run(args.folder, run))
    return 0


if __name__ == "__main__":
    main()
