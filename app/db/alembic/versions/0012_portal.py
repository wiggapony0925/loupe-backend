"""Create the careers + blog developer-portal tables.

Adds ``job_postings``, ``job_applications``, ``application_events`` and
``blog_posts`` — the storage behind the public careers/blog pages and the
admin portal. Status columns are plain strings (enum *values*) so the
schema stays portable across SQLite and Postgres. Seeds a handful of open
roles and published posts so the public pages aren't empty on first deploy.

Revision ID: 0012_portal
Revises: 0011_card_image_hash
Create Date: 2026-06-19 14:00:00
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

from app.db.types import UuidCol

revision: str = "0012_portal"
down_revision: str | Sequence[str] | None = "0011_card_image_hash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_postings",
        sa.Column("id", UuidCol(), primary_key=True),
        sa.Column("slug", sa.String(160), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("team", sa.String(80), nullable=False),
        sa.Column("location", sa.String(120), nullable=False),
        sa.Column(
            "employment_type", sa.String(20), nullable=False, server_default="full_time"
        ),
        sa.Column("summary", sa.String(400), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("slug", name="uq_job_postings_slug"),
    )
    op.create_index("ix_job_postings_slug", "job_postings", ["slug"], unique=True)
    op.create_index("ix_job_postings_status", "job_postings", ["status"])
    op.create_index(
        "ix_job_postings_status_created", "job_postings", ["status", "created_at"]
    )

    op.create_table(
        "job_applications",
        sa.Column("id", UuidCol(), primary_key=True),
        sa.Column(
            "job_id",
            UuidCol(),
            sa.ForeignKey("job_postings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("applicant_name", sa.String(160), nullable=False),
        sa.Column("applicant_email", sa.String(320), nullable=False),
        sa.Column("linkedin_url", sa.String(500), nullable=True),
        sa.Column("resume_url", sa.String(1024), nullable=True),
        sa.Column("cover_letter", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="submitted"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_job_applications_job_id", "job_applications", ["job_id"])
    op.create_index(
        "ix_job_applications_applicant_email", "job_applications", ["applicant_email"]
    )
    op.create_index("ix_job_applications_status", "job_applications", ["status"])
    op.create_index(
        "ix_job_applications_job_created", "job_applications", ["job_id", "created_at"]
    )

    op.create_table(
        "application_events",
        sa.Column("id", UuidCol(), primary_key=True),
        sa.Column(
            "application_id",
            UuidCol(),
            sa.ForeignKey("job_applications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column(
            "created_by_user_id",
            UuidCol(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("notified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_application_events_application_id", "application_events", ["application_id"]
    )

    op.create_table(
        "blog_posts",
        sa.Column("id", UuidCol(), primary_key=True),
        sa.Column("slug", sa.String(200), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("excerpt", sa.String(400), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("tag", sa.String(60), nullable=False, server_default="Update"),
        sa.Column(
            "author", sa.String(120), nullable=False, server_default="The Loupe Team"
        ),
        sa.Column("cover_image_url", sa.String(1024), nullable=True),
        sa.Column("read_minutes", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("slug", name="uq_blog_posts_slug"),
    )
    op.create_index("ix_blog_posts_slug", "blog_posts", ["slug"], unique=True)
    op.create_index("ix_blog_posts_status", "blog_posts", ["status"])
    op.create_index(
        "ix_blog_posts_status_published", "blog_posts", ["status", "published_at"]
    )

    _seed()


def _seed() -> None:
    """Insert starter open roles + published posts (idempotent-ish: only on a fresh table)."""
    now = datetime.now(timezone.utc)

    jobs = sa.table(
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
    op.bulk_insert(
        jobs,
        [
            {
                "id": uuid.uuid4(),
                "slug": "senior-full-stack-engineer",
                "title": "Senior Full-Stack Engineer",
                "team": "Engineering",
                "location": "Remote (US)",
                "employment_type": "full_time",
                "summary": "Own end-to-end features across our React web client, Expo app, and FastAPI backend.",
                "description": (
                    "We're looking for a senior engineer comfortable across the stack — "
                    "TypeScript/React on the front end, Python/FastAPI on the back end. "
                    "You'll ship user-facing features from database to pixel and help shape "
                    "the architecture as we scale to thousands of collectors."
                ),
                "status": "open",
            },
            {
                "id": uuid.uuid4(),
                "slug": "data-engineer-pricing",
                "title": "Data Engineer, Pricing",
                "team": "Data",
                "location": "Remote (US)",
                "employment_type": "full_time",
                "summary": "Build the pipelines that turn noisy marketplace data into clean, grade-aware valuations.",
                "description": (
                    "Design and operate the ingestion + valuation pipelines that power every "
                    "price in Loupe. Strong SQL and Python, a healthy obsession with data "
                    "quality, and an interest in marketplaces a big plus."
                ),
                "status": "open",
            },
            {
                "id": uuid.uuid4(),
                "slug": "product-designer",
                "title": "Product Designer",
                "team": "Design",
                "location": "Remote (US/EU)",
                "employment_type": "full_time",
                "summary": "Shape a product collectors love — from the vault to the charts to the smallest interaction.",
                "description": (
                    "Lead design across web and mobile. You'll own flows end-to-end, from "
                    "research through polished, shipped UI, in a small team that values craft."
                ),
                "status": "open",
            },
        ],
    )

    posts = sa.table(
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
    op.bulk_insert(
        posts,
        [
            {
                "id": uuid.uuid4(),
                "slug": "grade-aware-valuations",
                "title": "Why grade-aware valuations matter",
                "excerpt": (
                    "A raw card and a PSA 10 are not the same asset. Here's how Loupe prices "
                    "the difference — and why a single 'market price' is misleading."
                ),
                "body": (
                    "Ask most price trackers what a card is worth and you'll get one number. "
                    "But anyone who collects knows that number hides an enormous range — the "
                    "same card can trade for $5 raw and $500 in a PSA 10 slab.\n\n"
                    "## One card, many markets\n\n"
                    "Loupe treats every grade and grading house as its own market. We aggregate "
                    "recent sales and active listings per tier, so the value you see reflects the "
                    "card you actually own — not an average of everything.\n\n"
                    "## Honest by design\n\n"
                    "We date every figure, label its source, and let you override any card's "
                    "estimated value. The goal isn't a magic number; it's a number you can trust."
                ),
                "tag": "Product",
                "author": "The Loupe Team",
                "read_minutes": 4,
                "status": "published",
                "published_at": now,
            },
            {
                "id": uuid.uuid4(),
                "slug": "vault-as-a-portfolio",
                "title": "Treat your collection like a portfolio",
                "excerpt": (
                    "Cost basis, P/L, and concentration aren't just for stocks. The same lens "
                    "makes your collection far easier to manage."
                ),
                "body": (
                    "When you log what you paid for a card, your vault stops being a list and "
                    "starts being a portfolio. Suddenly you can see unrealized gains, your most "
                    "concentrated positions, and how your sets are trending.\n\n"
                    "## Cost basis changes everything\n\n"
                    "Adding a purchase price is optional, but it unlocks the most useful view in "
                    "Loupe: real profit and loss. We never assume a cost you didn't enter.\n\n"
                    "## Add it in seconds\n\n"
                    "Open any card, tap Add to collection, pick the grade, and optionally enter "
                    "what you paid. It shows up in your vault and analytics immediately."
                ),
                "tag": "Collecting",
                "author": "The Loupe Team",
                "read_minutes": 5,
                "status": "published",
                "published_at": now,
            },
            {
                "id": uuid.uuid4(),
                "slug": "loupe-on-web-and-mobile",
                "title": "Loupe comes to the web",
                "excerpt": (
                    "Everything you love about the mobile app, now in your browser — same "
                    "account, same data, fully synced."
                ),
                "body": (
                    "The Loupe web app is here. Browse the market, track prices, and manage your "
                    "entire collection from any browser — with the exact same account and data as "
                    "the iOS app.\n\n"
                    "## Built on shared foundations\n\n"
                    "Web and mobile run on the same backend and the same core logic, so a card you "
                    "add on your phone appears on the web instantly, and vice versa."
                ),
                "tag": "Announcement",
                "author": "The Loupe Team",
                "read_minutes": 3,
                "status": "published",
                "published_at": now,
            },
        ],
    )


def downgrade() -> None:
    op.drop_table("application_events")
    op.drop_table("job_applications")
    op.drop_table("job_postings")
    op.drop_table("blog_posts")
