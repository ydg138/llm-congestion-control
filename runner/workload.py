"""에이전트가 처리할 작업 목록을 시드로 고정해 만든다.

같은 시드와 같은 agent_id면 항상 같은 작업이 나온다. 컨트롤러 이름은 난수에 들어가지 않으므로
모든 컨트롤러가 똑같은 작업을 받는다. 그래야 결과 차이가 컨트롤러에서 왔다고 말할 수 있다.

출력 길이는 서버가 (서버 seed, workload_id)로 정한다. 여기서 정하는 max_output_tokens는 상한일 뿐이다.
"""

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class ClassConfig:
    """에이전트 종류 하나의 작업 크기 분포."""
    input_min: int              # 입력 토큰 최소
    input_max: int              # 입력 토큰 최대 (이 범위에서 고르게 뽑음)
    max_output_tokens: int      # 출력 상한. 재시도해도 같은 값


# 에이전트 종류(agent_class)별 분포 (docs/interface.md 3절의 agent_class)
AGENT_CLASSES = {
    "light": ClassConfig(input_min=50, input_max=150, max_output_tokens=64),       # 평균 입력 100
    # E4: 입력만 light의 10배 (범위도 같은 비율 ±50%, 평균 1000). 서버 input_capacity는 1500 이상이어야 한다
    # 출력 길이는 서버가 (서버 seed, workload_id)로 정하므로 상한이 같으면 출력 분포도 light와 같다
    "heavy_in": ClassConfig(input_min=500, input_max=1500, max_output_tokens=64),
}


@dataclass(frozen=True)
class Task:
    """작업 하나. task_id는 서버에 workload_id로 보낸다."""
    task_id: str
    input_tokens: int
    max_output_tokens: int

    @property
    def est_tokens(self) -> int:
        """C3가 보낼 때 예약할 토큰. 출력 길이는 끝나야 알므로 상한으로 잡는다."""
        return self.input_tokens + self.max_output_tokens


def make_tasks(agent_id: str, agent_class: str, n: int, seed: int,
               classes: dict[str, ClassConfig] = AGENT_CLASSES) -> list[Task]:
    """agent_id 에이전트가 처리할 작업 n개를 만든다."""
    cfg = classes[agent_class]
    rng = random.Random(f"{seed}:{agent_id}")   # 문자열 시드: 프로세스가 달라도 같은 난수
    tasks = []
    for i in range(n):
        task_id = f"s{seed}-{agent_id}-{i:03d}"  # 시드가 다르면 서버가 주는 출력 길이도 달라짐
        input_tokens = rng.randint(cfg.input_min, cfg.input_max)
        tasks.append(Task(task_id, input_tokens, cfg.max_output_tokens))
    return tasks
