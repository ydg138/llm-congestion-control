"""P2 비동기 컨트롤러용 1회 호출. 재시도와 윈도 조절은 호출자가 결정합니다."""

import argparse
import asyncio
import json
import time
import uuid

import aiohttp


async def generate(session: aiohttp.ClientSession, base_url: str, agent_id: str,
                   request_id: str, input_tokens: int = 100, timeout: float = 10.0,
                   *, workload_id: str | None = None, max_output_tokens: int | None = None) -> dict:
    payload = {"request_id": request_id, "agent_id": agent_id, "input_tokens": input_tokens}
    if workload_id is not None:
        payload["workload_id"] = workload_id
    if max_output_tokens is not None:
        payload["max_output_tokens"] = max_output_tokens
    started = time.perf_counter()
    async with session.post(base_url.rstrip("/") + "/generate",
                            json=payload,
                            timeout=aiohttp.ClientTimeout(total=timeout),
                            raise_for_status=False) as response:
        # 429/529도 상태·헤더를 그대로 반환하여 컨트롤러가 정책을 결정하게 함.
        body = await response.json()
        return {"status": response.status, "headers": dict(response.headers),
                "body": body, "elapsed_seconds": time.perf_counter() - started}


async def main(base_url):
    async with aiohttp.ClientSession() as session:
        result = await generate(session, base_url, "async-example", str(uuid.uuid4()))
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="P2 비동기 HTTP 연동 예시")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    asyncio.run(main(parser.parse_args().base_url))
