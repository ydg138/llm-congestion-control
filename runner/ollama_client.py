"""Ollama와 통신하는 부분 (중계 서버 runner/real_adapter.py가 쓴다).

/api/generate 응답은 NDJSON 스트림이다: 한 줄에 JSON 조각 하나, 마지막 조각은 done=true와
토큰 수·시간(나노초)을 담는다. 우리 API로 바꾸는 계산은 runner/relay_rules.py에 있다.
"""

import json
import time
from dataclasses import dataclass

import aiohttp
from aiohttp.http_exceptions import HttpProcessingError

from runner.relay_rules import RelayConfig, ollama_payload


@dataclass
class StreamResult:
    """Ollama 요청 하나의 결과. 호출하는 쪽이 만들어 넘기므로 중간에 취소돼도 받은 조각 수가 남는다."""
    status: int | None = None           # Ollama HTTP 상태 (연결 실패면 None)
    first_chunk: float | None = None    # 첫 비어 있지 않은 조각을 받은 시각 (perf_counter)
    finished: float | None = None       # 마지막 조각(done=true)을 받은 시각
    chunks: int = 0                     # 비어 있지 않은 조각 수 (오류·취소 때 생성량 추정에 씀. 실제 토큰 수와 다를 수 있음)
    final: dict | None = None           # 마지막 조각: eval_count, prompt_eval_count, *_duration, done_reason
    error: str = ""                     # 정상이면 빈 칸
    message: str = ""                   # 오류 문구 (앞 200자, 원인 확인용): Ollama가 보낸 문구 또는 이상한 eval_count 값


def make_session(cfg: RelayConfig) -> aiohttp.ClientSession:
    """Ollama용 세션. 연결 수 제한은 두지 않는다 (동시 요청 수는 한도와 Ollama가 정한다).

    read_bufsize의 2배가 NDJSON 한 줄의 한도다. 마지막 조각에 큰 context 배열이 올 수 있어 넉넉히 둔다.
    """
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0), read_bufsize=cfg.read_bufsize)


async def stream_generate(session, cfg: RelayConfig, payload: dict, result: StreamResult,
                          timeout: float | None = None) -> None:
    """POST /api/generate를 스트리밍으로 보내고 result를 채운다.

    error: http_<상태>(503 포함), backend_error(스트림 안의 error), timeout, connection_error,
           invalid_response(JSON이 아니거나 한 줄이 한도보다 김), incomplete_stream(done 없이 끝남)
    invalid_response는 real_adapter.outcome도 쓴다 (마지막 조각의 eval_count가 0~상한 정수가 아님).
    어느 쪽인지는 message로 구분한다: 여기서 정하면 빈 칸, outcome이 정하면 "eval_count=<받은 값>".
    취소(CancelledError)는 잡지 않는다. 그때까지 받은 조각 수는 result에 남는다.
    """
    url = cfg.ollama_url.rstrip("/") + "/api/generate"
    limit = aiohttp.ClientTimeout(total=timeout or cfg.ollama_timeout_seconds)
    try:
        async with session.post(url, json=payload, timeout=limit) as response:
            result.status = response.status
            if response.status != 200:
                result.error = f"http_{response.status}"
                result.message = (await response.text())[:200]
                return
            await read_stream(response, result)
    except TimeoutError:
        result.error = "timeout"
    except (ValueError, HttpProcessingError):       # JSON이 아님, 한 줄이 한도보다 김(LineTooLong)
        result.error = "invalid_response"
    except (aiohttp.ClientError, OSError):
        result.error = "connection_error"


async def read_stream(response, result: StreamResult) -> None:
    """NDJSON을 한 줄씩 읽는다. 조각 하나가 TCP에서 나뉘어 와도 readline이 한 줄로 모아 준다."""
    while line := await response.content.readline():
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, dict):
            raise ValueError("JSON 객체가 아님")
        if item.get("error"):
            result.error = "backend_error"
            result.message = str(item["error"])[:200]
            return
        if item.get("response"):                    # 비어 있지 않은 조각 = 생성된 토큰
            result.chunks += 1
            if result.first_chunk is None:
                result.first_chunk = time.perf_counter()
        if item.get("done") is True:
            result.final = item
            result.finished = time.perf_counter()
            return
    result.error = "incomplete_stream"


async def warmup(session, cfg: RelayConfig) -> StreamResult:
    """실험 전에 같은 num_ctx로 토큰 1개를 생성해 모델을 올려 둔다. 실패하면 RuntimeError.

    num_ctx가 바뀌면 Ollama가 모델을 다시 올린다고 알고 있어(확인 안 됨) 실험과 같은 값을 쓴다.
    """
    result = StreamResult()
    await stream_generate(session, cfg, ollama_payload(cfg, "Say hi.", num_predict=1, seed=0), result,
                          timeout=cfg.warmup_timeout_seconds)
    if result.final is None:
        raise RuntimeError(f"Ollama 워밍업 실패: {result.error} {result.message}".strip())
    return result


async def server_info(session, cfg: RelayConfig) -> dict:
    """GET /api/version, /api/ps의 원본 JSON. 필드 이름을 가정하지 않고 그대로 stats에 남긴다."""
    info = {}
    for name in ("version", "ps"):
        url = cfg.ollama_url.rstrip("/") + "/api/" + name
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=cfg.info_timeout_seconds)) as response:
            response.raise_for_status()
            info[name] = await response.json()
    return info
