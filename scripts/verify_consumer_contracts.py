"""Verify this API still provides what its consumers declared they need.

CONSUMER-DRIVEN CONTRACTS, WITHOUT A BROKER. The usual shape of this is Pact:
consumers publish contracts to a Pact Broker, providers pull and verify them,
and `can-i-deploy` asks the broker whether a release is safe. That model exists
to solve a coordination problem between MANY TEAMS deploying MANY SERVICES
independently. Loupe has one API and two clients in one person's hands, so a
broker would be a service to host, authenticate, back up and keep alive in
return for coordination nobody needs.

So git is the broker. The consumer generates a contract from its own source and
commits it to ``contracts/``; this script is the provider verification step; and
the CI job that runs it is `can-i-deploy` — it fails the build before the deploy
workflow can start. Every property that matters is kept: the contract is a
versioned artifact, it is derived from what the consumer really calls rather
than hand-maintained, and the provider cannot merge a change that breaks it.
What is given up is multi-version compatibility matrices, which need more than
one deployed version of a consumer to matter.

WHAT COUNTS AS A BREACH
  * an endpoint a consumer calls is gone, or lost the method it uses
  * a response field a consumer REQUIRES is no longer in the response schema
  * a request field the consumer does not send became required

WHAT IS DELIBERATELY NOT CHECKED. Business logic, database state, and values.
A contract's job is structure and HTTP semantics; asserting on values makes it
a second, slower copy of the test suite that fails for reasons that are not
contract breaks.

THE HONEST LIMIT — READ THIS BEFORE TRUSTING A PASS. 53 of 325 operations
declare no ``response_model``, so FastAPI emits ``{"type": "object",
"additionalProperties": true}`` and the schema says nothing about their fields.
Field-level verification is impossible there. This script reports those as
UNVERIFIABLE rather than passing them silently, because a green check that
quietly skipped a third of the surface is worse than no check — it is a false
assurance. Run with ``--list-unverifiable`` to get the punch-list of endpoints
that want a response_model.

FIELD PATHS are relative to the response_model — that is, INSIDE the envelope
that ``register_envelope_middleware`` wraps around every /v1 response. A leading
``data.`` is stripped if a consumer includes it, since both readings are
natural. Use ``[]`` for "each element of this array":

    summary.raw.amount        houses[].grades[].population
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_DIR = REPO_ROOT / "contracts"

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


# ── outcomes ────────────────────────────────────────────────────────────────


@dataclass
class Report:
    breaches: list[str] = field(default_factory=list)
    unverifiable: list[str] = field(default_factory=list)
    checked_endpoints: int = 0
    checked_fields: int = 0

    @property
    def ok(self) -> bool:
        return not self.breaches


# ── OpenAPI navigation ──────────────────────────────────────────────────────


def _deref(
    schema: dict[str, Any] | None, spec: dict[str, Any]
) -> dict[str, Any] | None:
    """Follow ``$ref`` until it lands on a real schema.

    Bounded rather than recursive-until-blowup: a self-referential model (a
    comment with replies, say) would otherwise spin here forever.
    """
    seen = 0
    while isinstance(schema, dict) and "$ref" in schema and seen < 20:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            return None
        node: Any = spec
        for part in ref[2:].split("/"):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        schema = node
        seen += 1
    return schema if isinstance(schema, dict) else None


def _branches(schema: dict[str, Any], spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Every schema a value could satisfy.

    ``anyOf``/``oneOf`` is how FastAPI spells "optional" (``anyOf: [X, null]``)
    and how it spells a union; ``allOf`` is how it spells inheritance. A field
    lookup has to see through all three or every nullable field reads as
    missing.
    """
    out: list[dict[str, Any]] = [schema]
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(key) or []:
            resolved = _deref(sub, spec)
            if resolved is not None:
                out.extend(_branches(resolved, spec))
    return out


def _step(schema: dict[str, Any] | None, name: str, spec: dict[str, Any]):
    """One segment of a dot-path. Returns (found, next_schema)."""
    schema = _deref(schema, spec)
    if schema is None:
        return False, None
    for branch in _branches(schema, spec):
        props = branch.get("properties")
        if isinstance(props, dict) and name in props:
            return True, _deref(props[name], spec)
        # A dict-valued model (additionalProperties: {...}) accepts any key.
        extra = branch.get("additionalProperties")
        if isinstance(extra, dict):
            return True, _deref(extra, spec)
    return False, None


def _unwrap_array(schema: dict[str, Any] | None, spec: dict[str, Any]):
    schema = _deref(schema, spec)
    if schema is None:
        return None
    for branch in _branches(schema, spec):
        items = branch.get("items")
        if items is not None:
            return _deref(items, spec)
    return None


def _is_opaque(schema: dict[str, Any] | None) -> bool:
    """A response FastAPI emitted with no response_model.

    ``{"type": "object", "additionalProperties": true}`` and no properties —
    it permits anything, so it can neither confirm nor deny a field.
    """
    if not isinstance(schema, dict):
        return True
    if schema.get("properties") or schema.get("$ref"):
        return False
    if any(schema.get(k) for k in ("anyOf", "oneOf", "allOf", "items")):
        return False
    return schema.get("type") == "object"


def resolve_field(root: dict[str, Any] | None, path: str, spec: dict[str, Any]):
    """Walk a dot-path. Returns (verdict, detail) where verdict is
    'present' | 'missing' | 'unverifiable'."""
    schema = _deref(root, spec)
    if _is_opaque(schema):
        return "unverifiable", "response has no declared schema"

    # Both "data.x" and "x" are natural ways to say the same thing, since the
    # envelope is added by middleware and never appears in the response_model.
    parts = path.split(".")
    if parts and parts[0] == "data":
        parts = parts[1:]

    for raw in parts:
        if not raw:
            continue
        name = raw
        arrays = 0
        while name.endswith("[]"):
            name = name[:-2]
            arrays += 1
        if name:
            # Opacity is a property of the schema we are looking IN, so it has
            # to be decided before the step. Asking after it is the bug this
            # comment exists to prevent: `_step` returns (False, None) on a
            # miss, `_is_opaque(None)` is True, and so every genuinely missing
            # field reported as "unverifiable" — the gate passed a removed
            # field, which is the one thing it exists to catch.
            parent = _deref(schema, spec)
            if _is_opaque(parent):
                return "unverifiable", f"no declared schema at {raw!r}"
            found, schema = _step(parent, name, spec)
            if not found:
                return "missing", f"no field {name!r}"
        for _ in range(arrays):
            schema = _unwrap_array(schema, spec)
            if schema is None:
                return "missing", f"{name!r} is not an array"
    return "present", ""


# ── verification ────────────────────────────────────────────────────────────


def _success_schema(operation: dict[str, Any], spec: dict[str, Any]):
    """The 2xx JSON body schema, or None when the operation returns no JSON."""
    responses = operation.get("responses") or {}
    for code in ("200", "201", "202"):
        response = responses.get(code)
        if not response:
            continue
        content = response.get("content") or {}
        media = content.get("application/json")
        if media and "schema" in media:
            return _deref(media["schema"], spec)
    return None


def _required_request_fields(
    operation: dict[str, Any], spec: dict[str, Any]
) -> set[str]:
    body = operation.get("requestBody") or {}
    media = (body.get("content") or {}).get("application/json") or {}
    schema = _deref(media.get("schema"), spec)
    required: set[str] = set()
    if schema:
        for branch in _branches(schema, spec):
            required.update(branch.get("required") or [])
    for param in operation.get("parameters") or []:
        if param.get("required") and param.get("in") in ("query", "path"):
            required.add(param["name"])
    return required


def verify(contract: dict[str, Any], spec: dict[str, Any], report: Report) -> None:
    consumer = contract.get("consumer", "unknown-consumer")
    paths = spec.get("paths") or {}

    for entry in contract.get("endpoints") or []:
        method = str(entry.get("method", "")).lower()
        path = entry.get("path", "")
        where = f"[{consumer}] {method.upper()} {path}"
        report.checked_endpoints += 1

        operations = paths.get(path)
        if operations is None:
            report.breaches.append(
                f"{where}: endpoint is GONE from the API "
                f"(consumer calls it at {entry.get('callSite', 'unknown')})"
            )
            continue

        operation = operations.get(method)
        if operation is None:
            available = sorted(m.upper() for m in operations if m in HTTP_METHODS)
            report.breaches.append(
                f"{where}: method no longer accepted (API offers {available})"
            )
            continue

        schema = _success_schema(operation, spec)

        for field_path in entry.get("requiredResponseFields") or []:
            report.checked_fields += 1
            verdict, detail = resolve_field(schema, field_path, spec)
            if verdict == "missing":
                report.breaches.append(
                    f"{where}: required response field {field_path!r} is gone ({detail})"
                )
            elif verdict == "unverifiable":
                report.unverifiable.append(
                    f"{where}: cannot check {field_path!r} — {detail}"
                )

        # A field that became required is as breaking as a field that vanished:
        # the consumer's existing requests start failing validation. But this
        # can only be judged when the contract's request list is EXHAUSTIVE.
        #
        # An absent or empty `requestFields` means "nobody wrote down what this
        # call sends", not "it sends nothing". Reading it the second way makes
        # every unenumerated POST a breach — the first run of this check
        # reported that POST /v1/social/posts/{post_id}/comments fails to send
        # `body`, which is the entire content of the request. A gate that cries
        # wolf on a dozen endpoints gets switched off, so it stays quiet until a
        # consumer explicitly claims its list is complete.
        if not entry.get("requestFieldsComplete"):
            continue
        sent = {str(f).split(".")[0] for f in (entry.get("requestFields") or [])}
        for name in sorted(_required_request_fields(operation, spec)):
            if name in sent:
                continue
            if "{" + name + "}" in path:
                continue  # path parameter, supplied by construction
            report.breaches.append(
                f"{where}: request field {name!r} is now REQUIRED but this "
                f"consumer does not send it"
            )


def load_spec() -> dict[str, Any]:
    """Build the schema from the live app, not from a committed copy.

    Reading a checked-in openapi.json would verify the contract against
    whatever the schema looked like when someone last remembered to regenerate
    it, which is the drift this whole mechanism exists to catch.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from app.main import app

    return app.openapi()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contracts", default=str(CONTRACTS_DIR))
    parser.add_argument(
        "--list-unverifiable",
        action="store_true",
        help="print every field that could not be checked, and why",
    )
    args = parser.parse_args()

    contracts_dir = Path(args.contracts)
    files = sorted(contracts_dir.glob("*.json"))
    if not files:
        print(f"No consumer contracts in {contracts_dir} — nothing to verify.")
        print("A consumer publishes one with `npm run contract:publish`.")
        return 0

    spec = load_spec()
    report = Report()
    for path in files:
        verify(json.loads(path.read_text()), spec, report)

    print(
        f"Checked {report.checked_endpoints} endpoints and {report.checked_fields} "
        f"fields across {len(files)} consumer contract(s)."
    )

    if report.unverifiable:
        print(
            f"\n{len(report.unverifiable)} field(s) could NOT be verified — the "
            f"operation declares no response_model, so its schema permits "
            f"anything. These are not passes."
        )
        shown = (
            report.unverifiable if args.list_unverifiable else report.unverifiable[:5]
        )
        for line in shown:
            print(f"  ? {line}")
        if not args.list_unverifiable and len(report.unverifiable) > 5:
            print(
                f"  … {len(report.unverifiable) - 5} more "
                f"(--list-unverifiable to see them all)"
            )

    if report.breaches:
        print(f"\nCONTRACT BROKEN — {len(report.breaches)} breach(es):\n")
        for line in report.breaches:
            print(f"  ✗ {line}")
        print(
            "\nA consumer in production depends on each of these. Either restore "
            "what was removed, or ship the consumer change first and republish "
            "its contract."
        )
        return 1

    print("\nAll consumer contracts satisfied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
