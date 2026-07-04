"""Whole-API smoke: sweep every GET route + drive the core user flows.

Two passes against a running local backend:

1. **Spec sweep** — every parameterless GET in /openapi.json is called with
   an admin token. Success = anything but a 5xx (a semantically-correct 4xx
   is fine; a crash is not).
2. **Flows** — read journeys: search → card detail → watchlist → price
   alert (via upstream id materialization) → sealed → analytics/home.
3. **Journeys** — write lifecycles: register/login/change-password,
   profile + settings + recents, vault (grade → collection → items),
   sealed holdings CRUD, scanner + scan-job pipeline, statement
   generation, identify-by-text, waitlist.

Usage (backend running on :8000 with ADMIN_EMAILS set to SMOKE_EMAIL):

    SMOKE_EMAIL=you@example.com .venv/bin/python scripts/api_smoke.py
"""

from __future__ import annotations

import os
import uuid

import httpx

BASE = os.environ.get("SMOKE_BASE", "http://localhost:8000")
OWNER = os.environ.get("SMOKE_EMAIL", "ninjeff06@gmail.com")

client = httpx.Client(base_url=BASE, timeout=45.0)
TOKEN = ""
failures: list[str] = []
warnings: list[str] = []


def auth() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def login() -> None:
    global TOKEN
    resp = client.post(
        "/v1/auth/dev-login", json={"email": OWNER, "display_name": "Smoke"}
    )
    resp.raise_for_status()
    TOKEN = resp.json()["data"]["access_token"]


# ── Pass 1: spec sweep ────────────────────────────────────────────────────

# Parameterless GETs that legitimately aren't plain-JSON 200s for an admin.
SKIP = {
    "/health",  # covered implicitly by reaching this point
}


def sweep() -> None:
    spec = client.get("/openapi.json").json()
    routes = sorted(
        path
        for path, methods in spec["paths"].items()
        if "get" in methods and "{" not in path and path not in SKIP
    )
    print(f"── spec sweep: {len(routes)} parameterless GET routes")
    for path in routes:
        try:
            resp = client.get(path, headers=auth())
        except Exception as exc:
            failures.append(f"GET {path} → transport error: {exc}")
            print(f"  ✗ {path} transport error")
            continue
        if resp.status_code >= 500:
            failures.append(f"GET {path} → {resp.status_code}: {resp.text[:160]}")
            print(f"  ✗ {path} → {resp.status_code}")
        elif resp.status_code >= 400:
            warnings.append(f"GET {path} → {resp.status_code}")
            print(f"  ~ {path} → {resp.status_code}")
        else:
            print(f"  ✓ {path}")


# ── Pass 2: flows ─────────────────────────────────────────────────────────


def get(path: str, label: str, *, ok=(200,)) -> dict | list | None:
    resp = client.get(path, headers=auth())
    if resp.status_code in ok:
        print(f"  ✓ {label}")
        try:
            body = resp.json()
        except Exception:  # binary bodies (e.g. the image proxy) are a pass
            return {}
        return body.get("data") if isinstance(body, dict) else body
    failures.append(f"{label}: GET {path} → {resp.status_code}: {resp.text[:160]}")
    print(f"  ✗ {label} → {resp.status_code}")
    return None


def _write(method: str, path: str, label: str, json: dict | None, ok, headers=None):
    resp = client.request(method, path, headers=headers or auth(), json=json)
    if resp.status_code in ok:
        print(f"  ✓ {label}")
        try:
            body = resp.json()
            return body.get("data") if isinstance(body, dict) else body
        except Exception:
            return {}
    failures.append(f"{label}: {method} {path} → {resp.status_code}: {resp.text[:160]}")
    print(f"  ✗ {label} → {resp.status_code}")
    return None


def post(path: str, label: str, json: dict, *, ok=(200, 201, 204), headers=None):
    return _write("POST", path, label, json, ok, headers)


def patch(path: str, label: str, json: dict, *, ok=(200,)):
    return _write("PATCH", path, label, json, ok)


def put(path: str, label: str, json: dict, *, ok=(200, 204)):
    return _write("PUT", path, label, json, ok)


def delete(path: str, label: str, *, ok=(200, 204)):
    return _write("DELETE", path, label, None, ok)


_SEARCH_HIT: str | None = None  # composite upstream id shared with journeys()


def flows() -> None:
    global _SEARCH_HIT
    print("── flows")

    # Search → the id everything else hangs off.
    found = get("/v1/cards/search?q=charizard&tcg=pokemon&limit=5", "live search")
    results = (found or {}).get("results") or []
    if not results:
        failures.append("live search returned no results — flows aborted")
        return
    card = results[0]
    cid = card["id"]
    _SEARCH_HIT = cid

    get(f"/v1/cards/{cid}", "card detail (composite id)")
    get(f"/v1/cards/{cid}/prices", "price history")
    get(f"/v1/cards/{cid}/analytics", "market analytics", ok=(200, 404))
    get(f"/v1/cards/{cid}/marketplace-prices", "marketplace prices")
    get(f"/v1/cards/{cid}/canonical", "canonical card", ok=(200, 404))
    get(f"/v1/cards/{cid}/comps", "sale comps", ok=(200, 404))
    get(f"/v1/cards/{cid}/grade-summary", "grade summary", ok=(200, 404))
    get(f"/v1/cards/{cid}/listings", "live listings", ok=(200, 404))
    get(f"/v1/cards/{cid}/market", "card market", ok=(200, 404))
    get(f"/v1/cards/{cid}/valuation", "valuation ladder", ok=(200, 404))
    get(
        f"/v1/cards/{cid}/nearby-listings?lat=40.7&lng=-74.0",
        "nearby listings",
        ok=(200, 404),
    )
    get("/v1/market/indices/psa10/history?range=1M", "psa10 market index")
    image = (card.get("images") or {}).get("small") or card.get("image_url")
    if isinstance(image, dict):
        image = image.get("url")
    if image:
        from urllib.parse import quote

        get(f"/v1/img?u={quote(image, safe='')}", "image proxy (real url)")
    get(f"/v1/public/sparklines?ids={cid}", "public sparklines (real id)")
    get(
        "/v1/public/search?q=charizard&tcg=pokemon&page=2&page_size=20",
        "deep search p2",
    )
    get(
        "/v1/public/browse?game=pokemon&page=1&page_size=12&sort=newest",
        "browse newest",
    )

    # Price alert via upstream id — materializes a local card.
    alert = post(
        "/v1/alerts",
        "create price alert (upstream id)",
        {"upstream_id": cid, "condition": "above", "threshold_usd": 10},
    )
    get("/v1/alerts", "list alerts")
    if alert and alert.get("card_id"):
        local_id = alert["card_id"]
        post("/v1/watchlist", "watch materialized card", {"card_id": local_id})
        get("/v1/watchlist", "list watchlist")
        get(f"/v1/cards/{local_id}/ownership", "ownership block", ok=(200, 404))
        get(f"/v1/prices?card_id={local_id}", "graded price ladder (real id)")
        delete(f"/v1/watchlist/{local_id}", "unwatch card")
        if alert.get("id"):
            delete(f"/v1/alerts/{alert['id']}", "delete alert")

    # Sealed.
    sealed = get("/v1/sealed/search?limit=3", "sealed search")
    rows = sealed if isinstance(sealed, list) else (sealed or {}).get("results") or []
    if rows:
        sid = rows[0]["id"]
        get(f"/v1/sealed/{sid}", "sealed detail")
        get(f"/v1/sealed/{sid}/market", "sealed market")

    # Signed-in surfaces.
    get("/v1/home/feed", "home feed")
    get("/v1/analytics/overview", "portfolio analytics")
    get("/v1/reports", "statements list")
    get("/v1/collections", "collections")
    get("/v1/grades", "graded cards")
    get("/v1/me/entitlements", "entitlements")
    get("/v1/sets?tcg=pokemon&limit=5", "sets explorer")


# ── Pass 3: write journeys ────────────────────────────────────────────────


def journeys() -> None:
    print("── journeys")
    tag = uuid.uuid4().hex[:8]

    # Auth lifecycle: the exact path that caused the worst historical outage
    # ("Couldn't save" on register), so it gets first-class coverage.
    email = f"smoke-{tag}@example.com"
    pw1, pw2 = "Sm0ke-Pass-123!", "Sm0ke-Pass-456!"
    signup = post(
        "/v1/auth/register",
        "register new user",
        {"email": email, "password": pw1, "display_name": "Smoke Journey"},
        ok=(200, 201),
    )
    user_headers = None
    if signup and signup.get("access_token"):
        user_headers = {"Authorization": f"Bearer {signup['access_token']}"}
    login2 = post(
        "/v1/auth/login", "login with password", {"email": email, "password": pw1}
    )
    if login2 and login2.get("access_token"):
        user_headers = {"Authorization": f"Bearer {login2['access_token']}"}
    if user_headers:
        post(
            "/v1/auth/change-password",
            "change password",
            {"current_password": pw1, "new_password": pw2},
            headers=user_headers,
        )
        relog = post(
            "/v1/auth/login",
            "login with new password",
            {"email": email, "password": pw2},
        )
        if relog and relog.get("refresh_token"):
            post(
                "/v1/auth/refresh",
                "refresh token",
                {"refresh_token": relog["refresh_token"]},
            )
        old = client.get("/v1/me", headers=user_headers)
        if old.status_code == 401:
            print("  ✓ old session revoked after password change")
        else:
            failures.append(
                f"old session still valid after password change → {old.status_code}"
            )
            print(f"  ✗ old session NOT revoked → {old.status_code}")

    # Profile / settings / recents (as the admin smoke user).
    patch("/v1/me", "update profile", {"display_name": "Smoke Prime"})
    patch("/v1/me/settings", "update settings", {"push_notifications_enabled": True})
    put(
        "/v1/me/recents",
        "sync recents",
        {"searches": ["charizard"], "viewed": []},
    )

    # Vault: grade a card via upstream id, organize it into a collection.
    graded = post(
        "/v1/grades",
        "add graded card (upstream id)",
        {
            "upstream_id": _SEARCH_HIT or "",
            "grade": 9,
            "house": "psa",
            "purchase_price_usd": 50,
        },
        ok=(200, 201),
    )
    if graded and graded.get("id"):
        gid = graded["id"]
        patch(f"/v1/grades/{gid}", "edit graded card", {"notes": "smoke pass"})
        coll = post(
            "/v1/collections",
            "create collection",
            {"name": f"Smoke Binder {tag}", "description": "hard-pass"},
            ok=(200, 201),
        )
        if coll and coll.get("id"):
            post(
                f"/v1/collections/{coll['id']}/items",
                "add card to collection",
                {"graded_card_id": gid},
                ok=(200, 201, 204),
            )
            delete(
                f"/v1/collections/{coll['id']}/items/{gid}",
                "remove card from collection",
            )
            delete(f"/v1/collections/{coll['id']}", "delete collection")
        get("/v1/grades/summary", "vault summary (with data)")
        get("/v1/analytics/overview", "portfolio analytics (with data)", ok=(200,))

    # Sealed holdings on the seeded catalog.
    sealed = get("/v1/sealed/search?limit=1", "sealed search (seeded)")
    rows = sealed if isinstance(sealed, list) else (sealed or {}).get("results") or []
    if rows:
        pid = rows[0]["id"]
        holding = post(
            "/v1/sealed-holdings",
            "create sealed holding",
            {"product_id": pid, "quantity": 1, "purchase_price_usd": 99.99},
            ok=(200, 201),
        )
        if holding and holding.get("id"):
            patch(
                f"/v1/sealed-holdings/{holding['id']}",
                "update holding qty",
                {"quantity": 2},
            )
            get("/v1/sealed-holdings", "list holdings")
            delete(f"/v1/sealed-holdings/{holding['id']}", "delete holding")

    # Scanner + scan-job pipeline.
    scanner = post(
        "/v1/scanners",
        "register scanner",
        {"device_id": f"smoke-{tag}", "name": "Smoke Scanner"},
        ok=(200, 201),
    )
    if scanner and scanner.get("id"):
        post(f"/v1/scanners/{scanner['id']}/heartbeat", "scanner heartbeat", {})
    scan = post(
        "/v1/scans", "create scan job", {"angles": ["front", "back"]}, ok=(200, 201)
    )
    job = (scan or {}).get("job") or {}
    if job.get("id"):
        post(
            f"/v1/scans/{job['id']}/complete",
            "complete scan job",
            {"uploaded_angles": ["front", "back"]},
        )
        delete(f"/v1/scans/{job['id']}", "delete scan job")
    if scanner and scanner.get("id"):
        delete(f"/v1/scanners/{scanner['id']}", "delete scanner")

    # Statement generation for last month, then its artifacts.
    report = post(
        "/v1/reports",
        "generate statement",
        {"period": "monthly", "year": 2026, "month": 6},
        ok=(200, 201, 409),
    )
    if report and report.get("id"):
        detail = get(f"/v1/reports/{report['id']}", "statement detail")
        if detail and detail.get("status") == "ready":
            get(f"/v1/reports/{report['id']}/download", "statement download link")
        else:
            # No object storage in the scratch env — the PDF renders but the
            # upload fails, so the row is (correctly) marked failed and
            # download (correctly) 409s. Verified separately in prod.
            warnings.append(
                "statement PDF upload skipped — no object storage in this env "
                f"(report status={detail.get('status') if detail else '?'})"
            )
            print("  ~ statement PDF not stored (no object storage in this env)")

    # Identify by text (the no-photo path).
    post(
        "/v1/cards/identify/text",
        "identify by text",
        {"text": "Charizard ex 125/197", "tcg": "pokemon"},
        ok=(200, 404),
    )

    # Waitlist signup (public).
    post(
        "/v1/waitlist",
        "join waitlist",
        {"email": f"smoke-wait-{tag}@example.com"},
        ok=(200, 201),
    )


# ── Pass 4: admin operations ──────────────────────────────────────────────


def admin_ops() -> None:
    print("── admin ops")
    tag = uuid.uuid4().hex[:8]

    # Feature-flag lifecycle.
    flag = post(
        "/v1/admin/flags",
        "create feature flag",
        {"key": f"smoke_{tag}", "label": "Smoke flag", "enabled": True},
        ok=(200, 201),
    )
    if flag and flag.get("id"):
        patch(f"/v1/admin/flags/{flag['id']}", "toggle flag off", {"enabled": False})
        delete(f"/v1/admin/flags/{flag['id']}", "delete flag")

    # Blog: publish in the portal, read it on the public site, retract.
    blog = post(
        "/v1/admin/blog",
        "publish blog post",
        {
            "title": f"Smoke Dispatch {tag}",
            "excerpt": "Hard-pass coverage.",
            "body": "Every endpoint, exercised.",
            "status": "published",
        },
        ok=(200, 201),
    )
    if blog and blog.get("slug"):
        get(f"/v1/blog/posts/{blog['slug']}", "read blog post (public)")
    if blog and blog.get("id"):
        delete(f"/v1/admin/blog/{blog['id']}", "delete blog post")

    # Careers: open a role, apply as a candidate, advance the application.
    job = post(
        "/v1/admin/jobs",
        "open job posting",
        {
            "title": f"Smoke Engineer {tag}",
            "team": "Platform",
            "location": "Remote",
            "summary": "Exercise the pipeline.",
            "status": "open",
        },
        ok=(200, 201),
    )
    if job and job.get("id"):
        app_row = post(
            f"/v1/careers/jobs/{job['id']}/apply",
            "apply to job (public)",
            {
                "applicant_name": "Smoke Candidate",
                "applicant_email": f"smoke-app-{tag}@example.com",
            },
            ok=(200, 201),
        )
        if app_row and app_row.get("id"):
            patch(
                f"/v1/admin/applications/{app_row['id']}/status",
                "advance application",
                {"status": "reviewing", "notify": False},
            )
        delete(f"/v1/admin/jobs/{job['id']}", "close job posting")

    # Site announcement banner on, then off.
    patch(
        "/v1/admin/config/announcement",
        "set site announcement",
        {"enabled": True, "message": "Smoke test banner", "tone": "info"},
    )
    patch(
        "/v1/admin/config/announcement", "clear site announcement", {"enabled": False}
    )

    # User moderation on a throwaway account.
    mod_email = f"smoke-mod-{tag}@example.com"
    post(
        "/v1/auth/register",
        "create moderation target",
        {
            "email": mod_email,
            "password": "Sm0ke-Pass-123!",
            "display_name": "Mod Target",
        },
        ok=(200, 201),
    )
    users = get(f"/v1/admin/users?q={mod_email}", "find user in admin")
    rows = (users or {}).get("results") or []
    if rows:
        uid = rows[0]["id"]
        get(f"/v1/admin/users/{uid}", "admin user detail")
        post(f"/v1/admin/users/{uid}/ban", "ban user", {"reason": "smoke"})
        post(f"/v1/admin/users/{uid}/unban", "unban user", {})
        post(f"/v1/admin/users/{uid}/revoke-sessions", "revoke sessions", {})
        patch(f"/v1/admin/users/{uid}/role", "grant admin role", {"is_admin": True})
        patch(f"/v1/admin/users/{uid}/role", "revoke admin role", {"is_admin": False})
        delete(f"/v1/admin/users/{uid}", "delete user")


def main() -> int:
    login()
    sweep()
    flows()
    journeys()
    admin_ops()
    print(f"\n── {len(failures)} failures, {len(warnings)} warnings (4xx)")
    for f in failures:
        print("  FAIL", f)
    for w in warnings:
        print("  warn", w)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
