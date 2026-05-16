"""Scanner CRUD tests."""

import pytest


@pytest.mark.asyncio
async def test_pair_and_list_scanner(client, auth_headers):
    resp = await client.post(
        "/v1/scanners",
        headers=auth_headers,
        json={"device_id": "loupe-001", "name": "Desk"},
    )
    assert resp.status_code == 201, resp.text
    scanner_id = resp.json()["id"]

    list_resp = await client.get("/v1/scanners", headers=auth_headers)
    assert list_resp.status_code == 200
    assert any(s["id"] == scanner_id for s in list_resp.json())


@pytest.mark.asyncio
async def test_heartbeat_updates_timestamp(client, auth_headers):
    pair_resp = await client.post(
        "/v1/scanners", headers=auth_headers, json={"device_id": "loupe-hb", "name": "Heartbeat"}
    )
    scanner_id = pair_resp.json()["id"]
    hb = await client.post(
        f"/v1/scanners/{scanner_id}/heartbeat",
        headers=auth_headers,
        json={"firmware_version": "1.2.3"},
    )
    assert hb.status_code == 200
    body = hb.json()
    assert body["last_seen_at"] is not None
    assert body["firmware_version"] == "1.2.3"


@pytest.mark.asyncio
async def test_delete_scanner(client, auth_headers):
    pair_resp = await client.post(
        "/v1/scanners", headers=auth_headers, json={"device_id": "loupe-del", "name": "Delete me"}
    )
    scanner_id = pair_resp.json()["id"]
    resp = await client.delete(f"/v1/scanners/{scanner_id}", headers=auth_headers)
    assert resp.status_code == 204
    again = await client.get(f"/v1/scanners/{scanner_id}", headers=auth_headers)
    assert again.status_code == 404
