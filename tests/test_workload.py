"""작업량 생성과 요청 로그 형식을 손으로 정한 값으로 확인한다."""

import csv

from runner.request_log import COLUMNS, RequestLog, log_path
from runner.workload import AGENT_CLASSES, ClassConfig, make_tasks


def test_same_seed_gives_same_tasks():
    assert make_tasks("a1", "light", 20, seed=1) == make_tasks("a1", "light", 20, seed=1)


def test_different_seed_gives_different_tasks():
    a = make_tasks("a1", "light", 20, seed=1)
    b = make_tasks("a1", "light", 20, seed=2)
    assert [t.input_tokens for t in a] != [t.input_tokens for t in b]
    assert a[0].task_id == "s1-a1-000" and b[0].task_id == "s2-a1-000"


def test_agents_do_not_share_task_ids():
    a = {t.task_id for t in make_tasks("a1", "light", 50, seed=1)}
    b = {t.task_id for t in make_tasks("a2", "light", 50, seed=1)}
    assert len(a) == 50 and len(b) == 50
    assert not a & b


def test_light_tokens_in_range():
    for t in make_tasks("a1", "light", 200, seed=3):
        assert 50 <= t.input_tokens <= 150
        assert t.max_output_tokens == 64


def test_heavy_in_tokens_in_range():
    for t in make_tasks("a2", "heavy_in", 200, seed=3):
        assert 500 <= t.input_tokens <= 1500
        assert t.max_output_tokens == 64


def test_heavy_in_is_light_input_times_ten_same_output_cap():
    light, heavy = AGENT_CLASSES["light"], AGENT_CLASSES["heavy_in"]
    assert (heavy.input_min, heavy.input_max) == (10 * light.input_min, 10 * light.input_max)  # 500, 1500
    assert heavy.max_output_tokens == light.max_output_tokens                                 # 64


def test_heavy_in_same_seed_gives_same_tasks():
    assert make_tasks("a2", "heavy_in", 20, seed=1) == make_tasks("a2", "heavy_in", 20, seed=1)


def test_est_tokens_is_input_plus_max_output():
    classes = {"fixed": ClassConfig(input_min=100, input_max=100, max_output_tokens=64)}
    tasks = make_tasks("a1", "fixed", 3, seed=0, classes=classes)
    assert [t.input_tokens for t in tasks] == [100, 100, 100]
    assert tasks[0].est_tokens == 164       # 100 + 64


def test_columns_match_interface_section_3():
    expected = ("run_id, exp, controller, seed, agent_id, agent_class, task_id, req_no, attempt, "
                "t_send, t_done, status, input_tokens, max_output_tokens, output_tokens, ttft_ms, "
                "latency_ms, retry_after, cwnd, in_flight")
    assert COLUMNS == expected.split(", ")


def test_request_log_writes_header_and_blank_cells(tmp_path):
    path = log_path(tmp_path, "E1", "C2", 1, "run-x")
    assert path.name == "E1_C2_seed1_run-x.csv"
    log = RequestLog(path, run_id="run-x", exp="E1", seed=1)
    log.write({"agent_id": "a1", "status": 429, "retry_after": 0.25})
    log.close()
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0]) == COLUMNS
    assert rows[0]["run_id"] == "run-x" and rows[0]["status"] == "429"
    assert rows[0]["output_tokens"] == ""   # 429라 출력 토큰은 빈 칸
