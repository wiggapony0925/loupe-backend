"""The notification template catalog.

What's pinned: the registry's rendering rules (missing params are caller
bugs; a None param empties the body instead of printing "None"), the
catalog's internal consistency, and — through one real send — that the
inbox row and the push carry the SAME strings, which is the whole point
of rendering both from one template.
"""

from __future__ import annotations

import pytest

from app.models.notification import CATEGORIES
from app.services import notification_templates, push_service
from app.services.notification_templates import TEMPLATES, render
from tests.factories import make_user


@pytest.fixture(autouse=True)
def _no_push(monkeypatch):
    calls: list = []

    async def fake_send(user_id, **kwargs):
        calls.append((user_id, kwargs))
        return 1

    monkeypatch.setattr(push_service, "send_to_user", fake_send)
    return calls


def test_every_template_declares_a_known_category_and_a_kind() -> None:
    for template_id, t in TEMPLATES.items():
        assert t.category in CATEGORIES, template_id
        assert t.kind, template_id
        assert t.title, template_id


def test_dedupe_keys_are_recipient_or_actor_scoped() -> None:
    """A dedupe key that ignores who it's for would collapse notifications
    ACROSS people — one user's delivery suppressing another's."""
    for template_id, t in TEMPLATES.items():
        if t.dedupe is None:
            continue
        assert "{recipient_id}" in t.dedupe or "{actor_id}" in t.dedupe, template_id


def test_a_none_param_drops_the_body_instead_of_printing_none() -> None:
    r = render(
        "social_post_like",
        {
            "actor": "Ash",
            "actor_id": "a",
            "post_id": "p",
            "preview": None,
            "recipient_id": "r",
        },
    )
    assert r.body is None
    assert r.title == "Ash liked your post"


def test_a_missing_title_param_is_a_loud_caller_bug() -> None:
    with pytest.raises(KeyError):
        render("social_new_post", {"post_id": "p", "recipient_id": "r"})


def test_mention_surfaces_share_a_kind_but_not_copy() -> None:
    base = {
        "actor": "Ash",
        "post_id": "p",
        "comment_id": "c",
        "preview": "look",
        "recipient_id": "r",
    }
    in_post = render("social_mention_post", base)
    in_comment = render("social_mention_comment", base)
    assert in_post.kind == in_comment.kind == "social_mention"
    assert in_post.title != in_comment.title
    # Historical dedupe schemes preserved — a deploy must not re-notify
    # events that were already delivered under the old keys.
    assert in_post.dedupe_key == "social_post_mention:p:r"
    assert in_comment.dedupe_key == "social_comment:c:r"


@pytest.mark.asyncio
async def test_the_inbox_row_and_the_push_are_the_same_strings(
    db_session, _no_push
) -> None:
    user = await make_user(db_session)
    row = await notification_templates.send(
        db_session,
        user.id,
        "social_new_post",
        actor="Misty",
        post_id="deadbeef",
        summary="Pulled a base set holo",
        data={"post_id": "deadbeef"},
    )
    assert row is not None
    assert row.title == "Misty posted"
    assert row.body == "Pulled a base set holo"
    assert row.href == "/app/community/p/deadbeef"

    assert len(_no_push) == 1
    _, push_kwargs = _no_push[0]
    # The phone shows exactly what the inbox stores — same title, same
    # body, and the deep link rides along for the tap.
    assert push_kwargs["title"] == row.title
    assert push_kwargs["body"] == row.body
    assert push_kwargs["data"]["href"] == row.href
