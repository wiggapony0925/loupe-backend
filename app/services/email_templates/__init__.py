"""Every email Loupe sends, as pure ``build_*`` functions → :class:`EmailContent`.

Structure (see README.md in this folder for how to add a template):

- ``theme.py``  — palette / typography / reusable inline-style snippets (the "CSS")
- ``base.py``   — the document skeleton: ``render_email`` (html + text part)
- domain modules — one builder per email, grouped by area

Builders are pure (no I/O), which is what makes the /admin/email preview
gallery and the test suite render production-identical output. Delivery
lives in :mod:`app.services.email_service`.
"""

from app.services.email_templates.account import (
    build_admin_granted,
    build_ban_notice,
    build_verify_email,
    build_welcome,
)
from app.services.email_templates.alerts import (
    build_price_alert,
    build_statement_ready,
)
from app.services.email_templates.announcements import (
    build_blog_announcement,
    build_custom_announcement,
    build_support_message,
    unsubscribe_footer,
)
from app.services.email_templates.base import (
    EmailContent,
    render_email,
)
from app.services.email_templates.billing import (
    build_payment_failed,
    build_pro_activated,
    build_pro_canceled,
    build_pro_expiring,
)
from app.services.email_templates.engagement import (
    build_free_limit_reached,
    build_portfolio_digest,
    build_set_completed,
)
from app.services.email_templates.security import (
    build_account_locked,
    build_mfa_disabled,
    build_mfa_enabled,
    build_new_sign_in,
    build_password_changed,
    build_password_reset,
    build_reset_unavailable,
)
from app.services.email_templates.waitlist import (
    build_waitlist_confirmation,
    build_waitlist_invite,
)

__all__ = [
    "EmailContent",
    "build_account_locked",
    "build_admin_granted",
    "build_ban_notice",
    "build_blog_announcement",
    "build_custom_announcement",
    "build_free_limit_reached",
    "build_mfa_disabled",
    "build_mfa_enabled",
    "build_new_sign_in",
    "build_password_changed",
    "build_password_reset",
    "build_payment_failed",
    "build_portfolio_digest",
    "build_price_alert",
    "build_pro_activated",
    "build_pro_canceled",
    "build_pro_expiring",
    "build_reset_unavailable",
    "build_set_completed",
    "build_statement_ready",
    "build_support_message",
    "build_verify_email",
    "build_waitlist_confirmation",
    "build_waitlist_invite",
    "build_welcome",
    "render_email",
    "unsubscribe_footer",
]
