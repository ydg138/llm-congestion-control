# E6 주말 실행 순서 (P3)

데스크탑(RTX 5080, Ollama, llama3.1:8b)에서 **긴 입력 측정 → E6 seed 1 보정 → E6 본 실행**을 명령만 차례로 돌려 끝내기 위한 문서다. 모든 명령은 PowerShell, 저장소 폴더에서 실행한다. 창은 세 개를 쓴다.

| 창 | 하는 일 |
| --- | --- |
| A | `ollama serve` (Ollama 서버. 끝날 때까지 켜 둠) |
| B | 측정용 중계 서버 (2~3단계만) |
| C | 나머지 명령 |

예상 시간: 측정 약 5분, seed 1 보정 약 6분, 본 실행 약 20분. Ollama 재시작까지 넣어 약 45분.

## 0. 준비

```powershell
git switch feat/junyeong-real-adapter; git pull
.\.venv\Scripts\python.exe -m pytest -q      # 모두 통과해야 함
ollama list                                  # llama3.1:8b가 있어야 함 (없으면 ollama pull llama3.1:8b)
```

- **트레이의 Ollama를 끈다** (작업 표시줄 아이콘 → Quit Ollama). 로그인할 때 자동으로 뜬 Ollama는 이 창의 `$env:` 설정을 모르고, 포트 11434를 잡고 있어서 창 A의 `ollama serve`가 뜨지 않거나 다른 설정으로 돈다. `Get-Process ollama*`가 아무것도 출력하지 않으면 꺼진 것이다.
- 실행하는 동안 절전 모드와 다른 GPU 작업(게임, 영상 인코딩)은 끈다.

**(노트북에서 미리) 가짜 서버 리허설.** Ollama 없이 E6 명령과 확인 도구가 도는지 본다. 가짜 서버는 relay CSV를 쓰지 않으므로 relay_check는 "중계 서버 CSV 없음"이 정상이다.

```powershell
$PY = ".\.venv\Scripts\python.exe"
& $PY -m runner.run --exp E6 --seed 1 --tasks 10
$R = (Get-ChildItem results -Directory -Filter "E6_*" | Sort-Object Name | Select-Object -Last 1).FullName
& $PY -m runner.e4_check $R
& $PY -m runner.relay_check $R
```

**(선택) 노트북 Ollama 리허설.** 노트북에 Ollama가 있으면 작은 모델(`ollama pull qwen2.5:0.5b`)로 1~5단계를 짧게 돌려 본다. 절차 확인용이라 숫자는 쓰지 않는다. 바꿀 곳은 세 군데다.
- 2·3단계: 창 B·C 모두 `$M = "results\E6-measure_rehearsal"`, 창 B 중계 서버 명령 끝에 `--model qwen2.5:0.5b`
- 5단계 실행 명령: `& $PY -m runner.run --exp E6 --seed 1 --tasks 10 --backend ollama --ollama-model qwen2.5:0.5b`
- (모델 이름 옵션은 중계 서버 `--model`, 실행기 `--ollama-model`. 기본값은 둘 다 llama3.1:8b)

GPU가 없으면 입력 8000 요청이 중계 서버의 Ollama 제한 시간(55초)을 넘어 502가 날 수 있다. 그때는 8000을 빼고 돌린다.

## 1. Ollama 켜기 (측정용: 병렬 1)

창 A:

```powershell
$env:OLLAMA_NUM_PARALLEL = "1"
ollama serve
```

- `$env:`는 이 창에서만 유효하다. **`setx`는 쓰지 않는다.** setx는 사용자 환경 변수를 영구로 바꿔서 다음 실험과 트레이 Ollama까지 모르게 같은 설정으로 돌게 만들고, 정작 지금 창에는 적용되지 않는다.
- **확인**: 창 A의 처음 몇 줄 중 `server config`가 들어간 줄에서 `OLLAMA_NUM_PARALLEL:1`을 찾는다. 다른 값이면 Ctrl+C로 끄고, `Get-Process ollama*`로 트레이 Ollama가 남아 있는지 본 뒤 다시 켠다 (예전에 환경 변수가 안 먹은 적이 있다).
- 측정을 병렬 1로 하는 이유: Ollama는 문맥 메모리를 num_ctx × 병렬 수만큼 잡는다 (Ollama FAQ). num_ctx 16384 × 4면 16GB에 다 들어가지 않을 수 있다. 측정은 동시 1이라 병렬 1로 충분하다.

## 2. 워밍업

창 B (측정용 중계 서버. 한도는 모두 넉넉):

```powershell
$M = "results\E6-measure_1"
.\.venv\Scripts\python.exe -m runner.real_adapter --port 8001 --config runner\configs\e6_measure.json --num-ctx 16384 --log-dir "$M\server"
```

중계 서버는 시작할 때 같은 num_ctx로 모델을 올리고 토큰 1개를 생성한 뒤에 포트를 연다. `Running on http://127.0.0.1:8001`이 나오면 준비된 것이다. Ollama가 꺼져 있으면 워밍업 오류를 내고 바로 끝난다.

창 C:

```powershell
ollama ps                                    # PROCESSOR 열이 100% GPU여야 함 (CPU가 섞이면 측정이 느려짐)
$M = "results\E6-measure_1"
$PY = ".\.venv\Scripts\python.exe"
& $PY scripts\probe_ollama.py --backend mock --base-url http://127.0.0.1:8001 --concurrency 1 --repetitions 3 --input-tokens 500 --output "$M\warmup_500.csv"
```

워밍업 입력을 500으로 하는 이유: 프롬프트는 (작업 이름, 입력 크기)로 정해지므로 측정 크기와 다르게 해야 같은 프롬프트를 두 번 보내지 않는다.

## 3. 긴 입력 측정

창 C:

```powershell
foreach ($n in 100, 1000, 3000, 8000) {
  & $PY scripts\probe_ollama.py --backend mock --base-url http://127.0.0.1:8001 --concurrency 1 --repetitions 5 --input-tokens $n --output "$M\probe_$n.csv"
}
& $PY -m runner.prefill_rate "$M\server"
```

- 각 probe가 `Successful: 5; failed: 0`(워밍업은 3)으로 끝나야 한다.
- `prefill_rate` 출력: 입력 크기별 중앙값 표, 직선 맞춤 `prefill ≈ 고정 + 토큰당 × prompt_eval_count`, 덧붙는 토큰(템플릿·지시문) 중앙값, 뺀 행 수. 워밍업 500도 표의 한 줄로 들어간다 (중앙값이라 첫 요청이 튀어도 괜찮다).
- `prompt_eval_count 없음(캐시 적중)` 행은 Ollama가 0을 생략한 것이라 계산에서 뺀다. 덧붙는 토큰은 캐시에 맞은 템플릿 앞부분 몇 토큰만큼 작게 나올 수 있다 (확인 안 됨).
- **기록**: 표와 직선, 덧붙는 토큰을 그대로 복사해 둔다. 토큰당 시간은 P1에게 전달한다 (가짜 서버 `input_token_seconds` 보정). 덧붙는 토큰은 `RelayConfig.prompt_overhead_tokens` 후보로 기록만 하고 바꾸지 않는다.

끝나면 창 B와 창 A를 Ctrl+C로 끈다.

## 4. Ollama 다시 켜기 (E6용: 병렬 4)

창 A:

```powershell
$env:OLLAMA_NUM_PARALLEL = "4"
ollama serve
```

**확인**: `server config` 줄에 `OLLAMA_NUM_PARALLEL:4`. 아니면 1단계처럼 끄고 다시 켠다. 병렬 1이면 Ollama가 초당 약 3.7건밖에 처리하지 못해 E6-requests 한도(초당 3건)보다 느려지므로 반드시 4여야 한다.

## 5. E6 seed 1 보정

창 C (실행기가 실행마다 중계 서버를 새로 띄우고 끈다. 첫 실행은 모델을 올리느라 몇 초 더 걸린다):

```powershell
& $PY -m runner.run --exp E6 --seed 1 --backend ollama
$R = (Get-ChildItem results -Directory -Filter "E6_*" | Sort-Object Name | Select-Object -Last 1).FullName
& $PY -m runner.e4_check $R
& $PY -m runner.relay_check $R
```

`$R`은 가장 최근 E6 결과 폴더다 (실행기 마지막 줄의 `결과 폴더:`와 같아야 함). 측정 폴더는 `E6-measure`라 `E6_*`에 걸리지 않는다.

**합격 기준** (조건별, C2·C3 모두):

| 확인 | 도구 | E6-requests | E6-input |
| --- | --- | --- | --- |
| 실행 상태 | 실행기 마지막 출력, runs.csv | completed·대조 OK | completed·대조 OK |
| 의도한 버킷만 바닥 | e4_check 최소 잔량 | 요청만 0% 근처, 입력·출력은 높음 | 입력만 0% 근처, 요청·출력은 높음 |
| Ollama 오류 | relay_check | 502 0 (invalid_response 0) · 529 0 · 취소 0 | 같음 |
| Ollama 대기열 | relay_check `대기 추정(>4)` | 10% 미만 | **기록만** (합격 기준 아님) |

E6-input의 대기 추정을 기록만 하는 이유: 처음 약 10초 동안 light가 입력 버킷을 거의 다 가져가 Ollama에 초당 10건 이상 보내는 것은 구조적이라(가짜 서버 리허설에서 C2·C3 모두 light가 8~10초에 끝남, heavy 429 약 4,800번), 한도를 낮춰도 해결되지 않는다. 에이전트별 값에서 a1만 높으면 이 몰림이다. 결과를 공유할 때 전체·에이전트별 값을 같이 적는다.

**참고값** (합격 기준 아님, 기록만): relay_check의 `참고:` 뒤.
- TTFT 중앙값: 기준은 0.014초 + 입력 × (3단계 토큰당). a1(light)은 입력 약 100, a2(heavy_in)는 약 1000.
- 출력 토큰당: 기준 0.0063초는 동시 1에서 잰 값이다. 병렬 4로 동시에 처리하면 느려지는 게 정상이고, 보고서에 한계로 적는다.
- 적재 최대: 0에 가까워야 한다 (실행 중에 모델을 다시 올리지 않음).

**기준을 못 넘으면**:
- E6-requests의 대기 추정이 10% 이상: `e6_requests.json`의 `capacity`·`refill_rate`를 4 → 3으로 낮춘다 (11/27 적용. 3에서도 10% 이상이면 2로).
- 의도하지 않은 버킷이 바닥: 그 버킷을 키운다. 단 `e6_input.json`의 `input_capacity`는 heavy_in 최대 입력 1500 이상이어야 한다 (아니면 422).
- 502·invalid_response·529·취소가 있음: 본 실행을 미루고 `$R\server\relay_requests_*.csv`의 error 열을 본다.
- 값을 바꾸면 JSON만 고치고 seed 1을 다시 돌린다. 그다음 `tests/test_experiments.py`의 `test_e6_configs_by_hand` 값과 `runner/experiments.py`의 손계산 주석(초당 4건, 50초 등)을 맞추고, `pytest -q` 통과 후 바꾸기 전후 값과 이유를 적어 커밋한다.

## 6. E6 본 실행

```powershell
& $PY -m runner.run --exp E6 --backend ollama       # 병목 2종 × C2·C3 × 시드 1~3 = 12회, 약 20분
$R = (Get-ChildItem results -Directory -Filter "E6_*" | Sort-Object Name | Select-Object -Last 1).FullName
& $PY -m runner.e4_check $R
& $PY -m runner.relay_check $R
```

5단계와 같은 기준으로 12줄을 본다. 분석에는 이 폴더만 쓴다 (5단계 보정 폴더는 쓰지 않음).

**결과 해석**: E6의 주 비교는 E6-requests다 (요청 병목이라 요청마다 1씩 빼므로 크기에 따른 비대칭이 없다). E6-input은 같이 도는 구간이 짧고(리허설 8~10초, 슬로 스타트 포함), heavy 한 건이 충전 1초 분량이라(E4는 0.2초) 큰 요청이 막히는 현상이 E4보다 강하므로, 이 한계와 함께 해석한다.

## 7. 마무리

```powershell
Compress-Archive -Path $R, results\E6-measure_1 -DestinationPath "$R.zip"
```

창 A를 Ctrl+C로 끈다. zip을 팀에 공유하고 아래를 PR #17과 주간 보고에 적는다.
- 3단계 `prefill_rate` 출력 (표, 직선, 덧붙는 토큰)
- 5·6단계 `e4_check`·`relay_check` 출력 (E6-input의 대기 추정은 전체·에이전트별 값을 함께), 한도를 바꿨다면 전후 값과 이유
- Ollama 버전(stats JSON의 `ollama.version`), GPU, `OLLAMA_NUM_PARALLEL` (측정 1, E6 4)
