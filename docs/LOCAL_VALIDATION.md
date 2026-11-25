# 로컬 LLM 측정과 모의 서버 비교

이 도구는 P3가 로컬 모델을 준비한 뒤 P1 서버 모델을 비교·보정할 수 있도록 합니다. 실제 모델은 이 작업에서 설치하거나 실행하지 않았으며, 아래 명령을 직접 실행해야 실제 측정 결과가 생성됩니다. 모델 자동 보정은 수행하지 않습니다.

## 1. 실제 Ollama 서버 측정

Ollama를 별도로 실행하고 이미 설치된 모델 이름을 지정하세요. 실제 `/api/generate` 호출은 `--backend ollama`와 명시적인 `--model`을 사용할 때만 실행됩니다. 유료 API 키는 사용하지 않습니다.

```powershell
.\.venv\Scripts\python.exe scripts/probe_ollama.py --backend ollama --model YOUR_INSTALLED_MODEL --concurrency 1 2 4 --repetitions 5 --max-output-tokens 64 --timeout 120 --output results/ollama.csv
```

기본 주소는 `http://127.0.0.1:11434`입니다. `--prompt-file prompts.txt`로 UTF-8 프롬프트를 지정하거나 `--prompt "..."`를 사용할 수 있습니다. 프롬프트와 생성된 답변은 CSV에 저장하지 않습니다. 메타데이터에는 프롬프트의 SHA-256만 기록합니다.

한 반복은 지정한 동시성 수만큼 한꺼번에 요청하는 wave입니다. 기본값 1·2·4, 반복 5회이면 총 35회 요청입니다. 지속 부하나 혼잡 제어 정책을 구현하는 실행기가 아닙니다. 자동 재시도는 하지 않으며 429/529 및 연결 실패도 결과에 기록합니다. `--pause 1`로 wave 사이 1초 휴식을 줄 수 있습니다.

자동 warmup은 하지 않습니다. 첫 호출의 모델 로딩 지연, 서버 캐시, GPU·모델 버전과 설정을 별도로 기록하고 동일한 상태끼리 비교하세요. 동일 seed를 보내더라도 실제 모델의 출력이 완전히 결정적이라고 보장하지 않습니다.

## 2. 모의 서버 측정

모의 서버를 실행한 뒤 같은 동시성·반복 조건으로 호출합니다.

```powershell
.\.venv\Scripts\python.exe scripts/probe_ollama.py --backend mock --concurrency 1 2 4 --repetitions 5 --input-tokens 100 --max-output-tokens 64 --output results/mock.csv
```

기본 주소는 `http://127.0.0.1:8000`입니다. `--base-url`로 변경할 수 있습니다. 실제 프롬프트의 토큰 수를 모의 서버에 자동 적용하지 않습니다. 실제 CSV의 `input_tokens`를 확인해 `--input-tokens`와 출력 길이 모델을 맞추세요. 각 작업에는 `probe-반복-작업번호`의 workload_id를 보내므로 서버의 seed가 같으면 같은 작업의 길이를 비교할 수 있습니다.

모의 서버의 요청·입력·출력 토큰 한도를 낮게 설정하면 429가 결과에 기록됩니다. 지연 곡선 비교 시에는 한도 영향을 배제할지, 한도까지 포함할지 실험 목적에 맞게 설정하고 기록해야 합니다.

## 3. 비교 파일 생성

```powershell
.\.venv\Scripts\python.exe scripts/compare_measurements.py --real results/ollama.csv --mock results/mock.csv --output results/comparison.json --min-samples 5
```

동시성별 성공·실패 수, 성공 요청의 평균·p95와 평균의 상대 차이 `(mock-real)/real`를 계산합니다. 성공 표본이 min-samples보다 적거나 기준 평균이 0이면 상대 차이는 null입니다. 5개 표본은 도구의 최소 표시 기준일 뿐 통계적 신뢰도를 보장하지 않습니다. 결과가 안정적인지 반복 수를 늘려 확인하세요.

실제와 모의 서버의 출력 길이가 다르면 처리 시간이 다른 것이 자연스러우므로 input/output_tokens 평균도 함께 확인하세요. 조건이 다른 측정값 사이의 상대 차이는 모델 정확도의 증거가 아닙니다. unmatched_concurrency는 한쪽에만 있는 동시성 조건입니다.

## 시간의 의미

| CSV 필드 | Ollama | 모의 서버 |
|---|---|---|
| ttft_seconds | 클라이언트에서 첫 비어 있지 않은 response 스트림 조각을 받은 시간 | 완료 응답에 실린 서버 내부 queue+prefill 측정값 |
| ttft_kind | client_first_response_chunk | mock_server_ttft |
| total_seconds | 클라이언트가 호출을 시작해 마지막 응답까지 받은 시간 | 클라이언트가 전체 JSON 응답을 받은 시간 |
| server_prefill_seconds | prompt_eval_duration / 1e9 | prefill_seconds |
| server_decode_seconds | eval_duration / 1e9 | decode_seconds |

TTFT는 측정 위치와 전송 방식이 다르므로 비교 도구가 두 kind의 상대 오차 계산을 생략합니다. 실제 첫 스트림 조각이 항상 정확히 토큰 1개인 것도 아닙니다. thinking 조각은 답변의 첫 토큰으로 세지 않습니다. 모의 prefill은 처리 시작 이후 고정 오버헤드와 입력 길이별 지연이며 큐 대기는 제외합니다. Ollama prompt_eval_duration과 정확히 같은 구간이라고 가정하지 말고 별도로 해석하세요.

Ollama 응답 스키마와 시간 단위 근거: [Ollama Generate API 공식 문서](https://docs.ollama.com/api/generate). `prompt_eval_count`, `eval_count`는 토큰 수이며 `prompt_eval_duration`, `eval_duration`은 나노초입니다.

## 결과물

- 요청별 CSV: 측정값, 상태, 고정된 오류 분류. 응답 본문·프롬프트 저장 없음.
- `*.csv.meta.json`: 모델 이름·프롬프트 해시·반복·동시성·타임아웃 및 모의 서버의 측정 전후 설정/변경 이력. 실제 Ollama seed는 도구에서 42로 고정.
- 비교 JSON: 동시성별 요약과 비교 불가능/표본 부족 표시.

이 도구만으로 E6 완료를 주장할 수 없습니다. 실제 모델 측정 후 서버 지연 계수와 출력 길이 범위를 조정하고, 개선 전후와 남은 차이를 P4 보고서에 기록해야 합니다.
