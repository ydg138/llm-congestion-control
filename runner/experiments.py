"""실험 조건 정의. 실행은 runner/run.py가 한다 (python -m runner.run --exp minimum ...).

조건 하나 = 서버를 한 번 새로 띄워 돌리는 단위. 같은 조건을 시드만 바꿔 SEEDS만큼 반복한다.
같은 시드면 모든 컨트롤러가 같은 작업을 받으므로, 결과 차이는 컨트롤러에서 온 것이라고 말할 수 있다.
"""

from dataclasses import dataclass
from pathlib import Path

from runner.agent import AgentSpec

CONFIGS = Path(__file__).resolve().parent / "configs"
# docs/week12.md 제안: 지연 약 0.8초 × 한도 초당 10건 → 적정 동시성 약 8, 요청 수 한도가 병목
E1_SERVER = CONFIGS / "e1_server.json"

SEEDS = (1, 2, 3, 4, 5)                 # 같은 조건을 시드만 바꿔 5회 반복


@dataclass(frozen=True)
class Condition:
    """실험 조건 하나 (시드만 빼고)."""
    exp: str                            # 실험 이름 (파일 이름의 <exp>)
    label: str                          # 파일 이름의 <controller> 자리
    agents: tuple[AgentSpec, ...]       # (agent_id, agent_class, 컨트롤러, 시작 지연 초)
    tasks: int                          # 에이전트당 작업 수
    server_config: Path


# E1: 에이전트 1개, 한도 고정. 고정 동시성(C0) 네 가지, 지수 백오프(C1), AIMD(C2)
E1_CONTROLLERS = ("C0-1", "C0-8", "C0-32", "C0-64", "C1", "C2")
E1 = [Condition("E1", c, (AgentSpec("a1", "light", c),), 100, E1_SERVER) for c in E1_CONTROLLERS]

# E3: 같은 컨트롤러 에이전트 2개. a2가 10초 늦게 합류해도 AIMD가 공정한 몫으로 수렴하는가 (Chiu & Jain)
E3 = [Condition("E3", "C2", (AgentSpec("a1", "light", "C2", 0.0),
                             AgentSpec("a2", "light", "C2", 10.0)), 200, E1_SERVER)]

EXPERIMENTS = {"E1": E1, "E3": E3, "minimum": E1 + E3}     # minimum: 최소 단계 관문


def select(exp: str, controller: str | None = None,
           seed: int | None = None) -> list[tuple[Condition, int]]:
    """돌릴 (조건, 시드) 목록. 시드를 바깥 반복으로 둔다: 시드 1의 모든 조건 → 시드 2의 모든 조건 → ...

    실행 중에 PC가 바빠져도 한 컨트롤러에만 몰리지 않고 모든 조건에 고르게 퍼진다.
    """
    conds = [c for c in EXPERIMENTS[exp] if controller is None or c.label == controller]
    seeds = SEEDS if seed is None else (seed,)
    return [(c, s) for s in seeds for c in conds]
