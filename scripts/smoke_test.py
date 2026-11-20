"""독립 프로세스 HTTP 연동 검증. 실행 위치와 무관하게 프로젝트를 찾습니다.

실행: .venv의 Python으로 이 파일 실행. 기존 8000번 서버는 건드리지 않습니다.
"""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]


def exchange(base_url, path, method="GET", payload=None, raw=None):
    data = raw if raw is not None else (
        json.dumps(payload).encode("utf-8") if payload is not None else None)
    request = Request(base_url + path, data=data, method=method,
                      headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        response = urlopen(request, timeout=3)
    except HTTPError as exc:
        response = exc
    with response:
        text = response.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            body = text
        return response.status, response.headers, body


def start_server_process(args, log):
    options = {}
    if os.name == "nt" and sys.prefix != sys.base_prefix:
        # Windows venv's python.exe launches a child. Own the interpreter directly
        # so wait() also means its port and CSV handles have been released.
        options = {"executable": sys._base_executable,
                   "env": {**os.environ, "__PYVENV_LAUNCHER__": sys.executable}}
    return subprocess.Popen([sys.executable, "-m", "server.mock_llm", *args],
                            cwd=ROOT, stdout=log, stderr=log, **options)


def start_server(port, log, log_dir):
    process = start_server_process(["--host", "127.0.0.1",
         "--port", str(port), "--log-dir", log_dir, "--capacity", "2", "--refill-rate", "1",
         "--service-delay", "0", "--base-ttft", "0", "--input-token-seconds", "0",
         "--output-token-seconds", "0"], log)
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"서버 조기 종료: {process.returncode}")
            try:
                status, _, body = exchange(base_url, "/health")
                if status == 200:
                    return process, base_url, body["run_id"]
            except (URLError, TimeoutError, OSError):
                pass
            time.sleep(0.1)
        raise RuntimeError("서버 시작 시간 초과")
    except BaseException:
        stop_server(process)
        raise


def stop_server(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def require(condition, description):
    if not condition:
        raise AssertionError(description)
    print("PASS:", description)


def main() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = None
    succeeded = False
    with tempfile.TemporaryDirectory() as log_dir, tempfile.TemporaryFile(mode="w+b") as log:
        try:
            process, base_url, run_id = start_server(port, log, log_dir)
            require(exchange(base_url, "/")[0] == 200, "기본 안내 화면 200")
            def send(agent, number):
                return exchange(base_url, "/generate", "POST", {
                    "request_id": str(number), "agent_id": agent, "input_tokens": 100})

            status, headers, generated = send("agent-a", 1)
            require(status == 200, "첫 에이전트 성공 200")
            require(headers["X-API-Version"] == "2.0", "API 2.0")
            require(16 <= generated["output_tokens"] <= 64, "작업별 실제 모의 출력량")
            require("ttft_seconds" in generated, "TTFT 및 처리 시간 신호")
            require(send("agent-b", 2)[0] == 200, "다른 에이전트 성공 200")
            status, headers, body = send("agent-c", 3)
            require(status == 429, "세 에이전트가 하나의 한도를 공유해 429")
            require(int(headers["Retry-After"]) >= 1, "Retry-After 정수 초 제공")
            require(body["error"] == "rate_limit_exceeded", "429 JSON 오류 계약")
            require(body["request_id"] == "3", "오류에서도 요청 식별자 유지")
            time.sleep(int(headers["Retry-After"]) + 0.05)
            require(send("agent-a", 4)[0] == 200, "실제 시간 경과 후 회복")
            require(exchange(base_url, "/generate", "POST", {})[0] == 400,
                    "잘못된 입력 400")
            require(exchange(base_url, "/generate")[0] == 405, "잘못된 HTTP 메서드 405")
            require(exchange(base_url, "/generate", "POST", raw=b"x" * 70000)[0] == 413,
                    "64 KiB 초과 본문 413")
            _, _, stats = exchange(base_url, "/stats")
            require(stats["accepted"] == 3 and stats["rate_limited"] == 1,
                    "정상·한도 거절 통계 일치")
            require(stats["output_tokens_reserved"] == 0 and stats["output_tokens_consumed"] > 0,
                    "출력 토큰 예약 정산 완료")
            require(exchange(base_url, "/generate", "POST", {"request_id": "large",
                    "agent_id": "a", "input_tokens": 10001})[0] == 422,
                    "버킷 최대 용량 초과는 422")

            stop_server(process)
            process = None
            process, base_url, next_run_id = start_server(port, log, log_dir)
            require(next_run_id != run_id, "재시작 시 새 실행 ID")
            _, _, stats = exchange(base_url, "/stats")
            require(stats["accepted"] == 0 and stats["rate_limited"] == 0,
                    "재시작 시 통계 초기화")
            require(send("agent-a", 5)[0] == 200 and send("agent-b", 6)[0] == 200,
                    "재시작 시 버킷 가득 충전")
            print("독립 프로세스 HTTP 연동 검증 완료")
            succeeded = True
            return 0
        except Exception as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1
        finally:
            if process is not None:
                stop_server(process)
            log.seek(0)
            output = log.read().decode("utf-8", errors="replace")
            if not succeeded:
                print(output, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
