"""System-health probes for the admin Operations overview.

Surfaces the failure modes that have actually bitten production — chiefly
**schema drift** (a model column shipped without an applied migration) and the
worker's Redis dependency — plus a presence guard for required production
config. Every check is read-only and reports status only, never secret values.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.session import get_engine
from app.schemas.ops import HealthCheck, HealthReport, OverallStatus
from app.utils.logger import get_logger

logger = get_logger("admin.health")

# loupe-backend/ — three parents up from app/services/admin/health_service.py.
_BACKEND_ROOT = Path(__file__).resolve().parents[3]
_ALEMBIC_DIR = _BACKEND_ROOT / "app" / "db" / "alembic"

# Status severity for rolling individual checks up into one headline. Optional
# integrations ("unconfigured") never fail the overall report.
_SEVERITY = {"ok": 0, "unconfigured": 0, "warn": 1, "down": 2}


async def report(db: AsyncSession) -> HealthReport:
    """Run every probe and fold them into a single report."""
    settings = get_settings()
    checks: list[HealthCheck] = [
        await _check_database(db),
        await _check_migrations(),
        await _check_redis(settings),
        *_check_providers(settings),
        _check_billing(settings),
        _check_email(settings),
        _check_prod_hardening(settings),
    ]
    overall: OverallStatus = "ok"
    for c in checks:
        if _SEVERITY[c.status] > _SEVERITY[overall]:
            overall = "warn" if c.status == "warn" else "down"
    return HealthReport(status=overall, generated_at=datetime.now(UTC), checks=checks)


async def _check_database(db: AsyncSession) -> HealthCheck:
    try:
        await db.execute(text("SELECT 1"))
        return HealthCheck(
            key="database",
            label="Database",
            status="ok",
            detail="Connection healthy.",
            category="core",
        )
    except Exception as exc:  # pragma: no cover - exercised only on outage
        return HealthCheck(
            key="database",
            label="Database",
            status="down",
            detail=f"Query failed: {type(exc).__name__}.",
            category="core",
        )


async def _check_migrations() -> HealthCheck:
    """Compare the code's Alembic head(s) to the database's applied head(s).

    A mismatch is schema drift — the single most damaging outage class for this
    app — so it is surfaced first and loudly.
    """
    try:
        from alembic.config import Config
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
        code_heads = set(ScriptDirectory.from_config(cfg).get_heads())

        def _db_heads(conn: Connection) -> set[str]:
            return set(MigrationContext.configure(conn).get_current_heads())

        engine = get_engine()
        async with engine.connect() as conn:
            db_heads = await conn.run_sync(_db_heads)
    except Exception as exc:
        return HealthCheck(
            key="migrations",
            label="Migrations",
            status="warn",
            detail=f"Could not determine revision: {type(exc).__name__}.",
            category="core",
        )

    if db_heads == code_heads:
        head = next(iter(code_heads), "—")
        return HealthCheck(
            key="migrations",
            label="Migrations",
            status="ok",
            detail=f"Database at head ({head}).",
            category="core",
        )
    missing = ", ".join(sorted(code_heads - db_heads)) or "—"
    return HealthCheck(
        key="migrations",
        label="Migrations",
        status="warn",
        detail=f"Schema drift — unapplied: {missing}. Run alembic upgrade head.",
        category="core",
    )


async def _check_redis(settings: Settings) -> HealthCheck:
    """Ping Redis — the queue/cache the background worker depends on."""
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.redis_url)
        try:
            await asyncio.wait_for(client.ping(), timeout=2.0)
        finally:
            await client.aclose()
        return HealthCheck(
            key="redis",
            label="Redis / queue",
            status="ok",
            detail="Reachable.",
            category="infra",
        )
    except Exception as exc:
        return HealthCheck(
            key="redis",
            label="Redis / queue",
            status="down",
            detail=f"Unreachable ({type(exc).__name__}) — worker tasks will stall.",
            category="infra",
        )


def _check_providers(settings: Settings) -> list[HealthCheck]:
    """Report catalog/pricing provider configuration (presence, never keys)."""
    pokemon = (
        "API key set (higher rate limit)."
        if settings.pokemon_tcg_api_key
        else "Keyless — works but rate-limited."
    )
    pricing_token = settings.pricecharting_token or settings.pricecharting_api_key
    return [
        HealthCheck(
            key="provider_pokemon",
            label="Pokémon TCG",
            status="ok",
            detail=pokemon,
            category="data",
        ),
        HealthCheck(
            key="provider_scryfall",
            label="Magic (Scryfall)",
            status="ok",
            detail="Keyless public API.",
            category="data",
        ),
        HealthCheck(
            key="provider_ygo",
            label="Yu-Gi-Oh (YGOPRODeck)",
            status="ok",
            detail="Keyless public API.",
            category="data",
        ),
        HealthCheck(
            key="provider_pricecharting",
            label="PriceCharting",
            status="ok" if pricing_token else "unconfigured",
            detail="Token set — sealed/graded fallback live."
            if pricing_token
            else "No token — sealed/graded price fallback disabled.",
            category="data",
        ),
    ]


def _check_billing(settings: Settings) -> HealthCheck:
    if not settings.stripe_secret_key:
        return HealthCheck(
            key="billing",
            label="Billing (Stripe)",
            status="unconfigured",
            detail="No secret key — comp-only mode (paywall shows “soon”).",
            category="config",
        )
    if not settings.stripe_webhook_secret:
        return HealthCheck(
            key="billing",
            label="Billing (Stripe)",
            status="warn",
            detail="Secret set but webhook signing secret missing.",
            category="config",
        )
    return HealthCheck(
        key="billing",
        label="Billing (Stripe)",
        status="ok",
        detail="Checkout + webhook configured.",
        category="config",
    )


def _check_email(settings: Settings) -> HealthCheck:
    ok = settings.email_enabled
    return HealthCheck(
        key="email",
        label="Email (Resend)",
        status="ok" if ok else "unconfigured",
        detail="Transactional email live."
        if ok
        else "Not configured — status updates log only.",
        category="config",
    )


def _check_prod_hardening(settings: Settings) -> HealthCheck:
    """In production, flag required-but-missing security envs."""
    if not settings.is_production:
        return HealthCheck(
            key="prod_hardening",
            label="Production hardening",
            status="ok",
            detail=f"Relaxed in {settings.app_env}.",
            category="config",
        )
    missing: list[str] = []
    if not settings.mfa_secret_key:
        missing.append("MFA_SECRET_KEY")
    if not settings.jwt_private_key_pem or not settings.jwt_public_key_pem:
        missing.append("JWT keypair")
    if not settings.admin_email_set:
        missing.append("ADMIN_EMAILS")
    if missing:
        return HealthCheck(
            key="prod_hardening",
            label="Production hardening",
            status="warn",
            detail=f"Missing in prod: {', '.join(missing)}.",
            category="config",
        )
    return HealthCheck(
        key="prod_hardening",
        label="Production hardening",
        status="ok",
        detail="All required production secrets present.",
        category="config",
    )


__all__ = ["report"]
