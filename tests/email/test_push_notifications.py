"""Push notifications: device registry, Expo transport, alert trigger."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.push_token import PushToken
from app.models.user import UserSettings
from app.services import push_service
from tests.factories import make_user


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body or {}
        self.text = f"status {status_code}"

    def json(self):
        return self._body


class _FakeClient:
    calls: list[dict] = []
    script: list[_FakeResponse] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        _FakeClient.calls.append({"url": url, "json": json})
        return _FakeClient.script.pop(0)


@pytest.fixture
def expo(monkeypatch):
    monkeypatch.setattr(push_service.httpx, "AsyncClient", _FakeClient)
    _FakeClient.calls = []
    _FakeClient.script = []
    return _FakeClient


# ── Registry endpoints ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_is_an_idempotent_upsert(
    client, created_user, auth_headers, db_session
):
    body = {"token": "ExponentPushToken[abc123]", "platform": "ios"}
    assert (
        await client.post("/v1/me/push-tokens", json=body, headers=auth_headers)
    ).status_code == 204
    assert (
        await client.post("/v1/me/push-tokens", json=body, headers=auth_headers)
    ).status_code == 204

    rows = (await db_session.execute(select(PushToken))).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_id == created_user.id

    # Unregister removes it.
    resp = await client.delete(
        "/v1/me/push-tokens/ExponentPushToken[abc123]", headers=auth_headers
    )
    assert resp.status_code == 204
    assert (await db_session.execute(select(PushToken))).scalars().all() == []


# ── Transport ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_pushes_to_every_device_and_prunes_dead_ones(db_session, expo):
    user = await make_user(db_session)
    db_session.add_all(
        [
            PushToken(user_id=user.id, token="ExponentPushToken[live1]"),
            PushToken(user_id=user.id, token="ExponentPushToken[dead1]"),
        ]
    )
    await db_session.commit()

    expo.script = [
        _FakeResponse(
            200,
            {
                "data": [
                    {"status": "ok"},
                    {"status": "error", "details": {"error": "DeviceNotRegistered"}},
                ]
            },
        )
    ]
    accepted = await push_service.send_to_user(
        user.id, title="Hi", body="There", data={"k": "v"}
    )
    assert accepted == 1
    sent = expo.calls[0]["json"]
    assert {m["to"] for m in sent} == {
        "ExponentPushToken[live1]",
        "ExponentPushToken[dead1]",
    }
    assert sent[0]["title"] == "Hi"

    # The dead token was pruned; the live one remains.
    left = (await db_session.execute(select(PushToken.token))).scalars().all()
    assert left == ["ExponentPushToken[live1]"]


@pytest.mark.asyncio
async def test_push_respects_the_user_setting(db_session, expo):
    user = await make_user(db_session)
    db_session.add(PushToken(user_id=user.id, token="ExponentPushToken[x1]"))
    settings_row = (
        await db_session.execute(
            select(UserSettings).where(UserSettings.user_id == user.id)
        )
    ).scalar_one()
    settings_row.push_notifications_enabled = False
    await db_session.commit()

    accepted = await push_service.send_to_user(user.id, title="t", body="b")
    assert accepted == 0
    assert expo.calls == []  # never reached the provider


@pytest.mark.asyncio
async def test_price_alert_push_formats_the_move(db_session, expo):
    user = await make_user(db_session)
    db_session.add(PushToken(user_id=user.id, token="ExponentPushToken[p1]"))
    await db_session.commit()

    expo.script = [_FakeResponse(200, {"data": [{"status": "ok"}]})]
    accepted = await push_service.send_price_alert_push(
        user.id,
        card_name="Charizard ex",
        condition="above",
        price_usd=262.35,
        threshold_usd=250.0,
        card_id="card-1",
    )
    assert accepted == 1
    msg = expo.calls[0]["json"][0]
    assert msg["title"] == "▲ Charizard ex — $262.35"
    assert "climbed above your $250.00 alert" in msg["body"]
    assert msg["data"] == {"type": "price_alert", "cardId": "card-1"}
