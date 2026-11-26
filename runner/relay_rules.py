"""Ollama 중계 서버(runner/real_adapter.py)의 규칙. 네트워크 없이 값만 계산하는 순수 함수.

- 프롬프트: 요청의 input_tokens만큼 단어를 고른다 (작업·입력 크기마다 다르게)
- 문맥 길이 num_ctx: 조건에 나오는 에이전트 종류에서 계산 (실행마다 고정)
(목표 출력 길이는 real_adapter.py가 가짜 서버와 같은 계산 LLMModel.plan으로 정한다)
- Ollama 요청 본문, Ollama 마지막 조각 → 우리 API 2.0의 200 본문, 정산할 생성량
"""

import json
import math
import random
from dataclasses import dataclass

NS_PER_SECOND = 1_000_000_000           # Ollama의 시간 필드는 나노초

# 흔한 영단어. 대부분 토큰 1개라고 알고 있지만 확인하지 않았다 → 긴 입력 측정 뒤 prompt_overhead_tokens로 보정
WORDS = ("time", "year", "people", "way", "day", "man", "thing", "woman", "life", "child",
         "world", "school", "state", "family", "student", "group", "country", "problem", "hand", "part",
         "place", "case", "week", "company", "system", "program", "question", "work", "number", "night",
         "point", "home", "water", "room", "mother", "area", "money", "story", "fact", "month",
         "right", "study", "book", "eye", "job", "word", "business", "issue", "side", "kind",
         "head", "house", "service", "friend", "father", "power", "hour", "game", "line", "end")
INSTRUCTION = "\n\nUsing the words above, write a long essay. Keep writing and do not stop early."


@dataclass(frozen=True)
class RelayConfig:
    """중계 서버 설정. 숫자는 여기서만 정한다."""
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "llama3.1:8b"
    num_ctx: int = 4096                 # 실행마다 고정 (바뀌면 Ollama가 모델을 다시 올린다고 알고 있음)
    keep_alive: str = "30m"             # 실행 사이에 모델을 내리지 않게
    ollama_timeout_seconds: float = 55.0    # 실행기 요청 타임아웃(60초)보다 먼저 걸리게
    warmup_timeout_seconds: float = 110.0   # 모델을 처음 올리는 시간 포함. run.py의 준비 대기(120초)보다 짧게
    info_timeout_seconds: float = 10.0      # /api/version, /api/ps
    prompt_overhead_tokens: int = 0     # 템플릿·지시문이 더하는 토큰 (긴 입력 측정 뒤 정함)
    context_margin: int = 256           # 입력 + 출력 상한 외의 여유
    context_min: int = 4096             # num_ctx 최소값
    context_step: int = 1024            # num_ctx를 이 단위로 올림
    read_bufsize: int = 1 << 20         # aiohttp 읽기 버퍼. readline 한 줄 한도는 이것의 2배


@dataclass(frozen=True)
class Timing:
    """중계 서버가 잰 시각 (time.perf_counter 초)."""
    arrived: float                      # 본문 검증을 마친 시각 (가짜 서버의 arrived와 같은 자리)
    first_chunk: float                  # 첫 비어 있지 않은 조각을 받은 시각
    finished: float                     # 마지막 조각을 받은 시각


def make_prompt(workload_id: str, input_tokens: int, overhead: int) -> str:
    """단어 (input_tokens - overhead)개 + 지시문. 같은 (작업, 입력 크기)면 항상 같은 문자열.

    작업·입력 크기마다 단어가 달라 앞부분이 겹치지 않는다 (Ollama 접두사 캐시를 피함).
    """
    rng = random.Random(f"{workload_id}:{input_tokens}")    # 문자열 시드: 프로세스가 달라도 같은 난수
    words = [rng.choice(WORDS) for _ in range(max(0, input_tokens - overhead))]
    return " ".join(words) + INSTRUCTION


def context_size(classes: dict, agents, cfg: RelayConfig) -> int:
    """조건의 에이전트 종류 중 가장 큰 (입력 최대 + 출력 상한) + 여유를 context_step 단위로 올림."""
    need = max(classes[a.agent_class].input_max + classes[a.agent_class].max_output_tokens
               for a in agents) + cfg.context_margin
    return max(cfg.context_min, math.ceil(need / cfg.context_step) * cfg.context_step)


def ollama_payload(cfg: RelayConfig, prompt: str, num_predict: int, seed: int) -> dict:
    """Ollama /api/generate 요청 본문. num_predict는 목표 출력 길이, num_ctx는 실행마다 같은 값."""
    return {"model": cfg.model, "prompt": prompt, "stream": True, "keep_alive": cfg.keep_alive,
            "options": {"num_ctx": cfg.num_ctx, "num_predict": num_predict, "seed": seed}}


def seconds(nanoseconds) -> float | None:
    """Ollama 나노초 → 초. 값이 없거나 숫자가 아니면 None."""
    if isinstance(nanoseconds, bool) or not isinstance(nanoseconds, (int, float)):
        return None
    return nanoseconds / NS_PER_SECOND


def is_count(value, maximum: float = math.inf) -> bool:
    """Ollama가 준 토큰 수가 0 이상 maximum 이하 정수인가. bool은 int의 하위형이라 뺀다 (mock_llm.integer와 같은 규칙)."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= maximum


def reason_key(done_reason) -> str:
    """done_reasons 집계 키. 문자열이 아니면 JSON 표기로 바꾼다 (리스트도 키로 쓸 수 있게, 없으면 "null")."""
    return done_reason if isinstance(done_reason, str) else json.dumps(done_reason)


def settled_tokens(status, final: dict, chunks: int, maximum: int) -> int:
    """정산할 생성량. 어떤 값이 와도 0~maximum 정수를 돌려준다 (settle_output이 예외를 내지 않게).

    200이고 eval_count가 정상(0~maximum 정수)이면 Ollama가 센 eval_count.
    그 밖(eval_count 키 없음, 502, 취소)은 받은 조각 수를 maximum으로 자른 값 (실제 토큰 수가 아닌 추정값).
    """
    count = final.get("eval_count")
    if status == 200 and is_count(count, maximum):
        return count
    return min(chunks, maximum)


def success_body(base: dict, inputs: int, maximum: int, target: int, final: dict, generated: int,
                 timing: Timing, plan, service_delay: float) -> dict:
    """Ollama 마지막 조각(final)과 중계 서버가 잰 시각을 우리 200 본문으로 바꾼다.

    base는 request_id·agent_id·workload_id·run_id·config_revision. 가짜 서버 200의 키는 모두 둔다.
    generated는 settle이 정산한 생성량 (output_tokens에 그대로 쓴다. eval_count를 다시 읽지 않는다).
    plan은 같은 요청에 대한 가짜 서버 모델의 계획 (보정용 예측값).
    """
    prefill = seconds(final.get("prompt_eval_duration"))
    load = seconds(final.get("load_duration"))
    ttft = timing.first_chunk - timing.arrived              # 첫 토큰까지 (Ollama 안의 대기 포함)
    elapsed = timing.finished - timing.arrived
    # 추정값: Ollama는 안에서 기다린 시간을 알려 주지 않는다. 첫 조각 시각에서 prefill·모델 적재 시간을 빼
    # 처리 시작 시각을 추정하고, 그 전은 대기(queue_wait), 그 뒤 끝까지는 처리(service, 적재 포함)로 본다.
    # 그래서 가짜 서버와 같이 queue_wait + service = server_elapsed가 된다 (docs/API.md 200 응답)
    if prefill is None:
        queue_wait = service = None                         # Ollama가 시간을 주지 않으면 나눌 수 없다
    else:
        queue_wait = max(0.0, ttft - prefill - (load or 0.0))
        service = elapsed - queue_wait
    body = dict(base)
    body.update(
        output="ollama response (text omitted)", input_tokens=inputs,
        output_tokens=generated,
        max_output_tokens=maximum, target_output_tokens=target,
        prompt_eval_count=final.get("prompt_eval_count"), done_reason=final.get("done_reason"),
        queue_wait_seconds=queue_wait, prefill_seconds=prefill,
        decode_seconds=seconds(final.get("eval_duration")), load_seconds=load,
        ttft_seconds=ttft, service_seconds=service, server_elapsed_seconds=elapsed,
        model_prefill_seconds=service_delay + plan.prefill_seconds,
        model_decode_seconds=plan.decode_seconds, token_interval_seconds=plan.token_interval_seconds)
    return body
