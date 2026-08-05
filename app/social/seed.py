"""Seed a handful of demo collectors so Community isn't a ghost town.

A social feature with nobody in it is worse than no social feature: the first
user claims a handle, opens "Suggested for you", finds an empty list, and
concludes the product is broken. These accounts give a new user someone to
follow on day one.

**They are unmistakably demo accounts, on purpose.** Each one:

* uses a reserved ``@loupe.demo`` email domain that no real signup can reach,
* has ``password_hash = None``, so nobody can ever log in as one,
* carries a bio that says what it is.

That last point matters. Fabricating profiles that pass for real people would
mean the follower counts a user sees are theatre — and a user who later works
out that "ash_ketchum" was never a person has been misled about the size of the
community they joined. Being visibly a demo costs nothing and keeps the number
on the screen honest.

Idempotent and non-destructive: it inserts only what's missing, and never
edits an account or profile that already exists (so a handle a real user has
since claimed always wins).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.social.models import SocialProfile
from app.utils.logger import get_logger

logger = get_logger("social.seed")

DEMO_EMAIL_DOMAIN = "loupe.demo"

# (username, display_name, location, bio)
DEMO_COLLECTORS: tuple[tuple[str, str, str, str], ...] = (
    (
        "vintage_vault",
        "Vintage Vault",
        "Portland, OR",
        "Demo account · Base Set and WOTC-era holos. Slabs only.",
    ),
    (
        "gradehunter",
        "Grade Hunter",
        "Austin, TX",
        "Demo account · Chasing PSA 10s. I post every pre-grade guess.",
    ),
    (
        "sealed_and_stored",
        "Sealed & Stored",
        "Manchester, UK",
        "Demo account · Sealed booster boxes, never opened. Ask me about storage.",
    ),
    (
        "modern_meta",
        "Modern Meta",
        "Toronto, ON",
        "Demo account · Modern-era chase cards and alt arts.",
    ),
    (
        "the_binder",
        "The Binder",
        "Osaka, JP",
        "Demo account · Japanese exclusives. Raw, sleeved, loved.",
    ),
)


async def seed_demo_collectors(db: AsyncSession) -> int:
    """Ensure the demo collectors exist. Returns how many were created."""
    created = 0
    for username, display_name, location, bio in DEMO_COLLECTORS:
        # A real user may have claimed this handle since the last run — their
        # claim wins, and we leave both rows untouched.
        taken = (
            await db.execute(
                select(SocialProfile.user_id).where(SocialProfile.username == username)
            )
        ).first()
        if taken:
            continue

        email = f"{username}@{DEMO_EMAIL_DOMAIN}"
        account = (
            await db.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if account is None:
            account = User(
                email=email,
                display_name=display_name,
                # No password hash — these accounts are unauthenticatable by
                # construction, not merely by policy.
                password_hash=None,
            )
            db.add(account)
            await db.flush()

        # The display name lives on the account row, not the profile.
        db.add(
            SocialProfile(
                user_id=account.id,
                username=username,
                bio=bio,
                location=location,
                is_private=False,
            )
        )
        created += 1

    if created:
        await db.commit()
        logger.info("seeded %d demo collectors", created)
    return created
