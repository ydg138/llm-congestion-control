"""E6 보정 확인 도구(runner/relay_check.py)를 손으로 정한 relay CSV로 확인한다."""

import csv

import pytest

from runner import relay_check
from runner.real_adapter import RELAY_COLUMNS
from runner.relay_check import check_run, summarize


def row(agent, status, ttft="", decode="", output=0, active=1, load="", done="", error=""):
    r = dict.fromkeys(RELAY_COLUMNS, "")                    # 중계 서버가 쓰는 열 그대로
    r.update(agent_id=agent, status=str(status), ttft_seconds=str(ttft), decode_seconds=str(decode),
             output_tokens=str(output), active_at_start=str(active), load_seconds=str(load),
             done_reason=done, error=error)
    return r


# active_at_start: a1은 1·5·1·6, a2는 2·4·1·1 (병렬 4를 넘는 것은 a1의 5·6뿐 = 초반 light 몰림 모양)
ROWS = [row("a1", 200, 0.02, 0.063, 10, active=1, load=0.0, done="stop"),
        row("a1", 200, 0.04, 0.189, 30, active=5, load=0.0, done="length"),
        row("a1", 200, 0.03, 0.126, 20, active=1, load=0.0, done="length"),
        row("a2", 200, 0.12, 0.252, 40, active=2, load=1.5, done="length"),
        row("a2", 502, 0.05, output=3, active=4, error="invalid_response eval_count=-1"),
        row("a2", 502, active=1, error="timeout"),
        row("a2", 529, active=1),
        row("a1", "cancelled", active=6)]


def test_summarize_by_hand():
    s = summarize(ROWS, parallel=4)
    assert (s["requests"], s["502"], s["invalid_response"], s["529"], s["cancelled"]) == (8, 2, 1, 1, 1)
    assert s["waited"] == 2 / 8                             # active 5·6만 4를 넘음
    assert s["waited_by_agent"] == {"a1": 2 / 4, "a2": 0.0}     # 둘 다 a1
    assert s["done_reasons"] == {"length": 3, "stop": 1}    # 200만
    assert s["ttft"] == {"a1": 0.03, "a2": 0.12}            # a1: 0.02·0.04·0.03의 중앙값, 502의 0.05는 안 셈
    assert s["decode_per_token"] == pytest.approx(0.0063)   # (0.063 + 0.189 + 0.126 + 0.252) ÷ (10 + 30 + 20 + 40)
    assert s["load_max"] == 1.5
    s = summarize(ROWS, parallel=1)                         # 1을 넘는 것: a1의 5·6, a2의 2·4
    assert (s["waited"], s["waited_by_agent"]) == (4 / 8, {"a1": 2 / 4, "a2": 2 / 4})


def test_summarize_without_rows():
    s = summarize([], parallel=4)
    assert (s["requests"], s["waited"], s["waited_by_agent"]) == (0, 0.0, {})
    assert (s["decode_per_token"], s["load_max"]) == (None, None)


RUN = {"exp": "E6-input", "controller": "C3", "seed": "1", "run_id": "r1", "status": "completed",
       "check": "OK", "csv": "E6-input_C3_seed1_r1.csv"}


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def test_check_run_reports_one_line(tmp_path):
    write(tmp_path / "server" / "relay_requests_r1.csv", ROWS)
    assert check_run(tmp_path, RUN, 4) == (
        "E6-input C3 seed1: Ollama 요청 8건 | 502 2 (invalid_response 1) · 529 1 · 취소 1"
        " | 대기 추정(>4) 25% (a1 50%, a2 0%) | done_reason length 3, stop 1"
        " | 참고: TTFT 중앙값 a1 0.030초, a2 0.120초 · 출력 토큰당 0.0063초 · 적재 최대 1.50초")


def test_check_run_without_relay_csv(tmp_path):
    expected = "E6-input C3 seed1: 중계 서버 CSV 없음 (가짜 서버 실행이거나 서버가 뜨지 않음, completed)"
    assert check_run(tmp_path, RUN, 4) == expected
    assert "중계 서버 CSV 없음" in check_run(tmp_path, {**RUN, "run_id": "", "status": "aborted"}, 4)


def test_main_reads_runs_csv(tmp_path, capsys):
    write(tmp_path / "server" / "relay_requests_r1.csv", ROWS)
    write(tmp_path / "runs.csv", [RUN, {**RUN, "controller": "C2", "run_id": "r2"}])
    assert relay_check.main([str(tmp_path), "--parallel", "1"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "대기 추정(>1) 50% (a1 50%, a2 50%)" in lines[0]
    assert lines[1].startswith("E6-input C2 seed1: 중계 서버 CSV 없음")
