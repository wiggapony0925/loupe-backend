"""Create ``user_reports`` table for monthly / yearly portfolio statements.

Each row tracks one generated PDF statement attached to a user. The PDF
binary lives in object storage (GCS / S3); this table is the registry
that powers `/v1/reports` listing + download.

The DDL is emitted as raw SQL because SQLAlchemy's column-level Enum
ignores ``create_type=False`` inside ``_on_table_create`` when the Enum
isn't bound to a MetaData (it re-emits ``CREATE TYPE`` with
``checkfirst=False``), which breaks repeatable migrations on Postgres.
Raw SQL with a ``DO`` block sidesteps that bug entirely and stays
idempotent.

Revision ID: 0006_user_reports
Revises: 0005_price_alerts
Create Date: 2026-05-21 10:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_user_reports"
down_revision: str | Sequence[str] | None = "0005_price_alerts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE report_period_enum AS ENUM ('monthly', 'yearly');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE report_status_enum AS ENUM ('pending', 'ready', 'failed');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_reports (
            id              UUID PRIMARY KEY,
            user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            period          report_period_enum NOT NULL,
            period_start    DATE NOT NULL,
            period_end      DATE NOT NULL,
            status          report_status_enum NOT NULL DEFAULT 'pending',
            storage_key     VARCHAR(512),
            file_size_bytes BIGINT,
            title           VARCHAR(120) NOT NULL,
            error_message   VARCHAR(500),
            generated_at    TIMESTAMP WITH TIME ZONE,
            created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT uq_user_reports_user_period_start
                UNIQUE (user_id, period, period_start)
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_reports_user_id "
        "ON user_reports (user_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_reports_user_created_at "
        "ON user_reports (user_id, created_at);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_user_reports_user_created_at;")
    op.execute("DROP INDEX IF EXISTS ix_user_reports_user_id;")
    op.execute("DROP TABLE IF EXISTS user_reports;")
    op.execute("DROP TYPE IF EXISTS report_status_enum;")
    op.execute("DROP TYPE IF EXISTS report_period_enum;")
