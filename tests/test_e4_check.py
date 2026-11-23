"""E4 빠른 확인 도구를 손으로 정한 요청 로그·서버 CSV로 확인한다."""

import csv

import pytest

from runner.e4_check import Costs, check_run, overlap_seconds, short_share


def row(agent, t_send, t_done, status):
    return {"agent_id": agent, "t_send": str(t_send), "t_done": str(t_done), "status": str(status)}


LOG = [row("a1", 0.0, 0.9, 200), row("a1", 29.0, 30.0, 200),        # a1은 30초에 끝남
       row("a2", 0.1, 1.0, 200), row("a2", 150.0, 160.0, 200),
       row("a2", 165.0, 170.0, 429)]                                  # 429는 끝 시각에 안 셈


def test_overlap_is_first_end_minus_last_start():
    assert overlap_seconds(LOG) == pytest.approx(29.9)                # min(30, 160) − max(0.0, 0.1)


def test_overlap_zero_if_one_agent_never_succeeds():
    assert overlap_seconds([row("a1", 0.0, 5.0, 200), row("a2", 0.0, 1.0, 429)]) == 0.0


def test_short_share_counts_samples_below_cost():
    samples = [{"r": v} for v in ("0.4", "5", "0.9", "12")]
    assert short_share(samples, "r", 1) == 0.5                        # 0.4, 0.9 두 개 / 4개
    assert short_share([], "r", 1) == 0.0


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def test_check_run_reports_one_line(tmp_path):
    write(tmp_path / "E4-input_C2_seed1_r1.csv", LOG)
    write(tmp_path / "server" / "server_state_r1.csv",
          [{"requests_remaining": "150", "input_tokens_remaining": "300", "output_tokens_remaining": "19000"},
           {"requests_remaining": "180", "input_tokens_remaining": "2500", "output_tokens_remaining": "19500"}])
    run = {"exp": "E4-input", "controller": "C2", "seed": "1", "run_id": "r1", "status": "completed",
           "check": "OK", "csv": "E4-input_C2_seed1_r1.csv"}
    assert check_run(tmp_path, run, Costs()) == (
        "E4-input C2 seed1: completed·대조 OK | 같이 돈 구간 29.9초"
        " | 한도 부족 표본 requests 0% · input 50% · output 0%")       # 입력 300 < 1000 한 번


def test_check_run_without_files(tmp_path):
    run = {"exp": "E4-input", "controller": "C2", "seed": "1", "run_id": "", "status": "aborted",
           "check": "-", "csv": ""}
    assert check_run(tmp_path, run) == "E4-input C2 seed1: 요청 로그나 서버 CSV가 없음 (aborted)"
