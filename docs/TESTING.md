# Testing strategy

How pit-backtest is verified. Per ADR 0002 decision 18 this document was
moved from M1 to M4 and now describes the full verification surface as of
the M4 validation milestone.

The guiding principle (per `session_rules.md` rule 6): tests using
`:memory:` databases, mocks, and synthetic fixtures are necessary but NOT
sufficient. Every numerical claim is either hand-computable from a small
fixture or reconciled against an independent real-data source.

## Test layers

### 1. Hand-computable unit fixtures

Most analytics and engine invariants are pinned against fixtures small
enough to verify by hand. Examples:

- **Total-return reconstruction** (`tests/data/test_tr_reconstruction.py`):
  a toy 3-day dividend fixture whose total return `TR[3] = 1.0352238806...`
  is recomputed by hand in the methodology note and pinned to 1e-9.
- **PSR / DSR / MinTRL** (`tests/analytics/test_sharpe.py`): the
  Bailey-LdP 2014 worked example (SR_hat=1.5, T=60, gamma_3=-0.5,
  gamma_4=5, N=30, V[{SR_n}]=0.4) pins DSR = 0.766 within 1e-3 (per
  ADR 0013; the methodology note's original 0.971 was a quantile error).
- **Drawdown** (`tests/analytics/test_drawdown.py`): a 22-bar nav fixture
  with hand-pinned `max_drawdown = 50/120`, a `DrawdownDurationReport`
  with `days=10` + `is_censored_at_end=True`, and `calmar = -2.36678091`.
- **Concentration / HHI** (`tests/analytics/test_concentration.py`): a
  uniform N-bar series pins `HHI = 1/N`; a single-bar series pins `HHI = 1`.
- **CV splitters** (`tests/validation/test_cv.py`): a T=20, k=5 purged
  k-fold fixture with hand-pinned train/test/purged/embargo index tuples;
  CPCV N=6 k=2 pins exactly 5 paths and 15 combinations.
- **Scorecard render** (`tests/analytics/test_scorecard_markdown.py`):
  a fixed `Scorecard` renders a six-section Markdown document; the
  censored-drawdown marker, Decimal shortfall formatting, and `None`
  risk-adjusted rendering are pinned.

### 2. SPY reconciliation against an independent source

The M1 acceptance harness reconciles the engine's SPY buy-and-hold total
return against the SSGA fund-level total return
(`tests/integration/test_spy_reconciliation.py` and the
`examples/spy_buy_and_hold.py --compare-to-ssga` CLI). Per-window
tolerances (ADR 0008): 25 bps at 1y, 8 bps at 3y, 7 bps at 5y, 15 bps
at 10y. This catches dividend-handling and adjustment-frame bugs that a
synthetic fixture cannot, because SSGA's published TR is computed by a
third party with its own data pipeline.

### 3. Synthetic-data differential checks

`examples/constant_weight_three_names.py --diff-against-reference`
verifies the engine's constant-weight equity curve matches a
hand-derived reference closed form on a synthetic 3-name bundle, so the
per-bar accounting (cash, shares, nav) is exercised without depending on
vendor data.

### 4. Corporate-action fixtures

Splits, cash dividends, and delistings-with-cash-proceeds are pinned
against inline synthetic bundles in `tests/data/test_sharadar_adapter.py`
(dispatch table) and the data-quality contracts in
`tests/data/test_contracts.py`. Spin-offs-as-cash and the Chapter-11
zero-proceeds approximation carry documented bias notes.

### 5. CPCV verification (splitter-level; orchestration body is M5)

CPCV is verified at the **splitter** level in M4:
`tests/validation/test_cv.py` pins that `CPCVSplitter` produces the
correct `phi(N, k) = (k/N) * C(N, k)` path count, that `path_assignments`
forms an exact `(combination, group)` test-cell partition, and that purge
+ embargo + train + test form a clean partition of the observation index.

The CPCV **orchestration body** (`Runner.run_cpcv`, which would run a
strategy on each split and stitch per-fold predictions into per-path
equity curves) is deferred to M5. Its stubbed signature is
underspecified, and a CPCV "path" requires the fit/predict strategy
semantics the M5 single-factor momentum study introduces. See the
ADR 0003 M4 PR 5 amendment footer. There is therefore intentionally NO
end-to-end CPCV golden test in M4; the M4 CPCV acceptance is met by the
splitter + the `BacktestPathDistribution` container.

### 6. Trial-registry concurrency

`tests/validation/test_trial_registry.py` spawns two cold-start processes
that each write 50 trials to the same SQLite WAL database and asserts the
final row count is 100 with both processes exiting 0 (ADR 0002 acceptance
criterion 4: the registry survives concurrent writes).

### 7. Determinism and lint guards

`tests/lint/` holds AST-level guards (e.g. the Runner worker sets
`POLARS_MAX_THREADS=1` before importing Polars) and determinism
invariants. Every analytics query orders its SQL / sorts its tuples so
results are stable across runs.

## Type checking and the em-dash ban

`mypy --strict` runs clean across the `src/` tree on every change. The
project bans the em-dash (U+2014) and the spaced double-hyphen surrogate
everywhere (code, docs, commits); a sweep runs before each PR.

## Deferred to v1.1: differential testing against zipline-reloaded

Per ADR 0002 (the reviewer flagged the Windows toolchain cost as the most
likely line item to break the timeline), differential testing of three
benchmark strategies against `zipline-reloaded` is deferred to v1.1. The
M5 worked study ships its own honest DSR conclusion (passing whether or
not the strategy clears the DSR threshold) rather than a cross-engine
differential. When v1.1 reactivates this, it lands here as an eighth test
layer.
