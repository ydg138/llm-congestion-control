"""중계 서버 규칙(runner/relay_rules.py)을 손으로 계산한 값으로 확인한다."""

import pytest

from runner.agent import AgentSpec
from runner.relay_rules import (
    INSTRUCTION, RelayConfig, Timing, context_size, make_prompt, ollama_payload, seconds,
    settled_tokens, success_body, target_tokens,
)
from runner.workload import AGENT_CLASSES, ClassConfig
from server.config import ServerConfig
from server.model import LLMModel

# 가짜 서버 200 본문의 키 (server/mock_llm.py의 generate)
MOCK_200_KEYS = {"request_id", "agent_id", "workload_id", "run_id", "config_revision", "output",
                 "input_tokens", "output_tokens", "max_output_tokens", "queue_wait_seconds",
                 "prefill_seconds", "decode_seconds", "ttft_seconds", "service_seconds",
                 "server_elapsed_seconds", "model_prefill_seconds", "model_decode_seconds",
                 "token_interval_seconds"}


def words_of(prompt):
    return prompt.removesuffix(INSTRUCTION).split()


def test_prompt_has_input_minus_overhead_words():
    assert len(words_of(make_prompt("s1-a1-000", 100, 30))) == 70          # 100 - 30
    assert make_prompt("s1-a1-000", 10, 30) == INSTRUCTION                 # 입력보다 overhead가 크면 0개


def test_prompt_same_for_same_task_and_size_different_otherwise():
    a = make_prompt("s1-a1-000", 100, 0)
    assert a == make_prompt("s1-a1-000", 100, 0)
    assert words_of(a)[:10] != words_of(make_prompt("s1-a1-001", 100, 0))[:10]    # 다른 작업
    assert words_of(a)[:10] != words_of(make_prompt("s1-a1-000", 1000, 0))[:10]   # 같은 작업, 다른 입력 크기


def test_context_size_rounds_up_to_step():
    cfg = RelayConfig()
    light = [AgentSpec("a1", "light", "C2")]
    assert context_size(AGENT_CLASSES, light, cfg) == 4096                # 150 + 64 + 256 = 470 → 최소 4096
    classes = {**AGENT_CLASSES, "big": ClassConfig(4000, 4000, 640)}
    mixed = light + [AgentSpec("a2", "big", "C2")]
    assert context_size(classes, mixed, cfg) == 5120                      # 4000 + 640 + 256 = 4896 → 5120


def test_target_tokens_matches_mock_server_plan():
    model = LLMModel(ServerConfig().model())
    expected = model.plan("s1-a1-000", 100, 64).output_tokens
    assert target_tokens(model, "s1-a1-000", 100, 64) == expected          # 가짜 서버와 같은 길이
    assert 16 <= expected <= 64                                            # 기본 출력 범위 16~64
    assert target_tokens(model, "s1-a1-000", 100, 5) <= 5                  # 상한 이하


def test_ollama_payload_fixes_num_ctx_and_streams():
    payload = ollama_payload(RelayConfig(num_ctx=5120), "hello", num_predict=40, seed=42)
    assert payload["stream"] is True and payload["model"] == "llama3.1:8b"
    assert payload["options"] == {"num_ctx": 5120, "num_predict": 40, "seed": 42}


def test_seconds_and_settled_tokens():
    assert seconds(250_000_000) == 0.25                                    # 나노초 → 초
    assert seconds(None) is None and seconds(True) is None
    assert settled_tokens(70, 64) == 64                                    # 상한으로 자름
    assert settled_tokens(5, 3) == 3


def test_success_body_has_mock_keys_and_hand_computed_times():
    plan = LLMModel(ServerConfig().model()).plan("s1-a1-000", 100, 64)
    base = {"request_id": "r", "agent_id": "a1", "workload_id": "s1-a1-000", "run_id": "x",
            "config_revision": 1}
    final = {"done": True, "eval_count": 70, "prompt_eval_count": 112, "done_reason": "length",
             "prompt_eval_duration": 250_000_000, "load_duration": 50_000_000, "eval_duration": 900_000_000}
    timing = Timing(arrived=10.0, first_chunk=10.5, finished=11.5)
    body = success_body(base, 100, 64, 40, final, chunks=70, timing=timing, plan=plan, service_delay=0.05)
    assert MOCK_200_KEYS <= set(body)
    assert body["output_tokens"] == 64                                     # eval_count 70을 상한 64로
    assert body["ttft_seconds"] == pytest.approx(0.5)                      # 10.5 - 10.0
    assert body["server_elapsed_seconds"] == pytest.approx(1.5)            # 11.5 - 10.0
    assert body["queue_wait_seconds"] == pytest.approx(0.2)                # 0.5 - 0.25 - 0.05
    assert body["service_seconds"] == pytest.approx(1.3)                   # 추정 처리 시작 10.2 → 끝 11.5
    assert (body["prefill_seconds"], body["decode_seconds"]) == (0.25, 0.9)
    assert body["model_prefill_seconds"] == pytest.approx(0.05 + plan.prefill_seconds)


@pytest.mark.parametrize("prefill_ns, load_ns", [
    (250_000_000, 50_000_000),          # 보통: 대기 0.2초
    (900_000_000, 0),                   # prefill이 첫 조각까지 시간(0.5초)보다 김 → 대기 0으로 자름
])
def test_queue_wait_plus_service_is_server_elapsed(prefill_ns, load_ns):
    plan = LLMModel(ServerConfig().model()).plan("w", 100, 64)
    final = {"done": True, "eval_count": 40, "prompt_eval_duration": prefill_ns, "load_duration": load_ns}
    body = success_body({}, 100, 64, 40, final, chunks=40,
                        timing=Timing(10.0, 10.5, 11.5), plan=plan, service_delay=0.0)
    assert body["queue_wait_seconds"] >= 0
    assert body["queue_wait_seconds"] + body["service_seconds"] == pytest.approx(body["server_elapsed_seconds"])


def test_success_body_without_eval_count_uses_chunks():
    plan = LLMModel(ServerConfig().model()).plan("w", 0, 64)
    body = success_body({}, 0, 64, 10, {"done": True}, chunks=12,
                        timing=Timing(0.0, 0.1, 0.2), plan=plan, service_delay=0.0)
    assert body["output_tokens"] == 12
    assert body["queue_wait_seconds"] is None and body["service_seconds"] is None   # 시간 정보가 없으면 못 나눔
