# Social

The community layer, kept as one self-contained vertical slice so it can
grow without spreading across the codebase.

```
app/social/
  models.py    SocialProfile, SocialFollow, SocialFollowRequest
  schemas.py   Pydantic request/response shapes
  service.py   All business logic (Instagram follow semantics)
  avatars.py   Profile-picture storage (S3-compatible blob store)
  router.py    /v1/social/* HTTP surface
```

## What ships in the MVP

- **Profiles** — claimable `@username` (lowercase, unique), bio, optional
  self-reported location, profile picture. Display name stays on `users`.
- **Follow graph** — follow/unfollow, follower & following lists + counts.
- **Private accounts** — following a private profile creates a *request*
  the owner accepts/declines. Going public auto-accepts everything pending.
  Existing followers survive going private (Instagram semantics).
- **User search** — handle/display-name typeahead for the Community page.
- **Shared collections** — `GET /users/{username}/collection` shows another
  collector's vault (grade-aware values, **never** cost basis or notes),
  gated by privacy.
- **Kill switch** — the `web_social` feature flag hides the whole surface.

## Where this is going (add new modules beside the MVP files)

| Idea | Sketch |
|------|--------|
| Trade requests | `trades.py` — offer items from your vault for theirs; state machine `proposed → countered → accepted/declined`; reuses `SocialFollow` for who may propose. |
| Posts | `posts.py` — pull showcases (photo or vault item + caption), reactions/comments; feed = posts from people you follow. |
| Direct messages | `dm.py` — per-pair threads; gate on mutual follow to stop spam. |
| Blocking & reporting | extend `models.py` with `social_blocks`; a block severs both follow edges (see flim's `block_severs_follows` trigger for the reference implementation). |
| Notifications | reuse `push_service` — "@x requested to follow you", "@y accepted". |

When adding a table: model here in `models.py` (it's imported by
`app/models/__init__.py` so alembic autogenerate + the test schema see it),
plus a numbered migration in `app/db/alembic/versions/`.
