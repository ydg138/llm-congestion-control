"""긴 입력 측정: 중계 서버 CSV(relay_requests_*.csv)로 Ollama의 입력 토큰당 prefill 시간을 낸다.

    python -m runner.prefill_rate results/E6-measure_1/server

인자는 중계 서버의 --log-dir 폴더다. 그 안의 relay_requests_*.csv를 모두 읽는다
(PowerShell은 python에 넘기는 인자의 *를 펼쳐 주지 않으므로 폴더를 받는다).
- 200인 행만 쓴다. prompt_eval_count가 비었거나 0인 행은 뺀다: Ollama는 0인 값을 응답에서 생략하므로
  접두사 캐시가 입력을 모두 맞혀 새로 계산한 입력이 없었다는 뜻이다. prefill_seconds가 빈 행도 뺀다. 뺀 개수를 출력한다
- 요청 입력 크기(input_tokens)별 중앙값: 한두 번 튀는 값(첫 요청 등)에 덜 흔들린다
- 직선 맞춤: 크기별 중앙값 점으로 prefill ≈ 고정 시간 + 토큰당 시간 × prompt_eval_count (최소제곱).
  토큰당 시간은 가짜 서버 input_token_seconds 보정값 후보 (P1)
- 덧붙는 토큰: prompt_eval_count − 요청 입력의 중앙값. 템플릿·지시문이 더한 토큰
  (RelayConfig.prompt_overhead_tokens 후보. 기록만 하고 바꾸지는 않는다)
"""

import argparse
import csv
import sys
from pathlib import Path
from statistics import median

NO_COUNT, NO_PREFILL = "prompt_eval_count 없음(캐시 적중)", "prefill_seconds 없음"


def read_rows(folder: Path) -> list[dict]:
    """폴더 안 relay_requests_*.csv의 모든 행."""
    rows = []
    for path in sorted(folder.glob("relay_requests_*.csv")):
        with open(path, encoding="utf-8", newline="") as f:
            rows.extend(csv.DictReader(f))
    return rows


def usable(rows) -> tuple[list[tuple[int, int, float]], dict[str, int]]:
    """200인 행 → (요청 입력, prompt_eval_count, prefill_seconds) 목록과 이유별로 뺀 행 수."""
    points, dropped = [], {NO_COUNT: 0, NO_PREFILL: 0}
    for r in rows:
        if r["status"] != "200":
            continue                                        # 502·529·취소는 Ollama가 끝까지 처리하지 않음
        if int(r["prompt_eval_count"] or 0) == 0:
            dropped[NO_COUNT] += 1                          # Ollama가 0을 생략 = 접두사 캐시 적중
        elif not r["prefill_seconds"]:
            dropped[NO_PREFILL] += 1
        else:
            points.append((int(r["input_tokens"]), int(r["prompt_eval_count"]), float(r["prefill_seconds"])))
    return points, dropped


def by_size(points) -> list[tuple[int, int, float, float]]:
    """요청 입력 크기별 (크기, 건수, prompt_eval_count 중앙값, prefill_seconds 중앙값). 크기 순."""
    groups = {}
    for size, count, prefill in points:
        groups.setdefault(size, []).append((count, prefill))
    return [(size, len(g), median(c for c, _ in g), median(p for _, p in g)) for size, g in sorted(groups.items())]


def fit_line(xy) -> tuple[float, float]:
    """최소제곱 직선 y = 절편 + 기울기 × x → (절편, 기울기). x 값이 2개 이상 달라야 한다."""
    mx = sum(x for x, _ in xy) / len(xy)
    my = sum(y for _, y in xy) / len(xy)
    slope = sum((x - mx) * (y - my) for x, y in xy) / sum((x - mx) ** 2 for x, _ in xy)
    return my - slope * mx, slope


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="긴 입력 측정: 입력 토큰당 prefill 시간")
    p.add_argument("folder", type=Path, help="중계 서버의 --log-dir 폴더 (relay_requests_*.csv가 있는 곳)")
    args = p.parse_args(argv)
    rows = read_rows(args.folder)
    points, dropped = usable(rows)
    print(f"행 {len(rows)}개 중 200이 아닌 행 {sum(r['status'] != '200' for r in rows)}개는 안 셈. "
          f"뺀 행: " + ", ".join(f"{reason} {n}개" for reason, n in dropped.items()))
    sizes = by_size(points)
    print("입력  건수  prompt_eval_count  prefill(초)  토큰당(초)  덧붙는 토큰   (모두 중앙값)")
    for size, n, count, prefill in sizes:
        print(f"{size:>5} {n:>5} {count:>18.0f} {prefill:>12.4f} {prefill / count:>11.6f} {count - size:>12.0f}")
    if len({count for _, _, count, _ in sizes}) < 2:
        print("크기가 2개 이상이어야 직선을 맞춘다")
        return 1
    intercept, slope = fit_line([(count, prefill) for _, _, count, prefill in sizes])
    print(f"직선 맞춤(크기 {len(sizes)}개의 중앙값): prefill ≈ {intercept:.4f}초 + {slope:.6f}초 × prompt_eval_count")
    print(f"덧붙는 토큰(prompt_eval_count − 요청 입력) 중앙값: {median(c - s for s, c, _ in points):.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
