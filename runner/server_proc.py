"""가짜 LLM 서버(P1)를 실행마다 새로 띄우고 끈다. Windows 전용 (.venv\\Scripts\\python.exe, taskkill).

서버를 새로 띄우면 버킷·통계가 초기화되고 새 run_id가 생긴다 (docs/interface.md 1절).
"""

import json
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen


@dataclass(frozen=True)
class ServerTimes:
    ready_seconds: float = 15.0     # 서버가 /health에 답할 때까지 기다리는 최대 시간
    flush_seconds: float = 0.3      # 끄기 전 대기: 서버가 0.1초마다 쓰는 CSV 표본이 2~3번 더 기록되게
    stop_seconds: float = 5.0       # 끈 뒤 프로세스가 끝나고 포트가 닫힐 때까지 기다리는 최대 시간
    http_seconds: float = 3.0       # /health·/stats 요청 하나의 최대 시간
    poll_seconds: float = 0.1       # 준비·종료를 다시 확인하는 간격


def free_port() -> int:
    """포트 0으로 열면 OS가 빈 포트를 준다. 바로 닫고 그 번호를 서버에 넘긴다."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_open(port: int) -> bool:
    """누군가 이 포트에서 연결을 받고 있으면 True."""
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_server(server_dir: Path, config: Path | None, log_dir: Path, port: int,
                 out_path: Path) -> subprocess.Popen:
    """서버를 띄운다. 서버는 cwd=server_dir로 돌므로 경로는 모두 절대경로로 넘긴다."""
    python = server_dir / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        raise FileNotFoundError(f"서버 venv가 없음: {python} (서버의 docs/SERVER_README.md 참고)")
    cmd = [str(python), "-m", "server.mock_llm", "--port", str(port),
           "--log-dir", str(log_dir.resolve())]
    if config is not None:
        cmd += ["--config", str(config.resolve())]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as out:      # 서버 콘솔 출력 (디버깅용)
        return subprocess.Popen(cmd, cwd=server_dir, stdout=out, stderr=subprocess.STDOUT)


def get_json(base_url: str, path: str, times: ServerTimes = ServerTimes()) -> dict:
    with urlopen(base_url + path, timeout=times.http_seconds) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_ready(base_url: str, proc: subprocess.Popen, out_path: Path,
               times: ServerTimes = ServerTimes()) -> str:
    """/health가 답할 때까지 기다리고 run_id를 돌려준다."""
    deadline = time.monotonic() + times.ready_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"서버가 시작하자마자 꺼짐 (종료 코드 {proc.returncode}). 출력: {out_path}")
        try:
            return get_json(base_url, "/health", times)["run_id"]
        except OSError:                                     # 아직 연결을 받지 않음
            time.sleep(times.poll_seconds)
    raise RuntimeError(f"서버가 {times.ready_seconds:g}초 안에 준비되지 않음. 출력: {out_path}")


def stop_server(proc: subprocess.Popen, port: int, times: ServerTimes = ServerTimes()) -> None:
    """서버 프로세스 트리를 끄고 포트가 닫혔는지 확인한다."""
    time.sleep(times.flush_seconds)                         # 서버 CSV의 마지막 표본이 기록되게
    if proc.poll() is None:                                 # 이미 끝난 PID에는 쓰지 않는다 (PID 재사용 위험)
        # 런처(.venv의 python.exe)와 자식 인터프리터를 함께 끈다 (/T: 프로세스 트리, /F: 강제)
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        try:
            proc.wait(times.stop_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()                                     # taskkill이 실패했을 때의 마지막 수단
            proc.wait(times.stop_seconds)
    deadline = time.monotonic() + times.stop_seconds
    while port_open(port):
        if time.monotonic() > deadline:
            raise RuntimeError(f"서버를 껐는데 포트 {port}가 아직 열려 있음. "
                               f"'netstat -ano | findstr :{port}'로 PID를 찾아 끌 것")
        time.sleep(times.poll_seconds)
