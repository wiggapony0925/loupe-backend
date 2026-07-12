"""Worker supervisor — the crash-loop killer.

A bare ``run_worker`` call dies with the container when Redis is
unreachable, putting Cloud Run into a restart loop (the exact failure mode
the fleet hit when the broker was re-provisioned). The supervisor must
retry with backoff instead, and exit cleanly only when arq itself returns.
"""

from __future__ import annotations

from unittest.mock import patch

from app import worker_entry


def test_supervisor_retries_crashes_with_backoff_then_exits_clean():
    calls = {"runs": 0, "sleeps": []}

    def fake_run_worker(_settings):
        calls["runs"] += 1
        if calls["runs"] <= 3:
            raise ConnectionError("redis unreachable")
        return  # clean exit (SIGTERM drain)

    def fake_sleep(seconds):
        calls["sleeps"].append(seconds)

    with (
        patch.object(worker_entry, "run_worker", fake_run_worker),
        patch.object(worker_entry.time, "sleep", fake_sleep),
    ):
        worker_entry._run_supervised()

    # Three crashes → three backoff sleeps → fourth run returns cleanly.
    assert calls["runs"] == 4
    assert calls["sleeps"] == [5.0, 10.0, 20.0]
    assert worker_entry._state["status"] == "running"


def test_supervisor_backoff_caps_at_max():
    calls = {"runs": 0, "sleeps": []}

    def fake_run_worker(_settings):
        calls["runs"] += 1
        if calls["runs"] <= 8:
            raise ConnectionError("still down")
        return

    with (
        patch.object(worker_entry, "run_worker", fake_run_worker),
        patch.object(worker_entry.time, "sleep", lambda s: calls["sleeps"].append(s)),
    ):
        worker_entry._run_supervised()

    # 5 → 10 → 20 → 40 → 80 → 160 → capped at 300 thereafter.
    assert calls["sleeps"] == [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0]


def test_supervisor_resets_backoff_after_a_healthy_stretch():
    calls = {"runs": 0, "sleeps": []}
    # Fake clock: first run "lasts" 10 minutes (healthy stretch), the rest
    # crash instantly — the attempt counter must restart from 1.
    clock = {"now": 0.0}

    def fake_monotonic():
        return clock["now"]

    def fake_run_worker(_settings):
        calls["runs"] += 1
        if calls["runs"] == 1:
            clock["now"] += 600.0  # ran fine for 10 min, then crashed
            raise ConnectionError("blip after a healthy stretch")
        if calls["runs"] == 2:
            raise ConnectionError("crash right away")
        return

    with (
        patch.object(worker_entry, "run_worker", fake_run_worker),
        patch.object(worker_entry.time, "sleep", lambda s: calls["sleeps"].append(s)),
        patch.object(worker_entry.time, "monotonic", fake_monotonic),
    ):
        worker_entry._run_supervised()

    # Healthy stretch reset the counter → first sleep is the FLOOR again,
    # the immediate second crash escalates normally.
    assert calls["sleeps"] == [5.0, 10.0]
