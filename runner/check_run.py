"""요청 로그 CSV와 같은 이름의 stats JSON을 대조한다. 실행 결과를 분석에 써도 되는지 확인하는 용도.

    python -m runner.check_run results/<exp>_<controller>_seed<seed>_<run_id>.csv

하나라도 맞지 않으면 종료 코드 1. runner.status가 aborted인 실행은 분석에서 뺀다 (docs/interface.md 3절).
멈춘 실행은 멈추는 순간 응답을 기다리던 요청이 요청 로그에 없어 서버 숫자와 조금 어긋날 수 있다.
"""

import csv
import json
import sys
from collections import Counter
from pathlib import Path

from runner.request_log import COLUMNS


def read_run(csv_path: Path) -> tuple[list, list[dict], dict]:
    """(헤더, 행 목록, stats JSON)을 읽는다."""
    stats_path = csv_path.with_name(csv_path.stem + "_stats.json")
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    with open(csv_path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader), stats


def compare(header: list, rows: list[dict], stats: dict) -> list[tuple[str, object, object]]:
    """(확인 항목, 요청 로그 쪽 값, 기대값) 목록. 항목마다 두 값이 같아야 한다."""
    status = Counter(r["status"] for r in rows)
    ok_tasks = [r["task_id"] for r in rows if r["status"] == "200"]
    items = [
        ("runner.status", stats.get("runner", {}).get("status"), "completed"),
        ("헤더 = interface 3절", header == COLUMNS, True),
        ("run_id = stats run_id", {r["run_id"] for r in rows}, {stats.get("run_id")}),
        ("200 행 수 = completed", status["200"], stats.get("completed")),
        ("429 행 수 = rate_limited", status["429"], stats.get("rate_limited")),
        ("529 행 수 = overloaded", status["529"], stats.get("overloaded")),
        ("200 행의 task_id 중복 없음", len(set(ok_tasks)), len(ok_tasks)),
    ]
    for agent, s in stats.get("agent_stats", {}).items():
        mine = Counter(r["status"] for r in rows if r["agent_id"] == agent)
        items.append((f"{agent} 200 행 수 = completed", mine["200"], s["completed"]))
        items.append((f"{agent} 429 행 수 = rate_limited", mine["429"], s["rate_limited"]))
    return items


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("사용법: python -m runner.check_run <요청 로그 CSV>", file=sys.stderr)
        return 2
    header, rows, stats = read_run(Path(args[0]))
    print(f"{args[0]}: 행 {len(rows)}개, status {dict(Counter(r['status'] for r in rows))}")
    reason = stats.get("runner", {}).get("reason")
    if reason:
        print(f"runner.reason: {reason}")
    items = compare(header, rows, stats)
    for name, got, want in items:
        print(f"{'OK  ' if got == want else 'FAIL'} {name}: {got} / 기대 {want}")
    return 0 if all(got == want for _, got, want in items) else 1


if __name__ == "__main__":
    sys.exit(main())
