"""Scanner CRUD tests."""

import pytest

from tests.conftest import assert_envelope_error, assert_envelope_ok


@pytest.mark.asyncio
async def test_pair_and_list_scanner(client, auth_headers):
    resp = await client.post(
        "/v1/scanners",
        headers=auth_headers,
        json={"device_id": "loupe-001", "name": "Desk"},
    )
    created = assert_envelope_ok(resp, expected_status=201)
    scanner_id = created["id"]

    list_resp = await client.get("/v1/scanners", headers=auth_headers)
    listing = assert_envelope_ok(list_resp)
    assert any(s["id"] == scanner_id for s in listing)


@pytest.mark.asyncio
async def test_heartbeat_updates_timestamp(client, auth_headers):
    pair_resp = await client.post(
        "/v1/scanners",
        headers=auth_headers,
        json={"device_id": "loupe-hb", "name": "Heartbeat"},
    )
    scanner = assert_envelope_ok(pair_resp, expected_status=201)
    scanner_id = scanner["id"]
    hb = await client.post(
        f"/v1/scanners/{scanner_id}/heartbeat",
        headers=auth_headers,
        json={"firmware_version": "1.2.3"},
    )
    body = assert_envelope_ok(hb)
    assert body["last_seen_at"] is not None
    assert body["firmware_version"] == "1.2.3"


@pytest.mark.asyncio
async def test_delete_scanner(client, auth_headers):
    pair_resp = await client.post(
        "/v1/scanners",
        headers=auth_headers,
        json={"device_id": "loupe-del", "name": "Delete me"},
    )
    scanner = assert_envelope_ok(pair_resp, expected_status=201)
    scanner_id = scanner["id"]
    resp = await client.delete(f"/v1/scanners/{scanner_id}", headers=auth_headers)
    assert resp.status_code == 204
    again = await client.get(f"/v1/scanners/{scanner_id}", headers=auth_headers)
    assert_envelope_error(again, expected_status=404)
