"""Compatibility façade over :mod:`app.social.services`.

The implementation moved into per-domain modules; this keeps the historic
``from app.social import service`` call sites (router, seeds, tests)
working unchanged. New code should import the domain module it means.
"""

from __future__ import annotations

from app.social.services._common import (
    MAX_PAGE_SIZE,
    RESERVED_USERNAMES,
)
from app.social.services.collections import collection, friend_owners, portfolio_items
from app.social.services.discovery import discover, search, suggested
from app.social.services.engagement import like, unlike, view_profile
from app.social.services.graph import (
    accept_request,
    decline_request,
    follow,
    followers,
    following,
    incoming_requests,
    remove_follower,
    unfollow,
)
from app.social.services.profiles import (
    avatar_bytes,
    deactivate,
    get_me,
    set_avatar,
    upsert_me,
)

__all__ = [
    "MAX_PAGE_SIZE",
    "RESERVED_USERNAMES",
    "accept_request",
    "avatar_bytes",
    "collection",
    "deactivate",
    "decline_request",
    "discover",
    "follow",
    "followers",
    "following",
    "friend_owners",
    "get_me",
    "incoming_requests",
    "like",
    "portfolio_items",
    "remove_follower",
    "search",
    "set_avatar",
    "suggested",
    "unfollow",
    "unlike",
    "upsert_me",
    "view_profile",
]
