"""pytest configuration shared by all test modules.

The actual env vars (PYTHONHASHSEED=0) are set via pyproject.toml's
[tool.pytest.ini_options] env table per docs/methodology/determinism.md
Requirement 4.
"""

from __future__ import annotations
