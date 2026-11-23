"""E4 빠른 확인: 두 에이전트가 같이 돈 구간과, 어느 한도가 먼저 찼는지.

    python -m runner.e4_check results/E4_<시각>

결과 폴더의 runs.csv에 있는 실행마다 한 줄씩 출력한다.
- 같이 돈 구간: 요청 로그에서 에이전트마다 (첫 t_send, 마지막 200의 t_done)을 구해 min(끝) − max(시작)
- 한도 부족 표본: 서버 CSV(server/server_state_<run_id>.csv)의 0.1초 표본 가운데
  남은 한도가 요청 하나의 비용보다 작았던 비율. 의도한 병목 버킷이 가장 높아야 한다
429 본문의 limited_by는 요청 로그에 남지 않으므로 서버 CSV의 남은 한도로 본다.
"""

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Costs:
    """요청 하나가 버킷마다 쓰는 양. 남은 한도가 이보다 작으면 그 요청은 429를 받는다."""
    requests: float = 1         # 요청 버킷: 1건
    input: float = 1000         # 입력 버킷: heavy_in 평균 입력
    output: float = 64          # 출력 버킷: 출력 상한만큼 예약


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


def short_share(samples: list[dict], column: str, cost: float) -> float:
    """표본 중 남은 한도(column)가 cost보다 작았던 비율 (0~1). 표본이 없으면 0."""
    if not samples:
        return 0.0
    return sum(float(s[column]) < cost for s in samples) / len(samples)


def check_run(folder: Path, run: dict, costs: Costs = Costs()) -> str:
    """runs.csv 한 줄 → 보고 한 줄."""
    name = f"{run['exp']} {run['controller']} seed{run['seed']}"
    log = folder / run["csv"] if run.get("csv") else None
    state = folder / "server" / f"server_state_{run['run_id']}.csv"
    if log is None or not log.is_file() or not state.is_file():
        return f"{name}: 요청 로그나 서버 CSV가 없음 ({run['status']})"
    samples = read_csv(state)
    shares = {"requests": short_share(samples, "requests_remaining", costs.requests),
              "input": short_share(samples, "input_tokens_remaining", costs.input),
              "output": short_share(samples, "output_tokens_remaining", costs.output)}
    text = " · ".join(f"{bucket} {share:.0%}" for bucket, share in shares.items())
    return (f"{name}: {run['status']}·대조 {run['check']} | 같이 돈 구간 {overlap_seconds(read_csv(log)):.1f}초"
            f" | 한도 부족 표본 {text}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="E4 빠른 확인: 같이 돈 구간과 먼저 찬 한도")
    p.add_argument("folder", type=Path, help="결과 폴더 (results/E4_<시각>)")
    args = p.parse_args(argv)
    for run in read_csv(args.folder / "runs.csv"):
        print(check_run(args.folder, run))
    return 0


if __name__ == "__main__":
    main()
