"""check_run.compare가 맞는 실행은 통과시키고 틀린 숫자는 잡아내는지 손으로 만든 행으로 확인한다."""

from runner.check_run import compare
from runner.request_log import COLUMNS


def row(status, task_id, agent_id="a1"):
    return {"run_id": "r", "status": status, "task_id": task_id, "agent_id": agent_id}


ROWS = [row("429", "t0"), row("200", "t0"), row("200", "t1")]   # t0은 429 한 번 뒤 성공


def stats(**changes):
    s = {"runner": {"status": "completed", "reason": None}, "run_id": "r",
         "completed": 2, "rate_limited": 1, "overloaded": 0,
         "agent_stats": {"a1": {"completed": 2, "rate_limited": 1}}}
    s.update(changes)
    return s


def failed(items):
    return [name for name, got, want in items if got != want]


def test_matching_run_passes():
    assert failed(compare(COLUMNS, ROWS, stats())) == []


def test_count_mismatch_is_caught():
    assert failed(compare(COLUMNS, ROWS, stats(rate_limited=2))) == ["429 행 수 = rate_limited"]


def test_aborted_run_is_caught():
    aborted = stats(runner={"status": "aborted", "reason": "안전장치: 30초 동안 완료 없음"})
    assert failed(compare(COLUMNS, ROWS, aborted)) == ["runner.status"]


def test_duplicate_success_is_caught():
    rows = ROWS + [row("200", "t1")]                                # 같은 작업이 두 번 성공
    s = stats(completed=3, agent_stats={"a1": {"completed": 3, "rate_limited": 1}})
    assert failed(compare(COLUMNS, rows, s)) == ["200 행의 task_id 중복 없음"]
