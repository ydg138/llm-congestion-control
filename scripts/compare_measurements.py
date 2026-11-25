"""동시성별 실제/모의 서버 측정 비교. 결과만 계산하며 모델을 자동 수정하지 않음."""

import argparse
import csv
import json
from pathlib import Path

try:
    from scripts.measurements import compare
except ModuleNotFoundError:
    from measurements import compare


def load(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"backend", "concurrency", "status", "error", "total_seconds", "ttft_kind"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"Missing CSV columns: {sorted(required - set(reader.fieldnames or []))}")
        rows = list(reader)
    for row in rows:
        if not 1 <= int(row["concurrency"]) <= 256:
            raise ValueError("invalid concurrency")
    return rows


def main():
    parser = argparse.ArgumentParser(description="실제/모의 측정 결과 비교 JSON 작성")
    parser.add_argument("--real", required=True)
    parser.add_argument("--mock", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--min-samples", type=int, default=5)
    args = parser.parse_args()
    if args.min_samples < 1:
        parser.error("min-samples는 1 이상이어야 합니다")
    try:
        real, mock = load(args.real), load(args.mock)
        if any(row["backend"] != "ollama" for row in real):
            raise ValueError("--real requires Ollama measurements")
        if any(row["backend"] != "mock" for row in mock):
            raise ValueError("--mock requires mock measurements")
        result = compare(real, mock, args.min_samples)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        print(f"Saved comparison to {output}")
        if not result["matched_concurrency"]:
            print("No matching concurrency groups; comparison unavailable.")
            return 1
        return 0
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
