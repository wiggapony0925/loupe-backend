"""Seed real developer-portal content: progress blog posts + open roles.

Inserts published blog posts (current product progress) and two open job
postings (backend + frontend) so the public blog and careers pages render
real content instead of an empty state. Idempotent: each row is keyed by a
unique slug and removed on downgrade.

Revision ID: 0016_seed_portal_content
Revises: 0015_waitlist
Create Date: 2026-06-20 13:00:00
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from app.db.types import UuidCol

revision: str = "0016_seed_portal_content"
down_revision: str | Sequence[str] | None = "0015_waitlist"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_BLOG = sa.table(
    "blog_posts",
    sa.column("id", UuidCol()),
    sa.column("slug", sa.String),
    sa.column("title", sa.String),
    sa.column("excerpt", sa.String),
    sa.column("body", sa.Text),
    sa.column("tag", sa.String),
    sa.column("author", sa.String),
    sa.column("read_minutes", sa.Integer),
    sa.column("status", sa.String),
    sa.column("published_at", sa.DateTime(timezone=True)),
)

_JOB = sa.table(
    "job_postings",
    sa.column("id", UuidCol()),
    sa.column("slug", sa.String),
    sa.column("title", sa.String),
    sa.column("team", sa.String),
    sa.column("location", sa.String),
    sa.column("employment_type", sa.String),
    sa.column("summary", sa.String),
    sa.column("description", sa.Text),
    sa.column("status", sa.String),
)

_BLOG_SLUGS = (
    "building-loupe-value",
    "compare-any-grade-instantly",
    "what-we-shipped-this-month",
)
_JOB_SLUGS = ("backend-engineer", "frontend-engineer")


def _blogs() -> list[dict[str, object]]:
    now = datetime.now(UTC)
    posts: list[dict[str, object]] = [
        {
            "slug": "building-loupe-value",
            "title": "Building Loupe Value: from a wall of prices to one number",
            "excerpt": (
                "Most price sites throw last sale, low, market, and a grade ladder at "
                "you and let you guess. We built one defensible number instead."
            ),
            "tag": "Product",
            "read_minutes": 4,
            "body": (
                "Every card has a dozen prices — last sale, lowest listing, catalog "
                "market, and a different number for every grade. That's noise, not an "
                "answer.\n\n"
                "Loupe Value is our equilibrium estimate: the single point where the "
                "signals we actually trust agree. We weight realised sold comps above "
                "live listings, and live listings above an aggregated catalog market, "
                "then re-normalise over whichever signals are present so even a thin "
                "card still gets a value. We surface the inputs underneath the number, "
                "so it's transparent — not a black box.\n\n"
                "The sold-comps signal is now powered by real PriceCharting data, and "
                "the per-grade ladder rolls up from the same pipeline so the card "
                "detail needs a single round trip. One honest number, with its work "
                "shown."
            ),
        },
        {
            "slug": "compare-any-grade-instantly",
            "title": "Compare any grade, instantly: our new interactive charts",
            "excerpt": (
                "Overlay a PSA 10 against a TAG 10, scrub the timeline, and read every "
                "line's price at once — the same chart now lives everywhere."
            ),
            "tag": "Engineering",
            "read_minutes": 3,
            "body": (
                "We rebuilt the price chart as one reusable, dependency-free SVG "
                "primitive that powers the landing page, card detail, and your "
                "dashboard.\n\n"
                "Tap a Compare chip and another grade tier drops onto the chart as a "
                "second coloured line. Scrub the timeline and a crosshair tooltip "
                "reads out every line's value at that moment; when the lines converge "
                "it collapses to a single green equilibrium price. Search now shows a "
                "Robinhood-style sparkline next to each result, and the whole thing is "
                "finally crisp on mobile.\n\n"
                "Reusable, theme-aware, and fast — the way a financial chart should be."
            ),
        },
        {
            "slug": "what-we-shipped-this-month",
            "title": "What we shipped this month: search, scanning, and your collection",
            "excerpt": (
                "Deeper and more reliable search, web card scanning, full grading "
                "scales, and a portfolio view of your collection."
            ),
            "tag": "Changelog",
            "read_minutes": 3,
            "body": (
                "A quick roundup of recent progress:\n\n"
                "- Search goes deep: popular names like Mewtwo now return a deep, "
                "pageable result set with relevance ranking, plus a last-good "
                "fallback so a slow upstream never looks broken.\n"
                "- Real grading scales: PSA, BGS, CGC, SGC, and TAG with the actual "
                "grades each house issues, priced per tier.\n"
                "- Market discovery: full-width 'Best from' rails per game.\n"
                "- Your collection as a portfolio: a big value-over-time chart on the "
                "dashboard, mirroring the mobile app.\n\n"
                "More soon — we're just getting started."
            ),
        },
    ]
    for p in posts:
        p["id"] = uuid.uuid4()
        p["author"] = "The Loupe Team"
        p["status"] = "published"
        p["published_at"] = now
    return posts


def _jobs() -> list[dict[str, object]]:
    jobs: list[dict[str, object]] = [
        {
            "slug": "backend-engineer",
            "title": "Backend Engineer",
            "team": "Platform",
            "location": "Remote (US)",
            "summary": (
                "Build the pricing, catalog, and valuation services that power Loupe "
                "across web and mobile."
            ),
            "description": (
                "We're hiring a backend engineer to own and extend the Loupe "
                "platform: a FastAPI + SQLAlchemy + Postgres backend that ingests "
                "multi-TCG catalogs, synthesizes price history, and computes our "
                "equilibrium valuations.\n\n"
                "You'll work on the provider fan-out and caching layer, the "
                "sold-comps and grade-ladder pipelines, and the public storefront "
                "API. Strong async Python, clean data modelling, and a bias for "
                "well-tested, observable services.\n\n"
                "Bonus: experience with pricing/marketplace data, Redis, and Cloud "
                "Run."
            ),
        },
        {
            "slug": "frontend-engineer",
            "title": "Frontend Engineer",
            "team": "Web",
            "location": "Remote (US)",
            "summary": (
                "Craft the web experience — interactive charts, the storefront, and "
                "the collector dashboard — in React + TypeScript."
            ),
            "description": (
                "We're hiring a frontend engineer to build the Loupe web client: a "
                "React 19 + TypeScript SPA with a shared @loupe/core layer, SCSS "
                "modules, and a custom SVG charting primitive.\n\n"
                "You'll own card detail, search, the markets surface, and the "
                "portfolio dashboard — reusable, accessible, theme-aware components "
                "that feel as good as a top-tier fintech app. Strong React, an eye "
                "for detail, and care about performance and clean, bundled code.\n\n"
                "Bonus: data-viz/charting, design-systems, and React Native "
                "experience."
            ),
        },
    ]
    for j in jobs:
        j["id"] = uuid.uuid4()
        j["employment_type"] = "full_time"
        j["status"] = "open"
    return jobs


def upgrade() -> None:
    op.bulk_insert(_BLOG, _blogs())
    op.bulk_insert(_JOB, _jobs())


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.delete(_BLOG).where(_BLOG.c.slug.in_(_BLOG_SLUGS))  # type: ignore[arg-type]
    )
    bind.execute(
        sa.delete(_JOB).where(_JOB.c.slug.in_(_JOB_SLUGS))  # type: ignore[arg-type]
    )
