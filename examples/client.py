"""P2/P3 연동 예시: 한 번 요청하고 HTTP 상태·헤더·본문을 반환합니다.

자동 재시도나 동시성 제어는 하지 않습니다. 해당 정책은 P2 컨트롤러의 책임입니다.
Python 표준 라이브러리만 사용하며 다른 폴더에서도 직접 실행할 수 있습니다.
"""

import argparse
import json
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def generate(base_url: str, agent_id: str, request_id: str,
             input_tokens: int = 100, timeout: float = 10.0,
             *, workload_id: str | None = None, max_output_tokens: int | None = None) -> dict:
    """HTTP 오류도 결과로 반환. 연결 오류/타임아웃은 호출자에게 전달합니다."""
    payload = {"request_id": request_id, "agent_id": agent_id,
               "input_tokens": input_tokens}
    if workload_id is not None:
        payload["workload_id"] = workload_id
    if max_output_tokens is not None:
        payload["max_output_tokens"] = max_output_tokens
    request = Request(base_url.rstrip("/") + "/generate",
                      data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
    started = time.perf_counter()
    try:
        response = urlopen(request, timeout=timeout)
    except HTTPError as exc:
        # urllib는 429/529를 예외로 전달하므로 응답 객체로 받아 상태를 보존합니다.
        response = exc
    with response:
        raw = response.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = raw
        return {"status": response.status, "headers": dict(response.headers),
                "body": body, "elapsed_seconds": time.perf_counter() - started}


def main() -> int:
    parser = argparse.ArgumentParser(description="가짜 LLM 서버에 요청 1회 전송")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--agent-id", default="example-agent")
    parser.add_argument("--request-id", default=None)
    parser.add_argument("--input-tokens", type=int, default=100)
    parser.add_argument("--workload-id")
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("timeout은 0보다 커야 합니다")
    try:
        result = generate(args.base_url, args.agent_id,
                          args.request_id or str(uuid.uuid4()),
                          args.input_tokens, args.timeout, workload_id=args.workload_id,
                          max_output_tokens=args.max_output_tokens)
    except (URLError, TimeoutError, OSError) as exc:
        print(json.dumps({"error": "connection_failed", "detail": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # 429/529도 정상적으로 수신한 실험 결과입니다.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
