"""Seed the developer-portal page feature flags.

Every admin page is gated by an ``admin_*`` feature flag so it can be toggled
from the Feature flags page without a deploy. This seeder makes those flags
exist in the DB (enabled) so they show up and are togglable — it runs at startup
and is **idempotent + non-destructive**: it only inserts keys that are missing,
and never touches an existing flag (so an admin's on/off choice always wins).

Keys mirror ``ADMIN_PAGES[].flag`` in
``loupe-web/src/features/admin/adminPages.ts``. The two core pages (Overview,
Feature flags) are intentionally NOT gated, so they're absent here.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feature_flag import FeatureFlag
from app.utils.logger import get_logger

logger = get_logger("admin.flag_seed")

# (key, label, description) for every gated admin page.
ADMIN_PAGE_FLAGS: tuple[tuple[str, str, str], ...] = (
    ("admin_health", "Admin · System health", "Operations: live system health page."),
    (
        "admin_database",
        "Admin · Database",
        "Operations: database schema & rows explorer.",
    ),
    ("admin_cloud", "Admin · Google Cloud", "Operations: Cloud Run & Cloud SQL panel."),
    ("admin_env", "Admin · Environment", "Operations: environment config manager."),
    (
        "admin_integrations",
        "Admin · Integrations",
        "Operations: second-party service catalog.",
    ),
    ("admin_email", "Admin · Email", "Operations: email template gallery & tests."),
    (
        "admin_notifications",
        "Admin · Notifications",
        "Operations: compose push + in-app notifications.",
    ),
    ("admin_audit", "Admin · Audit log", "Operations: admin activity trail."),
    ("admin_revenue", "Admin · Revenue", "Monetization: MRR & churn."),
    ("admin_pro", "Admin · Loupe Pro", "Monetization: plans & feature gates."),
    ("admin_announce", "Admin · Announcements", "Monetization: global banner editor."),
    (
        "admin_community",
        "Admin · Community",
        "People: story lifecycle bench + social dev tools.",
    ),
    (
        "admin_moderation",
        "Admin · Moderation",
        "People: community review queue (reports + auto-flags).",
    ),
    ("admin_users", "Admin · Users", "People: user search, roles, bans."),
    (
        "admin_featured",
        "Admin · Featured collectors",
        "People: curate the Community featured rail.",
    ),
    ("admin_catalog", "Admin · Catalog", "Catalog: coverage by game."),
    (
        "admin_carousels",
        "Admin · Carousels",
        "Catalog: live control of every marketplace carousel.",
    ),
    (
        "admin_pricecharting",
        "Admin · PriceCharting",
        "Catalog: PriceCharting tier detection + fallback chain.",
    ),
    ("admin_cards", "Admin · Card data", "Catalog: explore & override card data."),
    ("admin_card_tree", "Admin · Card tree", "Catalog: card data-lineage tree."),
    ("admin_grades", "Admin · Grade review", "Catalog: QA graded cards."),
    ("admin_scanner", "Admin · Scanner", "Catalog: identify funnel."),
    (
        "admin_ai",
        "Admin · Loupe AI",
        "Tools: chatbot conversations, thumbs feedback, accuracy.",
    ),
    ("admin_pulse", "Admin · Live pulse", "Growth: live activity feed."),
    ("admin_engagement", "Admin · Engagement", "Growth: retention & funnel."),
    ("admin_retention", "Admin · Retention", "Growth: cohort triangle."),
    ("admin_jobs", "Admin · Jobs", "Hiring: open roles."),
    ("admin_applications", "Admin · Applications", "Hiring: inbound applications."),
    ("admin_blog", "Admin · Blog", "Content: blog posts."),
    ("admin_waitlist", "Admin · Scanner waitlist", "Content: hardware waitlist."),
    (
        "admin_legal",
        "Admin · Law",
        "Legal: edit and publish the Terms, Privacy Policy, and other documents.",
    ),
    ("admin_insights", "Admin · Ask your data", "Tools: NL→SQL data queries."),
    ("admin_api", "Admin · API inspector", "Tools: live API traffic."),
    ("admin_console", "Admin · API console", "Tools: run GET requests."),
    ("admin_simulator", "Admin · Device simulator", "Tools: device preview."),
    ("admin_navkeys", "Admin · Nav keys", "Tools: sign-in deep links."),
)


async def seed_admin_flags(db: AsyncSession) -> int:
    """Insert any missing admin-page flags (enabled). Returns the number added.

    Idempotent: existing flags are left exactly as-is, so this is safe to run on
    every boot and never clobbers an admin's on/off choice.
    """
    existing = {key for (key,) in (await db.execute(select(FeatureFlag.key))).all()}
    added = 0
    for key, label, description in ADMIN_PAGE_FLAGS:
        if key in existing:
            continue
        db.add(FeatureFlag(key=key, label=label, description=description, enabled=True))
        added += 1
    if added:
        await db.commit()
        logger.info("Seeded %d admin-page feature flag(s).", added)
    return added


__all__ = ["ADMIN_PAGE_FLAGS", "seed_admin_flags"]
