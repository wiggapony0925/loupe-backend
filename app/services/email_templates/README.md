# Email templates

Every email Loupe sends lives here as a pure `build_*` function that returns
`EmailContent(subject, html, text)`. No I/O in this package — delivery,
background dispatch, and provider plumbing live in
`app/services/email_service.py`.

## Layout

| File | What's in it |
| --- | --- |
| `theme.py` | The "CSS": palette, typography, reusable inline-style snippets. Mail clients strip `<style>` tags, so styles must be inlined — this file is the single place to change them. |
| `base.py` | `render_email(heading, body_html, cta, preheader=…, footer_html=…)` → the full HTML document (Outlook-safe tables, hidden preheader, dark-mode meta) **and** the plain-text part in one call. Also the shared helpers: `esc`, `paragraphs`, `usd`, `display_name`, `app_url`. |
| `account.py` | Welcome (with optional confirm-email CTA), verify-email resend, ban notice, admin granted. |
| `security.py` | Password changed/reset, reset-unavailable (social accounts), 2FA on/off. |
| `billing.py` | Pro activated / ended (transition-only — renewals stay silent). |
| `alerts.py` | Price alert fired, statement ready. |
| `waitlist.py` | Scanner waitlist confirmation + invite. |
| `announcements.py` | Blog + admin-composed announcements (with the unsubscribe footer) and one-to-one support messages (without it). |

## Adding a template

1. Write `build_<name>(…) -> EmailContent` in the right domain module.
   Escape every user-supplied value with `esc(...)`; compose body HTML from
   `<p>/<ul>` fragments only and let `render_email` own the document.
2. Re-export it from `__init__.py`.
3. Add a `send_<name>` wrapper in `email_service.py` (build → `queue_content`
   for background delivery, or `send_email` when the caller needs the
   provider result synchronously). Pick a `category` tag — it segments
   analytics in the Resend dashboard.
4. Register it in `app/services/admin/email_preview_service.py` with sample
   data so it shows up (and is test-sendable) on the `/admin/email` portal
   page — the gallery renders the exact production builder, so previews
   can't drift.
5. Add a render test in `tests/email/` (the gallery loop covers structure;
   assert anything template-specific, e.g. escaping or money formatting).

## Rules

- **Plain text part is automatic** — `render_email` derives it, links become
  `label (url)`. Never hand-write it.
- **Announcement-class mail** (goes to everyone) must pass
  `footer_html=unsubscribe_footer(url)` and be sent with per-recipient
  one-click `List-Unsubscribe` headers. Transactional mail must not.
- **No raw HTML from input** — plain-text bodies go through `paragraphs()`.
