# P1 LLM 혼잡 제어 실험 서버 — 김준수

Python + aiohttp로 구현한 **실험용 시뮬레이터**입니다. 실제 LLM 텍스트를 생성하지 않고 요청·입력·출력 한도, 대기열, 출력 길이와 생성 지연을 재현합니다. API **2.0**.

## 설치와 시작

Python **3.11 이상** 필요. Windows Python 3.11에서 검증했습니다. 소스 전달 시 `.venv`는 제외하고 각 PC에서 설치합니다.

Windows PowerShell, 팀 저장소의 루트 폴더:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-p1-lock.txt
.\.venv\Scripts\python.exe -m server.mock_llm
```

macOS/Linux(명령 제공, 해당 OS 실측 검증은 하지 않음):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-p1-lock.txt
.venv/bin/python -m server.mock_llm
```

설치 후 `start_server.cmd`를 더블클릭할 수도 있습니다. 창을 유지하고 종료는 Ctrl+C. 기본 화면은 http://127.0.0.1:8000/ 입니다. `/health`, `/stats`, `/config`에서 상태·통계·설정을 확인합니다. `/generate`는 POST 전용입니다.

## 구현된 기능

- 요청 1개 + 입력 토큰 실제량 + 최대 출력량을 **함께 검사하고 전부 가능할 때만 예약**.
- 부족하면 429와 원인·Retry-After. 한 요청이 버킷 최대 용량보다 크면 422(그 설정에서는 기다려도 불가능).
- 최대 출력량 예약 후 완료 시 미사용량 정산. 취소 시 생성 전이면 전액, 생성 중이면 남은 출력 예약량 반환. 요청·입력 차감은 유지.
- 작업 ID + seed에 따른 결정적인 출력 길이. 같은 작업 재시도와 컨트롤러 간 비교에 동일 길이 사용.
- 입력 길이에 따른 prefill, 출력 길이에 따른 decode, FIFO 큐와 처리 슬롯, 큐 초과 529.
- 서버 내부 TTFT·대기·처리 시간 제공. 응답은 완료 후 JSON 1개이며 스트리밍은 아님.
- 인증된 설정 변경 API와 시간 기반 시나리오. 설정 버전·변경 내역 기록.
- 동기·비동기 클라이언트, Ollama/모의 서버 측정 CSV, 비교 JSON 도구.

## 설정과 시나리오

```powershell
.\.venv\Scripts\python.exe -m server.mock_llm --config examples/config.json --scenario examples/scenario.json
```

예시는 시작 후 5초에 요청 한도를 축소, 10초 복구, 15초 처리 슬롯 축소, 20초 복구합니다. 시나리오는 서버 시작 시각 기준입니다. 실험 실행기도 시작 시점을 맞추고 `/config`의 실제 변경 시각을 기록하세요.

모든 설정은 CLI로 덮어쓸 수 있습니다(예: `--input-capacity 10000 --input-refill-rate 5000 --output-capacity 2000 --output-refill-rate 1000`). 충전 속도는 **초당**입니다. 분당 값 R을 모사하려면 R/60을 사용하거나 실험 시간 축소 배율을 명시하세요. 버킷 용량은 별도의 순간 허용량입니다.

HTTP로 실험 도중 변경하려면 서버를 실행하기 전에 환경변수를 설정합니다.

```powershell
$env:MOCK_LLM_ADMIN_TOKEN = 'choose-your-experiment-key'
.\.venv\Scripts\python.exe -m server.mock_llm --config examples/config.json
```

별도 터미널의 예시:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/admin/config -Headers @{Authorization='Bearer choose-your-experiment-key'} -ContentType 'application/json' -Body '{"refill_rate":5,"slots":2}'
```

환경변수가 없으면 변경 API는 비활성(403)입니다. 시나리오 파일은 이 토큰 없이 시작 설정으로 적용됩니다. 단일 실험에서 수동 변경과 시나리오를 혼합하면 적용 직전 상태에 따라 충돌할 수 있어 `/config`의 상태·변경 내역을 확인하세요.

## 팀원 연동

```python
import asyncio
from aiohttp import ClientSession
from examples.async_client import generate

async def main():
    async with ClientSession() as session:
        result = await generate(session, 'http://127.0.0.1:8000', 'agent-a', 'attempt-1',
                                input_tokens=100, workload_id='job-1', max_output_tokens=64)
        print(result)

asyncio.run(main())
```

`request_id`는 시도 식별자, `workload_id`는 재시도·컨트롤러가 바뀌어도 같은 작업 식별자입니다. workload_id를 생략하면 request_id를 사용합니다. 자동 재시도나 혼잡 윈도 제어는 예제에 없으며 P2가 결정합니다. **422는 입력/설정을 바꿔야 하고, 429는 한도 부족, 529는 처리 과부하**입니다. 같은 작업의 max_output_tokens도 같게 유지해야 출력량 비교가 공정합니다.

```powershell
.\.venv\Scripts\python.exe examples/client.py --workload-id job-1 --max-output-tokens 64
```

같은 PC는 127.0.0.1 사용. 다른 PC에서 접속하려면 서버에 `--host 0.0.0.0`을 지정하고 클라이언트는 서버의 실제 LAN IP로 연결합니다(예: http://192.168.0.10:8000). 방화벽·네트워크 허용이 필요합니다. 한 실험은 한 서버 프로세스를 공유하세요. 여러 서버는 독립 상태입니다. 이 실험 서버는 공개 서비스용 TLS/사용자 인증/CORS를 제공하지 않습니다.

## 측정·검증·전달

서버를 실행하면 현재 폴더의 `results/server/`에 실행별 CSV 두 개가 생깁니다. 저장 위치는 `--log-dir 경로`로 지정할 수 있습니다.

- `server_state_<run_id>.csv`에는 시작 시점부터 약 0.1초마다 대기 요청 수, 사용 중인 슬롯 수, 요청·입력·출력 버킷 잔량이 기록됩니다. 그래프의 가로축에는 실제 경과 시간인 `elapsed_seconds`를 사용하세요.
- `agent_stats_<run_id>.csv`에는 에이전트별 완료·429 누계가 바뀐 표본이 기록됩니다. E3에서 최종 건수를 구할 때는 각 `agent_id`의 **마지막 행**을 읽으세요. 실행 중에는 `GET /stats`의 `agent_stats`에서도 확인할 수 있습니다.

다음 실험을 시작할 때는 현재 서버를 **Ctrl+C**로 종료한 뒤 같은 설정으로 다시 실행하세요. 새 서버에서는 버킷이 가득 차고 통계가 0에서 시작합니다. 준영의 요청 로그에는 응답 헤더의 `X-Run-Id`를 기록하면 해당 실행의 서버 CSV를 찾을 수 있습니다.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts/smoke_test.py
.\.venv\Scripts\python.exe scripts/probe_ollama.py --backend mock --concurrency 1 2 4 --repetitions 5 --output results/mock.csv
```

측정 도구는 고정 동시 요청 묶음으로 측정하며 P3의 전체 실험 실행기를 대신하지 않습니다. 기본 서버 한도가 낮으면 429가 나오는 것이 정상입니다. 측정 조건은 `*.csv.meta.json`, 요청별 서버 실행 ID와 설정 버전은 CSV에 저장합니다.

실제 로컬 모델 측정 방법은 [로컬 검증 안내](LOCAL_VALIDATION.md), API 상세는 [API 계약](API.md), 점검 범위는 [검증 기록](REVIEW.md)에 있습니다.

## 남은 실제 작업

서버 기능과 측정 도구 구현은 실제 팀 실험·모델 보정 완료를 의미하지 않습니다. P2 컨트롤러/P3 실행기 연결, 실제 Ollama 모델 측정, 서버 계수 조정과 한계 분석이 필요합니다. 현재 계수는 예시이며 현실의 특정 제공자를 그대로 재현한다고 보장하지 않습니다. LLM 생성은 모의이고 전체 텍스트는 항상 `mock response`입니다. 기록한 TTFT는 실제 스트림 수신 시간이 아닙니다.

## 파일 구성

- `server/rate_limit.py`: 3종 한도와 출력 예약·정산
- `server/model.py`: 결정적 출력 길이·지연·부분 생성량
- `server/config.py`: 설정과 시나리오 검증
- `server/mock_llm.py`: HTTP·큐·취소·동적 변경
- `examples/`: 클라이언트·설정·시나리오
- `scripts/`: 프로세스 검증·측정·비교
- `tests/`: 계산·HTTP·취소·설정 변경·측정 도구 검증
