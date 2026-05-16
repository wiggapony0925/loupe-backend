"""End-to-end scan-ingest test (uses the in-memory _StubS3 fallback)."""

import pytest


@pytest.mark.asyncio
async def test_create_and_complete_scan(client, auth_headers):
    create = await client.post(
        "/v1/scans",
        headers=auth_headers,
        json={"angles": ["front", "back", "top", "bottom"]},
    )
    assert create.status_code == 201, create.text
    body = create.json()
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
    assert complete.status_code == 200, complete.text
    assert complete.json()["status"] in ("complete", "processing")

    listing = await client.get("/v1/scans", headers=auth_headers)
    assert listing.status_code == 200
    assert any(s["id"] == job_id for s in listing.json())


@pytest.mark.asyncio
async def test_get_unknown_scan_404(client, auth_headers):
    import uuid

    resp = await client.get(f"/v1/scans/{uuid.uuid4()}", headers=auth_headers)
    assert resp.status_code == 404
