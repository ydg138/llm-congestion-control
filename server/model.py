"""작업 ID로 출력 길이를 고정하는 순수 LLM 지연 모델.

실제 텍스트/LLM을 생성하지 않습니다. 같은 seed와 workload_id에는 같은
숨겨진 출력 길이를 부여하므로 컨트롤러의 요청 순서와 재시도에 영향을 받지 않습니다.
시간 단위는 초이며 첫 토큰 이후에 output_tokens - 1개의 간격이 있습니다.
"""

from dataclasses import dataclass
import hashlib
import json
import math


MAX_OUTPUT_TOKENS = 1_000_000
MAX_INPUT_TOKENS = 1_000_000_000
MAX_GENERATION_SECONDS = 3600.0


def _integer(name: str, value: int, lower: int, upper: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise ValueError(f"{name} must be an integer in [{lower}, {upper}]")


@dataclass(frozen=True)
class ModelConfig:
    seed: int = 42
    output_min: int = 16
    output_max: int = 64
    base_ttft: float = 0.01
    input_token_seconds: float = 0.0001
    output_token_seconds: float = 0.001

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _integer("seed", self.seed, 0, 2**32 - 1)
        _integer("output_min", self.output_min, 1, MAX_OUTPUT_TOKENS)
        _integer("output_max", self.output_max, 1, MAX_OUTPUT_TOKENS)
        if self.output_min > self.output_max:
            raise ValueError("output_min must not exceed output_max")
        for name in ("base_ttft", "input_token_seconds", "output_token_seconds"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not 0 <= value <= 60 or not math.isfinite(value)):
                raise ValueError(f"{name} must be finite and in [0, 60] seconds")


@dataclass(frozen=True)
class GenerationPlan:
    output_tokens: int
    prefill_seconds: float
    decode_seconds: float
    token_interval_seconds: float
    total_seconds: float

    def generated_tokens(self, elapsed_processing_seconds: float) -> int:
        """취소 시점까지 생성한 토큰 수. 큐 대기 시간은 넣지 않습니다."""
        elapsed = elapsed_processing_seconds
        if (isinstance(elapsed, bool) or not isinstance(elapsed, (int, float))
                or not math.isfinite(elapsed) or elapsed < 0):
            raise ValueError("elapsed_processing_seconds must be finite and nonnegative")
        if elapsed < self.prefill_seconds:
            return 0
        if self.token_interval_seconds == 0 or elapsed >= self.total_seconds:
            return self.output_tokens
        tokens = min(self.output_tokens, 1 + int(
            (elapsed - self.prefill_seconds) / self.token_interval_seconds))
        # 차감/나눗셈의 부동소수점 오차는 계획에서 사용한 토큰 시각으로 재확인.
        while tokens < self.output_tokens and elapsed >= (
                self.prefill_seconds + tokens * self.token_interval_seconds):
            tokens += 1
        while tokens > 1 and elapsed < (
                self.prefill_seconds + (tokens - 1) * self.token_interval_seconds):
            tokens -= 1
        return tokens


class LLMModel:
    def __init__(self, config: ModelConfig):
        if not isinstance(config, ModelConfig):
            raise ValueError("config must be ModelConfig")
        config.validate()
        self.config = config

    def plan(self, workload_id: str, input_tokens: int,
             max_output_tokens: int) -> GenerationPlan:
        if not isinstance(workload_id, str) or not workload_id.strip() or len(workload_id) > 128:
            raise ValueError("workload_id must be a nonblank string of 1 to 128 characters")
        _integer("input_tokens", input_tokens, 0, MAX_INPUT_TOKENS)
        _integer("max_output_tokens", max_output_tokens, 1, MAX_OUTPUT_TOKENS)
        # ensure_ascii를 사용해 UTF-8 인코딩과 무관하게 ID 전체를 안정적으로 표현.
        key = json.dumps([self.config.seed, workload_id], ensure_ascii=True,
                         separators=(",", ":")).encode("ascii")
        width = self.config.output_max - self.config.output_min + 1
        # 모듈로 편향을 피하는 rejection sampling. 전역 RNG 상태를 사용하지 않음.
        cutoff = (2**256 // width) * width
        counter = 0
        while True:
            sample = int.from_bytes(hashlib.sha256(key + b":" + str(counter).encode()).digest(), "big")
            if sample < cutoff:
                sampled_output = self.config.output_min + sample % width
                break
            counter += 1
        output_tokens = min(sampled_output, max_output_tokens)
        prefill = self.config.base_ttft + input_tokens * self.config.input_token_seconds
        decode = (output_tokens - 1) * self.config.output_token_seconds
        total = prefill + decode
        if total > MAX_GENERATION_SECONDS:
            raise ValueError(f"planned generation must not exceed {MAX_GENERATION_SECONDS:g} seconds")
        return GenerationPlan(output_tokens, prefill, decode,
                              self.config.output_token_seconds, total)
