"""E4 빠른 확인 도구를 손으로 정한 요청 로그·stats JSON·서버 CSV로 확인한다."""

import csv
import json

import pytest

from runner.e4_check import check_run, min_remaining, overlap_seconds, utilization


def row(agent, t_send, t_done, status):
    return {"agent_id": agent, "t_send": str(t_send), "t_done": str(t_done), "status": str(status)}


LOG = [row("a1", 0.0, 0.9, 200), row("a1", 29.0, 30.0, 200),        # a1은 30초에 끝남
       row("a2", 0.1, 1.0, 200), row("a2", 150.0, 160.0, 200),
       row("a2", 165.0, 170.0, 429)]                                  # 429는 끝 시각에 안 셈


def test_overlap_is_first_end_minus_last_start():
    assert overlap_seconds(LOG) == pytest.approx(29.9)                # min(30, 160) − max(0.0, 0.1)


def test_overlap_zero_if_one_agent_never_succeeds():
    assert overlap_seconds([row("a1", 0.0, 5.0, 200), row("a2", 0.0, 1.0, 429)]) == 0.0


def test_utilization_is_used_over_capacity_plus_refill():
    # 용량 10, 초당 20, 4초 → 받을 수 있었던 양 10 + 20 × 4 = 90
    assert utilization(90, 10, 20, 4) == 1.0
    assert utilization(45, 10, 20, 4) == 0.5


def test_min_remaining_is_lowest_sample_over_capacity():
    samples = [{"r": v} for v in ("8", "0.3", "10")]
    assert min_remaining(samples, "r", 10) == pytest.approx(0.03)     # 0.3 / 10
    assert min_remaining([], "r", 10) == 1.0


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


RUN = {"exp": "E4-input", "controller": "C2", "seed": "1", "run_id": "r1", "status": "completed",
       "check": "OK", "csv": "E4-input_C2_seed1_r1.csv"}


def test_check_run_reports_one_line(tmp_path):
    write(tmp_path / RUN["csv"], LOG)
    write(tmp_path / "server" / "server_state_r1.csv",
          [{"requests_remaining": "100", "input_tokens_remaining": "10000", "output_tokens_remaining": "20000"},
           {"requests_remaining": "80", "input_tokens_remaining": "300", "output_tokens_remaining": "19800"}])
    stats = {"elapsed_seconds": 10.0, "accepted": 50, "input_tokens_consumed": 59400, "output_tokens_consumed": 4400,
             "quotas": {"requests": {"capacity": 100, "refill_rate_per_second": 10.0},          # 100 + 100 = 200
                        "input_tokens": {"capacity": 10000, "refill_rate_per_second": 5000.0},  # 10000 + 50000
                        "output_tokens": {"capacity": 20000, "refill_rate_per_second": 20000.0}}}  # 20000 + 200000
    (tmp_path / "E4-input_C2_seed1_r1_stats.json").write_text(json.dumps(stats), encoding="utf-8")
    assert check_run(tmp_path, RUN) == (
        "E4-input C2 seed1: completed·대조 OK | 같이 돈 구간 29.9초"
        " | 사용률 요청 25% · 입력 99% · 출력 2%"                       # 50/200, 59400/60000, 4400/220000
        " | 최소 잔량 요청 80% · 입력 3% · 출력 99%")                   # 80/100, 300/10000, 19800/20000


def test_check_run_without_server_stats(tmp_path):
    write(tmp_path / RUN["csv"], LOG)
    write(tmp_path / "server" / "server_state_r1.csv", [{"requests_remaining": "1"}])
    (tmp_path / "E4-input_C2_seed1_r1_stats.json").write_text(json.dumps({"error": "/stats 응답 없음"}),
                                                              encoding="utf-8")
    assert check_run(tmp_path, RUN) == "E4-input C2 seed1: stats JSON에 서버 /stats가 없음 (completed)"


def test_check_run_without_files(tmp_path):
    run = {**RUN, "run_id": "", "status": "aborted", "check": "-", "csv": ""}
    assert check_run(tmp_path, run) == "E4-input C2 seed1: 요청 로그·stats JSON·서버 CSV 중 없는 파일이 있음 (aborted)"
