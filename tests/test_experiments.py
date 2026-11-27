"""실험 정의(E1·E3·E4·E6)와 반복 순서를 확인한다."""

import json

import pytest

from client.factory import NAMES, make_controller
from runner.experiments import (
    E1, E1_SERVER, E3, E4, E4_SERVERS, E6, E6_MEASURE, E6_SEEDS, E6_SERVERS, EXPERIMENTS, SEEDS, select,
)
from runner.real_adapter import load_config
from runner.relay_rules import RelayConfig
from runner.workload import AGENT_CLASSES

E1_LABELS = ["C0-1", "C0-8", "C0-32", "C0-64", "C1", "C2"]
E4_LABELS = ["C2"] + (["C3"] if "C3" in NAMES else [])     # C3는 PR #12가 factory에 넣은 뒤에 생긴다


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


def test_e4_light_and_heavy_in_same_controller_start_together():
    assert [(c.exp, c.label) for c in E4] == [(f"E4-{n}", l) for n in ("input", "requests") for l in E4_LABELS]
    for c in E4:
        assert [(a.agent_id, a.agent_class, a.controller, a.start_delay) for a in c.agents] == [
            ("a1", "light", c.label, 0.0), ("a2", "heavy_in", c.label, 0.0)]
        assert all(a.agent_class in AGENT_CLASSES for a in c.agents)
        make_controller(c.label)                            # factory에 있는 이름인지
    assert {c.exp: c.tasks for c in E4} == {"E4-input": 750, "E4-requests": 300}
    assert {c.exp: c.server_config for c in E4} == {"E4-input": E4_SERVERS["input"],
                                                    "E4-requests": E4_SERVERS["requests"]}


def test_e4_runs_and_minimum_unchanged():
    assert len(select("E4")) == 2 * len(E4_LABELS) * 5      # 병목 2종 × 컨트롤러 × 시드 5
    assert [(c.exp, s) for c, s in select("E4", controller="C2", seed=1)] == [("E4-input", 1), ("E4-requests", 1)]
    assert len(select("minimum")) == 35                     # E4는 minimum에 넣지 않는다


def per_second(cfg: dict, input_tokens: int, max_output: int = 64) -> dict:
    """버킷마다 이 크기 요청을 초당 몇 건까지 받을 수 있나 = 충전 속도 ÷ 요청당 비용."""
    return {"requests": cfg["refill_rate"] / 1,
            "input_tokens": cfg["input_refill_rate"] / input_tokens,
            "output_tokens": cfg["output_refill_rate"] / max_output}  # 출력은 상한만큼 예약하므로 64로 본다


@pytest.mark.parametrize("neck, bucket", [("input", "input_tokens"), ("requests", "requests")])
def test_e4_server_config_fills_intended_bucket_first(neck, bucket):
    cfg = json.loads(E4_SERVERS[neck].read_text(encoding="utf-8"))
    for input_tokens in (100, 1500):                        # light 평균, heavy_in 최대
        rates = per_second(cfg, input_tokens)
        assert min(rates, key=rates.get) == bucket          # 가장 먼저 바닥나는 버킷
    assert cfg["input_capacity"] >= AGENT_CLASSES["heavy_in"].input_max     # 아니면 heavy_in 요청이 422
    assert cfg["output_capacity"] >= AGENT_CLASSES["heavy_in"].max_output_tokens


def test_e4_temporary_configs_by_hand():
    """임시 설정 손계산. 입력 100이면 e4_input은 요청 200·입력 50·출력 312.5건/초."""
    cfg = json.loads(E4_SERVERS["input"].read_text(encoding="utf-8"))
    assert per_second(cfg, 100) == {"requests": 200, "input_tokens": 50, "output_tokens": 312.5}
    cfg = json.loads(E4_SERVERS["requests"].read_text(encoding="utf-8"))
    assert (cfg["refill_rate"], cfg["capacity"]) == (20, 10)   # week13.md: 초당 20건, 용량 10


# ---------- E6: E4 축소판을 Ollama에서 ----------

def test_e6_same_agents_as_e4_with_own_tasks_and_configs():
    assert [(c.exp, c.label) for c in E6] == [(f"E6-{n}", l) for n in ("input", "requests") for l in E4_LABELS]
    for c in E6:
        assert [(a.agent_id, a.agent_class, a.controller, a.start_delay) for a in c.agents] == [
            ("a1", "light", c.label, 0.0), ("a2", "heavy_in", c.label, 0.0)]
    assert {c.exp: c.tasks for c in E6} == {"E6-input": 100, "E6-requests": 100}
    assert {c.exp: c.server_config for c in E6} == {"E6-input": E6_SERVERS["input"],
                                                    "E6-requests": E6_SERVERS["requests"]}


def test_e6_is_twelve_runs_with_own_seeds():
    assert E6_SEEDS == (1, 2, 3) and SEEDS == (1, 2, 3, 4, 5)   # E6만 3회, 나머지는 그대로
    plan = select("E6")
    assert len(plan) == 12                                  # 병목 2종 × C2·C3 × 시드 3
    assert sorted({s for _, s in plan}) == [1, 2, 3]
    assert [(c.exp, c.label) for c, _ in select("E6", seed=1)] == [
        ("E6-input", "C2"), ("E6-input", "C3"), ("E6-requests", "C2"), ("E6-requests", "C3")]
    assert len(select("E4")) == 20 and len(select("minimum")) == 35     # 다른 실험은 그대로


@pytest.mark.parametrize("path", [E6_SERVERS["input"], E6_SERVERS["requests"], E6_MEASURE])
def test_e6_configs_load(path):
    load_config(path)                                       # 모르는 키·범위 밖 값이면 ValueError (가짜 서버도 같은 검사)


@pytest.mark.parametrize("neck, bucket", [("input", "input_tokens"), ("requests", "requests")])
def test_e6_server_config_fills_intended_bucket_first(neck, bucket):
    cfg = json.loads(E6_SERVERS[neck].read_text(encoding="utf-8"))
    for input_tokens in (100, 1500):                        # light 평균, heavy_in 최대
        rates = per_second(cfg, input_tokens)
        assert min(rates, key=rates.get) == bucket
    assert cfg["input_capacity"] >= AGENT_CLASSES["heavy_in"].input_max     # 2000 ≥ 1500, 아니면 422
    assert cfg["output_capacity"] >= AGENT_CLASSES["heavy_in"].max_output_tokens


def test_e6_configs_by_hand():
    """보정 전 추정치 손계산. 입력 100이면 e6_input은 요청 200·입력 10·출력 312.5건/초."""
    cfg = json.loads(E6_SERVERS["input"].read_text(encoding="utf-8"))
    assert per_second(cfg, 100) == {"requests": 200, "input_tokens": 10, "output_tokens": 312.5}
    assert cfg["input_capacity"] == 2000
    cfg = json.loads(E6_SERVERS["requests"].read_text(encoding="utf-8"))
    assert (cfg["refill_rate"], cfg["capacity"]) == (3, 3)


def test_e6_measure_passes_one_long_request():
    """입력 8000·출력 64 한 건이 모든 버킷과 num_ctx 16384를 통과한다."""
    cfg = json.loads(E6_MEASURE.read_text(encoding="utf-8"))
    assert cfg["capacity"] >= 1 and cfg["input_capacity"] >= 8000 and cfg["output_capacity"] >= 64
    assert 8000 + 64 + RelayConfig.context_margin <= 16384  # 8320 ≤ 16384


def test_select_filters_controller_and_seed():
    got = [(c.exp, c.label, s) for c, s in select("minimum", controller="C2")]
    assert got == [(e, "C2", s) for s in SEEDS for e in ("E1", "E3")]
    assert [(c.label, s) for c, s in select("E1", controller="C0-8", seed=3)] == [("C0-8", 3)]
