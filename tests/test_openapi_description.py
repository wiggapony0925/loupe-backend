"""OpenAPI description assembly test."""

from documentation.render_openapi import build_full_description


def test_description_includes_overview():
    desc = build_full_description()
    assert "Loupe Backend Overview" in desc
    assert "Tag Reference" in desc
    assert "Upstream Services" in desc
