"""Admin "Ask your data" — natural-language questions → read-only SQL via Claude.

Security model (defense in depth):
  1. The generated SQL must be a SINGLE ``SELECT``/``WITH`` statement — multiple
     statements and a denylist of write/DDL keywords are rejected up front.
  2. Execution happens in an explicit **READ ONLY** Postgres transaction with a
     statement timeout, which blocks writes even via writable CTEs — the real
     guard. The query is wrapped in an outer ``LIMIT`` so result size is bounded.
  3. The endpoint is super-admin-only and audited.

Degrades gracefully: with no ``ANTHROPIC_API_KEY`` the tool reports
``configured=False`` and never calls out.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from app.config import get_settings
from app.db.base import Base
from app.db.session import get_engine
from app.schemas.insights import AskResponse
from app.utils.logger import get_logger

logger = get_logger("admin.nl_query")

_ROW_CAP = 200
_STATEMENT_TIMEOUT_MS = 5000

# Defense-in-depth denylist. The read-only transaction is the true guard; this
# also catches writable CTEs (e.g. WITH x AS (DELETE ... RETURNING ...) SELECT).
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|merge|"
    r"copy|call|do|vacuum|reindex|attach|pragma|set|begin|commit|rollback|"
    r"savepoint|lock|listen|notify|prepare|execute|cluster|refresh)\b",
    re.IGNORECASE,
)
_STARTS_OK = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


def configured() -> bool:
    return bool(get_settings().anthropic_api_key)


def schema_summary() -> str:
    """Compact, model-friendly description of the tables and columns."""
    lines: list[str] = []
    for name, table in sorted(Base.metadata.tables.items()):
        cols = ", ".join(f"{c.name} {c.type}" for c in table.columns)
        lines.append(f"{name}({cols})")
    return "\n".join(lines)


def validate_sql(sql: str) -> str:
    """Return cleaned single-statement SELECT SQL, or raise ``ValueError``."""
    cleaned = sql.strip()
    # Strip a code fence if the model wrapped it.
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", cleaned).strip()
    # One statement only: drop a single trailing ';', reject any others.
    cleaned = cleaned.rstrip(";").strip()
    if ";" in cleaned:
        raise ValueError("Only a single statement is allowed.")
    if not _STARTS_OK.match(cleaned):
        raise ValueError("Only SELECT (or WITH … SELECT) queries are allowed.")
    if _FORBIDDEN.search(cleaned):
        raise ValueError("Query contains a disallowed keyword.")
    return cleaned


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
        return value
    return str(value)


async def _execute_readonly(sql: str) -> tuple[list[str], list[dict], bool]:
    """Run validated SQL in a read-only, time-bounded, row-capped transaction."""
    engine = get_engine()
    is_pg = engine.dialect.name == "postgresql"
    wrapped = f"SELECT * FROM (\n{sql}\n) AS _loupe_q LIMIT {_ROW_CAP + 1}"

    async with engine.connect() as conn:
        if is_pg:
            # Must be the first statements in the transaction.
            await conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            await conn.exec_driver_sql(
                f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}"
            )
        result = await conn.execute(text(wrapped))
        mappings = result.mappings().all()
        await conn.rollback()

    truncated = len(mappings) > _ROW_CAP
    mappings = mappings[:_ROW_CAP]
    columns = list(mappings[0].keys()) if mappings else []
    rows = [{k: _json_safe(v) for k, v in row.items()} for row in mappings]
    return columns, rows, truncated


async def _generate_sql(question: str, dialect: str) -> str:
    """Ask Claude to translate the question into one read-only SQL query."""
    from anthropic import AsyncAnthropic

    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    system = (
        f"You translate a question into exactly ONE read-only SQL SELECT query "
        f"for a {dialect} database. Rules: a single SELECT (or WITH … SELECT) "
        "statement; no INSERT/UPDATE/DELETE/DDL; no semicolons; always include a "
        "LIMIT (<= 200). Respond with ONLY the SQL — no prose, no code fences.\n\n"
        f"Schema:\n{schema_summary()}"
    )
    resp = await client.messages.create(
        model=settings.nl_query_model,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


async def ask(question: str) -> AskResponse:
    """Full flow: generate → validate → execute. Always returns a response."""
    if not configured():
        return AskResponse(
            configured=False,
            question=question,
            error="Set ANTHROPIC_API_KEY to enable natural-language queries.",
        )

    dialect = get_engine().dialect.name
    try:
        raw_sql = await _generate_sql(question, dialect)
        sql = validate_sql(raw_sql)
    except Exception as exc:  # generation or validation failed
        logger.warning("ask-your-data generation failed: %s", exc)
        return AskResponse(
            configured=True,
            question=question,
            error=f"Couldn't build a safe query: {exc}",
        )

    try:
        columns, rows, truncated = await _execute_readonly(sql)
    except Exception as exc:
        logger.warning("ask-your-data execution failed: %s", exc)
        return AskResponse(
            configured=True,
            question=question,
            sql=sql,
            error=f"Query failed: {type(exc).__name__}.",
        )

    return AskResponse(
        configured=True,
        question=question,
        sql=sql,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
    )


__all__ = ["ask", "configured", "schema_summary", "validate_sql"]
