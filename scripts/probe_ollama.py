"""Ollama 또는 모의 서버를 지정된 동시성으로 측정해 CSV로 저장합니다."""

import argparse
import asyncio
import csv
import hashlib
import json
import math
from pathlib import Path
import time
import uuid

import aiohttp

try:
    from scripts.measurements import FIELDS, numeric
except ModuleNotFoundError:
    from measurements import FIELDS, numeric

MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def nanoseconds(value):
    number = numeric(value)
    return "" if number is None else number / 1_000_000_000


async def probe_one(session, *, backend, base_url, model, prompt, concurrency,
                    repetition, request_id, workload_id, timeout=120,
                    input_tokens=100, max_output_tokens=64):
    row = dict.fromkeys(FIELDS, "")
    row.update(backend=backend, concurrency=concurrency, repetition=repetition,
               request_id=request_id, workload_id=workload_id, status=0)
    if backend == "ollama":
        path = "/api/generate"
        payload = {"model": model, "prompt": prompt, "stream": True,
                   "options": {"num_predict": max_output_tokens, "seed": 42}}
        row["ttft_kind"] = "client_first_response_chunk"
    else:
        path = "/generate"
        payload = {"request_id": request_id, "agent_id": f"probe-{request_id}",
                   "workload_id": workload_id, "input_tokens": input_tokens,
                   "max_output_tokens": max_output_tokens}
        row["ttft_kind"] = "mock_server_ttft"
    started = time.perf_counter()
    try:
        async with session.post(base_url.rstrip("/") + path, json=payload,
                                timeout=aiohttp.ClientTimeout(total=timeout),
                                raise_for_status=False) as response:
            row["status"] = response.status
            if response.status != 200:
                row["error"] = f"http_{response.status}"
                return row
            if backend == "mock":
                chunks = []
                size = 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise ValueError("response too large")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError("expected object")
                row.update(input_tokens=body.get("input_tokens", ""),
                           server_run_id=body.get("run_id", ""),
                           config_revision=body.get("config_revision", ""),
                           output_tokens=body.get("output_tokens", ""),
                           ttft_seconds=body.get("ttft_seconds", ""),
                           server_prefill_seconds=body.get("prefill_seconds", body.get("model_prefill_seconds", "")),
                           server_decode_seconds=body.get("decode_seconds", body.get("model_decode_seconds", "")))
                if row["ttft_seconds"] == "":
                    row["ttft_kind"] = "unavailable"
            else:
                size = 0
                done = False
                # readline handles NDJSON split across arbitrary TCP chunks.
                while True:
                    line = await response.content.readline()
                    if not line:
                        break
                    size += len(line)
                    if size > MAX_RESPONSE_BYTES:
                        raise ValueError("response too large")
                    if not line.strip():
                        continue
                    item = json.loads(line)
                    if not isinstance(item, dict):
                        raise ValueError("expected object")
                    if item.get("error"):
                        row["error"] = "backend_error"
                        break
                    # Thinking chunks are not visible answer tokens; definition is recorded.
                    if isinstance(item.get("response"), str) and item["response"] and row["ttft_seconds"] == "":
                        row["ttft_seconds"] = time.perf_counter() - started
                    if item.get("done") is True:
                        done = True
                        row.update(input_tokens=item.get("prompt_eval_count", ""),
                                   output_tokens=item.get("eval_count", ""),
                                   server_prefill_seconds=nanoseconds(item.get("prompt_eval_duration")),
                                   server_decode_seconds=nanoseconds(item.get("eval_duration")))
                        break
                if not done and not row["error"]:
                    row["error"] = "incomplete_stream"
    except asyncio.TimeoutError:
        row["error"] = "timeout"
    except (ValueError, UnicodeError, RecursionError):
        row["error"] = "invalid_or_oversized_response"
    except (aiohttp.ClientError, OSError):
        row["error"] = "connection_error"
    finally:
        row["total_seconds"] = time.perf_counter() - started
    return row


async def run(args):
    prompt = Path(args.prompt_file).read_text(encoding="utf-8") if args.prompt_file else args.prompt
    if len(prompt.encode("utf-8")) > 1024 * 1024:
        raise ValueError("prompt must be <= 1 MiB")
    base_url = args.base_url or ("http://127.0.0.1:11434" if args.backend == "ollama" else "http://127.0.0.1:8000")
    run_id = str(uuid.uuid4())
    rows = []
    server_config = None
    server_config_after = None
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=max(args.concurrency))) as session:
        if args.backend == "mock":
            async with session.get(base_url.rstrip("/") + "/config",
                                   timeout=aiohttp.ClientTimeout(total=10)) as response:
                response.raise_for_status()
                server_config = await response.json()
        for concurrency in args.concurrency:
            for repetition in range(args.repetitions):
                # Fixed waves; this is a measurement tool, not a congestion controller.
                batch = [probe_one(session, backend=args.backend, base_url=base_url,
                                   model=args.model, prompt=prompt, concurrency=concurrency,
                                   repetition=repetition,
                                   request_id=f"{run_id}-{concurrency}-{repetition}-{i}",
                                   workload_id=f"probe-{repetition}-{i}", timeout=args.timeout,
                                   input_tokens=args.input_tokens, max_output_tokens=args.max_output_tokens)
                         for i in range(concurrency)]
                rows.extend(await asyncio.gather(*batch))
                if args.pause:
                    await asyncio.sleep(args.pause)
        if args.backend == "mock":
            async with session.get(base_url.rstrip("/") + "/config",
                                   timeout=aiohttp.ClientTimeout(total=10)) as response:
                response.raise_for_status()
                server_config_after = await response.json()
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    metadata = {"backend": args.backend, "model": args.model, "run_id": run_id,
                "mock_config_at_start": server_config,
                "mock_config_at_end": server_config_after,
                "concurrency": args.concurrency, "repetitions": args.repetitions,
                "pause_seconds": args.pause, "timeout_seconds": args.timeout,
                "max_output_tokens": args.max_output_tokens, "mock_input_tokens": args.input_tokens,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "warmup": "No automatic warmup; record warm/cold state separately.",
                "ttft_note": "Ollama: first nonempty response chunk at client. Mock: server modeled TTFT reported after completion."}
    destination.with_suffix(destination.suffix + ".meta.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {len(rows)} requests to {destination}")
    failures = sum(bool(row["error"]) for row in rows)
    print(f"Successful: {len(rows) - failures}; failed: {failures}")
    return 1 if failures == len(rows) else 0


def main():
    parser = argparse.ArgumentParser(description="로컬 LLM/모의 서버 측정 CSV 작성")
    parser.add_argument("--backend", choices=["ollama", "mock"], default="ollama")
    parser.add_argument("--base-url")
    parser.add_argument("--model", help="Ollama에서 실행 중인 모델 이름. 자동 설치하지 않음")
    parser.add_argument("--prompt", default="Explain network congestion control in three sentences.")
    parser.add_argument("--prompt-file")
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 2, 4])
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--pause", type=float, default=0)
    parser.add_argument("--input-tokens", type=int, default=100)
    parser.add_argument("--max-output-tokens", type=int, default=64)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.backend == "ollama" and not args.model:
        parser.error("--backend ollama에는 --model이 필요합니다")
    if (not 1 <= args.repetitions <= 10000 or any(not 1 <= n <= 256 for n in args.concurrency)
            or sum(args.concurrency) * args.repetitions > 100000):
        parser.error("동시성 1~256, 반복 1~10000, 총 요청 수 100000 이하를 사용하세요")
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 3600:
        parser.error("timeout은 0 초과 3600 이하 유한수여야 합니다")
    if not math.isfinite(args.pause) or not 0 <= args.pause <= 3600:
        parser.error("pause는 0~3600 유한수여야 합니다")
    if not 0 <= args.input_tokens <= 10**9 or not 1 <= args.max_output_tokens <= 10**6:
        parser.error("input-tokens는 0~1e9, max-output-tokens는 1~1e6이어야 합니다")
    try:
        return asyncio.run(run(args))
    except (ValueError, OSError, aiohttp.ClientError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
