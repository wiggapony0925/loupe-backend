# Social

The community layer, kept as one self-contained vertical slice so it can
grow without spreading across the codebase.

```
app/social/
  models.py        Profiles, the follow graph, AND the feed's tables
  schemas.py       Pydantic request/response shapes
  avatars.py       Profile-picture storage (S3-compatible blob store)
  post_media.py    Feed-image storage + an intrinsic-size probe
  router.py        /v1/social/* — profiles, graph, shared collections
  feed_router.py   /v1/social/* — posts, comments, hashtags
  service.py       Façade over services/ (historic import path)
  services/
    _common.py     username resolution, privacy gate, directory rows
    profiles.py graph.py engagement.py discovery.py collections.py
    feed_common.py caption parsing, cursors, the feed privacy predicate
    posts.py comments.py hashtags.py feed_notify.py
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

## The feed

Posts, threaded comments, hashtags and mentions — migration `0050_social_feed`.

- **Posts** — a caption plus up to four images, optionally *about* a catalog
  card. Soft-deleted, so removing one doesn't vaporise other people's replies.
- **Three feeds, defined server-side.** `following` (people you follow, plus
  your own), `foryou` (recent posts ranked by engagement over a decaying age
  term, excluding your own), `mine`. What a tab CONTAINS is a product
  decision; the clients ask for one and render the answer, so web and native
  can never disagree about what "Following" means.
- **One privacy rule, expressed as SQL.** `visible_posts_predicate` gates
  every feed, tag page, permalink, like and comment — a gate that has to be
  remembered per call site is one that will eventually be forgotten at one.
  A private account's post 404s rather than 403s: distinguishing the two
  would confirm the post exists.
- **No denormalised counters.** Likes and comments are counted per page with
  one grouped query over exactly the ids on screen. Exact, and no write path
  can drift them. Counters can be added later as a cache over this truth.
- **Threading is one level deep**, like Instagram. Replying to a reply
  attaches to its top-level parent — the service flattens it, so a client can
  pass whatever the user tapped.
- **Hashtags/mentions are extracted on write** into their own tables, and the
  payload lists them, so a client links only the words that actually resolve.
- **Cursors, not offsets**, for feeds: a feed gains rows at the top while it
  is being read, and offset paging answers that by showing a post twice.
- **Notifications** — `feed_notify.py` owns the wording of every community
  notification and is best-effort: a failed inbox write never fails the like.

## Where this is going (add new modules beside the existing files)

| Idea | Sketch |
|------|--------|
| Direct messages | `dm.py` — per-pair threads; gate on mutual follow to stop spam. The clients already leave room for the entry point. |
| Trade requests | `trades.py` — offer items from your vault for theirs; state machine `proposed → countered → accepted/declined`; reuses `SocialFollow` for who may propose. |
| Blocking & reporting | extend `models.py` with `social_blocks`; a block severs both follow edges (see flim's `block_severs_follows` trigger for the reference implementation). |
| Reposts / quotes | a nullable `repost_of_id` on `social_posts`; the feed query already pages by `(created_at, id)`. |
| Saved posts | a `social_post_saves` edge, and a fourth tab that reads it. |

When adding a table: model here in `models.py` (it's imported by
`app/models/__init__.py` so alembic autogenerate + the test schema see it),
plus a numbered migration in `app/db/alembic/versions/`.
