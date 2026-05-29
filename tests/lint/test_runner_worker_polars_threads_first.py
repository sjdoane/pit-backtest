"""Lint: _worker_run_one_param sets POLARS_MAX_THREADS=1 as its first
executable statement.

Per ADR 0010 lock #5 and docs/methodology/determinism.md Requirement 5:
the env var assignment MUST run before any Polars-importing module is
loaded. This lint AST-walks the function body of _worker_run_one_param
and asserts the first executable statement is the env-var assignment.

The first non-docstring statement should be an `import os` followed by
the env-var assignment, OR the env-var assignment directly (where `os`
was imported at module level). Either pattern is acceptable as long as
no Polars-importing statement precedes the env-var assignment.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from pit_backtest.engine import runner


def _get_first_non_docstring_statements(
    func_node: ast.FunctionDef, n: int = 3
) -> list[ast.stmt]:
    """Return the first n executable statements of a function body,
    skipping a leading docstring if present.
    """
    body = func_node.body
    # Skip the docstring expression (an Expr node wrapping a Constant str).
    if (
        len(body) > 0
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body[:n]


def test_worker_run_one_param_sets_polars_threads_before_factory_call() -> None:
    """Per ADR 0010 lock #5: the first executable statements of
    _worker_run_one_param are (in order) an `import os` and an
    assignment to os.environ["POLARS_MAX_THREADS"].
    """
    source = inspect.getsource(runner)
    tree = ast.parse(source)

    func_node: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_worker_run_one_param":
            func_node = node
            break
    assert func_node is not None, "_worker_run_one_param not found in runner.py"

    first_statements = _get_first_non_docstring_statements(func_node, n=3)
    assert len(first_statements) >= 2, (
        "_worker_run_one_param body too short to verify import-order invariant"
    )

    # First statement: import os.
    first = first_statements[0]
    assert isinstance(first, ast.Import), (
        f"First statement in _worker_run_one_param must be `import os`; "
        f"got {ast.dump(first)}"
    )
    assert any(alias.name == "os" for alias in first.names), (
        f"First import must include `os`; got {[a.name for a in first.names]}"
    )

    # Second statement: assignment to os.environ["POLARS_MAX_THREADS"].
    second = first_statements[1]
    assert isinstance(second, ast.Assign), (
        f"Second statement must be the env-var assignment; got {ast.dump(second)}"
    )
    # The target must be os.environ["POLARS_MAX_THREADS"].
    targets = second.targets
    assert len(targets) == 1
    target = targets[0]
    assert isinstance(target, ast.Subscript), (
        f"Assignment target must be a subscript; got {ast.dump(target)}"
    )
    # Target value: os.environ
    sub_value = target.value
    assert isinstance(sub_value, ast.Attribute)
    assert sub_value.attr == "environ"
    assert isinstance(sub_value.value, ast.Name)
    assert sub_value.value.id == "os"
    # Subscript key: "POLARS_MAX_THREADS"
    sub_slice = target.slice
    assert isinstance(sub_slice, ast.Constant)
    assert sub_slice.value == "POLARS_MAX_THREADS"
    # Assigned value: "1"
    assert isinstance(second.value, ast.Constant)
    assert second.value.value == "1"


def test_no_polars_import_at_module_top_of_runner() -> None:
    """Per ADR 0010 lock #5 the runner module must not import Polars at
    module top level so a spawn-bootstrapped worker can set
    POLARS_MAX_THREADS before any Polars import occurs.

    The runner can import Polars-touching modules (e.g.,
    pit_backtest.analytics.distribution which imports nothing Polars
    at module top), but `import polars` directly at the top of
    runner.py would defeat the bootstrap.
    """
    runner_path = Path(runner.__file__)
    tree = ast.parse(runner_path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "polars", (
                    f"runner.py imports polars at module top; this defeats "
                    f"POLARS_MAX_THREADS=1 worker bootstrap per ADR 0010 lock #5"
                )
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "polars", (
                f"runner.py imports from polars at module top; this defeats "
                f"POLARS_MAX_THREADS=1 worker bootstrap per ADR 0010 lock #5"
            )
