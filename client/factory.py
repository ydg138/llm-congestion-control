"""이름("C2", "C0-8" 등)으로 컨트롤러를 만든다. 실행기는 이 함수만 부르면 된다.

예:
    make_controller("C0-32")              # 고정 동시성 32
    make_controller("C1", seed=3)         # 지터 시드 3
    make_controller("C2", ssthresh=16)    # 매개변수 덮어쓰기
"""

from client.controllers import (
    AIMDConfig, AIMDWindow, BackoffConfig, FixedBackoff, FixedConfig, FixedWindow,
)

# E1에서 이름만 바꿔 돌릴 수 있는 목록
NAMES = ("C0-1", "C0-8", "C0-32", "C0-64", "C1", "C2")


def make_controller(name: str, seed: int = 0, **params):
    """name의 '-' 뒤 숫자는 window로 쓴다 (C0·C1만). seed는 무작위를 쓰는 C1에만 넘긴다."""
    kind, _, size = name.partition("-")
    if size:
        if kind not in ("C0", "C1"):
            raise ValueError(f"{name}: '-숫자'는 C0·C1에서만 쓸 수 있다")
        params.setdefault("window", int(size))
    if kind == "C0":
        return FixedWindow(FixedConfig(**params))
    if kind == "C1":
        params.setdefault("seed", seed)
        return FixedBackoff(BackoffConfig(**params))
    if kind == "C2":
        return AIMDWindow(AIMDConfig(**params))
    raise ValueError(f"알 수 없는 컨트롤러 이름: {name}")
