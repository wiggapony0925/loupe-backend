"""Tests for the auth.jwt module (issue + verify, error paths)."""

import uuid

import pytest

from app.auth.jwt import issue_token, verify_token


def test_issue_and_verify_access_token():
    user_id = uuid.uuid4()
    token, ttl = issue_token(user_id, "access")
    assert ttl > 0
    claims = verify_token(token, "access")
    assert claims["sub"] == str(user_id)
    assert claims["typ"] == "access"


def test_verify_token_wrong_type_rejected():
    user_id = uuid.uuid4()
    token, _ = issue_token(user_id, "access")
    with pytest.raises(Exception):
        verify_token(token, "refresh")


def test_verify_token_garbage_rejected():
    with pytest.raises(Exception):
        verify_token("not.a.jwt", "access")
