"""실행별 CSV. 서버 상태는 0.1초마다, 에이전트 누계는 바뀔 때 기록한다."""

import asyncio
import csv
import logging
import time
from pathlib import Path


SAMPLE_SECONDS = 0.1


async def record_experiment(state, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / f"server_state_{state.run_id}.csv"
    agents_path = directory / f"agent_stats_{state.run_id}.csv"
    with (state_path.open("x", encoding="utf-8", newline="") as state_file,
          agents_path.open("x", encoding="utf-8", newline="") as agents_file):
        samples = csv.writer(state_file)
        agents = csv.writer(agents_file)
        samples.writerow(("run_id", "elapsed_seconds", "queued", "active", "requests_remaining",
                          "input_tokens_remaining", "output_tokens_remaining"))
        agents.writerow(("run_id", "elapsed_seconds", "agent_id", "completed", "rate_limited"))
        agents_file.flush()
        previous_counts = {}

        def write_rows(state_row, agent_rows):
            samples.writerow(state_row)
            state_file.flush()
            if agent_rows:
                agents.writerows(agent_rows)
                agents_file.flush()

        async def sample():
            async with state.lock:
                elapsed = round(time.perf_counter() - state.started_at, 6)
                quotas = state.quota.snapshot()
                state_row = (state.run_id, elapsed, len(state.waiting), state.active,
                             quotas["requests"]["remaining"], quotas["input_tokens"]["remaining"],
                             quotas["output_tokens"]["remaining"])
                agent_rows = []
                for agent_id, counters in state.agent_stats.items():
                    counts = (counters["completed"], counters["rate_limited"])
                    if previous_counts.get(agent_id) != counts:
                        agent_rows.append((state.run_id, elapsed, agent_id, *counts))
                        previous_counts[agent_id] = counts
            # Disk writes can be slow on shared folders; keep them outside the request loop.
            await asyncio.to_thread(write_rows, state_row, agent_rows)

        stop = asyncio.Event()

        async def run():
            tick = 1
            try:
                while True:
                    deadline = state.started_at + tick * SAMPLE_SECONDS
                    try:
                        await asyncio.wait_for(stop.wait(), max(0, deadline - time.perf_counter()))
                    except asyncio.TimeoutError:
                        pass
                    final_sample = stop.is_set()
                    await sample()
                    if final_sample:
                        break
                    # Skip missed ticks instead of emitting synthetic historical samples.
                    # Windows timers can wake slightly early. Advance past the sampled
                    # tick even then, so one deadline never produces duplicate rows.
                    tick = max(tick + 1, int((time.perf_counter() - state.started_at) / SAMPLE_SECONDS) + 1)
            except OSError:
                logging.getLogger(__name__).exception("Could not write server logs for %s", state.run_id)
                raise

        await sample()
        task = asyncio.create_task(run())
        try:
            yield
        finally:
            stop.set()
            await task
