"""Unit tests for the Redis-free ingestion runner (`app.tasks.run`)."""

from __future__ import annotations

import importlib
from typing import Any

import pytest

# The ``app.tasks`` package re-exports each task *function* under the same name
# as its submodule, so a plain ``import app.tasks.catalog_sync`` yields the
# function, not the module. Resolve the real module objects via importlib so we
# can monkeypatch the functions the runner imports at call time.
catalog_sync_mod = importlib.import_module("app.tasks.catalog_sync")
image_index_mod = importlib.import_module("app.tasks.image_index")
price_backfill_mod = importlib.import_module("app.tasks.price_backfill")
price_snapshot_mod = importlib.import_module("app.tasks.price_snapshot")
run_mod = importlib.import_module("app.tasks.run")


@pytest.fixture
def recording(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace each real task with a stub that records the order it ran in."""
    calls: list[str] = []

    def _stub(name: str):
        async def _inner(*_a: Any, **_k: Any) -> dict[str, Any]:
            calls.append(name)
            return {"task": name, "ok": True}

        return _inner

    monkeypatch.setattr(catalog_sync_mod, "catalog_sync", _stub("catalog_sync"))
    monkeypatch.setattr(price_backfill_mod, "backfill_prices", _stub("price_backfill"))
    monkeypatch.setattr(price_snapshot_mod, "snapshot_prices", _stub("price_snapshot"))
    monkeypatch.setattr(image_index_mod, "index_card_images", _stub("image_index"))
    return calls


@pytest.mark.asyncio
async def test_run_all_runs_every_task_in_pipeline_order(recording: list[str]) -> None:
    results = await run_mod.run_task("all")
    # Ran each step exactly once, in the documented dependency order.
    assert recording == list(run_mod.PIPELINE)
    assert set(results) == set(run_mod.PIPELINE)
    assert results["price_snapshot"] == {"task": "price_snapshot", "ok": True}


@pytest.mark.asyncio
async def test_run_single_task_runs_only_that_one(recording: list[str]) -> None:
    result = await run_mod.run_task("price_snapshot")
    assert recording == ["price_snapshot"]
    assert result == {"task": "price_snapshot", "ok": True}


@pytest.mark.asyncio
async def test_unknown_task_raises_valueerror(recording: list[str]) -> None:
    with pytest.raises(ValueError, match="unknown task"):
        await run_mod.run_task("bogus")
    assert recording == []


@pytest.mark.asyncio
async def test_all_swallows_one_failure_and_keeps_going(
    monkeypatch: pytest.MonkeyPatch, recording: list[str]
) -> None:
    async def _boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        recording.append("price_backfill")
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(price_backfill_mod, "backfill_prices", _boom)

    results = await run_mod.run_task("all")
    # Every step was attempted despite the middle one failing.
    assert recording == list(run_mod.PIPELINE)
    assert "error" in results["price_backfill"]
    assert results["price_snapshot"]["ok"] is True


@pytest.mark.asyncio
async def test_single_task_failure_propagates(
    monkeypatch: pytest.MonkeyPatch, recording: list[str]
) -> None:
    async def _boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("nope")

    monkeypatch.setattr(price_snapshot_mod, "snapshot_prices", _boom)

    with pytest.raises(RuntimeError, match="nope"):
        await run_mod.run_task("price_snapshot")
