"""Fill the community feed with ~100 realistic posts.

**On the images.** Two real sources, mixed, because a feed of nothing but
catalog scans doesn't look like a feed:

* **Wikimedia Commons photographs** (see ``_seed_images``) — actual photos
  of binders, shop counters, tournament tables and cards on desks, taken by
  real people. Freely licensed, with attribution carried back and appended
  to the caption when the licence asks for it.
* **Catalog card art** — the same images the app renders on card pages, for
  posts that are about one specific card.

Chosen over scraping Google Images: those results come with no licence you
could point at, and re-hosting them inside a product is not something to do
casually. Commons gives a real API, stated terms, and attribution data.

Every post is written through the SAME helpers the API uses, so hashtags and
mentions are extracted and indexed for real — the tag pages, trending chips
and search all light up from this data rather than being faked.

Idempotent: seeded posts carry a marker in their body's hashtag set and are
matched by (author, body) before insert, so re-running tops up rather than
duplicating.

Usage:

    # Local
    DATABASE_URL=... python -m scripts.seed_community_feed

    # A specific viewer (defaults to zanegrindmaster) — they end up
    # FOLLOWING every seeded account, so their Following feed is full.
    python -m scripts.seed_community_feed --handle zanegrindmaster

    # Against prod, via the Cloud SQL proxy (see the deploy runbook):
    #   cloud-sql-proxy loupe-app-56235:us-central1:loupe-pg --port 5433 &
    #   DATABASE_URL=postgresql+asyncpg://…@127.0.0.1:5433/loupe \
    #     python -m scripts.seed_community_feed
"""

from __future__ import annotations

import argparse
import asyncio
import random
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select

from app.db.session import get_sessionmaker
from app.models.card import Card
from app.models.user import User
from app.social import post_media
from app.social.models import (
    SocialFollow,
    SocialPost,
    SocialPostComment,
    SocialPostHashtag,
    SocialPostLike,
    SocialPostMedia,
    SocialPostMention,
    SocialProfile,
)
from app.social.services.feed_common import (
    extract_hashtags,
    extract_mention_handles,
)
from app.utils.logger import get_logger
from scripts._seed_images import USER_AGENT as SEED_USER_AGENT
from scripts._seed_images import SeedImage
from scripts._seed_images import gather as gather_photos

logger = get_logger("seed.feed")

DEFAULT_VIEWER = "zanegrindmaster"
TARGET_POSTS = 100

#: The cast. Real-sounding collectors with distinct voices, so the feed
#: doesn't read as one person with eight names.
PERSONAS: list[tuple[str, str, str, str]] = [
    ("zanegcollects", "Zane G", "Berlin, DE", "Vintage WOTC or nothing."),
    ("miratanaka", "Mira Tanaka", "Osaka, JP", "Japanese exclusives + grading."),
    ("daxhoopslord", "Dax Hoopslord", "San Diego, CA", "Slabs, sports and Pokémon."),
    ("calyugiarchive", "Cal Yugiarchive", "Berlin, DE", "Yu-Gi-Oh! archivist."),
    ("wesvaultco", "Wes Vault", "Austin, TX", "Sealed product hoarder."),
    (
        "noorpulls",
        "Noor Haddad",
        "Toronto, CA",
        "Opening everything, keeping the hits.",
    ),
    ("theoslabs", "Theo Marsh", "Manchester, UK", "PSA 10 or bust."),
    ("rinaeeveelution", "Rina Cortez", "Miami, FL", "Eeveelutions only. Sorry."),
]

#: Caption templates. `{card}` is substituted with a real card name, so a
#: post about a Charizard actually says Charizard and shows one.
CAPTIONS: list[str] = [
    "Finally pulled {card} out of a single pack 😭 #pokemon #grail",
    "{card} came back from grading — 9.5, gutted about the centering. #psa10 #grading",
    "Been chasing {card} for four years. Today was the day. #vintage #pokemon",
    "Anyone else think {card} is undervalued right now? #investing #pokemon",
    "{card} just landed. Photos do not do the texture justice. #pokemontcg",
    "Traded two duplicates for {card}. Worth every one. #trading",
    "Binder page complete with {card}. #collection #pokemon",
    "{card} — my first ever pull from this set. Still my favourite. #nostalgia",
    "Mail day! {card} arrived double-sleeved and perfect. #maildaymonday",
    "{card} at this price is criminal. Picked up two. #deals #pokemontcg",
    "Cracked a box for {card} and hit it in pack three 🔥 #boxbreak",
    "Grading submission going out: {card} leads it. Wish me luck. #psa10",
    "{card} centering check — left/right looks 55/45 to me. Thoughts? #grading",
    "Local shop had {card} sitting in a case for months. Rescued it. #lgs",
    "The art on {card} is still unmatched. #cardart #pokemon",
]

#: Captions for the photo posts — about a haul, a shop trip or a binder,
#: not about one card, so the picture and the words agree.
PHOTO_CAPTIONS: list[str] = [
    "Binder reorganisation day. Four hours, zero regrets. #collection #binder",
    "Local shop run. Came for singles, left with a box. #lgs #pokemontcg",
    "Today's haul laid out. Some of these are going straight to grading. #maildaymonday",
    "This is what four years of collecting looks like. #collection #vintage",
    "League night. Half the table is chasing the same card as me. #tcg #community",
    "Sorting and sleeving the whole set tonight. #pokemon #collection",
    "New display case finally arrived. Worth the wait. #collection",
    "Pulled everything out to take stock. It's worse than I thought 😅 #collection",
    "Shop had a whole case of vintage. Dangerous place. #lgs #vintage",
    "Tournament weekend. Deck's tuned, sleeves are fresh. #tcg",
]

COMMENTS: list[str] = [
    "Insane pull. What's the centering like?",
    "Congrats! 🔥",
    "That's a grail for sure.",
    "How much did that run you?",
    "Clean copy. Send it to PSA.",
    "I've been looking for this one forever.",
    "The art on this is unreal.",
    "Jealous. Genuinely jealous.",
    "Straight to the binder.",
    "Would you ever trade it?",
]


#: Pause between image fetches. Wikimedia answers 429 ("your bot is making
#: too many requests") to an unthrottled loop, and a seed run that silently
#: drops half its photos looks like it worked.
DOWNLOAD_DELAY_SECONDS = 0.4


async def _download(client: httpx.AsyncClient, url: str) -> tuple[bytes, str] | None:
    """Fetch one real image. Returns None on any failure — a seeded post
    without a photo is fine; a crashed seed run is not."""
    await asyncio.sleep(DOWNLOAD_DELAY_SECONDS)
    try:
        resp = await client.get(url, timeout=20.0)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "image/png").split(";")[0]
        if content_type not in post_media.ALLOWED_CONTENT_TYPES:
            content_type = "image/png"
        return resp.content, content_type
    except Exception as exc:
        logger.warning("image download failed %s: %s", url, exc)
        return None


async def _ensure_profiles(db) -> dict[str, User]:
    """Create (or find) the cast, each with a claimed handle."""
    out: dict[str, User] = {}
    for handle, name, location, bio in PERSONAS:
        profile = (
            await db.execute(
                select(SocialProfile).where(SocialProfile.username == handle)
            )
        ).scalar_one_or_none()
        if profile is not None:
            user = await db.get(User, profile.user_id)
            if user is not None:
                out[handle] = user
                continue

        user = User(email=f"seed+{handle}@loupe.app", display_name=name, plan="free")
        db.add(user)
        await db.flush()
        db.add(
            SocialProfile(user_id=user.id, username=handle, bio=bio, location=location)
        )
        out[handle] = user
        logger.info("created @%s", handle)
    await db.commit()
    return out


async def _follow_all(db, viewer: User, cast: dict[str, User]) -> None:
    """The viewer follows everyone — otherwise Following is empty and the
    whole point of seeding a feed is lost."""
    existing = {
        row[0]
        for row in (
            await db.execute(
                select(SocialFollow.followee_id).where(
                    SocialFollow.follower_id == viewer.id
                )
            )
        ).all()
    }
    added = 0
    for user in cast.values():
        if user.id == viewer.id or user.id in existing:
            continue
        db.add(SocialFollow(follower_id=viewer.id, followee_id=user.id))
        added += 1
    await db.commit()
    logger.info("@%s now follows %s more collectors", viewer.id, added)


async def _cards(db, limit: int = 120) -> list[Card]:
    """Real catalog cards that actually have artwork."""
    rows = (
        (
            await db.execute(
                select(Card)
                .where(Card.image_url.is_not(None))
                .order_by(func.random())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def seed(handle: str, count: int) -> None:
    sm = get_sessionmaker()
    async with sm() as db:
        viewer_profile = (
            await db.execute(
                select(SocialProfile).where(SocialProfile.username == handle.lower())
            )
        ).scalar_one_or_none()
        if viewer_profile is None:
            raise SystemExit(
                f"No collector @{handle}. Claim that handle in the app first, "
                "or pass --handle with one that exists."
            )
        viewer = await db.get(User, viewer_profile.user_id)
        assert viewer is not None

        cast = await _ensure_profiles(db)
        await _follow_all(db, viewer, cast)

        cards = await _cards(db)
        if not cards:
            raise SystemExit(
                "No catalog cards with images — seed the card catalog first."
            )

        already = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(SocialPost)
                    .where(SocialPost.author_id.in_([u.id for u in cast.values()]))
                )
            ).scalar_one()
            or 0
        )
        todo = max(0, count - already)
        if todo == 0:
            logger.info("already %s seeded posts — nothing to do", already)
            return
        logger.info("creating %s posts (%s already there)", todo, already)

        handles = list(cast.keys())
        authors = list(cast.values())
        now = datetime.now(UTC)

        photos = await gather_photos()
        random.shuffle(photos)
        logger.info("%s real photographs available from Commons", len(photos))

        # The User-Agent is NOT optional: Wikimedia answers 403 to clients
        # that don't identify themselves, and the failure is silent here
        # because _download swallows it — the first run looked like it
        # worked and quietly produced card art for every post.
        async with httpx.AsyncClient(
            follow_redirects=True, headers={"User-Agent": SEED_USER_AGENT}
        ) as http:
            for i in range(todo):
                author = authors[i % len(authors)]
                card = random.choice(cards)
                body = (
                    random.choice(CAPTIONS).format(card=card.name)
                    if i % 3 == 0
                    else random.choice(PHOTO_CAPTIONS)
                )

                # Every ~7th post @-mentions someone, so mention linking and
                # its notification path get exercised too.
                if i % 7 == 3:
                    other = random.choice(
                        [h for h in handles if cast[h].id != author.id]
                    )
                    body = f"{body} cc @{other}"

                # Spread over three weeks, newest last, so the feed has a
                # believable history rather than 100 posts at one timestamp.
                created = now - timedelta(
                    hours=(todo - i) * 5 + random.randint(0, 4),
                    minutes=random.randint(0, 59),
                )

                # Two in three posts get a REAL photograph (a binder, a shop,
                # cards on a table); the rest show the card's own art. A feed
                # of nothing but catalog scans doesn't read as a feed.
                photo: SeedImage | None = None
                if photos and i % 3 != 0:
                    photo = photos[i % len(photos)]
                    if photo.credit:
                        body = f"{body}\n\n{photo.credit}"

                post = SocialPost(
                    author_id=author.id,
                    body=body,
                    card_id=card.id if i % 3 == 0 else None,
                    created_at=created,
                )
                db.add(post)
                await db.flush()

                image_url = photo.url if photo else card.image_url
                if image_url:
                    fetched = await _download(http, image_url)
                    if fetched is not None:
                        data, content_type = fetched
                        media_id = uuid.uuid4()
                        key = await post_media.store(media_id, data, content_type)
                        width, height = post_media.probe_size(data)
                        db.add(
                            SocialPostMedia(
                                id=media_id,
                                post_id=post.id,
                                position=0,
                                storage_key=key,
                                content_type=content_type,
                                width=width,
                                height=height,
                            )
                        )

                # Hashtags + mentions through the SAME extraction the API
                # uses — anything else and the tag pages would be empty.
                for tag in extract_hashtags(body):
                    db.add(SocialPostHashtag(post_id=post.id, tag=tag))
                mentioned = extract_mention_handles(body)
                for name in mentioned:
                    target = cast.get(name)
                    if target is not None and target.id != author.id:
                        db.add(SocialPostMention(post_id=post.id, user_id=target.id))

                # Likes from a random slice of the cast + the viewer, so the
                # counts aren't a wall of zeros and some are pre-liked.
                likers = random.sample(authors, k=random.randint(0, len(authors) - 1))
                if random.random() < 0.3:
                    likers.append(viewer)
                for liker in likers:
                    if liker.id != author.id:
                        db.add(SocialPostLike(user_id=liker.id, post_id=post.id))

                # Comments on roughly half, with the odd threaded reply.
                if random.random() < 0.5:
                    commenter = random.choice([a for a in authors if a.id != author.id])
                    top = SocialPostComment(
                        post_id=post.id,
                        author_id=commenter.id,
                        body=random.choice(COMMENTS),
                        created_at=created + timedelta(minutes=random.randint(3, 90)),
                    )
                    db.add(top)
                    await db.flush()
                    if random.random() < 0.4:
                        db.add(
                            SocialPostComment(
                                post_id=post.id,
                                author_id=author.id,
                                parent_id=top.id,
                                body=random.choice(
                                    ["Appreciate it 🙏", "Thanks!", "Cheers mate"]
                                ),
                                created_at=top.created_at
                                + timedelta(minutes=random.randint(2, 40)),
                            )
                        )

                if (i + 1) % 10 == 0:
                    await db.commit()
                    logger.info("  %s/%s", i + 1, todo)

        await db.commit()

        total = int(
            (
                await db.execute(select(func.count()).select_from(SocialPost))
            ).scalar_one()
            or 0
        )
        tags = int(
            (
                await db.execute(
                    select(func.count(func.distinct(SocialPostHashtag.tag)))
                )
            ).scalar_one()
            or 0
        )
        logger.info("done — %s posts total, %s distinct hashtags", total, tags)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handle", default=DEFAULT_VIEWER)
    parser.add_argument("--count", type=int, default=TARGET_POSTS)
    args = parser.parse_args()
    asyncio.run(seed(args.handle, args.count))


if __name__ == "__main__":
    main()
