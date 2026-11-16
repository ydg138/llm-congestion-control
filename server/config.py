"""실험 설정: 시간은 초, 충전 속도는 초당 개수."""

import math
from dataclasses import asdict, dataclass, replace

from .model import ModelConfig
from .rate_limit import TokenBucket


@dataclass(frozen=True)
class ServerConfig:
    capacity: int = 5
    refill_rate: float = 1.0
    input_capacity: int = 10000
    input_refill_rate: float = 1000.0
    output_capacity: int = 1000
    output_refill_rate: float = 100.0
    slots: int = 2
    queue_size: int = 16
    service_delay: float = 0.05
    seed: int = 42
    output_min: int = 16
    output_max: int = 64
    base_ttft: float = 0.01
    input_token_seconds: float = 0.0001
    output_token_seconds: float = 0.001

    def model(self):
        return ModelConfig(**{key: getattr(self, key) for key in
                              ("seed", "output_min", "output_max", "base_ttft",
                               "input_token_seconds", "output_token_seconds")})

    def validate(self):
        for capacity, rate in [(self.capacity, self.refill_rate),
                               (self.input_capacity, self.input_refill_rate),
                               (self.output_capacity, self.output_refill_rate)]:
            TokenBucket(capacity, rate)
        for name, minimum in [("slots", 1), ("queue_size", 0)]:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= 10000:
                raise ValueError(f"{name} must be an integer in [{minimum}, 10000]")
        if (isinstance(self.service_delay, bool) or not isinstance(self.service_delay, (int, float))
                or not 0 <= self.service_delay <= 3600 or not math.isfinite(self.service_delay)):
            raise ValueError("service_delay must be finite, 0..3600 seconds")
        self.model().validate()
        return self

    def changed(self, changes):
        if not isinstance(changes, dict) or not changes:
            raise ValueError("changes must be a nonempty object")
        unknown = set(changes) - set(asdict(self))
        if unknown:
            raise ValueError("unknown configuration fields: " + ", ".join(sorted(unknown)))
        return replace(self, **changes).validate()


def validate_scenario(events, initial):
    if not isinstance(events, list) or len(events) > 1000:
        raise ValueError("scenario must be a list of at most 1000 events")
    previous = -1.0
    config = initial
    for event in events:
        if not isinstance(event, dict) or set(event) != {"at_seconds", "changes"}:
            raise ValueError("each event requires only at_seconds and changes")
        when = event["at_seconds"]
        if (isinstance(when, bool) or not isinstance(when, (int, float))
                or not math.isfinite(when) or not 0 <= when <= 86400 or when < previous):
            raise ValueError("scenario times must be sorted and within 0..86400 seconds")
        config = config.changed(event["changes"])
        previous = when
    return events
