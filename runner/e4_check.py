"""E4 빠른 확인: 두 에이전트가 같이 돈 구간과, 어느 한도가 먼저 찼는지.

    python -m runner.e4_check results/E4_<시각>

결과 폴더의 runs.csv에 있는 실행마다 한 줄씩 출력한다.
- 같이 돈 구간: 요청 로그에서 에이전트마다 (첫 t_send, 마지막 200의 t_done)을 구해 min(끝) − max(시작)
- 사용률: 버킷마다 쓴 양 ÷ 받을 수 있었던 양(처음 가득 찬 용량 + 충전 속도 × 서버 경과 시간). stats JSON으로 계산
- 최소 잔량: 서버 CSV(server/server_state_<run_id>.csv)의 0.1초 표본 중 가장 작은 잔량 ÷ 용량
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


def overlap_seconds(rows: list[dict]) -> float:
    """두 에이전트가 같이 돈 시간(초). 200을 하나도 못 받은 에이전트는 시작 시각에 끝난 것으로 본다."""
    starts, ends = {}, {}
    for r in rows:
        agent = r["agent_id"]
        starts[agent] = min(starts.get(agent, float("inf")), float(r["t_send"]))
        if r["status"] == "200":
            ends[agent] = max(ends.get(agent, 0.0), float(r["t_done"]))
    first_end = min(ends.get(agent, start) for agent, start in starts.items())   # 빠른 쪽이 끝난 시각
    return max(0.0, first_end - max(starts.values()))                            # 늦게 시작한 쪽부터


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
    return (f"{name}: {run['status']}·대조 {run['check']} | 같이 돈 구간 {overlap_seconds(read_csv(log)):.1f}초"
            f" | 사용률 {' · '.join(use)} | 최소 잔량 {' · '.join(low)}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="E4 빠른 확인: 같이 돈 구간과 먼저 찬 한도")
    p.add_argument("folder", type=Path, help="결과 폴더 (results/E4_<시각>)")
    args = p.parse_args(argv)
    for run in read_csv(args.folder / "runs.csv"):
        print(check_run(args.folder, run))
    return 0


if __name__ == "__main__":
    main()
