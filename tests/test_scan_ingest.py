"""End-to-end scan-ingest test (uses the in-memory _StubS3 fallback)."""

import pytest

from tests.conftest import assert_envelope_error, assert_envelope_ok


@pytest.mark.asyncio
async def test_create_and_complete_scan(client, auth_headers):
    create = await client.post(
        "/v1/scans",
        headers=auth_headers,
        json={"angles": ["front", "back", "top", "bottom"]},
    )
    body = assert_envelope_ok(create, expected_status=201)
    job = body["job"]
    assert job["status"] in ("queued", "uploading")
    assert len(body["uploads"]) == 4
    for u in body["uploads"]:
        assert u["upload_url"].startswith("http")
        assert u["s3_key"].endswith(".jpg")

    job_id = job["id"]
    complete = await client.post(
        f"/v1/scans/{job_id}/complete",
        headers=auth_headers,
        json={"uploaded_angles": ["front", "back", "top", "bottom"]},
    )
    cbody = assert_envelope_ok(complete)
    assert cbody["status"] in ("complete", "processing")

    listing = await client.get("/v1/scans", headers=auth_headers)
    list_data = assert_envelope_ok(listing)
    assert any(s["id"] == job_id for s in list_data)


@pytest.mark.asyncio
async def test_get_unknown_scan_404(client, auth_headers):
    import uuid

    resp = await client.get(f"/v1/scans/{uuid.uuid4()}", headers=auth_headers)
    assert_envelope_error(resp, expected_status=404)
