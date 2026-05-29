"""Lint: determinism invariants.

Per ADR 0009 lock #12 and docs/methodology/determinism.md trust
boundary item 12: ImpactedPriceSource carries mutable per-asset state.
Signal.compute() and Policy.target_positions() must NOT import the
decorator directly because reading its register from inside a
signal/policy would couple the determinism invariant to the order of
fills within a bar (which is M2-OK because one-fill-per-(asset, dt)
but v1.1-fragile with intraday slicing).

The lint walks the import statements in src/pit_backtest/signal/ and
src/pit_backtest/policy/ and asserts that ImpactedPriceSource is not
imported. Module-level imports are the only paths that need to be
checked because Python's import discipline at v1 does not include
runtime importlib calls in the inner loop.

This lint is the M2 PR B operationalization of trust boundary item 12;
other determinism invariants from the methodology doc (Polars pin,
PYTHONHASHSEED=0, no set iteration in signal/policy, etc.) are M3+
scope.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SIGNAL_ROOT = _REPO_ROOT / "src" / "pit_backtest" / "signal"
_POLICY_ROOT = _REPO_ROOT / "src" / "pit_backtest" / "policy"


def _find_impacted_source_imports(tree: ast.AST) -> list[ast.stmt]:
    """Find any `from pit_backtest.data.sources.base import ImpactedPriceSource`
    or `import pit_backtest.data.sources.base` statement.
    """
    offenders: list[ast.stmt] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("pit_backtest.data.sources.base"):
                for alias in node.names:
                    if alias.name == "ImpactedPriceSource":
                        offenders.append(node)
                        break
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pit_backtest.data.sources.base":
                    offenders.append(node)
                    break
    return offenders


@pytest.mark.parametrize(
    "root",
    [
        pytest.param(_SIGNAL_ROOT, id="signal"),
        pytest.param(_POLICY_ROOT, id="policy"),
    ],
)
def test_no_impacted_price_source_import_in_signal_or_policy(root: Path) -> None:
    """Per ADR 0009 lock #12 / determinism.md trust boundary item 12:
    signal and policy modules must not import ImpactedPriceSource.

    Importing the decorator exposes the cumulative-impact register to
    signal/policy code, which would couple the determinism invariant to
    within-bar fill order. The v1 one-fill-per-(asset, dt) constraint
    makes this M2-safe, but v1.1 intraday slicing would silently break
    determinism for any signal/policy that reads the register.
    """
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in _find_impacted_source_imports(tree):
            rel = path.relative_to(_REPO_ROOT)
            offenders.append(f"{rel}:{node.lineno}: ImpactedPriceSource import")
    assert not offenders, (
        "Signal/Policy modules must not import ImpactedPriceSource per "
        "determinism.md trust boundary item 12. Offenders:\n  "
        + "\n  ".join(offenders)
    )
