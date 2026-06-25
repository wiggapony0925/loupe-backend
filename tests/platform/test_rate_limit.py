"""Rate-limit client-IP resolution (X-Forwarded-For trust handling)."""

from __future__ import annotations

from types import SimpleNamespace

from app.config import Settings
from app.platform import rate_limit


class _Req:
    def __init__(self, xff: str | None = None, client_host: str | None = None) -> None:
        self.headers = {"x-forwarded-for": xff} if xff is not None else {}
        self.client = SimpleNamespace(host=client_host) if client_host else None


def _with_hops(monkeypatch, n: int) -> None:
    monkeypatch.setattr(
        rate_limit,
        "get_settings",
        lambda: Settings(rate_limit_trusted_proxy_hops=n),
    )


def test_default_hops_uses_leftmost(monkeypatch):
    # Backward-compatible default: leftmost XFF entry.
    _with_hops(monkeypatch, 0)
    assert rate_limit._client_key(_Req(xff="1.1.1.1, 2.2.2.2, 3.3.3.3")) == "1.1.1.1"


def test_hops_1_uses_rightmost_unspoofable(monkeypatch):
    # With one trusted proxy (e.g. direct Cloud Run), the real client IP is the
    # rightmost entry the proxy appended — a spoofed leftmost value is ignored.
    _with_hops(monkeypatch, 1)
    assert rate_limit._client_key(_Req(xff="9.9.9.9, 2.2.2.2, 8.8.8.8")) == "8.8.8.8"


def test_hops_2_uses_second_from_right(monkeypatch):
    _with_hops(monkeypatch, 2)
    assert rate_limit._client_key(_Req(xff="9.9.9.9, 5.5.5.5, 8.8.8.8")) == "5.5.5.5"


def test_hops_exceeding_entries_falls_back_to_leftmost(monkeypatch):
    _with_hops(monkeypatch, 3)
    assert rate_limit._client_key(_Req(xff="1.1.1.1")) == "1.1.1.1"


def test_no_xff_uses_socket(monkeypatch):
    _with_hops(monkeypatch, 1)
    assert rate_limit._client_key(_Req(client_host="10.0.0.5")) == "10.0.0.5"


def test_no_xff_no_socket_is_unknown(monkeypatch):
    _with_hops(monkeypatch, 0)
    assert rate_limit._client_key(_Req()) == "unknown"
