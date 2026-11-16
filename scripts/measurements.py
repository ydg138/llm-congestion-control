"""실제/모의 서버 측정에 쓰는 CSV 규약과 통계 계산."""

import math
import statistics

FIELDS = ["backend", "concurrency", "repetition", "request_id", "workload_id", "status",
          "server_run_id", "config_revision",
          "ttft_seconds", "ttft_kind", "total_seconds", "input_tokens", "output_tokens",
          "server_prefill_seconds", "server_decode_seconds", "error"]
METRICS = ["ttft_seconds", "total_seconds", "input_tokens", "output_tokens",
           "server_prefill_seconds", "server_decode_seconds"]


def percentile(values, percentile_value=95):
    """정렬한 표본의 선형 보간 백분위수. 빈 표본은 None."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile_value / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def numeric(value):
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def summarize(rows, min_samples=5):
    groups = {}
    for row in rows:
        concurrency = int(row["concurrency"])
        groups.setdefault(concurrency, []).append(row)
    result = {}
    for concurrency, group in sorted(groups.items()):
        successes = [row for row in group if str(row["status"]) == "200" and not row.get("error")]
        entry = {"requests": len(group), "successes": len(successes),
                 "failures": len(group) - len(successes),
                 "sufficient_samples": len(successes) >= min_samples,
                 "ttft_kinds": sorted({row.get("ttft_kind", "unknown") for row in successes}),
                 "metrics": {}}
        for metric in METRICS:
            values = [v for row in successes if (v := numeric(row.get(metric))) is not None]
            entry["metrics"][metric] = {"n": len(values),
                                         "mean": statistics.mean(values) if values else None,
                                         "p95": percentile(values)}
        result[str(concurrency)] = entry
    return result


def compare(real_rows, mock_rows, min_samples=5):
    real = summarize(real_rows, min_samples)
    mock = summarize(mock_rows, min_samples)
    comparisons = {}
    for concurrency in sorted(real.keys() & mock.keys(), key=int):
        a, b = real[concurrency], mock[concurrency]
        ttft_comparable = (a["ttft_kinds"] == b["ttft_kinds"] and
                           len(a["ttft_kinds"]) == 1 and a["ttft_kinds"] != ["unknown"])
        metrics = {}
        for metric in METRICS:
            mean_a = a["metrics"][metric]["mean"]
            mean_b = b["metrics"][metric]["mean"]
            comparable = metric != "ttft_seconds" or ttft_comparable
            enough = (a["metrics"][metric]["n"] >= min_samples and
                      b["metrics"][metric]["n"] >= min_samples)
            relative = ((mean_b - mean_a) / mean_a
                        if comparable and enough and mean_a not in (None, 0) and mean_b is not None
                        else None)
            metrics[metric] = {"real_mean": mean_a, "mock_mean": mean_b,
                               "comparable_measurement": comparable,
                               "sufficient_samples": enough,
                               "signed_relative_mean_error": relative}
        comparisons[concurrency] = {"metrics": metrics,
                                   "ttft_note": "same measurement kind" if ttft_comparable else
                                   "TTFT measurement kinds differ; relative error suppressed"}
    return {"min_samples": min_samples, "real": real, "mock": mock,
            "matched_concurrency": comparisons,
            "unmatched_real": sorted(real.keys() - mock.keys(), key=int),
            "unmatched_mock": sorted(mock.keys() - real.keys(), key=int),
            "note": "Diagnostic comparison only. Match workload, token lengths, warmup, and environment before interpreting differences."}
