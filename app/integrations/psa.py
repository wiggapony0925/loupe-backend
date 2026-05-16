"""PSA Public API provider — population reports.

API docs: https://www.psacard.com/publicapi. Token is bearer-style.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.config import get_settings
from app.integrations.base import BaseProvider, PopulationReport
from app.utils.logger import get_logger

logger = get_logger("integrations.psa")

_BASE = "https://api.psacard.com/publicapi"


class PsaProvider(BaseProvider):
    id = "psa"
    name = "PSA"

    def is_configured(self) -> bool:
        return bool(get_settings().psa_api_token)

    async def get_population(self, spec_or_query: str) -> list[PopulationReport] | None:
        if not self.is_configured() or not spec_or_query:
            return None
        token = get_settings().psa_api_token
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        # Best-effort: try by spec id, then fall back to None.
        url = f"{_BASE}/pop/GetPSASpecPopulation/{quote(spec_or_query)}"
        try:
            resp = await self._call_with_retry("GET", url, headers=headers)
            if resp is None or resp.status_code >= 400:
                return None
            data = resp.json() or {}
            return self._parse(data)
        except Exception as exc:
            logger.warning("psa get_population failed: %s", exc)
            return None

    @staticmethod
    def _parse(data: dict[str, Any]) -> list[PopulationReport] | None:
        # PSA response varies by endpoint; handle common shapes defensively.
        raw = (
            data.get("PSASpecPopulation")
            or data.get("Population")
            or data.get("Pop")
            or {}
        )
        if not isinstance(raw, dict):
            return None
        # Common keys: pop10, pop9, ..., totalPop
        out: list[PopulationReport] = []
        grade_keys = [
            ("pop10", "10"),
            ("pop9", "9"),
            ("pop8", "8"),
            ("pop7", "7"),
            ("pop6", "6"),
            ("pop5", "5"),
        ]
        for key, grade in grade_keys:
            val = raw.get(key) or raw.get(key.upper())
            try:
                pop = int(val) if val is not None else 0
            except (TypeError, ValueError):
                pop = 0
            if pop:
                out.append(
                    PopulationReport(
                        source="psa",
                        house="psa",
                        grade=grade,
                        population=pop,
                    )
                )
        return out or None


__all__ = ["PsaProvider"]
