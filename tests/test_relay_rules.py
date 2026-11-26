"""중계 서버 규칙(runner/relay_rules.py)을 손으로 계산한 값으로 확인한다."""

import pytest

from runner.agent import AgentSpec
from runner.relay_rules import (
    INSTRUCTION, RelayConfig, Timing, context_size, is_count, make_prompt, ollama_payload, reason_key,
    seconds, settled_tokens, success_body,
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


def test_ollama_payload_fixes_num_ctx_and_streams():
    payload = ollama_payload(RelayConfig(num_ctx=5120), "hello", num_predict=40, seed=42)
    assert payload["stream"] is True and payload["model"] == "llama3.1:8b"
    assert payload["options"] == {"num_ctx": 5120, "num_predict": 40, "seed": 42}


def test_seconds():
    assert seconds(250_000_000) == 0.25                                    # 나노초 → 초
    assert seconds(None) is None and seconds(True) is None


def test_is_count_accepts_only_int_between_0_and_maximum():
    assert is_count(0, 64) and is_count(64, 64) and is_count(10**9)        # 경계값, 상한 없음
    for bad in (-1, 65, True, 2.5, "5", None):
        assert not is_count(bad, 64)


def test_settled_tokens_uses_eval_count_only_for_valid_200():
    assert settled_tokens(200, {"eval_count": 3}, chunks=1, maximum=64) == 3       # 조기 종료: Ollama가 센 값
    assert settled_tokens(200, {}, chunks=70, maximum=64) == 64                    # 키 없음: 조각 수를 상한으로 자름
    assert settled_tokens(502, {"eval_count": 5}, chunks=2, maximum=64) == 2       # 502는 조각 수
    assert settled_tokens("cancelled", {}, chunks=3, maximum=64) == 3              # 취소도 조각 수
    assert settled_tokens(200, {"eval_count": None}, chunks=2, maximum=64) == 2    # 이상한 값이 와도 예외 없음


def test_reason_key_is_always_a_string():
    assert reason_key("stop") == "stop"
    assert reason_key(None) == "null"                                      # 없음: 예전 /stats JSON과 같은 표기
    assert reason_key(["x"]) == '["x"]'                                    # dict 키로 못 쓰는 값


def test_success_body_has_mock_keys_and_hand_computed_times():
    plan = LLMModel(ServerConfig().model()).plan("s1-a1-000", 100, 64)
    base = {"request_id": "r", "agent_id": "a1", "workload_id": "s1-a1-000", "run_id": "x",
            "config_revision": 1}
    final = {"done": True, "eval_count": 40, "prompt_eval_count": 112, "done_reason": "length",
             "prompt_eval_duration": 250_000_000, "load_duration": 50_000_000, "eval_duration": 900_000_000}
    timing = Timing(arrived=10.0, first_chunk=10.5, finished=11.5)
    body = success_body(base, 100, 64, 40, final, generated=40, timing=timing, plan=plan, service_delay=0.05)
    assert MOCK_200_KEYS <= set(body)
    assert body["output_tokens"] == 40                                     # settle이 정한 값 그대로
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
    body = success_body({}, 100, 64, 40, final, generated=40,
                        timing=Timing(10.0, 10.5, 11.5), plan=plan, service_delay=0.0)
    assert body["queue_wait_seconds"] >= 0
    assert body["queue_wait_seconds"] + body["service_seconds"] == pytest.approx(body["server_elapsed_seconds"])


def test_success_body_without_eval_count_uses_settled_value():
    plan = LLMModel(ServerConfig().model()).plan("w", 0, 64)
    body = success_body({}, 0, 64, 10, {"done": True}, generated=12,      # 키 없음: settle이 조각 수 12로 정산
                        timing=Timing(0.0, 0.1, 0.2), plan=plan, service_delay=0.0)
    assert body["output_tokens"] == 12
    assert body["queue_wait_seconds"] is None and body["service_seconds"] is None   # 시간 정보가 없으면 못 나눔
