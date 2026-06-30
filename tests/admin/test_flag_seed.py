"""Tests for the developer-portal feature-flag seeder."""

import pytest
from sqlalchemy import select

from app.models.feature_flag import FeatureFlag
from app.services.admin import flag_service
from app.services.admin.flag_seed import ADMIN_PAGE_FLAGS, seed_admin_flags


@pytest.mark.asyncio
async def test_seeds_all_admin_flags_enabled(db_session):
    added = await seed_admin_flags(db_session)
    assert added == len(ADMIN_PAGE_FLAGS)

    # Admins read these from the admin list (list_all), not the public map.
    by_key = {f.key: f for f in await flag_service.list_all(db_session)}
    for key, _label, _desc in ADMIN_PAGE_FLAGS:
        assert by_key[key].enabled is True  # present + enabled


@pytest.mark.asyncio
async def test_admin_flags_are_withheld_from_the_public_map(db_session):
    await seed_admin_flags(db_session)
    public = await flag_service.public_map(db_session)
    # No admin_* key leaks to the unauthenticated public flag map.
    assert not any(k.startswith("admin_") for k in public)


@pytest.mark.asyncio
async def test_seed_is_idempotent(db_session):
    first = await seed_admin_flags(db_session)
    second = await seed_admin_flags(db_session)
    assert first > 0
    assert second == 0  # nothing new to add the second time

    # No duplicates created.
    keys = [k for (k,) in (await db_session.execute(select(FeatureFlag.key))).all()]
    assert len(keys) == len(set(keys))


@pytest.mark.asyncio
async def test_seed_never_overwrites_an_admin_choice(db_session):
    # Admin disabled one page's flag.
    db_session.add(
        FeatureFlag(key="admin_health", label="x", description=None, enabled=False)
    )
    await db_session.commit()

    await seed_admin_flags(db_session)

    by_key = {f.key: f for f in await flag_service.list_all(db_session)}
    assert by_key["admin_health"].enabled is False  # left exactly as the admin set it


def test_seeder_keys_are_valid_flag_keys():
    # Keys must satisfy the flag service's key rule (lowercase/digits/underscore).
    for key, _label, _desc in ADMIN_PAGE_FLAGS:
        assert flag_service._KEY_RE.match(key), key
