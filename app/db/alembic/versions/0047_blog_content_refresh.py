"""Rewrite the seeded blog posts to describe the product as it actually is.

``0012_portal`` seeded six placeholder articles to give the portal something to
render. They shipped to production and became the *only* content in the blog —
and once the mobile notification inbox started sourcing "news" from published
posts, they became the demo text users saw in their inbox.

This replaces the body copy with accurate writing about features that really
exist today (grade-aware valuation, the scanner, Loupe Grade, alerts,
statements, Pro, Community), keeping every ``slug`` stable so existing links,
shares and notification hrefs still resolve.

Content-only: no schema change, and it updates by slug so a post the team has
since rewritten by hand is matched on identity rather than position. Rows that
no longer exist are skipped rather than re-inserted — deleting a post should
stay deleted.

Revision ID: 0047_blog_content_refresh
Revises: 0046_merge_heads
Create Date: 2026-08-05 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047_blog_content_refresh"
down_revision: str | Sequence[str] | None = "0046_merge_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


POSTS: list[dict[str, object]] = [
    {
        "slug": "grade-aware-valuations",
        "title": "A raw card and a PSA 10 are not the same asset",
        "tag": "Product",
        "read_minutes": 4,
        "excerpt": (
            "Most price tools quote one number per card. That number is wrong "
            "for almost everyone, because condition is most of the value."
        ),
        "body": (
            "Ask what a Charizard is worth and you'll get a single figure. But "
            "the same card in a PSA 10 slab and the same card loose in a "
            "binder can differ by an order of magnitude. A collection valued "
            "on one blended price isn't approximately right — it's wrong for "
            "every card you own.\n\n"
            "## Your total is grade-aware\n\n"
            "Loupe values each holding on the grade you actually own. When you "
            "add a card you tell us the condition — raw, or the grader and "
            "number on the slab — and that specific ladder rung is what feeds "
            "your total. Change a card's grade and your portfolio total moves, "
            "because it genuinely did.\n\n"
            "## One number, everywhere\n\n"
            "The figure on your vault, your analytics, your monthly statement "
            "and your price alerts all come from the same basis. There is no "
            "second definition of 'what my collection is worth' hiding in "
            "another tab.\n\n"
            "## When we don't know, we say so\n\n"
            "Graded market data is thinner than raw data, and some rungs have "
            "no recent sales at all. Where a ladder is modelled rather than "
            "observed, we show it as such. A confident wrong number is worse "
            "than an honest gap."
        ),
    },
    {
        "slug": "building-loupe-value",
        "title": "From a wall of prices to one number you can trust",
        "tag": "Product",
        "read_minutes": 4,
        "excerpt": (
            "Every marketplace quotes a different price. Here's how we turn "
            "that noise into a single figure — and what we do when sources "
            "disagree."
        ),
        "body": (
            "Look up one card and you'll find a dozen numbers: a marketplace "
            "ask, a recent sold, a graded comp, a shop's shelf price. None is "
            "wrong. They're answering different questions.\n\n"
            "## We prefer what actually happened\n\n"
            "Realised sales beat listings. Anyone can ask any price; a "
            "completed sale is two people agreeing. Where we have recent sold "
            "data we lead with it, and we fall back to asks only when there's "
            "nothing better.\n\n"
            "## Recency is weighted, not ignored\n\n"
            "A sale from last week says more than one from last spring. Prices "
            "refresh daily, and your portfolio history records a real point "
            "each day rather than back-filling a smooth line — which is why a "
            "flat stretch in your chart means a quiet market, not missing "
            "data.\n\n"
            "## Currency is a display layer\n\n"
            "Set your currency once and every surface follows it. The "
            "conversion happens at the edge, on live rates, so the underlying "
            "record stays in the currency the sale happened in."
        ),
    },
    {
        "slug": "compare-any-grade-instantly",
        "title": "See what grading would actually be worth",
        "tag": "Product",
        "read_minutes": 3,
        "excerpt": (
            "Before you spend $30 and wait months, it helps to know what a 10 "
            "would be worth — and how likely you are to get one."
        ),
        "body": (
            "Grading is a bet. You pay a fee, wait, and hope the card comes "
            "back high enough to justify both. The information that makes that "
            "bet reasonable is usually scattered across sold listings.\n\n"
            "## The whole ladder, side by side\n\n"
            "Open any card and you can see the price at each grade at once, so "
            "the gap between a 9 and a 10 stops being an abstraction. That gap "
            "is the entire economics of grading.\n\n"
            "## Pre-screen before you ship\n\n"
            "Loupe Grade takes a photo of your card and estimates centering "
            "and condition, then shows the PSA 10 upside against what you'd "
            "pay to find out. It won't replace a grader's eye, and it isn't "
            "meant to — it's there to stop you submitting a card that was "
            "never going to come back clean.\n\n"
            "## An estimate, clearly labelled\n\n"
            "We show you the measurement we made and the confidence behind it. "
            "It's a pre-screen, not a verdict."
        ),
    },
    {
        "slug": "vault-as-a-portfolio",
        "title": "Treat your collection like a portfolio",
        "tag": "Collecting",
        "read_minutes": 5,
        "excerpt": (
            "Cost basis, profit and loss, and concentration aren't just for "
            "stocks. The same lens makes a collection far easier to manage."
        ),
        "body": (
            "When you record what you paid, your vault stops being a list and "
            "becomes a position. You can see unrealised gains, which cards "
            "carry the most of your value, and whether a set is drifting up or "
            "down.\n\n"
            "## Cost basis is optional, and never invented\n\n"
            "Adding a purchase price unlocks real profit and loss. If you "
            "don't enter one we leave it out of the P/L rather than guessing — "
            "a made-up basis would quietly corrupt every number downstream.\n\n"
            "## Concentration is the useful surprise\n\n"
            "Most collections are far more concentrated than their owners "
            "expect. Analytics shows how much of your total sits in your top "
            "few cards, which is usually the first thing worth acting on.\n\n"
            "## Statements you can keep\n\n"
            "Every month Loupe can generate a PDF statement — closing value, "
            "the period's movers, and every holding — archived the way a "
            "brokerage would. It's the record you'll want for insurance, or "
            "for the version of you three years from now.\n\n"
            "## Collections, not one big pile\n\n"
            "Split your vault into collections and every total, chart and "
            "statement can be scoped to just that slice."
        ),
    },
    {
        "slug": "loupe-on-web-and-mobile",
        "title": "One collection, on every screen you own",
        "tag": "Announcement",
        "read_minutes": 3,
        "excerpt": (
            "Scan on your phone, study on a laptop. Same data, same numbers, "
            "no syncing to think about."
        ),
        "body": (
            "Collecting happens in two modes. There's the card shop and the "
            "mail day, where your phone is the only sensible tool. And there's "
            "the evening with a spreadsheet open, where a big screen wins.\n\n"
            "## The phone is the camera\n\n"
            "Point it at a card and Loupe identifies it — set, number and "
            "printing — then adds it to your vault at the grade you choose. "
            "Price alerts arrive as push notifications, so a card crossing "
            "your number reaches you wherever you are.\n\n"
            "## The web is the desk\n\n"
            "Filtering a few thousand cards, reconciling a bulk purchase, or "
            "reading a statement is simply better with a keyboard.\n\n"
            "## The backend is the source of truth\n\n"
            "Both apps call the same API and render the same computed values. "
            "That's a deliberate architectural choice, and it's why your total "
            "can't disagree with itself between devices."
        ),
    },
    {
        "slug": "what-we-shipped-this-month",
        "title": "What we shipped: Community, smarter search, and a real inbox",
        "tag": "Changelog",
        "read_minutes": 3,
        "excerpt": (
            "Profiles and follows, search that understands a description, and "
            "notifications that finally live on the server."
        ),
        "body": (
            "## Community\n\n"
            "Loupe now has profiles. Follow other collectors, see what they're "
            "building, and keep your own collection private if you'd rather — "
            "private accounts approve each follower individually, and nothing "
            "is shared until you say so.\n\n"
            "## Search that takes a description\n\n"
            "You don't always know the name. Describe the card — the "
            "orange dragon from the old base set — and search will work out "
            "what you meant, then hand you real catalog results.\n\n"
            "## A real notification inbox\n\n"
            "Alerts, articles and account notices now live in one place on the "
            "server rather than being assembled on your device. That means "
            "what you've read stays read when you reinstall, and the badge "
            "agrees across your phone and the web.\n\n"
            "## Set checklists\n\n"
            "Tap any set to see exactly which cards you have and which you're "
            "missing, with the missing ones ready to hunt.\n\n"
            "## Everywhere else\n\n"
            "Faster browsing on a much larger cached catalog, Japanese cards "
            "in search, and a long list of fixes to valuation edge cases."
        ),
    },
]


def upgrade() -> None:
    blog = sa.table(
        "blog_posts",
        sa.column("slug", sa.String),
        sa.column("title", sa.String),
        sa.column("excerpt", sa.Text),
        sa.column("body", sa.Text),
        sa.column("tag", sa.String),
        sa.column("read_minutes", sa.Integer),
    )
    conn = op.get_bind()
    for post in POSTS:
        # Match on slug: identity, not row order. A post the team has since
        # deleted stays deleted (0 rows updated is a fine outcome here).
        conn.execute(
            blog.update()
            .where(blog.c.slug == post["slug"])
            .values(
                title=post["title"],
                excerpt=post["excerpt"],
                body=post["body"],
                tag=post["tag"],
                read_minutes=post["read_minutes"],
            )
        )


def downgrade() -> None:
    # Content-only migration. The previous placeholder copy has no value worth
    # restoring, and rewriting user-visible text backwards would be surprising.
    pass
