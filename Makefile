# pit-backtest convenience targets.
#
# Windows-first project: these are thin aliases for Unix `make`. On Windows,
# where `make` is often absent, run the underlying python commands directly
# (each recipe is a single line, also documented in the README). Override the
# interpreter with e.g. `make figures PYTHON=/path/to/python` to use a specific
# virtualenv.
#
#   make figures          render every README figure from the COMMITTED study
#                         artifact in seconds (needs the `figures` extra:
#                         `pip install -e ".[figures]"`). No bundle required.
#   make figures-rebuild  regenerate that artifact from the real Sharadar bundle
#                         (~15-20 min, one-time; most contributors never run it).
#   make test             run the test suite.
#   make mypy             run the strict type gate over src/.

PYTHON ?= python

.PHONY: figures figures-rebuild test mypy

figures:
	$(PYTHON) scripts/figures/regenerate.py

figures-rebuild:
	$(PYTHON) scripts/figures/build_study_artifact.py

test:
	$(PYTHON) -m pytest

mypy:
	$(PYTHON) -m mypy --strict src
