"""실험 조건 정의. 실행은 runner/run.py가 한다 (python -m runner.run --exp minimum ...).

조건 하나 = 서버를 한 번 새로 띄워 돌리는 단위. 같은 조건을 시드만 바꿔 SEEDS만큼 반복한다 (E6는 E6_SEEDS).
같은 시드면 모든 컨트롤러가 같은 작업을 받으므로, 결과 차이는 컨트롤러에서 온 것이라고 말할 수 있다.
"""

from dataclasses import dataclass
from pathlib import Path

from client.factory import NAMES
from runner.agent import AgentSpec

CONFIGS = Path(__file__).resolve().parent / "configs"
# docs/week12.md 제안: 지연 약 0.8초 × 한도 초당 10건 → 적정 동시성 약 8, 요청 수 한도가 병목
E1_SERVER = CONFIGS / "e1_server.json"
# E4 병목 2종 (docs/week13.md "E4 서버 설정"). 지금은 P3 임시값, P1 설정이 오면 이 두 파일을 바꾼다
E4_SERVERS = {
    "input": CONFIGS / "e4_input.json",         # 입력 토큰 한도(초당 5000)만 먼저 참, 요청·출력은 넉넉
    "requests": CONFIGS / "e4_requests.json",   # 요청 수 한도(초당 20건, 용량 10)만 먼저 참, 입력·출력은 넉넉
}

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

# E4: light 1개 + heavy_in 1개(입력 10배)가 같은 컨트롤러로 동시에 시작. 토큰 공정성을 병목 2종에서 비교
# 병목은 exp 이름에 넣는다 (E4-input, E4-requests). 라벨은 컨트롤러 이름 그대로라 --controller C2가 된다
# C3는 client/factory.py에 들어오면(PR #12) 코드를 고치지 않아도 조건이 생긴다
E4_CONTROLLERS = tuple(c for c in ("C2", "C3") if c in NAMES)
# 에이전트당 작업 수. 두 에이전트가 같이 도는 구간이 약 30초가 되게 정했다 (빠른 쪽이 끝나면 구간 끝)
#   requests: 초당 20건을 반씩(요청 공정) → 에이전트마다 초당 10건 → 300개면 약 30초
#   input: 초당 5000토큰을 반씩(토큰 공정) → light 2500/100 = 초당 25건 → 750개면 약 30초
#          그 뒤 heavy_in이 혼자 남은 작업을 하므로 실행 하나는 약 165초 (750 × 1100토큰 ÷ 5000)
E4_TASKS = {"input": 750, "requests": 300}


def light_and_heavy(controller: str) -> tuple[AgentSpec, ...]:
    """E4·E6의 에이전트 묶음: light 1개 + heavy_in 1개가 같은 컨트롤러로 동시에 시작."""
    return (AgentSpec("a1", "light", controller), AgentSpec("a2", "heavy_in", controller))


E4 = [Condition(f"E4-{neck}", c, light_and_heavy(c), E4_TASKS[neck], server)
      for neck, server in E4_SERVERS.items() for c in E4_CONTROLLERS]

# E6: E4 축소판을 실제 Ollama에서 (--backend ollama, 중계 서버 runner/real_adapter.py가 한도를 검사).
# --backend mock(기본)이면 같은 설정으로 가짜 서버가 돈다 (노트북 리허설용. slots·지연 모델은 Ollama에 맞춤)
# 한도 값은 실측 전 추정치다. 주말에 데스크탑에서 seed 1로 보정한다 (docs/e6_runbook.md 5단계)
#   근거(11/20~21 측정, 동시 1): warm TTFT 0.014초, 출력 토큰당 0.0063초 → 요청 하나(출력 평균 40) 약 0.27초
#   병렬 4인 Ollama는 동시 디코드로 느려지는 것까지 쳐서 초당 9건 안팎을 처리한다고 본다.
#   한도가 그보다 충분히 낮아야 429가 나고, Ollama 대기열이 병목이 되지 않는다
E6_SERVERS = {
    # 입력 토큰 초당 1000, 용량 2000만 먼저 참. 용량은 heavy_in 최대 입력 1500 이상이어야 한다 (아니면 422)
    #   Ollama 부하 상한: light만 보내면 1000 ÷ 100 = 초당 10건 이상 (처음에는 용량 2000만큼 더).
    #   토큰을 반씩 나누면 초당 5.5건(light 5 + heavy 0.5)이지만, 가짜 서버 리허설(11/26)에서는 C2·C3 모두
    #   처음 약 10초 동안 light가 입력 버킷을 거의 다 가져갔다. 실제 Ollama에서는 seed 1 보정으로 확인한다
    #   heavy 한 건(입력 약 1000)이 충전 1초 분량이라(E4는 1000 ÷ 5000 = 0.2초), 잔량이 적을 때
    #   큰 요청만 막히는 현상이 E4보다 강하다 (리허설: heavy 429 약 4,800번)
    "input": CONFIGS / "e6_input.json",
    "requests": CONFIGS / "e6_requests.json",     # 요청 초당 4건, 용량 4만 먼저 참
}
E6_MEASURE = CONFIGS / "e6_measure.json"        # 긴 입력 측정용: 모든 한도 넉넉 (입력 8000 한 건 통과). 실험 조건 아님
# 에이전트당 작업 수
#   input: 100 × 100 + 100 × 1000 = 110,000토큰 ÷ 초당 1000 = 실행 하나 약 110초
#          (light는 리허설에서 8~10초에 끝나 같이 도는 구간이 짧다. 나머지는 heavy 혼자)
#   requests: 200건 ÷ 초당 4건 = 약 50초
#   → 시드 하나(4회) 약 6분, 시드 3개 약 20분
E6_TASKS = {"input": 100, "requests": 100}
E6 = [Condition(f"E6-{neck}", c, light_and_heavy(c), E6_TASKS[neck], server)
      for neck, server in E6_SERVERS.items() for c in E4_CONTROLLERS]
E6_SEEDS = (1, 2, 3)                    # 실제 Ollama는 오래 걸려 3회 (SEEDS와 따로)

EXPERIMENTS = {"E1": E1, "E3": E3, "E4": E4, "E6": E6,
               "minimum": E1 + E3}                          # minimum: 최소 단계 관문 (E4·E6는 넣지 않음)
EXP_SEEDS = {"E6": E6_SEEDS}            # 반복 횟수가 SEEDS와 다른 실험


def select(exp: str, controller: str | None = None,
           seed: int | None = None) -> list[tuple[Condition, int]]:
    """돌릴 (조건, 시드) 목록. 시드를 바깥 반복으로 둔다: 시드 1의 모든 조건 → 시드 2의 모든 조건 → ...

    실행 중에 PC가 바빠져도 한 컨트롤러에만 몰리지 않고 모든 조건에 고르게 퍼진다.
    """
    conds = [c for c in EXPERIMENTS[exp] if controller is None or c.label == controller]
    seeds = EXP_SEEDS.get(exp, SEEDS) if seed is None else (seed,)
    return [(c, s) for s in seeds for c in conds]
