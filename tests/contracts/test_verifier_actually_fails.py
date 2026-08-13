"""The verifier must FAIL on a breach. Everything else here depends on it.

WHY THIS FILE IS NOT PARANOIA. The first working version of
``verify_consumer_contracts.py`` passed a removed response field. ``_step()``
returns ``(False, None)`` when a field is absent, the code then asked
``_is_opaque(None)`` — which is True, since None is not a dict — and so every
genuinely missing field was classified "unverifiable" instead of "breach". The
contract suite was green, the can-i-deploy gate was green, and deleting a field
a client renders would have sailed through both.

A contract gate that cannot fail is worse than no gate, because it is trusted.
So the verifier is tested the way it is meant to be used: by breaking a real
OpenAPI schema in each of the ways an API actually breaks, and asserting it
notices. These mutate an in-memory copy of the schema; nothing on disk or in any
database is touched.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = REPO_ROOT / "scripts" / "verify_consumer_contracts.py"


def _verifier():
    name = "verify_consumer_contracts"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, VERIFIER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def V():
    return _verifier()


@pytest.fixture(scope="module")
def spec():
    from app.main import app

    return app.openapi()


#: Two endpoints with real, typed response models, and fields that really exist.
#: If either is ever removed from the API this fixture fails loudly, which is
#: the correct outcome — it would mean the examples need updating.
CONTRACT = {
    "consumer": "verifier-self-test",
    "endpoints": [
        {
            "method": "GET",
            "path": "/v1/me",
            "callSite": "tests/contracts/test_verifier_actually_fails.py",
            "requiredResponseFields": ["id", "email", "display_name"],
            # This consumer states its request list is exhaustive, which is what
            # licenses the "newly required field" check below.
            "requestFields": [],
            "requestFieldsComplete": True,
        },
        {
            "method": "POST",
            "path": "/v1/social/posts",
            "callSite": "tests/contracts/test_verifier_actually_fails.py",
            "requiredResponseFields": ["id", "body", "comment_count"],
        },
    ],
}


def _run(V, contract, spec):
    report = V.Report()
    V.verify(contract, spec, report)
    return report


def test_a_contract_the_api_satisfies_passes(V, spec):
    """The control. Without this, every assertion below could pass because the
    verifier fails on everything."""
    report = _run(V, CONTRACT, spec)
    assert report.ok, f"baseline contract should pass but reported: {report.breaches}"
    assert report.checked_fields == 6


def test_a_deleted_endpoint_is_a_breach(V, spec):
    broken = copy.deepcopy(spec)
    del broken["paths"]["/v1/me"]
    report = _run(V, CONTRACT, broken)
    assert not report.ok
    assert any("GONE" in b for b in report.breaches), report.breaches


def test_a_removed_method_is_a_breach(V, spec):
    broken = copy.deepcopy(spec)
    del broken["paths"]["/v1/social/posts"]["post"]
    report = _run(V, CONTRACT, broken)
    assert not report.ok
    assert any("method no longer accepted" in b for b in report.breaches), (
        report.breaches
    )


def test_a_removed_response_field_is_a_breach(V, spec):
    """THE regression. This is the case the first version passed."""
    broken = copy.deepcopy(spec)
    ref = broken["paths"]["/v1/me"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    model = broken["components"]["schemas"][ref.split("/")[-1]]
    removed = model["properties"].pop("display_name", None)
    assert removed is not None, "fixture drift: /v1/me no longer returns display_name"

    report = _run(V, CONTRACT, broken)
    assert not report.ok, (
        "removing a field a consumer requires was NOT reported as a breach — "
        f"unverifiable={report.unverifiable}"
    )
    assert any("display_name" in b for b in report.breaches), report.breaches
    # and it must be a BREACH, not quietly filed as unverifiable
    assert not any("display_name" in u for u in report.unverifiable)


def test_a_newly_required_request_field_is_a_breach(V, spec):
    """Requiring something the consumer never sends breaks it just as hard as
    deleting something it reads."""
    broken = copy.deepcopy(spec)
    broken["paths"]["/v1/me"]["get"].setdefault("parameters", []).append(
        {
            "name": "tenant_id",
            "in": "query",
            "required": True,
            "schema": {"type": "string"},
        }
    )
    report = _run(V, CONTRACT, broken)
    assert not report.ok
    assert any("tenant_id" in b and "REQUIRED" in b for b in report.breaches), (
        report.breaches
    )


def test_an_untyped_operation_is_unverifiable_and_never_a_silent_pass(V, spec):
    """The honest half.

    Operations with no response_model emit a schema that permits anything, so a
    field claim about them can be neither confirmed nor denied. Reporting that
    as a pass would be the same lie in the opposite direction.
    """
    contract = {
        "consumer": "verifier-self-test",
        "endpoints": [
            {
                "method": "GET",
                "path": "/v1/admin/card-tree",
                "callSite": "test",
                "requiredResponseFields": ["a_field_that_does_not_exist"],
            }
        ],
    }
    report = _run(V, contract, spec)
    assert report.ok, "an unverifiable field must not be reported as a breach"
    assert len(report.unverifiable) == 1, (
        "an unverifiable field must be REPORTED, not silently dropped"
    )


def test_a_path_parameter_is_not_mistaken_for_a_missing_request_field(V, spec):
    """Path params are required by definition and supplied by construction;
    counting them as unsent would make every parameterised endpoint a breach."""
    path = "/v1/social/posts/{post_id}"
    if path not in spec["paths"] or "get" not in spec["paths"][path]:
        pytest.skip(f"{path} GET not in the API")
    contract = {
        "consumer": "verifier-self-test",
        "endpoints": [
            {
                "method": "GET",
                "path": path,
                "callSite": "test",
                "requestFields": [],
                "requestFieldsComplete": True,
            }
        ],
    }
    report = _run(V, contract, spec)
    assert report.ok, report.breaches
