"""The provider side of consumer-driven contract testing.

This is the same check CI runs as the `can-i-deploy` gate, run here as a test so
it fails on a developer's machine in six seconds rather than in a pipeline ten
minutes later. Deleting an endpoint, renaming a response field, or making a
request field required will fail this the moment it happens, naming the consumer
and the call site that depended on it.

The contracts under ``contracts/`` are published by the consumers themselves
(``npm run contract:publish`` in loupe-frontend) and committed here. See
``scripts/verify_consumer_contracts.py`` for why git plays the part a Pact
Broker would play in a multi-team setup.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts"
VERIFIER = REPO_ROOT / "scripts" / "verify_consumer_contracts.py"


def _load_verifier():
    """Load the script by path — scripts/ is not an importable package.

    The sys.modules registration is not optional: ``@dataclass`` resolves its
    field types by looking the defining module up in ``sys.modules``, so a
    module executed without being registered there dies at class-creation time
    with a bare ``'NoneType' object has no attribute '__dict__'``.
    """
    name = "verify_consumer_contracts"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, VERIFIER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _contract_files() -> list[Path]:
    return sorted(CONTRACTS_DIR.glob("*.json")) if CONTRACTS_DIR.is_dir() else []


@pytest.fixture(scope="module")
def verifier():
    return _load_verifier()


@pytest.fixture(scope="module")
def spec():
    from app.main import app

    return app.openapi()


def test_the_verifier_script_exists_and_loads():
    """If this file is here, the mechanism it tests must be too."""
    assert VERIFIER.is_file(), f"missing {VERIFIER}"
    _load_verifier()


@pytest.mark.parametrize("contract_path", _contract_files(), ids=lambda p: p.stem)
def test_the_api_still_provides_what_this_consumer_declared(
    contract_path, verifier, spec
):
    """The gate. A breach here is a client this deploy would break."""
    contract = json.loads(contract_path.read_text())
    report = verifier.Report()
    verifier.verify(contract, spec, report)

    assert report.ok, (
        f"\n{contract.get('consumer', contract_path.stem)} depends on things this "
        f"API no longer provides:\n\n"
        + "\n".join(f"  - {b}" for b in report.breaches)
        + "\n\nEither restore them, or land the consumer change and republish "
        "its contract first.\n"
    )


def test_every_contract_is_well_formed():
    """A malformed contract verifies nothing while looking like it does."""
    for path in _contract_files():
        contract = json.loads(path.read_text())
        assert contract.get("consumer"), f"{path.name}: no 'consumer'"
        assert isinstance(contract.get("endpoints"), list), (
            f"{path.name}: 'endpoints' must be a list"
        )
        for i, entry in enumerate(contract["endpoints"]):
            assert entry.get("method"), f"{path.name}[{i}]: no method"
            assert str(entry.get("path", "")).startswith("/"), (
                f"{path.name}[{i}]: path must start with '/'"
            )


@pytest.mark.skipif(not _contract_files(), reason="no consumer contracts published yet")
def test_the_contract_covers_a_meaningful_share_of_what_it_checks(verifier, spec):
    """Guard against a green check that verified almost nothing.

    53 of this API's operations declare no ``response_model``, so their schema
    permits any shape and field-level assertions against them are meaningless.
    The verifier reports those as UNVERIFIABLE rather than passing them, and
    this test keeps that number visible: if unverifiable fields ever outnumber
    checked ones, the suite is mostly theatre and the fix is response models,
    not a looser contract.
    """
    report = verifier.Report()
    for path in _contract_files():
        verifier.verify(json.loads(path.read_text()), spec, report)

    if report.checked_fields == 0:
        pytest.skip("contract declares no response fields yet")

    unverifiable = len(report.unverifiable)
    assert unverifiable < report.checked_fields, (
        f"{unverifiable} of {report.checked_fields} contracted fields cannot be "
        f"verified because their operations have no response_model. The contract "
        f"is checking less than half of what it claims. Add response models to "
        f"the operations listed by:\n"
        f"    python scripts/verify_consumer_contracts.py --list-unverifiable"
    )
