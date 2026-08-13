"""Cross-dialect column types.

Postgres exposes rich types (UUID, JSONB, INET, NUMERIC); the test suite
uses SQLite via aiosqlite which lacks them. SQLAlchemy 2.0's :class:`Uuid`
transparently emits the right DDL per dialect, so ``UuidCol`` is that type
unchanged.

:class:`~sqlalchemy.JSON` is the exception, and this module used to claim
otherwise: it compiles to ``json`` on the postgres dialect, and only
:class:`~sqlalchemy.dialects.postgresql.JSONB` produces ``jsonb``. The
comment here said "JSONB" for two years while all twenty JSON columns on
the server were plain ``json``. ``0057_json_to_jsonb`` closed the gap for
the columns declared through ``JsonCol``; five columns still spell
``sqlalchemy.JSON`` inline (``users.mfa_backup_codes``,
``notifications.data``, ``ai_search_log.candidates``,
``ai_search_log.results``, ``email_log.headers``) and are therefore still
``json`` in postgres. Use ``JsonCol`` for new columns — a bare ``JSON``
import is how that list grew.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeDecorator

#: The one character ``jsonb`` cannot store that ``json`` could.
#:
#: ``json`` keeps the document as the text it arrived as, so it accepted
#: a NUL escape happily. ``jsonb`` decodes to postgres ``text`` on the way
#: in, and text cannot contain NUL — the write fails with
#: ``UntranslatableCharacterError: unsupported Unicode escape sequence``.
#:
#: That difference is a live regression risk, not a theoretical one. Fifteen
#: columns became jsonb in 0057 and several are fed directly by client JSON —
#: ``PUT /v1/users/recents`` writes a client-supplied search list into
#: ``user_recents.searches``, filtering only for ``isinstance(s, str)`` and
#: ``s.strip()``, and ``str.strip()`` does not remove NUL. Before 0057 such a
#: payload stored fine and returned 200; after it, it would raise on commit and
#: surface as an unhandled 500.
_NUL = "\x00"


def _strip_nul(value: Any) -> Any:
    """Recursively drop NUL from every string in a JSON document.

    Stripping rather than rejecting, deliberately: the previous behaviour was a
    successful write, so raising here would turn working requests into errors
    for a character that is never meaningful in a search term or a label. The
    document is otherwise untouched.
    """
    if isinstance(value, str):
        return value.replace(_NUL, "") if _NUL in value else value
    if isinstance(value, list):
        return [_strip_nul(v) for v in value]
    if isinstance(value, dict):
        return {
            (
                k.replace(_NUL, "") if isinstance(k, str) and _NUL in k else k
            ): _strip_nul(v)
            for k, v in value.items()
        }
    return value


#: JSONB on postgres, JSON (TEXT) on SQLite.
#:
#: WHY THE VARIANT. ``json`` stores the document as the text it arrived as:
#: every read reparses it, the containment operators (``@>``, ``?``, ``@@``)
#: are not defined for it, and it cannot carry a GIN index — so a filter
#: over a JSON column is a sequential scan with a per-row cast. ``jsonb``
#: stores a parsed, key-sorted binary form that those operators and indexes
#: work on. ``load_dialect_impl`` picks the whole type implementation per
#: dialect — DDL and bind/result processing both — so SQLite, which has
#: neither type and is where all but the tests/database suite run, keeps
#: exactly the plain JSON behaviour it has always had.
#:
#: THE TRADEOFF. jsonb is not free. It does not preserve key order,
#: insignificant whitespace, or duplicate keys, and it costs more on write:
#: the document is parsed on the way in, and an update rewrites the whole
#: value rather than patching it. Every payload behind this alias is
#: machine-generated and read back as a dict, so none of that is
#: load-bearing — but a column that ever needs a byte-exact round trip
#: wants ``sa.JSON`` spelled out, not this.
#: SANITISED ON THE WAY IN. NUL is removed from every string in the document
#: before it is bound — see ``_strip_nul``. This lives on the type rather than
#: at the endpoints because all fifteen jsonb columns share the exposure, and
#: patching the one route we happened to find would leave the other fourteen
#: waiting for someone to notice. SQLite is sanitised too, even though it would
#: accept the NUL: a dev database that stores what production rejects is the
#: precise class of drift the tests/database suite exists to prevent.
class _JsonColType(TypeDecorator):
    """JSONB on postgres, JSON (TEXT) on SQLite, NUL-safe on both."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        inner = JSONB() if dialect.name == "postgresql" else JSON()
        return dialect.type_descriptor(inner)

    def process_bind_param(self, value, dialect):
        return _strip_nul(value)


JsonCol = _JsonColType()

# UuidCol uses Postgres UUID natively and TEXT on SQLite.
UuidCol = Uuid

__all__ = ["JsonCol", "UuidCol"]
