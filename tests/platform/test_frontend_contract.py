"""Frontend ↔ backend wire contract tests.

For every endpoint the React Native app consumes, assert:

1. The envelope shape (``data`` / ``meta`` / ``pagination`` / ``error``).
2. The **exact** set of keys the frontend reads from ``data``
   (cross-referenced with ``loupe-frontend/src/api/types.ts`` and the
   matching React hook / component).
3. The data type of each key (``str`` / ``int`` / ``float`` / list / dict).
4. The numerical correctness of derived values (totals, deltas, point
   counts) given a deterministic seeded vault.

If a frontend component renders ``null`` or "—" when a field is missing,
the test asserts the key exists and is ``None`` rather than missing — a
missing key would silently break the component.

Seeded data per test
--------------------
The ``seeded_vault`` fixture builds a single user with:

* 1 ``Scanner`` (so ``GET /v1/scanners/status`` returns a real device).
* 2 ``Card`` rows whose ``metadata['price_history']`` carries 30 daily
  ``{date, priceUsd}`` points (so ``/v1/grades/history`` and
  ``/v1/grades/sparklines`` return non-empty series).
* 2 ``GradedCard`` rows owned by that user (one per card) with known
  ``estimated_value_usd`` so portfolio totals are exact integers.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.orm.attributes import flag_modified

from app.models.card import Card, CardSet
from app.models.enums import GradeHouseEnum, ScannerTransportEnum, TcgEnum
from app.models.grade import GradedCard
from app.models.scanner import Scanner
from app.models.user import User, UserSettings
from tests.conftest import assert_envelope_ok

# ---- Fixture -----------------------------------------------------------


def _price_history(anchor: float, days: int = 30) -> list[dict]:
    """Deterministic linear walk that ends at *anchor* on the last day."""
    today = datetime.now(UTC).date()
    start = today - timedelta(days=days - 1)
    # Start at 80% of anchor, finish at anchor — yields a known +25% delta.
    step = (anchor * 0.25) / max(days - 1, 1)
    out: list[dict] = []
    for i in range(days):
        out.append(
            {
                "date": (start + timedelta(days=i)).isoformat(),
                "priceUsd": round(anchor * 0.8 + step * i, 2),
            }
        )
    return out


@pytest_asyncio.fixture
async def seeded_vault(db_session) -> dict:
    """Create user + scanner + 2 cards + 2 graded cards."""
    user = User(
        email=f"contract+{uuid.uuid4().hex[:8]}@example.com",
        display_name="Contract Tester",
        apple_subject=f"apple-{uuid.uuid4().hex}",
    )
    db_session.add(user)
    await db_session.flush()
    db_session.add(UserSettings(user_id=user.id))

    cset = CardSet(
        tcg=TcgEnum.pokemon,
        name="Contract Set",
        code=f"CT-{uuid.uuid4().hex[:4]}",
    )
    db_session.add(cset)
    await db_session.flush()

    cards: list[Card] = []
    anchors = [100.0, 50.0]
    for idx, anchor in enumerate(anchors):
        c = Card(
            set_id=cset.id,
            tcg=TcgEnum.pokemon,
            name=f"Test Card {idx + 1}",
            number=str(idx + 1),
            rarity="Rare",
            year=2024,
            image_url=f"https://img.example/{idx + 1}.png",
        )
        c.card_metadata = {"price_history": _price_history(anchor)}
        flag_modified(c, "card_metadata")
        db_session.add(c)
        cards.append(c)
    await db_session.flush()

    scanner = Scanner(
        owner_id=user.id,
        device_id=f"loupe-{uuid.uuid4().hex[:8]}",
        name="Contract Scanner",
        firmware_version="1.0.0",
        transport=ScannerTransportEnum.ble,
        last_seen_at=datetime.now(UTC) - timedelta(minutes=2),
        is_active=True,
    )
    db_session.add(scanner)

    graded_at = datetime.now(UTC) - timedelta(days=10)
    grades = [
        GradedCard(
            user_id=user.id,
            card_id=cards[0].id,
            grade=Decimal("9.0"),
            house=GradeHouseEnum.psa,
            estimated_value_usd=Decimal("100.00"),
            graded_at=graded_at,
            subgrades={"centering": 9.0, "corners": 9.0, "edges": 9.0, "surface": 9.0},
        ),
        GradedCard(
            user_id=user.id,
            card_id=cards[1].id,
            grade=Decimal("8.0"),
            house=GradeHouseEnum.psa,
            estimated_value_usd=Decimal("50.00"),
            graded_at=graded_at,
        ),
    ]
    db_session.add_all(grades)
    await db_session.commit()
    for c in cards:
        await db_session.refresh(c)
    for g in grades:
        await db_session.refresh(g)
    await db_session.refresh(user)
    await db_session.refresh(scanner)

    return {
        "user": user,
        "cards": cards,
        "grades": grades,
        "scanner": scanner,
    }


@pytest.fixture
def vault_headers(seeded_vault) -> dict[str, str]:
    from app.auth.jwt import issue_token

    token, _ = issue_token(seeded_vault["user"].id, "access")
    return {"Authorization": f"Bearer {token}"}


# ---- Generic envelope sanity ------------------------------------------


def _assert_meta_shape(payload: dict) -> None:
    meta = payload["meta"]
    assert isinstance(meta["request_id"], str) and meta["request_id"]
    assert isinstance(meta["timestamp"], str) and meta["timestamp"].endswith("Z")
    assert meta["version"] == "v1"
    assert meta["duration_ms"] is None or isinstance(meta["duration_ms"], int | float)


def _assert_keys(obj: dict, required: set[str], *, label: str) -> None:
    missing = required - obj.keys()
    assert not missing, f"{label}: missing keys {missing}; got {sorted(obj.keys())}"


# ---- /health -----------------------------------------------------------


@pytest.mark.asyncio
async def test_health_contract(client):
    resp = await client.get("/health")
    # /health is not enveloped (system endpoint).
    assert resp.status_code == 200
    body = resp.json()
    # `useApiHealth` reads `.status` / `.timestamp` off this response.
    assert isinstance(body, dict)
    assert "status" in body, body


# ---- /v1/me ------------------------------------------------------------


@pytest.mark.asyncio
async def test_me_contract(client, seeded_vault, vault_headers):
    resp = await client.get("/v1/me", headers=vault_headers)
    data = assert_envelope_ok(resp)
    _assert_meta_shape(resp.json())
    # `User` interface in src/api/types.ts.
    _assert_keys(
        data,
        {"id", "email", "display_name", "avatar_url", "created_at"},
        label="GET /v1/me",
    )
    assert data["id"] == str(seeded_vault["user"].id)
    assert data["email"] == seeded_vault["user"].email


@pytest.mark.asyncio
async def test_me_settings_contract(client, vault_headers):
    resp = await client.get("/v1/me/settings", headers=vault_headers)
    data = assert_envelope_ok(resp)
    # `UserSettings` interface in src/api/types.ts.
    _assert_keys(
        data,
        {
            "currency",
            "theme",
            "live_sync_enabled",
            "push_notifications_enabled",
            "updated_at",
        },
        label="GET /v1/me/settings",
    )
    assert data["theme"] in {"system", "light", "dark"}
    assert isinstance(data["live_sync_enabled"], bool)
    assert isinstance(data["push_notifications_enabled"], bool)


# ---- /v1/grades --------------------------------------------------------


@pytest.mark.asyncio
async def test_grades_list_contract(client, seeded_vault, vault_headers):
    """Vault screen reads this. forensicApi.toCollectionCard depends on
    every key under `GradedCardWire`."""
    resp = await client.get("/v1/grades", headers=vault_headers)
    data = assert_envelope_ok(resp)
    assert isinstance(data, list)
    assert len(data) == 2, f"expected 2 grades, got {len(data)}"
    required = {
        "id",
        "user_id",
        "card_id",
        "scan_job_id",
        "grade",
        "house",
        "subgrades",
        "estimated_value_usd",
        "fingerprint_hash",
        "notes",
        "graded_at",
        "created_at",
        "updated_at",
        # Joined card fields — `forensicApi.toCollectionCard` reads each.
        "card_name",
        "card_image_url",
        "card_number",
        "card_set_name",
        "card_year",
        "card_tcg",
    }
    for row in data:
        _assert_keys(row, required, label="GET /v1/grades[*]")
        # Decimal-as-string per CONTRACT.md.
        assert isinstance(row["grade"], str), row["grade"]
        # Coercion the frontend does at the boundary.
        assert 0.0 <= float(row["grade"]) <= 10.0
        assert row["estimated_value_usd"] is None or isinstance(
            row["estimated_value_usd"], str
        )
        assert row["house"] in {"psa", "cgc", "bgs", "sgc", "tag", "loupe"}
        assert row["card_name"] is not None
        assert row["card_image_url"] is not None


@pytest.mark.asyncio
async def test_grades_summary_contract(client, vault_headers):
    """Command Center hero card reads `totalValueUsd`, `cardCount`,
    `avgGrade`, `avgAccuracy`."""
    resp = await client.get("/v1/grades/summary", headers=vault_headers)
    data = assert_envelope_ok(resp)
    _assert_keys(
        data,
        {"totalValueUsd", "cardCount", "avgGrade", "avgAccuracy"},
        label="GET /v1/grades/summary",
    )
    # 100 + 50 from the seeded vault.
    assert data["totalValueUsd"] == pytest.approx(150.0)
    assert data["cardCount"] == 2
    # (9.0 + 8.0) / 2.
    assert data["avgGrade"] == pytest.approx(8.5)
    # Backend explicitly returns null (UI shows "—"). Key must exist.
    assert data["avgAccuracy"] is None


@pytest.mark.asyncio
async def test_grades_history_contract(client, vault_headers):
    """Command Center chart reads `points[].date` and `points[].priceUsd`,
    plus the header reads `deltaUsd` / `deltaPct`."""
    resp = await client.get("/v1/grades/history?range=1M", headers=vault_headers)
    data = assert_envelope_ok(resp)
    _assert_keys(
        data,
        {"range", "points", "deltaUsd", "deltaPct"},
        label="GET /v1/grades/history",
    )
    assert data["range"] == "1M"
    assert isinstance(data["points"], list)
    assert len(data["points"]) > 0, (
        "1M range with seeded price_history should not be empty"
    )
    for p in data["points"]:
        _assert_keys(p, {"date", "priceUsd"}, label="history.points[*]")
        assert isinstance(p["date"], str)
        assert isinstance(p["priceUsd"], int | float)
    # Seeded walk ends at the anchor → ~150 USD on the final day.
    assert data["points"][-1]["priceUsd"] == pytest.approx(150.0, rel=0.05)
    # Walk goes 80% → 100% per card so deltaPct must be a meaningful
    # positive number. We don't pin the exact value because the service
    # owns the range/sampling math.
    assert 10.0 < data["deltaPct"] < 60.0, data["deltaPct"]
    assert isinstance(data["deltaUsd"], int | float)
    assert data["deltaUsd"] > 0


@pytest.mark.asyncio
async def test_grades_sparklines_contract(client, vault_headers):
    """Top Movers reads `cardId`, `points`, `deltaPct` per row."""
    resp = await client.get("/v1/grades/sparklines", headers=vault_headers)
    data = assert_envelope_ok(resp)
    assert isinstance(data, list)
    assert len(data) == 2, f"expected one sparkline per graded card, got {len(data)}"
    for row in data:
        _assert_keys(row, {"cardId", "points", "deltaPct"}, label="sparklines[*]")
        assert isinstance(row["cardId"], str)
        assert isinstance(row["points"], list) and len(row["points"]) > 0
        for n in row["points"]:
            assert isinstance(n, int | float)
        assert isinstance(row["deltaPct"], int | float)
        # Seeded as a monotonically increasing walk → positive delta.
        assert 5.0 < row["deltaPct"] < 60.0, row["deltaPct"]


# ---- /v1/scanners ------------------------------------------------------


@pytest.mark.asyncio
async def test_scanners_list_contract(client, seeded_vault, vault_headers):
    resp = await client.get("/v1/scanners", headers=vault_headers)
    data = assert_envelope_ok(resp)
    assert isinstance(data, list) and len(data) == 1
    row = data[0]
    # `Scanner` interface in src/api/types.ts.
    _assert_keys(
        row,
        {
            "id",
            "device_id",
            "name",
            "firmware_version",
            "transport",
            "is_active",
            "last_seen_at",
            "created_at",
        },
        label="GET /v1/scanners[*]",
    )
    assert row["transport"] in {"ble", "wifi", "offline"}
    assert row["device_id"] == seeded_vault["scanner"].device_id
    assert isinstance(row["is_active"], bool)


@pytest.mark.asyncio
async def test_scanner_status_contract(client, seeded_vault, vault_headers):
    """Command Center scanner widget reads this. Must be non-null for a
    user that has paired a device."""
    resp = await client.get("/v1/scanners/status", headers=vault_headers)
    data = assert_envelope_ok(resp)
    assert data is not None, "/v1/scanners/status returned null for a seeded user"
    _assert_keys(
        data,
        {
            "id",
            "device_id",
            "name",
            "firmware_version",
            "transport",
            "is_active",
            "last_seen_at",
            "created_at",
        },
        label="GET /v1/scanners/status",
    )
    assert data["device_id"] == seeded_vault["scanner"].device_id
    assert data["transport"] == "ble"


@pytest.mark.asyncio
async def test_scanner_status_null_when_unpaired(client, auth_headers):
    """A user with no scanners → ``data: null`` (not 404)."""
    resp = await client.get("/v1/scanners/status", headers=auth_headers)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["data"] is None
    assert payload["error"] is None


# ---- Auth required -----------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/v1/me",
        "/v1/me/settings",
        "/v1/grades",
        "/v1/grades/summary",
        "/v1/grades/history",
        "/v1/grades/sparklines",
        "/v1/scanners",
        "/v1/scanners/status",
    ],
)
@pytest.mark.asyncio
async def test_endpoint_requires_auth(client, path):
    """Every authenticated endpoint must reject anonymous requests with
    an enveloped error, not a stack trace."""
    resp = await client.get(path)
    assert resp.status_code in (401, 403), (path, resp.status_code, resp.text)
    payload = resp.json()
    assert payload.get("error") is not None, payload
    assert payload.get("data") is None
