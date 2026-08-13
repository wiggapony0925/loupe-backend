"""NUL must never reach a jsonb column.

WHY THIS FILE EXISTS. ``0057_json_to_jsonb`` converted fifteen columns from
``json`` to ``jsonb``, and the two types disagree about exactly one character.
``json`` stores the document as the text it arrived as, so a NUL escape was
stored happily. ``jsonb`` decodes to postgres ``text``, which cannot contain
NUL, so the same write raises ``UntranslatableCharacterError``.

Several of those columns are fed straight from client JSON — ``PUT
/v1/users/recents`` writes a client-supplied list into ``user_recents.searches``
after filtering only for ``isinstance(s, str)`` and ``s.strip()``, and
``str.strip()`` does not remove NUL. So before the guard in ``app/db/types.py``
this was a 200 that became an unhandled 500 the moment 0057 shipped.

These tests run against real postgres because the bug only exists there:
SQLite's JSON is TEXT and would accept the NUL, which is exactly why the whole
class of defect survived a 2,000-test SQLite suite.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.db.types import _strip_nul

NUL = chr(0)


def test_strip_nul_cleans_every_position_in_a_document():
    """Strings, list members, dict values AND dict keys."""
    dirty = {
        f"key{NUL}": f"value{NUL}here",
        "nested": [f"a{NUL}", {"deep": f"b{NUL}c"}],
        "untouched": 42,
        "none": None,
    }
    clean = _strip_nul(dirty)
    assert clean == {
        "key": "valuehere",
        "nested": ["a", {"deep": "bc"}],
        "untouched": 42,
        "none": None,
    }
    assert NUL not in repr(clean)


def test_strip_nul_leaves_clean_documents_identical():
    """No allocation-churn or reordering for the normal case."""
    clean = {"q": "Charizard", "ids": [1, 2, 3], "nested": {"ok": True}}
    assert _strip_nul(clean) == clean


@pytest.mark.asyncio
async def test_postgres_jsonb_really_does_reject_nul(pg_engine):
    """The premise. If this ever stops failing, the guard can go.

    A NUL reaches postgres by two different routes and is refused differently
    by each, which is worth pinning because only the second one looks like a
    JSON problem:

      * as a raw byte in the parameter — rejected by the connection encoding
        (``CharacterNotInRepertoireError: invalid byte sequence for UTF8``),
        before jsonb is consulted at all;
      * as a ``\\u0000`` escape inside otherwise-valid JSON text — accepted as
        json, rejected on the cast to jsonb
        (``UntranslatableCharacterError: unsupported Unicode escape sequence``).

    The application hits the second: SQLAlchemy serialises the Python string,
    turning the NUL into the escape.
    """
    raw_byte = f'{{"q":"a{NUL}b"}}'
    as_escape = '{"q":"a\\u0000b"}'

    async with pg_engine.connect() as conn:
        with pytest.raises(Exception) as raw_err:
            await conn.execute(sa.text("SELECT (:p)::jsonb"), {"p": raw_byte})
        assert "0x00" in str(raw_err.value) or "byte sequence" in str(raw_err.value)
        await conn.rollback()

        with pytest.raises(Exception) as esc_err:
            await conn.execute(sa.text("SELECT (:p)::jsonb"), {"p": as_escape})
        assert "unsupported Unicode escape" in str(esc_err.value)
        await conn.rollback()

        # ...while plain json still takes the escape. That asymmetry is the
        # whole reason 0057 introduced the risk, rather than it always existing.
        got = (await conn.execute(sa.text("SELECT (:p)::json"), {"p": as_escape})).scalar()
        assert got is not None


@pytest.mark.asyncio
async def test_a_nul_bearing_payload_survives_a_real_jsonb_write(pg_engine):
    """End to end through the ORM type, into the real jsonb column.

    This is the regression: the same insert raises without ``_strip_nul``.
    """
    from app.models.user import User  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from tests.factories import make_user  # noqa: PLC0415

    async with pg_engine.connect() as conn:
        trans = await conn.begin()
        session = async_sessionmaker(bind=conn, expire_on_commit=False)()
        try:
            user = await make_user(session)
            await session.flush()

            from app.models import UserRecents  # noqa: PLC0415

            session.add(
                UserRecents(
                    user_id=user.id,
                    searches=[f"Charizard{NUL}holo", "clean term"],
                    viewed=[{"id": str(uuid.uuid4()), "name": f"Pikachu{NUL}"}],
                )
            )
            # Without the guard this raises UntranslatableCharacterError.
            await session.flush()

            row = (
                await session.execute(
                    sa.text("SELECT searches, viewed FROM user_recents WHERE user_id = :u"),
                    {"u": user.id},
                )
            ).one()
            assert row.searches == ["Charizardholo", "clean term"]
            assert row.viewed[0]["name"] == "Pikachu"
        finally:
            await session.close()
            if trans.is_active:
                await trans.rollback()
