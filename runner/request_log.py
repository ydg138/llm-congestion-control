"""요청 로그 CSV. 요청 한 번(재시도 포함 각 시도)마다 한 줄을 쓴다.

열 순서는 docs/interface.md 3절과 같다. 순서를 정하는 곳은 아래 COLUMNS 한 곳뿐이다.

- 시각(t_send, t_done)은 실험 시작부터 지난 초
- attempt: 0이 첫 시도, 1이 첫 재시도
- status: HTTP 상태 코드. 응답을 못 받았으면 "timeout" 또는 "conn_error"
- output_tokens, ttft_ms: 200일 때만 값이 있고 나머지는 빈 칸
- latency_ms: 클라이언트가 잰 t_done - t_send
- cwnd, in_flight: 컨트롤러가 응답을 처리한 직후의 값
"""

import csv
from pathlib import Path

COLUMNS = [
    "run_id", "exp", "controller", "seed", "agent_id", "agent_class", "task_id", "req_no", "attempt",
    "t_send", "t_done", "status", "input_tokens", "max_output_tokens", "output_tokens", "ttft_ms",
    "latency_ms", "retry_after", "cwnd", "in_flight",
]


def log_path(results_dir: Path, exp: str, controller: str, seed: int, run_id: str) -> Path:
    """파일 이름 규칙: results/<exp>_<controller>_seed<seed>_<run_id>.csv"""
    return results_dir / f"{exp}_{controller}_seed{seed}_{run_id}.csv"


class RequestLog:
    """CSV 파일 하나에 요청 로그를 쓴다. run_id·exp·seed는 실행 전체에 같은 값이라 여기서 채운다."""

    def __init__(self, path: Path, run_id: str, exp: str, seed: int):
        self.common = {"run_id": run_id, "exp": exp, "seed": seed}
        self.file = open(path, "w", encoding="utf-8", newline="")
        self.writer = csv.DictWriter(self.file, fieldnames=COLUMNS)
        self.writer.writeheader()

    def write(self, row: dict) -> None:
        """row에 없는 열은 빈 칸으로, COLUMNS에 없는 키는 오류로 처리한다."""
        self.writer.writerow({**self.common, **row})

    def close(self) -> None:
        self.file.close()
