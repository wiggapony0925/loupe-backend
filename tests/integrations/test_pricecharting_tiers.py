"""Automatic PriceCharting tier detection + fallback description.

The whole point is that the app follows the live subscription with no code
changes — so these pin the capability → tier mapping across upgrade/downgrade,
and the dev-portal ``describe`` payload.
"""

from __future__ import annotations

import pytest

from app.integrations.pricecharting import tiers


def _caps(**kw) -> tiers.Capabilities:
    base: dict[str, object] = {
        "configured": True,
        "api_ok": False,
        "graded_fields": False,
        "csv_ok": False,
        "probed_at": None,
        "note": "",
    }
    base.update(kw)
    return tiers.Capabilities(**base)  # type: ignore[arg-type]


def test_tier_derives_from_capabilities():
    assert _caps(configured=False).tier is tiers.Tier.none
    assert _caps(api_ok=True).tier is tiers.Tier.collector
    assert _caps(api_ok=True, graded_fields=True).tier is tiers.Tier.premium
    # CSV wins outright — that's the Legendary bulk path.
    assert (
        _caps(api_ok=True, graded_fields=True, csv_ok=True).tier is tiers.Tier.legendary
    )
    assert _caps(csv_ok=True).tier is tiers.Tier.legendary


@pytest.mark.asyncio
async def test_detect_none_without_token(monkeypatch):
    monkeypatch.setattr(tiers, "token", lambda: None)
    caps = await tiers.detect(force=True)
    assert caps.configured is False
    assert caps.tier is tiers.Tier.none


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("graded", "csv_ok", "expected"),
    [
        (False, False, tiers.Tier.collector),
        (True, False, tiers.Tier.premium),
        (True, True, tiers.Tier.legendary),
    ],
)
async def test_detect_maps_probes_to_tier(monkeypatch, graded, csv_ok, expected):
    monkeypatch.setattr(tiers, "token", lambda: "tok")

    async def fake_api():
        return True, graded, "note"

    async def fake_csv():
        return csv_ok, "note"

    monkeypatch.setattr(tiers, "_probe_api", fake_api)
    monkeypatch.setattr(tiers, "_probe_csv", fake_csv)
    caps = await tiers.detect(force=True)
    assert caps.tier is expected


def test_describe_marks_one_active_rung_with_grade_map():
    d = tiers.describe(
        _caps(api_ok=True), mirror={"ready": False, "rows": 0, "synced_at": None}
    )
    assert d["tier"]["key"] == "collector"
    assert d["strategy"]["key"] == "api_raw"
    active = [rung for rung in d["fallback_chain"] if rung["active"]]
    assert len(active) == 1 and active[0]["tier"] == "collector"
    # The full chain is always shown (best → worst) so the portal can render it.
    assert [r["tier"] for r in d["fallback_chain"]] == [
        "legendary",
        "premium",
        "collector",
        "none",
    ]
    assert any(g["grade"] == "PSA 10" for g in d["grade_map"])
