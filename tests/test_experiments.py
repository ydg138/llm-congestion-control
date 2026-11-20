"""실험 정의(E1·E3)와 반복 순서를 확인한다."""

import json

from client.factory import make_controller
from runner.experiments import E1, E1_SERVER, E3, EXPERIMENTS, SEEDS, select

E1_LABELS = ["C0-1", "C0-8", "C0-32", "C0-64", "C1", "C2"]


def test_e1_has_six_single_agent_conditions():
    assert [c.label for c in E1] == E1_LABELS
    for c in E1:
        (a,) = c.agents                                     # 에이전트 1개
        assert (a.agent_class, a.controller, a.start_delay) == ("light", c.label, 0.0)
        assert c.tasks == 100
        make_controller(a.controller)                       # factory에 있는 이름인지


def test_e3_two_same_controller_agents_one_late():
    (c,) = E3
    assert [a.agent_id for a in c.agents] == ["a1", "a2"]
    assert [a.controller for a in c.agents] == ["C2", "C2"]
    assert [a.start_delay for a in c.agents] == [0.0, 10.0]  # a2는 10초 뒤 합류
    assert c.tasks == 200


def test_minimum_is_e1_plus_e3_times_five_seeds():
    assert EXPERIMENTS["minimum"] == E1 + E3
    assert SEEDS == (1, 2, 3, 4, 5)
    assert len(select("minimum")) == 35                     # (6 + 1) 조건 × 5 시드


def test_e1_server_config_has_week12_values():
    cfg = json.loads(E1_SERVER.read_text(encoding="utf-8"))
    got = (cfg["output_token_seconds"], cfg["slots"], cfg["queue_size"], cfg["capacity"], cfg["refill_rate"])
    assert got == (0.02, 64, 64, 3, 10)
    assert all(c.server_config == E1_SERVER for c in E1 + E3)   # E3도 같은 서버 설정


def test_select_puts_seed_outside():
    got = [(c.label, s) for c, s in select("E1")[:7]]
    assert got == [(label, 1) for label in E1_LABELS] + [("C0-1", 2)]   # 시드 1 전부 → 시드 2


def test_select_filters_controller_and_seed():
    got = [(c.exp, c.label, s) for c, s in select("minimum", controller="C2")]
    assert got == [(e, "C2", s) for s in SEEDS for e in ("E1", "E3")]
    assert [(c.label, s) for c, s in select("E1", controller="C0-8", seed=3)] == [("C0-8", 3)]
