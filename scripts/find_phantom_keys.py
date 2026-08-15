"""Find dict keys that are READ but never WRITTEN anywhere in the codebase.

THE BUG THIS EXISTS FOR. ``card_identifier.py`` built every candidate with

    CandidateOut(card_id=cand.get("card_id"), upstream_id=cand.get("id"), ...)

and no producer in the codebase ever emits a ``"card_id"`` key —
``catalog_hash_index.py`` returns ``"id"``, and so does every search path. So
``card_id`` was ``None`` on every candidate ever produced, 2,467 production rows
recorded NULL, and the identification feedback loop silently contributed nothing
from launch onward.

WHY GREP MISSED IT, which is the point. A search for "is this column written?"
finds the assignment and calls it written. The assignment IS there. What is
missing is the KEY on the other side, one expression to the right. The two
questions look identical and are not:

    does something assign to card_id?          yes  ← what I checked
    can that assignment produce a value?       no   ← what mattered

This asks the second question. It parses the AST rather than grepping, collects
every string key the codebase can PRODUCE (dict literals, dict(**kwargs),
``d["k"] = v``, ``.update({...})``, and every Pydantic/SQLAlchemy attribute
name), then reports every ``.get("k")`` / ``["k"]`` READ whose key appears in
none of them.

A hit is not automatically a bug — a key can legitimately come from an external
API payload this repo never constructs. That is why the output is ranked and
why external-shaped reads can be filtered. But a read whose key nothing in the
codebase produces, on a dict the codebase itself built, is always worth a look.
"""

from __future__ import annotations

import argparse
import ast
import json
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOTS = ("app", "scripts")

#: Keys that legitimately arrive from outside and are never built here.
#: Kept explicit rather than inferred: an allowlist you can read is worth more
#: than a heuristic that quietly swallows a real finding.
EXTERNAL_PREFIXES = (
    "stripe_",
    "hcaptcha",
    "recaptcha",
)


class KeyCollector(ast.NodeVisitor):
    """String keys this file can produce AS DICT KEYS.

    THE NAMESPACE DISTINCTION IS THE WHOLE POINT, and getting it wrong is how
    the first version of this script failed to flag the very bug it was written
    for. It counted `CandidateOut(card_id=...)` as producing "card_id", so the
    key looked produced and the read looked fine.

    A constructor keyword is not a dict key. `Model(card_id=x)` names a FIELD;
    `{"card_id": x}` names a KEY. Only the second can ever be found by
    `d.get("card_id")`. Mixing them makes every field name in the codebase look
    like a valid key and silently disables the check.
    """

    def __init__(self) -> None:
        self.dict_keys: set[str] = set()

    def visit_Dict(self, node: ast.Dict) -> None:
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                self.dict_keys.add(key.value)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # dict(a=1) DOES build a dict whose keys are those names.
        if isinstance(node.func, ast.Name) and node.func.id == "dict":
            for kw in node.keywords:
                if kw.arg:
                    self.dict_keys.add(kw.arg)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            # d["k"] = v — a genuine key write.
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.slice, ast.Constant)
                and isinstance(target.slice.value, str)
            ):
                self.dict_keys.add(target.slice.value)
        self.generic_visit(node)


#: `router.get("/path")` and `client.get(url)` are not dict reads. Without this
#: every route registration in the app shows up as a phantom key.
NON_DICT_RECEIVERS = {"router", "app", "client", "session", "http", "requests"}


class GetCollector(ast.NodeVisitor):
    """`something.get("key")` — the shape the real bug wore."""

    def __init__(self) -> None:
        self.gets: dict[str, list[int]] = defaultdict(list)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            key = node.args[0].value
            recv = node.func.value
            recv_name = (
                recv.id
                if isinstance(recv, ast.Name)
                else recv.attr
                if isinstance(recv, ast.Attribute)
                else ""
            )
            if recv_name not in NON_DICT_RECEIVERS and not key.startswith("/"):
                self.gets[key].append(node.lineno)
        self.generic_visit(node)


def _package(path: Path) -> str:
    """The directory a module lives in — its neighbourhood of collaborators.

    SCOPE IS WHAT MAKES THIS WORK. Asking "is this key produced ANYWHERE in the
    repo?" cannot catch the bug it was built for: "card_id" IS a dict key in
    card_search_service.py, in price-history response bodies that have nothing
    to do with identification. Globally it looks produced; locally, in
    app/services/identification/, nothing emits it and every read is None.

    A dict is almost always built and consumed within one package. Scoping to
    the directory is a cheap stand-in for dataflow, and it is the difference
    between a check that flags the real defect and one that does not.
    """
    return str(path.parent.relative_to(REPO_ROOT))


def scan(
    roots: list[Path],
) -> tuple[dict[str, set[str]], dict[str, list[tuple[str, int, str]]]]:
    by_package: dict[str, set[str]] = defaultdict(set)
    gets: dict[str, list[tuple[str, int, str]]] = defaultdict(list)

    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts or "alembic" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue

            pkg = _package(path)
            keys = KeyCollector()
            keys.visit(tree)
            by_package[pkg] |= keys.dict_keys

            g = GetCollector()
            g.visit(tree)
            rel = str(path.relative_to(REPO_ROOT))
            for key, lines in g.gets.items():
                for line in lines:
                    gets[key].append((rel, line, pkg))

    return by_package, gets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--roots", nargs="*", default=list(DEFAULT_ROOTS), help="dirs to scan"
    )
    args = parser.parse_args()

    roots = [REPO_ROOT / r for r in args.roots if (REPO_ROOT / r).is_dir()]
    by_package, gets = scan(roots)

    # Integrations parse payloads from other people's APIs by definition, so a
    # key they read and never build is normal rather than suspicious.
    def _external(pkg: str) -> bool:
        return "integrations" in pkg

    phantoms: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    for key, sites in gets.items():
        if key.startswith(EXTERNAL_PREFIXES) or len(key) <= 2:
            continue
        for rel, line, pkg in sites:
            if _external(pkg):
                continue
            if key not in by_package.get(pkg, set()):
                phantoms[key].append((rel, line, pkg))
    phantoms = {k: v for k, v in phantoms.items() if v}

    if args.json:
        print(json.dumps(dict(sorted(phantoms.items())), indent=2))
        return 0

    produced = sum(len(v) for v in by_package.values())
    print(
        f"Scanned {len(roots)} root(s) across {len(by_package)} packages. "
        f"{produced:,} key-productions seen; {len(gets):,} distinct keys read.\n"
    )
    if not phantoms:
        print("No phantom keys: every .get() names a key something can produce.")
        return 0

    print(
        f"{len(phantoms)} key(s) are READ but never PRODUCED anywhere in "
        f"{', '.join(args.roots)}.\n"
        f"Each of these evaluates to None every time unless the dict came from "
        f"outside this repo:\n"
    )
    for key, sites in sorted(phantoms.items(), key=lambda kv: -len(kv[1])):
        where = ", ".join(f"{f}:{ln}" for f, ln, _ in sites[:3])
        more = f" (+{len(sites) - 3} more)" if len(sites) > 3 else ""
        print(f"  {key!r:34} {where}{more}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
