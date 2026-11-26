"""E6 보정 확인: 중계 서버 CSV(server/relay_requests_<run_id>.csv)로 Ollama 쪽 상태를 실행마다 한 줄.

    python -m runner.relay_check results/E6_<시각> [--parallel 4]

결과 폴더의 runs.csv에 있는 실행마다 한 줄. 한도 쪽(의도한 버킷만 바닥인지)은 runner/e4_check.py로 본다.
합격 판단 (docs/e6_runbook.md 5단계):
- 502(그중 invalid_response), 529(Ollama 대기열이 가득 참), 취소(클라이언트가 끊음): 모두 0이어야 한다
- 대기 추정: Ollama로 보낼 때 Ollama에 가 있던 요청 수(active_at_start, 자기 포함)가 --parallel
  (OLLAMA_NUM_PARALLEL)을 넘은 행의 비율. 넘었으면 Ollama 안에서 기다렸을 수 있다.
  10% 미만이어야 Ollama 대기열이 병목이 아니라고 본다
그 밖: done_reason별 건수 (200만. length는 목표 길이까지 생성, stop은 모델이 일찍 멈춤)
참고값 ("참고:" 뒤, 합격 기준 아님):
- TTFT 중앙값 (에이전트별, 200만). 기준은 0.014초 + 입력 × 토큰당 prefill (runner/prefill_rate.py)
- 출력 토큰당 decode 시간 = Σdecode_seconds ÷ Σoutput_tokens (200만). 기준 0.0063초는 동시 1 측정이라
  병렬 처리에서는 느려지는 게 정상이다 (한계로 기록)
- 모델 적재 최대(load_seconds): 0에 가까워야 실행 중에 모델을 다시 올리지 않은 것
가짜 서버 실행(--backend mock)에는 이 CSV가 없어 "중계 서버 CSV 없음"을 출력한다.
"""

import argparse
import csv
import sys
from pathlib import Path
from statistics import median


def read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def summarize(rows: list[dict], parallel: int) -> dict:
    """relay CSV 행(Ollama로 보낸 요청마다 한 줄) → 한 줄에 쓸 값."""
    ok = [r for r in rows if r["status"] == "200"]
    reasons, ttft = {}, {}
    for r in ok:
        reasons[r["done_reason"] or "없음"] = reasons.get(r["done_reason"] or "없음", 0) + 1
        if r["ttft_seconds"]:
            ttft.setdefault(r["agent_id"], []).append(float(r["ttft_seconds"]))
    decode = [(float(r["decode_seconds"]), int(r["output_tokens"])) for r in ok if r["decode_seconds"]]
    generated = sum(n for _, n in decode)
    return {
        "requests": len(rows),
        "502": sum(r["status"] == "502" for r in rows),
        "invalid_response": sum(r["status"] == "502" and r["error"].startswith("invalid_response") for r in rows),
        "529": sum(r["status"] == "529" for r in rows),
        "cancelled": sum(r["status"] == "cancelled" for r in rows),
        # 보낼 때 Ollama에 가 있던 요청(자기 포함)이 병렬 수보다 많으면 Ollama 안에서 기다렸을 수 있다
        "waited": sum(int(r["active_at_start"]) > parallel for r in rows) / len(rows) if rows else 0.0,
        "done_reasons": dict(sorted(reasons.items())),
        "ttft": {agent: median(values) for agent, values in sorted(ttft.items())},
        "decode_per_token": sum(d for d, _ in decode) / generated if generated else None,
        "load_max": max((float(r["load_seconds"]) for r in rows if r["load_seconds"]), default=None),
    }


def seconds(value: float | None, spec: str) -> str:
    return "-" if value is None else f"{value:{spec}}초"


def format_line(name: str, s: dict, parallel: int) -> str:
    reasons = ", ".join(f"{k} {v}" for k, v in s["done_reasons"].items()) or "-"
    ttft = ", ".join(f"{agent} {t:.3f}초" for agent, t in s["ttft"].items()) or "-"
    return (f"{name}: Ollama 요청 {s['requests']}건 | 502 {s['502']} (invalid_response {s['invalid_response']})"
            f" · 529 {s['529']} · 취소 {s['cancelled']} | 대기 추정 {s['waited']:.0%} (>{parallel})"
            f" | done_reason {reasons} | 참고: TTFT 중앙값 {ttft}"
            f" · 출력 토큰당 {seconds(s['decode_per_token'], '.4f')} · 적재 최대 {seconds(s['load_max'], '.2f')}")


def check_run(folder: Path, run: dict, parallel: int) -> str:
    """runs.csv 한 줄 → 보고 한 줄."""
    name = f"{run['exp']} {run['controller']} seed{run['seed']}"
    path = folder / "server" / f"relay_requests_{run['run_id']}.csv"
    if not run.get("run_id") or not path.is_file():
        return f"{name}: 중계 서버 CSV 없음 (가짜 서버 실행이거나 서버가 뜨지 않음, {run['status']})"
    return format_line(name, summarize(read_csv(path), parallel), parallel)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="E6 보정 확인: Ollama 쪽 오류·대기·지연")
    p.add_argument("folder", type=Path, help="결과 폴더 (results/E6_<시각>)")
    p.add_argument("--parallel", type=int, default=4, help="Ollama를 켤 때 준 OLLAMA_NUM_PARALLEL")
    args = p.parse_args(argv)
    for run in read_csv(args.folder / "runs.csv"):
        print(check_run(args.folder, run, args.parallel))
    return 0


if __name__ == "__main__":
    sys.exit(main())
