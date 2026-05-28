# ADR 0003: Core architecture sketch with skeptical-reviewer pass

Status: Accepted.
Date: 2026-05-28.
Authors: Sam Doane (with critique and skeptical review captured below).

## Context

[`ADR 0001`](0001-spec-critique.md) locked 20 decisions about scope and design philosophy. [`ADR 0002`](0002-roadmap-review.md) locked 21 decisions about the M1 through M5 milestone breakdown. The combined constraints binding on the architecture:

- Six layers: data, signal, policy, execution, risk decomposition, analytics.
- Polars end-to-end with `.to_pandas()` adapter on every public result object.
- CPCV is primary; backtest results are `BacktestPathDistribution` instances; any single-Sharpe API on CPCV is a correctness bug.
- LdP chapter 14 scorecard is the default analytics output; raw Sharpe shown alone is a configuration error.
- SquareRootImpact with Almgren 2005 calibration is the default cost model; sensitivity bands at eta in [0.05, 0.30] are required; `--impact-model=bouchaud` flag substitutes beta=0.5.
- Dual-timestamp data model (`period_end_dt`, `available_dt`); typed `Universe` API with `is_member(asset_id, date) -> bool`.
- Persistent asset identifiers (Sharadar TICKERS-derived; CRSP PERMNO conceptually).
- Backtest and live execution share the same kernel; only the `Clock` implementation differs.
- `FillPriceModel` enum required on every `Order`; defaults are explicit, not implicit.
- Trial registry (SQLite WAL) feeds DSR; `confidence_tier` enum enforces render-path discipline.
- Performance budget: 20-year backtest on 500 names under 60 seconds on a documented baseline (GitHub Actions `ubuntu-latest`, 4 vCPU, 16 GB RAM).
- v1 data inventory: Sharadar SF1 ARQ + SEP + TICKERS + SP500 event log.
- America/New_York timezone convention; fractional shares supported by default.

This ADR proposes the class and protocol hierarchy, the event-loop design, the trust-boundary enumeration, the data-model schema, the policy-vs-execution interface, and the package layout. The skeptical-reviewer pass and my response follow.

## Proposed architecture

### Package layout

```
src/pit_backtest/
  __init__.py
  data/
    universe.py          # Universe protocol + SharadarSP500Universe
    records.py           # Pydantic models (PriceRecord, FundamentalRecord, CorporateAction)
    sources/
      sharadar.py        # PitDataSource implementation for Sharadar
    contracts.py         # Data quality contracts (M3)
    resolver.py          # AssetResolver: ticker history -> persistent AssetId
    adjustments.py       # Split and dividend adjustment with (dt, perspective_dt) semantics
  signal/
    base.py              # Signal protocol
    momentum.py          # Momentum12_1Signal for M5
  policy/
    base.py              # Policy protocol + AlgoStack composition
    algos.py             # SelectTopQuintile, WeighEqually, MonthlyRebalance, CashBufferConstraint
  execution/
    orders.py            # Order, Fill, FillPriceModel enum
    matching.py          # MatchingEngine, fill-price application, permanent-impact register
    clock.py             # Clock protocol, TestClock, LiveClock
    cost/
      base.py            # CostModel protocol; pre_trade_cost_estimate API
      impact.py          # NoImpact, FixedBps, LinearImpact, SquareRootImpact (Almgren 2005 default)
      commission.py      # Commission models with typed units; /100.0 regression test
      permanent_register.py  # Per-instrument permanent-impact accumulation
  risk/
    attribution.py       # Factor attribution (minimal for v1; expanded in v1.1)
  analytics/
    sharpe.py            # PSR, DSR, MinTRL
    drawdown.py          # max_drawdown, duration, Calmar
    concentration.py     # HHI
    scorecard.py         # LdP chapter 14 Markdown renderer
    distribution.py      # BacktestPathDistribution generic type
  validation/
    cv.py                # PurgedKFoldSplitter, WalkForwardSplitter, CPCVSplitter
    trial_registry.py    # SQLite WAL trial registry
    confidence_tier.py   # ConfidenceTier enum + render-path enforcement
  engine/
    bar_loop.py          # BarLoop driver (the per-bar dispatch)
    runner.py            # Multiprocess Runner for CPCV paths and parameter sweeps
    state.py             # PortfolioState (positions, cash, P&L)
  cli/
    main.py              # CLI entry point with --log-level, --impact-model flags
  utils/
    logging.py           # Structured logging (stdlib logging configured at boot)
    timezones.py         # America/New_York convention helpers
```

Tests mirror the layout under `tests/`. Examples live under `examples/`.

### Data model

Pydantic v2 models at API boundaries; Polars frames for bulk internal data. The data model captures the dual-timestamp commitment from ADR 0001 decision 9 and ADR 0002 decisions 6 and 12.

```python
# data/records.py

from datetime import datetime
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict

class AssetId(BaseModel):
    """Persistent identifier. Sharadar TICKERS provides `permaticker` which is
    used as the canonical key. Ticker is a property of (AssetId, date), not a
    persistent attribute.
    """
    model_config = ConfigDict(frozen=True)
    permaticker: int

class PriceRecord(BaseModel):
    asset_id: AssetId
    period_end_dt: datetime  # bar close, America/New_York 16:00 ET
    available_dt: datetime   # for daily bars, same as period_end_dt
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    cumulative_adjustment: Decimal  # split + dividend cumulative factor at period_end_dt

class FundamentalRecord(BaseModel):
    asset_id: AssetId
    period_end_dt: datetime  # quarter end (Sharadar `calendardate`)
    available_dt: datetime   # SEC submission date (Sharadar `datekey`)
    field: str               # e.g., "revenue", "book_value"
    value: Decimal
    flavor: Literal["ARQ", "ART", "ARY"]  # PIT flavors only; MRQ and MRY rejected at adapter level

class CorporateAction(BaseModel):
    asset_id: AssetId
    action_type: Literal["split", "cash_dividend", "delisting_cash",
                          "delisting_stock_acquisition", "delisting_zero",
                          "spinoff_as_cash"]
    ex_date: datetime  # all adjustments apply on ex-date per ADR 0001 decision 6
    # type-specific fields are subclassed via discriminated union; see below
```

For corporate actions, a discriminated union pattern keeps the type system honest:

```python
class SplitAction(CorporateAction):
    action_type: Literal["split"] = "split"
    ratio: Decimal  # 2.0 for 2-for-1 forward split; 0.5 for 1-for-2 reverse

class CashDividendAction(CorporateAction):
    action_type: Literal["cash_dividend"] = "cash_dividend"
    amount_per_share: Decimal

class DelistingCashAction(CorporateAction):
    action_type: Literal["delisting_cash"] = "delisting_cash"
    cash_proceeds_per_share: Decimal

class SpinoffAsCashAction(CorporateAction):
    action_type: Literal["spinoff_as_cash"] = "spinoff_as_cash"
    cash_equivalent_per_share: Decimal  # documented bias note in METHODOLOGY.md
```

The cumulative adjustment column on `PriceRecord` carries the `(dt, perspective_dt)` semantics. The data layer exposes both `adjusted_close(asset_id, dt, perspective_dt)` (default `perspective_dt = dt`, returning unadjusted; `perspective_dt = latest` returning fully back-adjusted) and `unadjusted_close(asset_id, dt)`. Ratio signals (book-to-price) consume unadjusted; return computation consumes adjusted.

### Universe protocol

```python
# data/universe.py

from typing import Protocol
from datetime import datetime
from collections.abc import Iterator

class Universe(Protocol):
    """Point-in-time universe membership API. ADR 0001 decision 9, ADR 0002
    decisions 3-6.
    """
    
    def is_member(self, asset_id: AssetId, dt: datetime) -> bool:
        """True if asset was in the universe at dt."""
        ...
    
    def members_at(self, dt: datetime) -> list[AssetId]:
        """All assets in the universe at dt."""
        ...
    
    def membership_spells(self, asset_id: AssetId) -> list[tuple[datetime, datetime]]:
        """All (start, end) intervals during which asset was a member."""
        ...

class SharadarSP500Universe(Universe):
    """Backed by the Sharadar SP500 event log."""
    ...
```

A `Universe` instance validates at backtest construction that every asset has either a delisting record or a confirmed-active flag across each membership spell. Gaps raise `UniverseValidationError` with the offending records surfaced (ADR 0002 decision 12: data quality contracts).

### Data source protocol

```python
# data/sources/__init__.py

class PitDataSource(Protocol):
    """ADR 0001 decision 10. The v1 implementation is SharadarDataSource."""
    
    def get_price(self, asset_id: AssetId, dt: datetime,
                  field: Literal["open", "high", "low", "close", "volume"]) -> Decimal:
        """Returns the raw bar value at dt. No adjustment."""
        ...
    
    def get_fundamental(self, asset_id: AssetId, available_dt: datetime,
                        field: str, flavor: Literal["ARQ", "ART", "ARY"]) -> Decimal | None:
        """Returns the most recent fundamental with available_dt <= the given dt.
        Returns None if no such record exists.
        """
        ...
    
    def get_corporate_actions(self, asset_id: AssetId,
                              start_dt: datetime, end_dt: datetime) -> list[CorporateAction]:
        """All corporate actions for asset_id with ex_date in [start_dt, end_dt]."""
        ...
    
    def members_at(self, universe_id: str, dt: datetime) -> list[AssetId]:
        """Universe membership; backs the Universe protocol."""
        ...
    
    def get_delisting(self, asset_id: AssetId) -> CorporateAction | None:
        """Delisting record if one exists; None if asset is still active."""
        ...

class SharadarDataSource(PitDataSource):
    """v1 implementation."""
    ...
```

Bulk reads return Polars frames. The protocol above is the per-row API used by the engine; under it, the implementation reads parquet snapshots committed at known SHA256 hashes (ADR 0002 decision 3).

### Signal protocol

```python
# signal/base.py

from typing import Protocol
import polars as pl

class Signal(Protocol):
    """Cross-sectional signal. Operates on the universe at a date.
    
    Output is a Polars frame with columns: asset_id, score, dt.
    Each Signal instance is responsible for its own input requirements; the
    engine passes a `point_in_time_view` callable that returns a time-sliced
    view of historical data up to `dt - 1 day`, never up to `dt`.
    """
    
    def required_lookback_days(self) -> int:
        """How many days of history this signal needs."""
        ...
    
    def compute(self, universe: Universe, dt: datetime,
                point_in_time_view: PitView) -> pl.DataFrame:
        """Compute the signal at dt. Returns columns: asset_id, score, dt."""
        ...

class Momentum12_1Signal(Signal):
    """JT1993 12-month total return excluding the most recent month, computed
    on adjusted close.
    """
    
    def required_lookback_days(self) -> int:
        return 273  # 252 trading days + 1 month buffer
    
    def compute(self, universe, dt, pit_view):
        ...
```

`PitView` is a callable returning a Polars LazyFrame sliced to `available_dt < dt`. The strict less-than excludes the bar at `dt` itself: signals are computed on yesterday's data, not today's. This is the structural lookahead protection.

### Policy protocol

```python
# policy/base.py

class Policy(Protocol):
    """Translates signals to target dollar positions, querying the cost
    estimator before committing.
    """
    
    def target_positions(self, signal_output: pl.DataFrame,
                         current_positions: PortfolioState,
                         cost_estimator: CostEstimator,
                         dt: datetime) -> TargetPositions:
        ...

class TargetPositions(BaseModel):
    dt: datetime
    targets: dict[AssetId, Decimal]  # signed dollar amounts; positive = long
```

The AlgoStack pattern from bt is the right composition mechanism for policy. Each `Algo` reads and writes a shared mutable context.

```python
# policy/algos.py

class AlgoContext(BaseModel):
    universe: Universe
    signal: pl.DataFrame
    current: PortfolioState
    cost_estimator: CostEstimator
    temp: dict[str, object]  # scratch for cross-algo communication
    perm: dict[str, object]  # persists across bars

class Algo(Protocol):
    def __call__(self, ctx: AlgoContext) -> bool:
        """Returns True to continue the stack, False to short-circuit."""
        ...

class AlgoStack(Algo):
    """Compose algos sequentially. Short-circuits on first False."""
    algos: list[Algo]

class SelectTopQuintile(Algo): ...
class WeighEqually(Algo): ...
class MonthlyRebalance(Algo): ...
class CashBufferConstraint(Algo): ...
```

### Execution protocol

```python
# execution/orders.py

from enum import Enum

class FillPriceModel(Enum):
    """ADR 0001 decision 7. Every Order requires one; no default."""
    OPEN = "open"
    CLOSE = "close"
    VWAP = "vwap"
    ARRIVAL = "arrival"
    NEXT_BAR_OPEN = "next_bar_open"

class Order(BaseModel):
    order_id: str
    asset_id: AssetId
    quantity: Decimal  # signed; positive = buy
    fill_price_model: FillPriceModel  # required
    submit_dt: datetime
    # MOO/MOC are special cases of OPEN and CLOSE with extra slippage; see cost layer

class Fill(BaseModel):
    order_id: str
    asset_id: AssetId
    quantity: Decimal  # may be < order quantity if partial
    fill_price: Decimal  # after temporary impact + commission per-share
    temporary_impact: Decimal  # for analytics
    permanent_impact: Decimal  # registered for next-bar mid adjustment
    commission: Decimal
    dt: datetime
```

```python
# execution/matching.py

class MatchingEngine:
    """Translates orders to fills, queries cost model, registers permanent
    impact. ADR 0001 decision 6 + ADR 0002 decision 5.
    """
    
    def __init__(self, cost_model: CostModel,
                 permanent_register: PermanentImpactRegister,
                 clock: Clock):
        ...
    
    def submit(self, order: Order, market_state: MarketState) -> Fill:
        """Determine fill price per order.fill_price_model, apply impact, apply
        commission, register permanent impact.
        """
        ...
```

Partial fills via participation-rate cap with rollover (ADR 0001 decision 7, ADR 0002 deferred details):

```python
class MatchingEngine:
    def __init__(self, ..., max_participation_pct: Decimal = Decimal("0.10"),
                 partial_fill_decay_bars: int = 3):
        ...
    
    def submit(self, order, market_state):
        # If order quantity exceeds max_participation_pct of bar volume,
        # fill the affordable portion and queue the rest. The queued
        # portion decays over partial_fill_decay_bars before being cancelled.
        ...
```

### Cost model protocol

```python
# execution/cost/base.py

class CostEstimate(BaseModel):
    expected_temporary_bps: Decimal  # impact in bps of notional
    expected_permanent_bps: Decimal
    expected_commission: Decimal     # dollars

class CostModel(Protocol):
    """ADR 0001 decisions 6 + 11."""
    
    def pre_trade_estimate(self, asset_id: AssetId, shares: Decimal,
                           direction: Literal["buy", "sell"],
                           dt: datetime, market_state: MarketState) -> CostEstimate:
        """Queried by the policy layer before committing to a trade list."""
        ...
    
    def compute_fill(self, order: Order, market_state: MarketState) -> Fill:
        """Queried by the matching engine when applying the fill."""
        ...

# execution/cost/impact.py

class SquareRootImpact(CostModel):
    """Almgren 2005 calibration. ADR 0001 decision 6 + ADR 0002 decision 7.
    
    Parameters labeled as a 1998-2000 calibration:
      eta = 0.142, beta = 0.6, gamma = 0.314
    
    Bouchaud override:
      --impact-model=bouchaud uses beta = 0.5.
    """
    
    def __init__(self, eta: Decimal = Decimal("0.142"),
                 beta: Decimal = Decimal("0.6"),
                 gamma: Decimal = Decimal("0.314")):
        ...

class LinearImpact(CostModel):
    """Almgren-Chriss 2000 linear model. Available; not default."""
    ...

class FixedBps(CostModel):
    """Single-parameter slippage. Available; not default."""
    ...

class NoImpact(CostModel):
    """Zero-cost. Only constructable with unsuitable_for_deployment=True;
    emits a runtime warning when used. ADR 0002 decision 5.
    """
    
    def __init__(self, unsuitable_for_deployment: Literal[True]):
        if not unsuitable_for_deployment:
            raise ValueError(
                "NoImpact requires unsuitable_for_deployment=True. "
                "Backtests with zero-cost slippage are not deployment-ready."
            )
        warnings.warn("NoImpact in use; results overstate strategy returns.")
```

### Clock protocol

```python
# execution/clock.py

class Clock(Protocol):
    """Time source. Injected at engine construction so backtest and live share
    the same kernel. ADR 0001 decision 5.
    """
    
    def now(self) -> datetime:
        ...

class TestClock(Clock):
    """Simulated clock; controlled by the BarLoop driver."""
    
    def advance_to(self, dt: datetime) -> None: ...
    def now(self) -> datetime: ...

class LiveClock(Clock):
    """Real wall-clock; for v1.1 live use."""
    def now(self) -> datetime:
        return datetime.now(tz=ZoneInfo("America/New_York"))
```

### Permanent impact register

```python
# execution/cost/permanent_register.py

class PermanentImpactRegister:
    """Per-instrument additive adjustment to mid-prices visible to the next
    bar's signal computation and portfolio valuation. ADR 0001 decision 11
    + ADR 0002 decision 6.
    """
    
    def __init__(self):
        self._cumulative: dict[AssetId, Decimal] = {}
    
    def record(self, asset_id: AssetId, permanent_impact_per_share: Decimal,
               direction: Literal["buy", "sell"]) -> None:
        # Buys push the price up; sells push it down.
        sign = 1 if direction == "buy" else -1
        self._cumulative[asset_id] = (
            self._cumulative.get(asset_id, Decimal(0)) + sign * permanent_impact_per_share
        )
    
    def apply(self, asset_id: AssetId, raw_price: Decimal) -> Decimal:
        """Apply the accumulated adjustment to a raw price."""
        return raw_price + self._cumulative.get(asset_id, Decimal(0))
```

### Analytics

```python
# analytics/sharpe.py

def psr(sr_hat: float, sr_star: float, T: int,
        gamma_3: float, gamma_4: float) -> float:
    """Probabilistic Sharpe Ratio. Bailey-LdP 2012."""
    ...

def dsr(sr_hat: float, T: int, gamma_3: float, gamma_4: float,
        v_sr: float, N_effective: int) -> float:
    """Deflated Sharpe Ratio. Bailey-LdP 2014.
    
    sr_0 derived from the False Strategy Theorem benchmark.
    Verified against the paper's numerical example: SR_hat=1.5, T=60,
    gamma_3=-0.5, gamma_4=5, N=30, V[{SR_n}]=0.4 -> DSR=0.971 (within 1e-3).
    """
    ...

def min_trl(sr_hat: float, sr_star: float, alpha: float,
            gamma_3: float, gamma_4: float) -> int:
    """Minimum Track Record Length."""
    ...

# analytics/scorecard.py

class Scorecard(BaseModel):
    """LdP chapter 14 scorecard. Six categories; Markdown renderer."""
    
    general: GeneralCharacteristics
    performance: Performance
    runs_and_drawdowns: RunsAndDrawdowns
    implementation_shortfall: ImplementationShortfall
    risk_adjusted: RiskAdjusted  # PSR, DSR, MinTRL
    attribution: Attribution
    
    def to_markdown(self) -> str:
        ...

# analytics/distribution.py

T = TypeVar("T")

class BacktestPathDistribution(Generic[T]):
    """Container for the multiple paths produced by CPCV. ADR 0001 decision 3
    + ADR 0002 decision 4.
    
    Provides aggregation methods that return statistics, never a single mean.
    """
    
    def __init__(self, paths: list[T], path_count: int):
        self._paths = paths
        self.path_count = path_count
        if path_count < 30:
            warnings.warn(f"CPCV path count {path_count} below stability threshold")
    
    def to_pandas(self) -> pd.DataFrame:
        """ADR 0001 decision 12 boundary adapter."""
        ...
    
    def percentiles(self, percentiles: list[float] = None) -> dict[float, T]:
        ...
    
    def median(self) -> T: ...
    def p10(self) -> T: ...
    def p90(self) -> T: ...
```

### Validation

```python
# validation/cv.py

class CVSplitter(Protocol):
    def split(self, observations: pl.DataFrame,
              label_horizons: pl.Series) -> Iterator[Split]:
        ...

class PurgedKFoldSplitter(CVSplitter):
    def __init__(self, k: int, embargo_pct: float = 0.05):
        ...

class WalkForwardSplitter(CVSplitter):
    """Single-path baseline; ADR 0002 decision 17."""
    def __init__(self, train_end: datetime, test_start: datetime):
        ...

class CPCVSplitter(CVSplitter):
    """N groups, k held out per combination; phi(N,k) paths."""
    def __init__(self, N: int, k: int, embargo_pct: float = 0.05):
        ...

# validation/trial_registry.py

class TrialRegistry:
    """SQLite WAL-backed. Single-machine concurrent (multiple notebooks
    plus pytest workers). ADR 0002 decision 19.
    """
    
    def __init__(self, db_path: Path):
        ...
    
    def record(self, dataset_fingerprint: str, strategy_family: str,
               sr_hat: float, T: int, gamma_3: float, gamma_4: float,
               metadata: dict) -> int:
        ...
    
    def effective_n_and_sr_variance(self, dataset_fingerprint: str,
                                    strategy_family: str) -> tuple[int, float]:
        """For DSR computation. PCA-based by default; ONC is v1.1."""
        ...

# validation/confidence_tier.py

class ConfidenceTier(Enum):
    SINGLE_RUN_PRE_SPECIFIED = "single_run_pre_specified"
    WALK_FORWARD_VALIDATED = "walk_forward_validated"
    CPCV_WITH_DSR_CORRECTION = "cpcv_with_dsr_correction"
    SWEEP_SELECTED_NO_CORRECTION = "sweep_selected_no_correction"

class BacktestResult(BaseModel):
    sr_hat: float
    psr: float | None
    dsr: float | None
    min_trl: int | None
    confidence_tier: ConfidenceTier
    # ... other scorecard fields
    
    def render_markdown(self) -> str:
        if (self.psr is None and self.dsr is None and
            self.confidence_tier not in (
                ConfidenceTier.SINGLE_RUN_PRE_SPECIFIED,
            )):
            raise RenderEnforcementError(
                "Render of raw SR without PSR/DSR requires "
                "ConfidenceTier.SINGLE_RUN_PRE_SPECIFIED with N=1."
            )
        ...
```

### The bar loop

```python
# engine/bar_loop.py

class BarLoop:
    """The per-bar dispatch driver. Single-process, sequential by design.
    
    For CPCV path-parallel execution, the Runner orchestrates multiple
    BarLoop instances in separate processes (multiprocessing).
    """
    
    def __init__(self, *,
                 data_source: PitDataSource,
                 universe: Universe,
                 signal: Signal,
                 policy: Policy,
                 cost_model: CostModel,
                 commission: Commission,
                 permanent_register: PermanentImpactRegister,
                 clock: TestClock,
                 trial_registry: TrialRegistry | None = None):
        ...
    
    def run(self, start_dt: datetime, end_dt: datetime) -> BacktestResult:
        """Per-bar sequence:
        1. clock.advance_to(dt)
        2. data_source.get_corporate_actions(...) -> apply splits, dividends
        3. signal.compute(universe, dt, pit_view) -> signal output (uses data
           strictly with available_dt < dt; the engine enforces this on the
           pit_view callable, not on the signal code)
        4. policy.target_positions(signal, current, cost_estimator, dt)
           -> target dollar amounts
        5. matching_engine.submit(orders, market_state) -> fills
           (queries cost_model.compute_fill; permanent_register.record)
        6. portfolio_state.apply(fills) -> updated positions, cash, P&L
        7. analytics.record_bar(dt, portfolio_state, fills, signal)
        """
        ...
```

```python
# engine/runner.py

class Runner:
    """Orchestrates CPCV paths or parameter sweeps across processes.
    
    Each child process gets a read-only view of the data source (Polars
    LazyFrames pickle cheaply) and runs an independent BarLoop. Results
    aggregate into a BacktestPathDistribution.
    """
    
    def run_cpcv(self, *,
                 cv_splitter: CPCVSplitter,
                 bar_loop_factory: Callable[[], BarLoop],
                 num_workers: int | None = None) -> BacktestPathDistribution[BacktestResult]:
        ...
    
    def run_sweep(self, *,
                  param_grid: list[dict],
                  bar_loop_factory: Callable[[dict], BarLoop]) -> pl.DataFrame:
        """Returns a frame of (params, BacktestResult) for sensitivity
        analysis. Sweep-mode results are tagged with
        ConfidenceTier.SWEEP_SELECTED_NO_CORRECTION until corrected.
        """
        ...
```

### Trust boundaries (enumerated)

ADR 0001 decision 2 made the structural lookahead claim honest by requiring an enumeration. The following are the v1 trust boundaries: places the engine relies on the user to not bypass the API's intent.

| # | Boundary | What the engine prevents | What the user can still do | Mitigation |
|---|----------|---------------------------|-----------------------------|------------|
| 1 | Arbitrary Python in `Signal.compute()` | `pit_view` only exposes `available_dt < dt` data | `import requests` to fetch live data inside compute | Documentation; lint rule in `tests/` that flags network imports in signal modules |
| 2 | Arbitrary Python in `Algo.__call__` | Same `pit_view` discipline | Same | Same |
| 3 | External feature joins via `additional_data` | The data layer rejects frames without an `available_dt` column | A user can populate `available_dt` incorrectly | Validation contract checks that `available_dt` values are not in the future relative to any `period_end_dt` in the same frame |
| 4 | Polars DataFrame mutation | Engine returns immutable Polars frames | A user with a `.clone()` can mutate the copy | Documentation; Polars-native immutability covers most cases |
| 5 | Closure variables in user code | Engine cannot inspect user closures | A user can close over a global DataFrame loaded outside the engine | Documentation only |
| 6 | `@lru_cache` on user-defined helpers | Engine cannot prevent caching | A cached helper may return stale results across backtest runs | Documentation; the trial registry includes a dataset fingerprint that detects this |
| 7 | Direct `pandas.read_csv` of a non-PIT file | Engine cannot intercept | A user can load a Yahoo Finance CSV with survivorship bias and pass it as `additional_data` | The `Universe` API requires the user to declare membership explicitly; the data quality contracts flag inconsistencies |

The documentation in `docs/METHODOLOGY.md` enumerates these in the same form. The engine's `--strict` flag (added at M3) raises on any frame passed to `additional_data` that fails the contracts in items 3 and 7.

### Event-loop choice

The proposed bar loop is single-process, sequential, per-bar dispatch. Considered and rejected alternatives:

- **Message bus (nautilus_trader style)**: provides better backtest-live parity. For v1 the simpler dispatch loop is sufficient because v1 is research-only. The kernel-sharing claim in ADR 0001 decision 5 is honored by the `Clock` injection pattern; the message bus is not required to keep that promise. v1.1 can refactor to a message bus if live trading is added.
- **Event queue (qstrader style)**: introduces a queue.Queue that holds typed events. Adds complexity without benefit for single-process daily-bar backtesting where the dispatch order is fully determined by the per-bar sequence above.
- **Async / await**: not needed for single-process backtesting. Adds Python complexity.

Multiprocess parallelism for CPCV and parameter sweeps lives in the `Runner`, not the `BarLoop`. Workers are independent processes with a read-only data view; no shared state.

### Data flow per bar (visual)

```
clock.advance_to(dt)
        |
        v
data_source: (apply corporate_actions on ex_date), produces PriceRecord, Fundamentals
        |
        v
signal.compute(universe, dt, pit_view): (pit_view limits to available_dt < dt), produces SignalOutput
        |
        v
policy.target_positions(signal, current, cost_estimator, dt)
        |                                |
        |                                +-- cost_estimator.pre_trade_estimate(...)
        v
matching_engine.submit(orders, market_state)
        |                                |
        |                                +-- cost_model.compute_fill(order, state)
        |                                +-- permanent_register.record(...)
        v
portfolio_state.apply(fills) --> positions, cash, P&L
        |
        v
analytics.record_bar(dt, portfolio_state, fills, signal)
```

### V1.1 extensibility seams

The architecture is designed so the following v1.1 items are isolated module additions, not refactors:

- **Differential testing against `zipline-reloaded`**: a new `tests/differential/` directory with a `zipline_adapter.py` that translates pit-backtest strategies into zipline equivalents. No engine changes.
- **Spin-offs as actual share distributions**: a new `SpinoffAsShares(CorporateAction)` discriminant added to the action union. Matching engine and portfolio state add handlers. No protocol changes.
- **Borrow availability and rate feed**: a new `BorrowDataSource` protocol consumed by a new `CarryCostModel`. The execution layer composes a `CarryCostModel` alongside the `CostModel`; portfolio short positions are charged from `CarryCostModel`. No `CostModel` protocol changes.
- **ONC clustering for effective N**: a new `ONCClusterer` consumed by `TrialRegistry.effective_n_and_sr_variance()`. The PCA-based default is replaced when configured.
- **Auction prices as a data-layer field**: `PriceRecord` extended with optional `auction_open` and `auction_close` columns. `FillPriceModel` extended with `AUCTION_OPEN` and `AUCTION_CLOSE`. No protocol churn for callers that do not opt in.
- **Live trading**: `LiveClock` already exists. The matching engine is the v1.1 work: replace `MatchingEngine` with a `LiveBrokerClient` implementation. The `Order` and `Fill` types are unchanged. The `BarLoop` is replaced by an event-driven loop that the `LiveBrokerClient` drives via callbacks; this is the work the kernel-sharing pattern is paying for.

## Skeptical reviewer's response

Same senior multi-strat-fund quant persona that reviewed ADRs 0001 and 0002. Reproduced verbatim.

### Reviewer summary verdict

Ship it after surgical changes. This is the strongest of the three documents. The author has internalized the lessons from ADR 0001 and 0002 and produced an architecture that is roughly 80% correct on first pass, which is rare. Most architectures the reviewer sees at this stage have one or two fatal flaws and five or six annoying ones; this one has zero fatal flaws and about eight annoying ones. The annoying ones are fixable in a focused half-day before Tuesday.

Structural skeleton is correct (six layers, protocol-based interfaces, sequential BarLoop, separate Runner for parallelism, dual-timestamp throughout). Data model has real thought. The trust boundary section is the kind of thing the reviewer rarely sees written down before week one.

Three over-engineered places (CPCV path generic, AlgoStack temp/perm dict pattern lifted wholesale from Zipline, PermanentImpactRegister as a separate component). Two under-engineered places (Pydantic at every boundary will burn on hot paths; CashDividendAction as a CorporateAction conflates two semantically different things). Fix and start coding Tuesday.

### What the reviewer thinks the architecture gets right

- Protocol-first separation of Universe, PitDataSource, Signal, Policy, CostModel, MatchingEngine, Clock. Three backtesters the reviewer has built conflated PriceSource with Universe and all three required surgery in year two.
- Strict-less-than rule on `pit_view` (`available_dt < dt`, not `<=`) is the single most important line in the document. Stops fundamentals leakage. Cites Novy-Marx 2013 quality factor replication mess as the canonical example.
- Discriminated union on CorporateAction is the right Pydantic v2 idiom; pattern-match exhaustively, type checker flags missing branches in v1.1.
- `fill_price_model` required on Order with no default. Every backtester with a default fill price model has eventually had a researcher run an experiment with the wrong default and not notice for a quarter.
- Almgren 2005 calibration (eta=0.142, beta=0.6, gamma=0.314) as default for SquareRootImpact is the right paper to anchor on.
- Bailey-LdP 2014 numerical anchor (DSR=0.971) as a test fixture saves two weeks of debugging when DSR numbers do not match Lopez de Prado's worked examples.
- Runner-outside-BarLoop separation for multiprocessing. Parallelize across paths and parameter combinations, never within a single backtest. One backtester the reviewer worked with attempted to parallelize at the bar level and the non-determinism made the test suite useless.
- ConfidenceTier enum with four explicit levels and RenderEnforcementError on raw SR without PSR/DSR is the single best architectural decision in this document.

### What the reviewer thinks the architecture gets wrong

- **AlgoStack with temp/perm dict pattern lifted from Quantopian/Zipline.** Wrong abstraction for a teaching artifact. Zipline invented this to avoid being opinionated about state contents; a teaching backtester wants the type system to tell you what flows between algos. Replace with explicit typed slots or drop AlgoStack entirely and make Policy.target_positions a single function. AlgoStack composition solves a v2 problem.
- **Signal.compute returning a frame with [asset_id, score, dt].** The `dt` column is redundant. Either drop or return `dict[AssetId, float]`.
- **PitDataSource exposing get_price and get_fundamental separately.** Add `get_table(table_name, ...)` now as forward-compatibility seam, or document that adding a new table type is a breaking API change.
- **MatchingEngine.submit returning a single Fill.** A real matching engine returns zero or more fills. Change to `list[Fill] | OrderStatus`.
- **Clock protocol with `now()` only.** Add `is_market_open(dt)` and `next_bar(dt)`. Otherwise every Signal and Policy reaches into a calendar object directly. Every backtester the reviewer has worked on has collapsed Clock + Calendar into one by v1.2.
- **PermanentImpactRegister as a separate component.** Wrong layering. Permanent impact is a property of the price you observe given the trades you have made. Belongs inside the data source layer as an `ImpactedPriceSource` decorator over the raw source. Current design creates weird coupling where Signal.compute has to know to use impacted prices and if you forget you get silently wrong backtests.
- **Trust boundary list missing two items.** RNG (any signal using `random.random()` or `np.random.*` without a passed-in `Generator` produces non-reproducible backtests; bitten twice in production) and the global Polars thread pool (concurrent Runner paths share the global pool causing non-deterministic ordering).
- **Engine step ordering.** "Data corp actions apply" before "signal.compute" is correct for splits and dividends but wrong for delistings. A stock that delists at close on day T should still have its signal computed using prices through T-1, with the delisting applied at the open of T+1. Make explicit and unit-test it.
- **No pre-flight check on Universe and PitDataSource compatibility.** Backtest construction should verify every asset in `Universe.members_at(start)` has prices at start. Otherwise NaN gaps and a researcher spends a day debugging.

### What the reviewer thinks the architecture missed

- **Slippage versus market impact.** CostModel conflates both into `compute_fill`. Different phenomena, decompose differently in attribution. Split or document clearly.
- **Borrow cost and short interest.** Architecture says nothing about whether v1 supports shorts. If yes, borrow cost is v1 with a stub. If no, add `LongOnlyPolicy` that rejects negative target weights at the protocol level.
- **Determinism guarantees.** No top-level invariant. Determinism requires pinned Polars version, pinned RNG seed, sorted output frames at every step, no `set` iteration in policy/signal layers. Write as a top-level guarantee or chase why CPCV path 47 is different on rerun.
- **Data freshness checks at startup.** Print SHA256 of parquet snapshots and warn if older than N days.
- **Cash and FX.** Single-currency USD assumption for v1 should be written down.
- **Performance instrumentation.** Add `BarLoop.timing_breakdown()` reporting per-step time. Without this the first budget blowout has no diagnosis.
- **Signal warm-up handling.** What happens if `required_lookback_days() = 252` and backtest starts with only 100 days of history? Define rejection behavior and error message now.

### Reviewer's specific pushback on choices

- **`AssetId = permaticker:int`.** Correct for v1, defensible for v1.1, wrong by v2. Locks in to Sharadar at the type level. Right move: `AssetId = NewType("AssetId", int)` with separate `IdentifierResolver` protocol mapping AssetId to (ticker, CUSIP, ISIN, permaticker). Costs 50 lines now, saves a type-system migration in v2.
- **Pydantic v2 at boundary + Polars internally.** "Boundary" not defined. Is CorporateAction passed from PitDataSource to MatchingEngine a boundary crossing? Same process. Pydantic validates on every bar if not configured carefully. Decide where the boundary is. Recommendation: Pydantic validation runs once at adapter load when parquet hits the constructor, never again. Internal passes validated by type hints only. `ConfigDict(validate_assignment=False, arbitrary_types_allowed=True, frozen=True)` is mandatory.
- **CostModel queried twice (pre_trade vs compute_fill) as one protocol.** Should be two. Pre-trade is called once per asset per bar (up to 500 times); compute_fill only on assets that got an order (10-50 per bar). Different speed requirements. Split into `PreTradeCostEstimator` and `FillCostComputer`. A single class can implement both. The current single-protocol design lets researchers put expensive computation in `pre_trade_estimate` and tank backtest speed.
- **Single-process BarLoop without message bus.** Correct decision. Write down the criterion for when to revisit: if v1.2 needs intraday tick data, the sequential model breaks and the message bus is needed.
- **PCA-based effective N as default.** Wrong. PCA on the trial correlation matrix assumes you have enough trials; with N<30 PCA eigenvalues are noise. Default should be `naive_effective_n = number_of_independent_strategy_families` (user-supplied) with PCA as opt-in for N>=50. PCA-as-default gives nonsense numbers on small trial counts and researchers trust them.
- **Partial fills via participation cap (10% ADV) with 3-bar decay.** 10% cap reasonable. 3-bar decay arbitrary; Almgren 2005 and Kissell 2014 support 5-10 bars. Make `decay_bars: int = 5` with citation in docstring.
- **CashDividendAction as a CorporateAction.** Wrong abstraction. Cash dividend is a cash flow into the portfolio; not the same as a split (unit transformation on existing shares). Right model: `CorporateAction` covers unit transformations (splits, mergers, delistings); `CashFlow` covers cash deposits and withdrawals (dividends, interest, fees). Two streams, both queried by BarLoop, both applied in their own steps. Elegance (single discriminated union) sacrificed for correctness.
- **PermanentImpactRegister as separate component.** Already covered. Bake into data source as `ImpactedPriceSource`.
- **Trust boundary list completeness.** 10-11 items, not 7. Add RNG, Polars thread pool, mutating frames inside plotting helpers, time-of-day import side effects.

### Reviewer's single most likely architectural decision to bite him

The Pydantic-at-boundary, Polars-internal split. Specifically: are CorporateAction objects flowing between PitDataSource and MatchingEngine validated on every bar or only at adapter load? Default Pydantic v2 behavior validates on every bar. With 5000 bars and 500 assets and corp action rate of 1 in 200 asset-bar slots, that is roughly 12,500 Pydantic validations per backtest just for corp actions. 60-second budget will not survive this without aggressive config.

Week-5 manifestation: M3 SPY reconciliation runs in 8 seconds first pass; M4 multi-asset with corp actions runs in 240 seconds; spend a week pinpointing Pydantic in the corp-action handling; rewrite to validate-once-at-load and pass dataclasses or NamedTuples through the inner loop. Canonical "Pydantic in a hot path" failure mode, seen at two firms.

### Reviewer's recommended changes before week 1

Half a day to a day of focused design work Monday morning before any code:

1. Split CashFlow from CorporateAction. Two protocols, two streams. Half a day. Saves a week in M4.
2. Move PermanentImpactRegister into data source as `ImpactedPriceSource`. Two hours. Saves a class of silent-bug failures.
3. Split CostModel into PreTradeCostEstimator and FillCostComputer. One hour. Prevents researchers putting slow code in hot path.
4. Write determinism invariant as top-level guarantee. List requirements (pinned RNG, pinned Polars, sorted outputs at every step).
5. Define Pydantic/Polars boundary explicitly. Default: Pydantic validates at adapter load only; internal passes use NamedTuple or attrs with slots=True.
6. Add `is_market_open` and `next_bar` to Clock protocol.
7. Change Signal.compute to return `dict[AssetId, float]`.
8. Change MatchingEngine.submit return type to `list[Fill]`.
9. Add RNG and Polars thread pool to trust boundary list.
10. Add startup data freshness check and snapshot SHA256 printout.

### Reviewer on Pydantic for high-frequency types

No. Do not use Pydantic for Fill, Order, PriceRecord, or anything crossing the inner loop more than a few hundred times per backtest. Overhead is measurable: Pydantic v2 with optimal config is 5-10x slower than plain dataclass for construction, and BarLoop will construct these by the millions.

Use `attrs` with `slots=True, frozen=True` or Python 3.12 dataclass with `slots=True, frozen=True` for Fill, Order, PriceRecord. Use Pydantic only at three places: adapter load when parquet/CSV becomes typed objects (validates once, converts to attrs/dataclass after); CLI argument parsing where users provide YAML/JSON config; BacktestResult and Scorecard which are user-facing render targets and run once per backtest. Everywhere else, use attrs or dataclass with slots.

Discriminated union on CorporateAction is borderline. If corp actions validated once at adapter load and stored as a Polars frame of action records (string `action_type` column and per-type columns), keep the Pydantic discriminated union as the "user sees this" type and convert to frame representation for internal use. If kept live in the inner loop, pay for isinstance checks. Make the call based on actual benchmark numbers, not architectural elegance.

This is the most important section of the review. Pydantic-everywhere is an anti-pattern in performance-sensitive code and the author has not yet drawn the line. Draw it before Tuesday.

### Reviewer's final position

Modify, then ship. Architecture fundamentally correct; modifications are all small (1-4 hours each). The Pydantic-boundary decision is the only one requiring real thought; the answer is Pydantic at user-facing surfaces only, attrs/dataclass with slots for everything in the BarLoop, written-down boundary document.

Do the changes Monday morning; write the Pydantic boundary doc Monday afternoon; start engine code Tuesday. Modified architecture supports M1-M5 without major restructuring, hits the 60-second budget, produces the teaching artifact the project is supposed to be.

Strongest of the three ADRs.

## My response to the reviewer

I am accepting this review almost in full. Three documents in, the reviewer has correctly identified the spots where I am pattern-matching to libraries I read in phase 1 (AlgoStack from bt, PermanentImpactRegister from my own reading of nautilus_trader) without checking whether the pattern fits the v1 problem. The accepted changes are below, the contested points are minimal, and the locked architecture follows.

### Accepted

1. **Pydantic at adapter load only; attrs with `slots=True, frozen=True` everywhere else.** This is the single most consequential change. `Fill`, `Order`, `PriceRecord`, `FundamentalRecord`, `CorporateAction`, `CashFlow`, `Bar`, `MarketState`, `TargetPositions` are all attrs classes with slots. Pydantic survives at three surfaces only: adapter load (parquet -> typed objects, validates once), CLI/config parsing, and the user-facing `BacktestResult` and `Scorecard` render path. The boundary is documented in `docs/decisions/0003-architecture.md` and in a top-level project rule.
2. **Split CashFlow from CorporateAction.** Two streams. `CorporateAction` covers unit transformations (splits, delistings as unit changes, stock acquisitions, spin-offs as shares in v1.1). `CashFlow` covers cash movements (dividends, delisting cash proceeds, spin-off-as-cash payouts, borrow fees in v1.1). Both queried per bar; both applied in their own BarLoop step.
3. **`PermanentImpactRegister` -> `ImpactedPriceSource` decorator.** The data source layer composes raw Sharadar prices with an `ImpactedPriceSource` that applies cumulative permanent impact from past fills. Signal.compute and portfolio valuation always see impacted prices. No separate register component.
4. **Split CostModel into `PreTradeCostEstimator` and `FillCostComputer`.** Two protocols, one concrete class can implement both (`SquareRootImpactCostModel implements PreTradeCostEstimator, FillCostComputer`). Pre-trade is called per-asset-per-bar; fill computation only on traded assets.
5. **Signal.compute returns `dict[AssetId, float]`** not a Polars DataFrame. Engine attaches `dt`.
6. **MatchingEngine.submit returns `list[Fill]`** to handle partial and multi-fill cases. Empty list = no fill.
7. **Clock protocol adds `is_market_open(dt)` and `next_bar(dt)`** alongside `now()`. Calendar and Clock collapse into Clock. The pandas-market-calendars backing is documented but hidden behind the interface.
8. **`AssetId = NewType("AssetId", int)` plus separate `IdentifierResolver` protocol.** `SharadarPermatickerResolver` is the v1 implementation. The NewType is a permaticker today; v2 can introduce other identifier kinds without a type migration.
9. **`PitDataSource.get_table(table_name, ...)` added now** as forward-compatibility seam. The v1 implementation just dispatches to the per-table methods; the seam exists for v1.1 alternative-data adapters.
10. **`Backtest.validate()` pre-flight check.** Verifies every asset in `Universe.members_at(start)` has prices, no membership gap exceeds the documented tolerance, all required signal lookback days are available. Failures raise with the offending assets surfaced.
11. **Determinism invariant as top-level guarantee.** Documented in `docs/METHODOLOGY.md` (M5): pinned Polars version; explicit `numpy.random.Generator` plumbed through every random consumer; sorted output frames at every step; no `set` iteration in policy/signal layers; per-process Polars thread pool sized to 1 inside `Runner` workers to keep aggregation order deterministic.
12. **Trust boundary list expanded to 11 items.** Adds: RNG (any signal using `random.random()` or `np.random.*` without an injected `Generator` produces non-reproducible backtests); Polars thread pool (global pool with concurrent workers causes non-deterministic ordering in some aggregations); mutating frames inside plotting/notebook helpers; module-level `import` with network side effects. Mitigations documented per item.
13. **Engine step ordering for delistings clarified.** A delisting at close on day T uses prices through T-1 for signal computation; the delisting cash flow is applied at the open of T+1 with cash credited to the portfolio. Unit test for this is in M3 acceptance criterion 4.
14. **Slippage and market impact split conceptually in `FillCostComputer.compute`.** Returns a `CostBreakdown` Pydantic model (user-facing render target) with separate `slippage_bps`, `temporary_impact_bps`, `permanent_impact_bps`, `commission` fields. The single number for total cost is a property of the breakdown.
15. **Long-only at v1.** `LongOnlyPolicy` is the v1 implementation; it rejects negative target weights at the protocol level. Short selling is v1.1. The CarryCostModel and BorrowDataSource are v1.1 protocols.
16. **Data freshness check at startup.** CLI and `Backtest.__init__` print the SHA256 of the parquet snapshots in use; warn if older than 30 days; warn loudly if older than 90.
17. **Single-currency USD assumption** documented in README and `docs/METHODOLOGY.md`.
18. **Performance instrumentation: `BarLoop.timing_breakdown()`** returns a dict of per-step time. CI dumps this on every benchmark run.
19. **Signal warm-up handling.** Backtest construction checks `signal.required_lookback_days()` against available history. Insufficient history raises `InsufficientHistoryError` with the requested lookback, available days, and the earliest viable start date in the message.
20. **Partial fill decay default 5 bars** (Almgren 2005, Kissell 2014) instead of 3. Cited in docstring.
21. **PCA-based effective N for DSR is opt-in for N>=50 trials.** Default is `naive_effective_n = number_of_independent_strategy_families` supplied by the user at the trial registry construction. With N<30, PCA raises `InsufficientTrialsForPCAError`.
22. **Drop AlgoStack with temp/perm dict from v1.** Replaced by a single `Policy.target_positions(signal_output, current_positions, cost_estimator, dt)` function. Concrete policies subclass `Policy`. The momentum study for M5 uses `LongOnlyMonthlyRebalancePolicy(signal_to_weights_fn=top_quintile_equal_weight)`. AlgoStack returns in v1.1 if needed; for v1, the type system carries the contract.
23. **Criterion to revisit single-process BarLoop documented.** If v1.2 needs intraday tick data or live trading, the sequential per-bar dispatch is replaced by an event-driven loop driven by the `LiveBrokerClient`. The criterion: any data stream whose arrival is not aligned to per-bar boundaries.

### Contested (minimal)

None. The reviewer's points are technically right or right enough that contesting is not productive. The 23 accepted changes above subsume every specific concern.

### Locked architecture

The architecture proposed above is locked with the 23 modifications applied. The key API shapes after modification:

**Records (attrs with slots=True, frozen=True)**:
```python
@attrs.frozen(slots=True)
class PriceRecord:
    asset_id: AssetId  # NewType(int)
    period_end_dt: datetime
    available_dt: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    cumulative_adjustment: Decimal

@attrs.frozen(slots=True)
class Order:
    order_id: str
    asset_id: AssetId
    quantity: Decimal
    fill_price_model: FillPriceModel  # required
    submit_dt: datetime

@attrs.frozen(slots=True)
class Fill:
    order_id: str
    asset_id: AssetId
    quantity: Decimal
    fill_price: Decimal
    slippage_bps: Decimal
    temporary_impact_bps: Decimal
    permanent_impact_per_share: Decimal
    commission: Decimal
    dt: datetime
```

**Pydantic (validates once at adapter load)**:
```python
class PriceRecordValidated(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    # same fields as PriceRecord; converted to attrs via .to_attrs() after validation
```

**Two corp-action streams**:
```python
@attrs.frozen(slots=True)
class CorporateAction:
    asset_id: AssetId
    ex_date: datetime
    action_type: Literal["split", "delisting_stock_acquisition", ...]
    # type-specific fields via attrs subclasses; discriminated by action_type

@attrs.frozen(slots=True)
class CashFlow:
    asset_id: AssetId | None  # None for portfolio-level cash flows
    dt: datetime
    flow_type: Literal["cash_dividend", "delisting_cash_proceeds",
                       "spinoff_cash_equivalent", "borrow_fee"]
    amount: Decimal
```

**ImpactedPriceSource decorator**:
```python
class ImpactedPriceSource(PitDataSource):
    """Wraps a raw PitDataSource. Maintains a per-asset cumulative
    permanent-impact register; applies it to every price read so Signal.compute
    always sees impacted prices. Reset by Backtest.__init__.
    """
    
    def __init__(self, raw: PitDataSource, fill_history: FillHistory):
        ...
    
    def get_price(self, asset_id, dt, field):
        raw_price = self._raw.get_price(asset_id, dt, field)
        impact = self._fill_history.cumulative_permanent_impact(asset_id, before=dt)
        return raw_price + impact
```

**Cost protocols (split)**:
```python
class PreTradeCostEstimator(Protocol):
    def estimate(self, asset_id: AssetId, shares: Decimal,
                 direction: Literal["buy", "sell"],
                 dt: datetime, market_state: MarketState) -> Decimal:
        """Returns expected total cost in bps. Fast path."""
        ...

class FillCostComputer(Protocol):
    def compute(self, order: Order, fill_state: FillState) -> CostBreakdown:
        """Returns the detailed breakdown (slippage, impact, commission)."""
        ...

class SquareRootImpactCostModel(PreTradeCostEstimator, FillCostComputer):
    """Default for v1. Almgren 2005 calibration."""
    ...
```

**Signal returns dict**:
```python
class Signal(Protocol):
    def required_lookback_days(self) -> int: ...
    
    def compute(self, universe: Universe, dt: datetime,
                pit_view: PitView) -> dict[AssetId, float]:
        ...
```

**Policy without AlgoStack**:
```python
class Policy(Protocol):
    def target_positions(self, signal_output: dict[AssetId, float],
                         current_positions: PortfolioState,
                         cost_estimator: PreTradeCostEstimator,
                         dt: datetime) -> TargetPositions:
        ...

class LongOnlyMonthlyRebalancePolicy(Policy):
    """v1 default. Rejects negative target weights at construction."""
    
    def __init__(self, signal_to_weights_fn: Callable[[dict[AssetId, float]], dict[AssetId, Decimal]]):
        ...
```

**Clock expanded**:
```python
class Clock(Protocol):
    def now(self) -> datetime: ...
    def is_market_open(self, dt: datetime) -> bool: ...
    def next_bar(self, dt: datetime) -> datetime: ...
```

**MatchingEngine returns list[Fill]**:
```python
class MatchingEngine:
    def submit(self, order: Order, market_state: MarketState) -> list[Fill]:
        ...
```

**Identifier resolver separate**:
```python
class IdentifierResolver(Protocol):
    def resolve_ticker(self, ticker: str, dt: datetime) -> AssetId: ...
    def get_ticker(self, asset_id: AssetId, dt: datetime) -> str: ...

class SharadarPermatickerResolver(IdentifierResolver):
    """v1 implementation using Sharadar TICKERS."""
    ...
```

**Trust boundary list (11 items)**: arbitrary Python in Signal.compute; arbitrary Python in Policy.target_positions; external feature joins via additional_data; Polars DataFrame mutation; closure variables; `@lru_cache` on user helpers; direct `pandas.read_csv` of non-PIT files; RNG (`random.random`, `np.random.*` without injected Generator); Polars global thread pool with concurrent Runner workers; mutating frames inside plotting/notebook helpers; module-level `import` with network side effects. Mitigations documented per item in `docs/METHODOLOGY.md`.

**Determinism invariant**: pinned Polars version; explicit `numpy.random.Generator` plumbed through every consumer; sorted output frames at every step; no `set` iteration in policy or signal layers; per-process Polars thread pool sized to 1 inside Runner workers.

### Status

This ADR is in **Accepted** status as of merge. The architecture defined above is binding on all M1 through M5 implementation. Revisiting any architectural decision requires a new ADR explicitly superseding the relevant section.

### What ships before Tuesday (week 1)

Pre-M1 Monday work, in this order:

1. Write `docs/methodology/total_return_reconstruction.md` (SPDR-published SPY TR, same-day-at-close reinvestment convention).
2. Write `docs/methodology/dataset_versioning.md` (Sharadar pull SHA256 commitment, pull date, snapshot path convention).
3. Write `docs/methodology/pydantic_polars_boundary.md` documenting where Pydantic is allowed and where it is not.
4. Write `docs/methodology/determinism.md` listing the determinism invariant and its requirements.
5. Create `src/pit_backtest/` package layout per the locked architecture; stub the protocols with `Protocol` and `...`; nothing implemented.

That is Monday. Tuesday is M1 day 1 with the SEP adapter and the total-return reconstruction.
