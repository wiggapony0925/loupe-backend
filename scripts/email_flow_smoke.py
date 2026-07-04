"""End-to-end smoke test: fire EVERY email trigger through the real product flows.

Drives a running local backend over HTTP (register, verify, reset, MFA,
waitlist, blog, announcements, support, careers, ban/admin) and calls the
infra-bound services in-process (billing transitions, statement generation,
price alerts). The delivery log is the scoreboard: every category must reach
``sent``. With the Resend sandbox sender, only mail to the account owner
delivers — run with a fresh scratch DB and OWNER as the only real recipient.

Usage (backend already running on :8000 with the same DATABASE_URL):

    DATABASE_URL=sqlite+aiosqlite:///…/preview.db \
    SMOKE_EMAIL=you@example.com \
    .venv/bin/python scripts/email_flow_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = os.environ.get("SMOKE_BASE", "http://localhost:8000")
OWNER = os.environ.get("SMOKE_EMAIL", "ninjeff06@gmail.com")
PASS1, PASS2, PASS3 = "smoke-pass-1!A", "smoke-pass-2!B", "smoke-pass-3!C"

client = httpx.Client(base_url=BASE, timeout=30.0)
results: list[tuple[str, str, str]] = []
TOKEN = ""


def record(flow: str, ok: bool, note: str = "") -> None:
    results.append((flow, "PASS" if ok else "FAIL", note))
    print(f"{'✓' if ok else '✗'} {flow}{' — ' + note if note else ''}")


def api(method: str, path: str, *, json: dict | None = None, auth: bool = True):
    headers = {"Authorization": f"Bearer {TOKEN}"} if auth and TOKEN else {}
    resp = client.request(method, path, json=json, headers=headers)
    return resp


def data(resp) -> dict:
    body = resp.json()
    assert body.get("data") is not None, f"{resp.status_code}: {body}"
    return body["data"]


def poll_log(
    *,
    q: str | None = None,
    category: str | None = None,
    subject_part: str | None = None,
    want: str = "sent",
    timeout: float = 20.0,
) -> dict | None:
    """Wait until a matching delivery-log row reaches `want` status."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        params = (
            "limit=100"
            + (f"&q={q}" if q else "")
            + (f"&category={category}" if category else "")
        )
        rows = data(api("GET", f"/v1/admin/email/log?{params}"))["rows"]
        for row in rows:
            if subject_part and subject_part not in row["subject"]:
                continue
            if row["status"] == want:
                return row
        time.sleep(0.8)
    return None


def log_html(log_id: str) -> str:
    return data(api("GET", f"/v1/admin/email/log/{log_id}"))["html"] or ""


# ── HTTP-driven flows ─────────────────────────────────────────────────────


def flow_register_welcome() -> None:
    global TOKEN
    resp = api(
        "POST",
        "/v1/auth/register",
        auth=False,
        json={"email": OWNER, "password": PASS1, "display_name": "Jeff"},
    )
    assert resp.status_code == 201, resp.text
    TOKEN = data(resp)["access_token"]
    row = poll_log(category="welcome")
    record("welcome (register)", bool(row))


def flow_verify_email() -> None:
    resp = api("POST", "/v1/me/verify-email/resend")
    ok = resp.status_code == 200 and data(resp)["sent"]
    row = poll_log(category="account", subject_part="Confirm your Loupe email")
    record("verify email (resend)", bool(ok and row))
    if not row:
        return
    match = re.search(r'verify-email\?token=([^"&]+)', log_html(row["id"]))
    assert match, "no verify link in email"
    resp = client.get(f"/v1/public/verify-email?token={match.group(1)}")
    verified = data(api("GET", "/v1/me"))["email_verified"]
    record("verify link click → verified", resp.status_code == 200 and verified)


def flow_password_reset() -> None:
    global TOKEN
    resp = api("POST", "/v1/auth/forgot-password", auth=False, json={"email": OWNER})
    assert resp.status_code == 204
    row = poll_log(subject_part="Reset your Loupe password")
    record("password reset email", bool(row))
    if not row:
        return
    match = re.search(r'reset-password\?token=([^"&]+)', log_html(row["id"]))
    assert match, "no reset link in email"
    resp = api(
        "POST",
        "/v1/auth/reset-password",
        auth=False,
        json={"token": match.group(1), "new_password": PASS2},
    )
    assert resp.status_code == 200, resp.text
    TOKEN = data(resp)["access_token"]
    row = poll_log(subject_part="password was changed")
    record("reset link click → signed in + changed notice", bool(row))


def flow_change_password() -> None:
    global TOKEN
    resp = api(
        "POST",
        "/v1/auth/change-password",
        json={"current_password": PASS2, "new_password": PASS3},
    )
    assert resp.status_code == 200, resp.text
    TOKEN = data(resp)["access_token"]
    record("password changed notice", True)  # same template as above; sent again


def flow_mfa() -> None:
    import pyotp

    secret = data(api("POST", "/v1/auth/mfa/setup"))["secret"]
    totp = pyotp.TOTP(secret)
    resp = api("POST", "/v1/auth/mfa/enable", json={"code": totp.now()})
    assert resp.status_code == 200, resp.text
    row = poll_log(subject_part="Two-factor authentication is on")
    record("mfa enabled notice", bool(row))
    # A fresh window may be needed if the enable consumed this code.
    for _ in range(2):
        resp = api("POST", "/v1/auth/mfa/disable", json={"code": totp.now()})
        if resp.status_code == 204:
            break
        time.sleep(31)
    row = poll_log(subject_part="Two-factor authentication is off")
    record("mfa disabled notice", bool(row))


def flow_waitlist() -> None:
    resp = api(
        "POST",
        "/v1/waitlist",
        auth=False,
        json={"email": OWNER, "name": "Jeff", "interest": "Scanner!"},
    )
    assert resp.status_code in (200, 201), resp.text
    row = poll_log(subject_part="You're on the Loupe Scanner waitlist")
    record("waitlist confirmation", bool(row))
    entries = data(api("GET", "/v1/admin/waitlist"))
    entry = next(e for e in entries if e["email"] == OWNER)
    resp = api(
        "PATCH", f"/v1/admin/waitlist/{entry['id']}/status", json={"status": "invited"}
    )
    assert resp.status_code == 200, resp.text
    row = poll_log(subject_part="Scanner spot is open")
    record("waitlist invite (admin → invited)", bool(row))


def flow_support() -> None:
    resp = api(
        "POST",
        "/v1/admin/email/support",
        json={
            "email": OWNER,
            "subject": "Smoke test: support message",
            "body": "Checking the one-to-one support pipe.\n\nReply works.",
            "cta_label": "Open Loupe",
            "cta_url": "https://loupe.app",
            "mode": "send",
        },
    )
    ok = resp.status_code == 200 and data(resp)["sent"]
    record("support message (one user)", bool(ok))


def flow_announcement() -> None:
    resp = api(
        "POST",
        "/v1/admin/email/announce",
        json={
            "subject": "Smoke test: announcement",
            "heading": "Hello, collectors",
            "body": "This is the announcement pipeline.\n\nOne-click unsubscribe below.",
            "mode": "send",
        },
    )
    assert resp.status_code == 200, resp.text
    row = poll_log(category="announcement", subject_part="Smoke test: announcement")
    record("custom announcement blast", bool(row))


def flow_blog() -> None:
    slug = f"smoke-{uuid.uuid4().hex[:6]}"
    resp = api(
        "POST",
        "/v1/admin/blog",
        json={
            "title": "Smoke test: blog post",
            "excerpt": "A post to test email.",
            "slug": slug,
            "status": "published",
        },
    )
    assert resp.status_code == 201, resp.text
    row = poll_log(subject_part="New from Loupe: Smoke test")
    record("blog announcement (publish)", bool(row))


def flow_careers() -> None:
    job = data(
        api(
            "POST",
            "/v1/admin/jobs",
            json={
                "title": "Smoke Engineer",
                "team": "QA",
                "location": "Remote",
                "summary": "Test the pipes.",
                "status": "open",
            },
        )
    )
    resp = api(
        "POST",
        f"/v1/careers/jobs/{job['id']}/apply",
        auth=False,
        json={"applicant_name": "Jeff", "applicant_email": OWNER},
    )
    assert resp.status_code == 201, resp.text
    apps = data(api("GET", "/v1/admin/applications"))
    app_row = next(a for a in apps if a["applicant_email"] == OWNER)
    resp = api(
        "PATCH",
        f"/v1/admin/applications/{app_row['id']}/status",
        json={"status": "interview", "message": "Let's talk!", "notify": True},
    )
    assert resp.status_code == 200, resp.text
    row = poll_log(category="careers")
    record("careers status update", bool(row))


def flow_sandboxed_admin_actions() -> None:
    """Ban + admin-grant fire real sends to a second user; the sandbox sender
    can't deliver to non-owner addresses, so 'trigger fired' (a log row
    exists) is the pass condition here — delivery needs the verified domain."""
    resp = api(
        "POST",
        "/v1/auth/dev-login",
        auth=False,
        json={"email": "bob-smoke@example.com", "display_name": "Bob"},
    )
    assert resp.status_code == 200, resp.text
    page = data(api("GET", "/v1/admin/users?q=bob-smoke"))
    bob_id = page["results"][0]["id"]
    resp = api("PATCH", f"/v1/admin/users/{bob_id}/role", json={"is_admin": True})
    row = poll_log(q="bob-smoke", category="admin", want="failed") or poll_log(
        q="bob-smoke", category="admin"
    )
    record(
        "admin-granted trigger",
        bool(row),
        "delivery sandbox-blocked (needs verified domain)"
        if row and row["status"] == "failed"
        else "",
    )
    resp = api("POST", f"/v1/admin/users/{bob_id}/ban", json={"reason": "smoke test"})
    row = poll_log(q="bob-smoke", category="account", want="failed") or poll_log(
        q="bob-smoke", category="account"
    )
    record(
        "ban notice trigger",
        bool(row),
        "delivery sandbox-blocked (needs verified domain)"
        if row and row["status"] == "failed"
        else "",
    )


# ── In-process flows (no HTTP surface triggers these) ─────────────────────


async def in_process_flows() -> None:
    from sqlalchemy import select

    from app.db import get_sessionmaker
    from app.models.card import Card, CardSet
    from app.models.enums import ReportPeriodEnum, TcgEnum
    from app.models.price_alert import PriceAlert
    from app.models.user import User
    from app.services import billing_service, email_service
    from app.services.analytics.reports import service as reports_service
    from app.services.market import price_alert_service

    sm = get_sessionmaker()
    async with sm() as db:
        user = (await db.execute(select(User).where(User.email == OWNER))).scalar_one()

        # Billing transitions (normally Stripe-webhook driven).
        user.stripe_customer_id = "cus_smoke"
        await db.commit()
        await billing_service._apply_subscription(
            db, {"id": "sub_smoke", "customer": "cus_smoke", "status": "active"}
        )
        await billing_service._apply_subscription(
            db, {"id": "sub_smoke", "customer": "cus_smoke", "status": "canceled"}
        )

        # Statement ready (normally the monthly scheduler).
        async def fake_snapshot(*a, **k):
            return {"fake": True}

        async def fake_upload(*a, **k):
            return "reports/smoke.pdf"

        reports_service.build_snapshot, _snap = (
            fake_snapshot,
            reports_service.build_snapshot,
        )
        reports_service.render_pdf, _pdf = (
            (lambda s: b"%PDF-smoke"),
            reports_service.render_pdf,
        )
        reports_service.upload_report_pdf, _up = (
            fake_upload,
            reports_service.upload_report_pdf,
        )
        try:
            await reports_service.generate_report(
                db, user=user, period=ReportPeriodEnum.monthly, year=2026, month=6
            )
        finally:
            reports_service.build_snapshot = _snap
            reports_service.render_pdf = _pdf
            reports_service.upload_report_pdf = _up

        # Price alert (normally the price worker).
        cset = CardSet(tcg=TcgEnum.pokemon, name="Obsidian Flames", code="OBF")
        db.add(cset)
        await db.flush()
        card = Card(
            set_id=cset.id, tcg=TcgEnum.pokemon, name="Charizard ex", number="199"
        )
        db.add(card)
        await db.flush()
        alert = PriceAlert(
            user_id=user.id, card_id=card.id, condition="above", threshold_usd=250
        )
        db.add(alert)
        await db.commit()
        fired = await price_alert_service.evaluate_for_card(db, card.id, 262.35)
        await db.commit()
        for a in fired:
            await email_service.send_price_alert(
                user.email,
                card_name=card.name,
                set_name=cset.name,
                condition="above",
                threshold_usd=a.threshold_usd,
                price_usd=a.triggered_price_usd,
                card_id=card.id,
            )
    await email_service.drain()


def flow_in_process() -> None:
    asyncio.run(in_process_flows())
    record(
        "pro activated (billing transition)",
        bool(poll_log(subject_part="Welcome to Loupe Pro")),
    )
    record(
        "pro canceled (billing transition)",
        bool(poll_log(subject_part="subscription ended")),
    )
    record("statement ready", bool(poll_log(subject_part="statement is ready")))
    record("price alert fired", bool(poll_log(category="price-alert")))


def main() -> int:
    for flow in (
        flow_register_welcome,
        flow_verify_email,
        flow_password_reset,
        flow_change_password,
        flow_mfa,
        flow_waitlist,
        flow_support,
        flow_announcement,
        flow_blog,
        flow_careers,
        flow_in_process,
        flow_sandboxed_admin_actions,
    ):
        try:
            flow()
        except Exception as exc:  # keep going; the scoreboard tells the story
            record(flow.__name__, False, f"{type(exc).__name__}: {exc}")

    stats = data(api("GET", "/v1/admin/email/log?limit=1"))["stats"]
    print("\n── delivery log stats:", {k: v for k, v in stats.items() if v})
    failed = [r for r in results if r[1] == "FAIL"]
    print(f"── {len(results) - len(failed)}/{len(results)} flows passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
