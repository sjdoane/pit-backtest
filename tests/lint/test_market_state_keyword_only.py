"""Lint: MarketState construction is keyword-only.

Per ADR 0009 lock #14: any positional MarketState(...) call would
silently shift fields if the optional `prior_close` field is later
moved or if new optional fields are added. The lint walks the AST of
src/pit_backtest/ and tests/ and asserts every MarketState(...) call
uses keyword arguments only.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src" / "pit_backtest"
_TESTS_ROOT = _REPO_ROOT / "tests"


def _find_market_state_calls(tree: ast.AST) -> list[ast.Call]:
    """Find all Call nodes whose function is `MarketState`.

    Catches both `MarketState(...)` and `matching.MarketState(...)` forms
    via the attribute / name distinction.
    """
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name: str | None = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name == "MarketState":
            found.append(node)
    return found


def _iter_python_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if p.is_file()]


@pytest.mark.parametrize(
    "root",
    [
        pytest.param(_SRC_ROOT, id="src"),
        pytest.param(_TESTS_ROOT, id="tests"),
    ],
)
def test_no_positional_market_state_construction(root: Path) -> None:
    """Every MarketState(...) call in src/pit_backtest/ and tests/ must
    use keyword arguments only. Positional construction would silently
    shift fields when the attrs class definition is reordered or extended.
    """
    offenders: list[str] = []
    for path in _iter_python_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            # Skip files that fail to parse (e.g., generated stubs); the
            # test suite separately catches parse errors at import time.
            continue
        for call in _find_market_state_calls(tree):
            if call.args:
                rel = path.relative_to(_REPO_ROOT)
                offenders.append(f"{rel}:{call.lineno}: positional MarketState(...)")
    assert not offenders, (
        "Found positional MarketState(...) constructions:\n  "
        + "\n  ".join(offenders)
    )
