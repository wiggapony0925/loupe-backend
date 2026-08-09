"""The community audit is part of CI, not a script someone remembers to run.

An audit that only runs by hand is an audit that stops running. This drives
the same harness the operator invokes, so a regression in ANY of the 49
community routes — a privacy leak, a missing auth gate, a broken cursor —
fails the build rather than waiting to be noticed in production.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_every_community_route_passes_its_audit():
    from scripts.community_audit import main

    failures = await main()
    assert failures == 0, f"{failures} community audit check(s) failed — see output"
