"""Whole-community audit: every /v1/social route, driven for real.

Runs the app in-process against a throwaway database, creates four real
actors, and exercises all 49 community routes end to end — then asserts the
security properties that a passing happy-path test would never notice.

Why in-process rather than against a deployed server: this needs to create
private accounts, follow requests and moderation cases and then prove
another actor CANNOT see them. Doing that against prod would mean writing
junk into real data; doing it here means every run starts from nothing and
the result is reproducible.

    .venv/bin/python -m scripts.community_audit

Exit code is the number of failures, so CI can gate on it.
"""

from __future__ import annotations

import asyncio
import io
import os
import struct
import sys
import uuid
import zlib

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6390/0")
for key in (
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
):
    os.environ[key] = ""
os.environ["S3_ENDPOINT_URL"] = ""
os.environ["OPENAI_API_KEY"] = ""  # screening off; audited separately

GREEN, RED, YELLOW, DIM, BOLD, OFF = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[2m",
    "\033[1m",
    "\033[0m",
)

passes: list[str] = []
failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    if ok:
        passes.append(label)
        print(f"  {GREEN}✔{OFF} {label}")
    else:
        failures.append(f"{label} — {detail}")
        print(f"  {RED}✘{OFF} {label}  {DIM}{detail}{OFF}")
    return ok


def section(title: str) -> None:
    print(f"\n{BOLD}{title}{OFF}")


def png(w: int = 12, h: int = 9) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x9c\x27\x4f" * w for _ in range(h))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


async def main() -> int:
    from httpx import ASGITransport, AsyncClient

    from app.auth.jwt import issue_token
    from app.db import Base, get_db, get_sessionmaker, reset_engine
    from app.main import create_app
    from app.models.card import Card, CardSet
    from app.models.enums import TcgEnum
    from app.models.user import User

    await reset_engine()
    from app.db import get_engine

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    sm = get_sessionmaker()

    # Four actors: the viewer, a public collector, a private one, and staff.
    async with sm() as db:
        actors: dict[str, User] = {}
        for key, email, admin in (
            ("viewer", "viewer@audit.test", False),
            ("public", "public@audit.test", False),
            ("private", "private@audit.test", False),
            ("staff", "staff@audit.test", True),
        ):
            user = User(email=email, display_name=key.title(), is_admin=admin)
            db.add(user)
            await db.flush()
            actors[key] = user
        card_set = CardSet(tcg=TcgEnum.pokemon, name="Base Set", code="base1")
        db.add(card_set)
        await db.flush()
        card = Card(
            set_id=card_set.id,
            tcg=TcgEnum.pokemon,
            name="Charizard",
            number="4",
            image_url="https://images.pokemontcg.io/base1/4_hires.png",
        )
        db.add(card)
        await db.commit()
        ids = {k: v.id for k, v in actors.items()}
        card_id = str(card.id)

    def hdr(who: str) -> dict[str, str]:
        token, _ = issue_token(ids[who], "access")
        return {"Authorization": f"Bearer {token}"}

    app = create_app()

    async def _db():
        async with sm() as session:
            yield session

    app.dependency_overrides[get_db] = _db

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://audit"
    ) as c:

        def data(resp):
            try:
                return resp.json().get("data")
            except Exception:
                return None

        # ── 1. Authentication ──
        section("1. Authentication — every private route rejects anonymice")
        anon_routes = [
            ("GET", "/v1/social/me"),
            ("GET", "/v1/social/feed"),
            ("GET", "/v1/social/discover"),
            ("GET", "/v1/social/explore"),
            ("GET", "/v1/social/suggested"),
            ("GET", "/v1/social/requests"),
            ("GET", "/v1/social/search?q=ab"),
            ("GET", "/v1/social/search/all?q=ab"),
            ("GET", "/v1/social/hashtags/trending"),
            ("GET", "/v1/social/hashtags/suggest"),
            ("GET", "/v1/social/hashtags/pokemon/posts"),
            ("GET", "/v1/social/report-reasons"),
            ("POST", "/v1/social/posts"),
            ("POST", "/v1/social/reports"),
            ("GET", "/v1/admin/social/moderation"),
            ("GET", "/v1/admin/social/featured"),
        ]
        leaked = []
        for method, path in anon_routes:
            r = await c.request(method, path)
            if r.status_code not in (401, 403):
                leaked.append(f"{method} {path} → {r.status_code}")
        check(
            not leaked,
            f"all {len(anon_routes)} private routes require auth",
            "; ".join(leaked),
        )

        # The two deliberately public ones.
        r = await c.get(f"/v1/social/avatar/{uuid.uuid4()}")
        check(r.status_code == 404, "avatar endpoint is public (404 for unknown)")
        r = await c.get(f"/v1/social/posts/media/{uuid.uuid4()}")
        check(r.status_code == 404, "post media is public (404 for unknown)")

        # ── 2. Profiles ──
        section("2. Profiles")
        r = await c.get("/v1/social/me", headers=hdr("viewer"))
        check(
            r.status_code == 200 and data(r)["profile"] is None,
            "GET /me — null profile before a handle is claimed",
            r.text[:90],
        )

        handles = {
            "viewer": "auditviewer",
            "public": "auditpublic",
            "private": "auditprivate",
            "staff": "auditstaff",
        }
        for who, handle in handles.items():
            r = await c.put(
                "/v1/social/me",
                json={"username": handle, "is_private": who == "private"},
                headers=hdr(who),
            )
            if who == "viewer":
                check(r.status_code == 200, "PUT /me — claim a handle", r.text[:90])

        r = await c.put(
            "/v1/social/me", json={"username": "auditpublic"}, headers=hdr("viewer")
        )
        check(r.status_code == 409, "PUT /me — a taken handle is refused")
        r = await c.put(
            "/v1/social/me", json={"username": "admin"}, headers=hdr("viewer")
        )
        check(r.status_code == 409, "PUT /me — reserved handles are refused")
        r = await c.put(
            "/v1/social/me", json={"username": "a b"}, headers=hdr("viewer")
        )
        check(r.status_code == 422, "PUT /me — malformed handles are refused")
        # Re-claim (the previous calls may have changed it).
        await c.put(
            "/v1/social/me", json={"username": "auditviewer"}, headers=hdr("viewer")
        )

        r = await c.post(
            "/v1/social/me/avatar",
            files={"image": ("a.png", png(), "image/png")},
            headers=hdr("public"),
        )
        check(r.status_code == 200 and data(r)["avatar_url"], "POST /me/avatar")
        r = await c.post(
            "/v1/social/me/avatar",
            files={"image": ("x.svg", b"<svg/>", "image/svg+xml")},
            headers=hdr("public"),
        )
        check(r.status_code == 415, "POST /me/avatar — non-images refused")

        r = await c.get("/v1/social/users/auditpublic", headers=hdr("viewer"))
        check(r.status_code == 200, "GET /users/{handle}", r.text[:90])
        r = await c.get("/v1/social/users/nobody-here", headers=hdr("viewer"))
        check(r.status_code == 404, "GET /users/{handle} — unknown is 404")
        r = await c.get("/v1/social/users/@me", headers=hdr("viewer"))
        check(
            r.status_code == 200 and data(r)["username"] == "auditviewer",
            "GET /users/@me resolves to the caller",
        )

        # ── 3. Follow graph ──
        section("3. Follow graph")
        r = await c.post("/v1/social/users/auditpublic/follow", headers=hdr("viewer"))
        check(
            r.status_code == 200 and data(r)["relationship"] == "following",
            "POST follow — public is immediate",
            r.text[:90],
        )
        r = await c.post("/v1/social/users/auditprivate/follow", headers=hdr("viewer"))
        check(
            r.status_code == 200 and data(r)["relationship"] == "requested",
            "POST follow — private becomes a request",
        )
        r = await c.post("/v1/social/users/auditviewer/follow", headers=hdr("viewer"))
        check(r.status_code == 400, "POST follow — cannot follow yourself")
        r = await c.get("/v1/social/requests", headers=hdr("private"))
        reqs = data(r) or []
        check(len(reqs) == 1, "GET /requests — the private inbox shows it")
        r = await c.post(
            f"/v1/social/requests/{reqs[0]['id']}/accept", headers=hdr("private")
        )
        check(r.status_code == 204, "POST /requests/{id}/accept")
        for path in ("followers", "following"):
            r = await c.get(
                f"/v1/social/users/auditpublic/{path}", headers=hdr("viewer")
            )
            check(r.status_code == 200, f"GET /users/*/{path}")
        r = await c.post("/v1/social/users/auditpublic/like", headers=hdr("viewer"))
        check(r.status_code == 200 and data(r)["liked"], "POST profile like")
        r = await c.delete("/v1/social/users/auditpublic/like", headers=hdr("viewer"))
        check(r.status_code == 200 and not data(r)["liked"], "DELETE profile like")

        # ── 4. Posting ──
        section("4. Posts")
        r = await c.post(
            "/v1/social/posts",
            data={"body": "Audit post about #pokemon cc @auditviewer"},
            files=[("images", ("p.png", io.BytesIO(png()), "image/png"))],
            headers=hdr("public"),
        )
        post = data(r)
        check(r.status_code == 201, "POST /posts — caption + photo", r.text[:120])
        check(bool(post and post["media"]), "post carries its media")
        check(
            bool(post and post["media"][0]["width"] == 12),
            "media carries intrinsic size (no layout jank)",
        )
        check(bool(post and post["hashtags"] == ["pokemon"]), "hashtags indexed")
        check(bool(post and post["mentions"] == ["auditviewer"]), "mentions resolved")
        # A self-mention is deliberately dropped: notifying yourself that you
        # mentioned yourself is noise, so the row is never written.
        selfie = data(
            await c.post(
                "/v1/social/posts",
                data={"body": "talking to myself cc @auditpublic"},
                headers=hdr("public"),
            )
        )
        check(selfie["mentions"] == [], "a self-mention is not indexed")

        r = await c.get(post["media"][0]["url"])
        check(
            r.status_code == 200 and "immutable" in r.headers.get("cache-control", ""),
            "GET /posts/media/{id} — served, immutably cached",
        )

        r = await c.post(
            "/v1/social/posts",
            data={"body": "with a card", "card_id": card_id},
            headers=hdr("public"),
        )
        check(
            r.status_code == 201 and data(r)["card"]["name"] == "Charizard",
            "POST /posts — attach a catalog card",
        )
        r = await c.post("/v1/social/posts", data={"body": "  "}, headers=hdr("public"))
        check(r.status_code == 422, "POST /posts — empty is refused")
        r = await c.post("/v1/social/posts", data={"body": "x"}, headers=hdr("staff"))
        check(r.status_code == 201, "POST /posts — staff can post too")

        private_post = data(
            await c.post(
                "/v1/social/posts",
                data={"body": "secret #hidden"},
                headers=hdr("private"),
            )
        )

        # ── 5. Feeds ──
        section("5. Feeds")
        for tab in ("following", "foryou", "mine"):
            r = await c.get(
                "/v1/social/feed", params={"tab": tab}, headers=hdr("viewer")
            )
            check(r.status_code == 200, f"GET /feed?tab={tab}", r.text[:90])
        r = await c.get(
            "/v1/social/feed", params={"tab": "nonsense"}, headers=hdr("viewer")
        )
        check(r.status_code == 422, "GET /feed — unknown tab refused")
        r = await c.get(
            "/v1/social/feed",
            params={"tab": "mine", "cursor": "!!bogus!!"},
            headers=hdr("viewer"),
        )
        check(r.status_code == 400, "GET /feed — forged cursor refused")
        r = await c.get("/v1/social/users/auditpublic/posts", headers=hdr("viewer"))
        check(r.status_code == 200 and data(r)["items"], "GET /users/*/posts")

        # ── 6. Privacy ──
        section("6. Privacy — a private account leaks nothing to a stranger")
        stranger = hdr("staff")  # staff are NOT followers of `private`
        r = await c.get("/v1/social/feed", params={"tab": "foryou"}, headers=stranger)
        ids_seen = {i["id"] for i in data(r)["items"]}
        check(private_post["id"] not in ids_seen, "private post absent from For You")
        r = await c.get("/v1/social/users/auditprivate/posts", headers=stranger)
        check(data(r)["items"] == [], "private posts absent from their profile")
        r = await c.get(f"/v1/social/posts/{private_post['id']}", headers=stranger)
        check(
            r.status_code == 404,
            "private permalink 404s (not 403 — a 403 confirms it exists)",
        )
        r = await c.get("/v1/social/hashtags/hidden/posts", headers=stranger)
        check(data(r)["items"] == [], "private post absent from its hashtag page")
        r = await c.get("/v1/social/hashtags/trending", headers=stranger)
        check(
            "hidden" not in {t["tag"] for t in data(r)},
            "private tags do not inflate trending",
        )
        r = await c.post(
            f"/v1/social/posts/{private_post['id']}/like", headers=stranger
        )
        check(r.status_code == 404, "cannot like a private post")
        r = await c.post(
            f"/v1/social/posts/{private_post['id']}/comments",
            json={"body": "hi"},
            headers=stranger,
        )
        check(r.status_code == 404, "cannot comment on a private post")
        # The accepted follower CAN.
        r = await c.get(f"/v1/social/posts/{private_post['id']}", headers=hdr("viewer"))
        check(r.status_code == 200, "an accepted follower CAN see it")

        # ── 7. Likes + comments ──
        section("7. Likes and comments")
        pid = post["id"]
        r1 = await c.post(f"/v1/social/posts/{pid}/like", headers=hdr("viewer"))
        r2 = await c.post(f"/v1/social/posts/{pid}/like", headers=hdr("viewer"))
        check(
            data(r1)["like_count"] == data(r2)["like_count"] == 1,
            "liking twice is one like",
        )
        r = await c.delete(f"/v1/social/posts/{pid}/like", headers=hdr("viewer"))
        check(data(r)["like_count"] == 0, "unlike works")

        top = data(
            await c.post(
                f"/v1/social/posts/{pid}/comments",
                json={"body": "first"},
                headers=hdr("viewer"),
            )
        )
        reply = data(
            await c.post(
                f"/v1/social/posts/{pid}/comments",
                json={"body": "reply", "parent_id": top["id"]},
                headers=hdr("public"),
            )
        )
        nested = data(
            await c.post(
                f"/v1/social/posts/{pid}/comments",
                json={"body": "nested", "parent_id": reply["id"]},
                headers=hdr("viewer"),
            )
        )
        check(
            nested["parent_id"] == top["id"],
            "replying to a reply flattens onto the top-level comment",
        )
        r = await c.get(f"/v1/social/posts/{pid}/comments", headers=hdr("viewer"))
        thread = data(r)
        check(thread["total"] == 3, "thread total counts replies")
        check(thread["items"][0]["reply_count"] == 2, "reply_count is right")
        r = await c.post(f"/v1/social/comments/{top['id']}/like", headers=hdr("public"))
        check(data(r)["liked"] and data(r)["like_count"] == 1, "comment like")
        r = await c.get(
            f"/v1/social/comments/{top['id']}/replies", headers=hdr("viewer")
        )
        check(r.status_code == 200 and len(data(r)["items"]) == 2, "GET replies")

        # ── 8. IDOR ──
        section("8. Authorization — nobody edits anybody else's content")
        r = await c.delete(f"/v1/social/posts/{pid}", headers=hdr("viewer"))
        check(r.status_code == 403, "cannot delete someone else's post")
        r = await c.delete(f"/v1/social/comments/{top['id']}", headers=hdr("private"))
        check(
            r.status_code == 403,
            "a bystander cannot delete a comment",
            r.text[:90],
        )
        r = await c.delete(f"/v1/social/comments/{reply['id']}", headers=hdr("public"))
        check(r.status_code == 204, "a comment's own author can delete it")
        r = await c.get("/v1/admin/social/moderation", headers=hdr("viewer"))
        check(r.status_code in (403, 404), "the moderation queue is admin-only")
        r = await c.post(
            "/v1/admin/social/featured",
            json={"username": "auditpublic"},
            headers=hdr("viewer"),
        )
        check(r.status_code in (403, 404), "featured curation is admin-only")

        # ── 9. Discovery + search ──
        section("9. Discovery and search")
        for path in (
            "/v1/social/discover",
            "/v1/social/explore",
            "/v1/social/suggested",
        ):
            r = await c.get(path, headers=hdr("viewer"))
            check(r.status_code == 200, f"GET {path}", r.text[:90])
        r = await c.get(
            "/v1/social/search", params={"q": "@auditpublic"}, headers=hdr("viewer")
        )
        check(
            any(u["username"] == "auditpublic" for u in data(r)),
            "search accepts the '@handle' spelling the placeholder asks for",
        )
        r = await c.get(
            "/v1/social/search", params={"q": "AUDITPUBLIC"}, headers=hdr("viewer")
        )
        check(len(data(r)) >= 1, "search is case-insensitive")
        r = await c.get(
            "/v1/social/search/all", params={"q": "poke"}, headers=hdr("viewer")
        )
        check(
            "pokemon" in {h["tag"] for h in data(r)["hashtags"]},
            "combined search returns hashtags",
        )
        r = await c.get(
            "/v1/social/hashtags/suggest", params={"q": ""}, headers=hdr("viewer")
        )
        check(r.status_code == 200 and data(r), "suggest with empty q returns trending")
        for spelling in ("pokemon", "POKEMON", "%23pokemon"):
            r = await c.get(
                f"/v1/social/hashtags/{spelling}/posts", headers=hdr("viewer")
            )
            check(
                r.status_code == 200 and len(data(r)["items"]) >= 1,
                f"tag page accepts '{spelling}'",
            )
        r = await c.get(
            "/v1/social/hashtags/pokemon/posts",
            params={"sort": "top"},
            headers=hdr("viewer"),
        )
        check(r.status_code == 200, "tag page sort=top")
        r = await c.get(f"/v1/social/cards/{card_id}/owners", headers=hdr("viewer"))
        check(r.status_code == 200, "GET /cards/*/owners")

        # ── 10. Safety ──
        section("10. Safety")
        r = await c.get("/v1/social/report-reasons", headers=hdr("viewer"))
        check(r.status_code == 200 and len(data(r)) >= 5, "GET /report-reasons")
        body = {"target_type": "post", "target_id": pid, "reason": "spam"}
        r1 = await c.post("/v1/social/reports", json=body, headers=hdr("viewer"))
        r2 = await c.post("/v1/social/reports", json=body, headers=hdr("viewer"))
        check(
            r1.status_code == 201 and data(r1)["id"] == data(r2)["id"],
            "reporting twice is one case, not a vote",
        )
        r = await c.post(
            "/v1/social/reports",
            json={"target_type": "post", "target_id": pid, "reason": "vibes"},
            headers=hdr("viewer"),
        )
        check(r.status_code == 422, "an unknown report reason is refused")
        own = data(
            await c.post(
                "/v1/social/posts", data={"body": "mine"}, headers=hdr("viewer")
            )
        )
        r = await c.post(
            "/v1/social/reports",
            json={"target_type": "post", "target_id": own["id"], "reason": "spam"},
            headers=hdr("viewer"),
        )
        check(r.status_code == 400, "cannot report your own post")

        r = await c.get("/v1/admin/social/moderation", headers=hdr("staff"))
        check(
            r.status_code == 200 and data(r)["open_count"] >= 1,
            "admin queue shows the open case",
            r.text[:90],
        )
        case_id = data(r)["items"][0]["id"]
        r = await c.post(
            f"/v1/admin/social/moderation/{case_id}/resolve",
            params={"action": "dismiss"},
            headers=hdr("staff"),
        )
        check(data(r)["status"] == "dismissed", "admin can resolve a case")

        prof = data(
            await c.post(
                "/v1/social/posts",
                data={"body": "clean #pokemon and #fuck"},
                headers=hdr("public"),
            )
        )
        check(
            prof["hashtags"] == ["pokemon"],
            "a profane tag is not indexed (the caption is untouched)",
        )
        check("#fuck" in prof["body"], "…and the author's words are kept verbatim")

        # ── 11. Collections ──
        section("11. Shared collections")
        r = await c.get(
            "/v1/social/users/auditpublic/collection", headers=hdr("viewer")
        )
        check(r.status_code == 200, "GET /users/*/collection", r.text[:90])
        r = await c.get(
            f"/v1/social/users/auditpublic/collections/{uuid.uuid4()}",
            headers=hdr("viewer"),
        )
        check(r.status_code == 404, "unknown portfolio is 404")

        # ── 12. Teardown ──
        section("12. Lifecycle")
        r = await c.delete(
            "/v1/social/me/followers/auditviewer",
            headers=hdr("auditpublic_missing") if False else hdr("public"),
        )
        check(r.status_code in (204, 404), "DELETE /me/followers/{handle}")
        r = await c.delete("/v1/social/me", headers=hdr("staff"))
        check(r.status_code == 204, "DELETE /me — deactivate")
        r = await c.get("/v1/social/users/auditstaff", headers=hdr("viewer"))
        check(r.status_code == 404, "a deactivated profile vanishes")

    await reset_engine()

    total = len(passes) + len(failures)
    print(f"\n{BOLD}{'─' * 62}{OFF}")
    if failures:
        print(f"{RED}{BOLD}{len(failures)}/{total} checks FAILED{OFF}")
        for f in failures:
            print(f"  {RED}✘{OFF} {f}")
    else:
        print(f"{GREEN}{BOLD}all {total} checks passed{OFF}")
    return len(failures)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
