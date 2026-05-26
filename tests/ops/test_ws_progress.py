"""WebSocket progress smoke test (auth required).

Every WS frame carries the universal ``{type, ts, request_id, data}``
envelope so the frontend can validate frames against a single contract.
"""

import pytest

from app.auth.jwt import issue_token
from app.platform.cache_config import SCAN_PUBSUB_CHANNEL


@pytest.mark.asyncio
async def test_ws_rejects_invalid_token():
    from starlette.testclient import TestClient

    from app.main import create_app

    app = create_app()
    with TestClient(app) as tc, pytest.raises(Exception):  # closed with 1008
        with tc.websocket_connect("/ws/scans?token=garbage"):
            pass


@pytest.mark.asyncio
async def test_ws_hello_frame(created_user):
    from starlette.testclient import TestClient

    from app.main import create_app

    app = create_app()
    token, _ = issue_token(created_user.id, "access")

    with TestClient(app) as tc, tc.websocket_connect(f"/ws/scans?token={token}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        # universal WS envelope shape
        assert "ts" in hello
        assert "request_id" in hello
        assert isinstance(hello["data"], dict)
        assert hello["data"]["user_id"] == str(created_user.id)


def test_pubsub_channel_format(created_user):
    assert SCAN_PUBSUB_CHANNEL.format(user_id=created_user.id).startswith(
        "loupe:scans:user:"
    )
