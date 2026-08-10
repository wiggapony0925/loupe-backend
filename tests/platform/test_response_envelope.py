"""The envelope middleware's shape detection.

``_looks_like_legacy_pagination`` decides whether a response body gets
rewritten — ``items`` promoted to ``data``, everything else discarded. That
rewrite is only correct for the four-field legacy page; a page model
carrying anything more (``NotificationPage.unread``) is a typed payload its
clients consume verbatim, and flattening it silently deletes fields.
"""

from __future__ import annotations

from app.http.response_envelope import _looks_like_legacy_pagination


def _page(**extra: object) -> dict[str, object]:
    return {"items": [], "total": 0, "page": 1, "page_size": 25, **extra}


def test_the_exact_legacy_shape_is_lifted() -> None:
    assert _looks_like_legacy_pagination(_page())


def test_a_page_with_extra_fields_is_left_alone() -> None:
    # NotificationPage: the badge count must reach the client.
    assert not _looks_like_legacy_pagination(_page(unread=3))


def test_non_pages_are_left_alone() -> None:
    assert not _looks_like_legacy_pagination({"items": [], "total": 0})
    assert not _looks_like_legacy_pagination(_page(items=None))
    assert not _looks_like_legacy_pagination(_page(total="12"))
    assert not _looks_like_legacy_pagination([1, 2, 3])
    assert not _looks_like_legacy_pagination(None)
