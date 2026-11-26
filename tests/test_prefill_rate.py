"""긴 입력 측정 계산(runner/prefill_rate.py)을 손으로 정한 relay CSV로 확인하고,
측정 절차(probe → 중계 서버 → relay CSV)가 지금 코드로 이어지는지 가짜 Ollama로 확인한다."""

import argparse
import asyncio
import csv

import pytest

from runner import prefill_rate
from runner.experiments import E6_MEASURE
from runner.prefill_rate import NO_COUNT, NO_PREFILL, by_size, fit_line, usable
from runner.real_adapter import load_config
from scripts import probe_ollama
from tests.test_ollama_client import OK_LINES, fake_ollama, serve
from tests.test_real_adapter import start_relay
from tests.test_relay_rules import words_of


def row(size, count, prefill, status="200"):
    return {"status": status, "input_tokens": str(size), "prompt_eval_count": str(count), "prefill_seconds": str(prefill)}


ROWS = [row(100, 130, 0.021), row(100, 130, 0.025), row(100, 130, 0.023),
        row(100, "", 0.001),                                # Ollama가 0을 생략 (접두사 캐시 적중) → 뺌
        row(1100, 1130, 0.121), row(1100, 1130, 0.125), row(1100, 1130, 0.123),
        row(1100, 1130, ""),                                # prefill 없음 → 뺌
        row(1100, "", "", status="502")]                    # 200이 아니면 처음부터 안 셈


def test_usable_drops_rows_without_count_or_prefill():
    points, dropped = usable(ROWS)
    assert len(points) == 6
    assert dropped == {NO_COUNT: 1, NO_PREFILL: 1}
    assert usable([row(100, 0, 0.01)])[1][NO_COUNT] == 1   # 0이 그대로 와도 캐시 적중으로 본다


def test_by_size_takes_medians():
    # 100: 0.021·0.025·0.023 → 0.023, 1100: 0.121·0.125·0.123 → 0.123
    assert by_size(usable(ROWS)[0]) == [(100, 3, 130, 0.023), (1100, 3, 1130, 0.123)]


def test_fit_line_by_hand():
    intercept, slope = fit_line([(130, 0.023), (1130, 0.123)])
    assert slope == pytest.approx(0.0001)                   # (0.123 − 0.023) ÷ (1130 − 130)
    assert intercept == pytest.approx(0.010)                # 0.023 − 130 × 0.0001
    assert fit_line([(0, 1), (1, 3), (2, 5)]) == pytest.approx((1, 2))


def write(folder, rows):
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "relay_requests_r1.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def test_main_prints_table_fit_and_dropped(tmp_path, capsys):
    write(tmp_path, ROWS)
    assert prefill_rate.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "200이 아닌 행 1개는 안 셈" in out and f"{NO_COUNT} 1개, {NO_PREFILL} 1개" in out
    assert "prefill ≈ 0.0100초 + 0.000100초 × prompt_eval_count" in out
    assert "중앙값: 30" in out                              # 130 − 100, 1130 − 1100


def test_main_needs_two_different_counts(tmp_path, capsys):
    write(tmp_path, ROWS[:3])                               # 크기 하나
    assert prefill_rate.main([str(tmp_path)]) == 1
    write(tmp_path, [row(100, 12, 0.25), row(8000, 12, 0.25)])  # 크기 둘인데 값이 같음 (잘림 의심)
    assert prefill_rate.main([str(tmp_path)]) == 1
    assert capsys.readouterr().out.count("2가지 이상이어야 직선을 맞춘다") == 2


def test_probe_through_relay_with_long_input(tmp_path):
    """docs/e6_runbook.md 3단계를 가짜 Ollama로: e6_measure·num_ctx 16384 중계 서버에 probe 입력 8000 두 번."""
    async def call():
        ollama_app, received = fake_ollama(OK_LINES)
        ollama_runner, ollama_url = await serve(ollama_app)
        relay_runner, relay_url = await start_relay(ollama_url, tmp_path / "server", load_config(E6_MEASURE),
                                                    num_ctx=16384)
        args = argparse.Namespace(backend="mock", base_url=relay_url, model=None, prompt="", prompt_file=None,
                                  concurrency=[1], repetitions=2, pause=0, timeout=10, input_tokens=8000,
                                  max_output_tokens=64, output=str(tmp_path / "probe_8000.csv"))
        code = await probe_ollama.run(args)
        await relay_runner.cleanup()
        await ollama_runner.cleanup()
        return code, received

    code, received = asyncio.run(call())
    assert code == 0
    probes = received[1:]                                   # received[0]은 중계 서버의 워밍업
    assert len(probes) == 2
    assert all(len(words_of(p["prompt"])) == 8000 and p["options"]["num_ctx"] == 16384 for p in probes)
    assert words_of(probes[0]["prompt"]) != words_of(probes[1]["prompt"])   # 반복마다 다른 프롬프트
    rows = prefill_rate.read_rows(tmp_path / "server")
    assert [(r["status"], r["input_tokens"], r["prompt_eval_count"], r["prefill_seconds"]) for r in rows] == [
        ("200", "8000", "12", "0.25")] * 2                  # 가짜 Ollama의 마지막 조각 값
