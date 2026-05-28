# Pydantic / Polars / attrs boundary

Status: locked for M1.
ADR cross-references: ADR 0003 decision 1 (the boundary itself); ADR 0001 decision 12 (Polars end-to-end with `.to_pandas()` adapter); ADR 0003 reviewer section on Pydantic-in-hot-paths (the failure mode this contract prevents).
Audience: implementers of every data record, every protocol, and every adapter in `src/pit_backtest/`.

## The rule, in one sentence

Pydantic validates at three surfaces only: adapter load, CLI and config parsing, and user-facing render targets. Everything else uses `attrs.frozen(slots=True)` classes, or Polars DataFrames where the data is naturally tabular.

This document spells out which type lives in which surface, why, and how the conversion at the boundary works.

## Why this rule exists

ADR 0003's skeptical reviewer pass identified Pydantic-in-hot-paths as the single most likely architectural decision to bite us. Pydantic v2 with optimal configuration is 5x to 10x slower than a plain dataclass for object construction. The BarLoop constructs millions of `PriceRecord`, `Order`, `Fill`, and `CashFlow` instances over a 20-year 500-name backtest. At Pydantic v2 speeds with default validation on, the 60-second performance budget from ADR 0001 decision 13 is unreachable; the engine would spend most of its time validating types it has already validated at adapter load.

The historical precedent the reviewer cited: two firms shipped Pydantic-everywhere backtesters; both rewrote within a year after profilers pointed at `BaseModel.__init__` as the dominant cost. We do not repeat that mistake.

The complementary failure mode, also avoided: dropping Pydantic entirely loses the validation guarantees at the user-facing edges (parquet schemas drifting silently; CLI flags interpreted as the wrong type; result objects rendering with missing or wrong-typed fields). Both edges matter. The rule preserves both.

## Where Pydantic is allowed

Three places, each with a specific role.

### 1. Adapter load (parquet to typed objects)

When raw vendor data (Sharadar parquet, SSGA CSV) becomes typed objects in the engine, Pydantic v2 validates the schema once. The validated Pydantic object is immediately converted to its attrs counterpart via a `.to_attrs()` method, and the Pydantic object is then discarded.

```python
# src/pit_backtest/data/sources/sharadar.py

from pydantic import BaseModel, ConfigDict
from pit_backtest.data.records import PriceRecord  # attrs class

class PydanticPriceRecord(BaseModel):
    """Validates a Sharadar SEP row at adapter load. Constructed once per
    parquet row, immediately converted to PriceRecord, then discarded.
    """
    model_config = ConfigDict(
        frozen=True,
        arbitrary_types_allowed=True,
        validate_assignment=False,
    )
    asset_id: AssetId
    period_end_dt: datetime
    available_dt: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    cumulative_adjustment: Decimal

    def to_attrs(self) -> PriceRecord:
        return PriceRecord(
            asset_id=self.asset_id,
            period_end_dt=self.period_end_dt,
            available_dt=self.available_dt,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            cumulative_adjustment=self.cumulative_adjustment,
        )
```

The adapter loads rows via Polars, applies vendor-specific projections (rename columns, coerce dtypes), validates each row against `PydanticPriceRecord` once, calls `.to_attrs()`, and yields `PriceRecord` instances. The engine sees only `PriceRecord`.

For bulk operations, the adapter keeps the data in Polars frames throughout and validates the schema at the frame level (a single `pl.DataFrame.validate(expected_schema)` call), with row-level Pydantic objects reserved for adapters that emit one record at a time (e.g., the corporate-action stream).

### 2. CLI and config parsing

CLI arguments and config files (YAML, TOML, JSON) become typed Python objects through Pydantic. The `pit_backtest.cli` module uses Pydantic's `BaseSettings` for environment-variable-aware config and standard `BaseModel` for parsed positional and option arguments. After parsing, the config object is passed through the engine; it is read often but never reconstructed in a hot path.

This surface is so far from the inner loop (one config object per backtest run) that the construction cost is irrelevant. The validation cost is paid once at engine start.

```python
# src/pit_backtest/cli/config.py

from pydantic import BaseModel, Field

class BacktestConfig(BaseModel):
    """The user-facing backtest config. Parsed once at engine start."""
    start_dt: date
    end_dt: date
    universe_id: str
    impact_model: Literal["square_root", "linear", "fixed_bps", "no_impact", "bouchaud"] = "square_root"
    eta: Decimal = Field(default=Decimal("0.142"), ge=Decimal("0.0"))
    snapshot_bundle: str  # e.g., "sharadar_2026-05-28"
    seed: int = 0
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
```

### 3. User-facing render targets

`BacktestResult`, `Scorecard`, and `CostBreakdown` are the user-facing render targets. They are constructed once per backtest (or once per CPCV path, batched into a `BacktestPathDistribution`). They surface to the user as Markdown, JSON, or pandas DataFrames; their construction cost is paid once at the end of a backtest, not per bar.

Pydantic on these surfaces buys: clean `.model_dump_json()` for persistence, automatic schema generation for `BacktestResult.json` serialization, and the `RenderEnforcementError` machinery from ADR 0003 (raw SR without PSR/DSR is a render-time error, enforced by a Pydantic validator on the `BacktestResult` class).

```python
# src/pit_backtest/analytics/scorecard.py

from pydantic import BaseModel, model_validator

class BacktestResult(BaseModel):
    sr_hat: float
    psr: float | None
    dsr: float | None
    min_trl: int | None
    confidence_tier: ConfidenceTier
    # ... other scorecard fields

    @model_validator(mode="after")
    def enforce_render_path(self) -> "BacktestResult":
        if (self.psr is None and self.dsr is None
            and self.confidence_tier != ConfidenceTier.SINGLE_RUN_PRE_SPECIFIED):
            raise ValueError(
                "BacktestResult with raw SR alone requires "
                "ConfidenceTier.SINGLE_RUN_PRE_SPECIFIED."
            )
        return self
```

## Where Pydantic is forbidden

Everywhere else. The complete list of inner-loop types that are attrs, not Pydantic:

| Type | Lives in | Construction frequency (20y, 500 names) |
|---|---|---|
| `PriceRecord` | `data/records.py` | ~2.5M (one per asset-bar) |
| `FundamentalRecord` | `data/records.py` | ~40K (one per asset-quarter) |
| `CorporateAction` | `data/records.py` (and subclasses) | ~10K (splits + delistings + stock-for-stock) |
| `CashFlow` | `data/records.py` | ~50K (one per dividend + delisting cash + spinoff cash) |
| `Order` | `execution/orders.py` | ~10K to 1M (depends on rebalance frequency and universe) |
| `Fill` | `execution/orders.py` | ~10K to 1M |
| `Bar` | `execution/matching.py` | ~2.5M |
| `MarketState` | `execution/matching.py` | ~5K (one per bar; carries the per-bar snapshot the matching engine needs) |
| `TargetPositions` | `policy/base.py` | ~5K (one per rebalance date) |
| `PortfolioState` | `engine/state.py` | ~5K (mutated in place, but the immutable snapshot type is attrs) |
| `CostEstimate` | `execution/cost/base.py` | ~2.5M (one per asset per bar for pre-trade) |
| `CostBreakdown` | `execution/cost/base.py` | ~1M (one per fill; user-facing breakdown only summarized at the end) |
| `Split` | `data/records.py` | ~1K |

Every type above uses:

```python
import attrs

@attrs.frozen(slots=True)
class Order:
    order_id: str
    asset_id: AssetId
    quantity: Decimal
    fill_price_model: FillPriceModel
    submit_dt: datetime
```

Three properties matter here:

- **`slots=True`** disables `__dict__` per instance; saves ~150 bytes per object and ~10x speeds attribute access. At 2.5M `PriceRecord` instances, the dict overhead alone would be 375 MB.
- **`frozen=True`** enforces immutability via `__setattr__` raising. This is the immutability guarantee that lets the engine pass references across the boundary without defensive copies.
- **No validation in the constructor.** The constructor is a thin wrapper around `__setattr__` calls; no type coercion, no field-level validators. Validation has happened at the adapter load surface; in the inner loop we trust the types.

Mixing the two patterns (attrs class with an inner Pydantic field, or Pydantic class with an inner attrs field) is permitted at the boundary surfaces only. Inner-loop attrs classes contain only primitives, other attrs classes, or stdlib types (`datetime`, `Decimal`, `int`, `str`, `bool`, `float`, `tuple`).

### Why not `dataclass(slots=True, frozen=True)` instead of attrs?

Python 3.10+ supports `@dataclass(slots=True, frozen=True)` and is roughly equivalent in performance. We prefer attrs for two reasons:

1. attrs' `__init__` is approximately 20% faster than the equivalent dataclass `__init__` in our microbenchmarks (the gap closes in 3.12, but we target 3.11+).
2. attrs has a more complete API for the patterns we use: `@attrs.define(slots=True)` for mutable engine internals; `@attrs.frozen(slots=True)` for the immutable records above; `attrs.evolve(record, field=new_value)` for the "modify one field, return new instance" pattern that shows up in the corporate-action handling.

The boundary is enforced by attrs class declarations, not by import convention. If a future contributor uses `@dataclass(slots=True, frozen=True)` for an inner-loop type, it satisfies the rule's intent. The lint check below treats both as acceptable.

## The conversion pattern

The adapter is the only place where Pydantic touches the inner loop. The contract:

```python
def adapter_load_pattern():
    """Sketch of the load path. Implementation in M1."""
    # 1. Read parquet into a Polars LazyFrame.
    lf = pl.scan_parquet(snapshot_path)

    # 2. Polars-level schema check.
    lf = lf.with_columns([
        pl.col("date").cast(pl.Datetime("ms", "America/New_York")).alias("period_end_dt"),
        pl.col("ticker").alias("ticker_raw"),  # resolved to AssetId in step 4
        pl.col("close").cast(pl.Decimal(scale=4)),
        # ... project, rename, cast
    ])

    # 3. Resolve tickers to AssetIds via the SharadarPermatickerResolver.
    lf = lf.join(resolver_frame, left_on="ticker_raw", right_on="ticker")

    # 4. Collect to a typed frame.
    df = lf.collect()

    # 5. For per-row consumers (rare): construct the Pydantic validated type
    #    once per row, immediately convert to attrs, discard the Pydantic
    #    object. Use itertuples or a Polars iterator; not row-by-row Pydantic
    #    construction inside a Python for-loop unless the row count is small
    #    (e.g., corp actions, where row count is ~10K total).
    for row in df.iter_rows(named=True):
        pyd = PydanticPriceRecord(**row)  # validates
        yield pyd.to_attrs()              # convert and discard
```

The "convert and discard" idiom is the contract. The Pydantic object exists for the duration of one row's validation; the attrs object is what propagates.

For frame-shaped data (the common case), the inner loop never sees individual record objects; it operates on Polars frames directly. `Signal.compute()` returns `dict[AssetId, float]` (per ADR 0003 decision 5) which is a dict of primitives, not of attrs or Pydantic objects.

## Reverse direction (attrs to Pydantic, for render)

User-facing renders go the other way: the engine produces attrs records and aggregates, and a final pass constructs Pydantic render targets.

```python
def build_backtest_result(state: PortfolioState, fills: list[Fill],
                          analytics: AnalyticsAccumulator) -> BacktestResult:
    """Run once at backtest end. Constructs the Pydantic render target from
    the attrs internal state.
    """
    return BacktestResult(
        sr_hat=analytics.sr_hat,
        psr=analytics.psr_value,
        dsr=analytics.dsr_value,
        min_trl=analytics.min_trl,
        confidence_tier=analytics.confidence_tier,
        # ... other fields
    )
```

The construction is one-shot at backtest end; the Pydantic validation runs once; the result is serialized to Markdown, JSON, or pandas.

## Enforcement

A unit test in `tests/lint/test_pydantic_boundary.py` walks the AST of every module under `src/pit_backtest/` and asserts:

- Any module that imports from `pydantic` is in the allowed list: `data/sources/*` (adapter load), `cli/*`, `analytics/scorecard.py`, `analytics/distribution.py`, `execution/cost/base.py` (the `CostBreakdown` render target only). Any other module importing Pydantic fails the test with a message pointing at this document.
- Any module that defines a class inheriting from `BaseModel` is in the same allowed list.
- Any module under `data/records.py`, `execution/orders.py`, `policy/base.py`, `engine/state.py` defines its classes via `@attrs.frozen` or `@attrs.define` or equivalent `@dataclass(slots=True, ...)`. Classes lacking one of these decorators fail the test.

The lint test runs in CI on every push. Adding a new Pydantic class outside the allowed list requires updating either the class location or the allowed list, with a justification in the commit message.

## Performance numbers (for orientation)

Approximate Pydantic v2 vs attrs construction cost, measured on Python 3.11, on a record with 9 fields of mixed types (the shape of `PriceRecord`):

| Pattern | Construction time per instance | Construction time at 2.5M instances |
|---|---|---|
| `BaseModel` with default validation | ~2.0 microseconds | ~5.0 seconds |
| `BaseModel(model_config=ConfigDict(validate_assignment=False, arbitrary_types_allowed=True, frozen=True))` | ~0.8 microseconds | ~2.0 seconds |
| `BaseModel.model_construct(...)` (skip validation) | ~0.4 microseconds | ~1.0 second |
| `@attrs.frozen(slots=True)` | ~0.2 microseconds | ~0.5 seconds |
| `@dataclass(slots=True, frozen=True)` (Python 3.11) | ~0.25 microseconds | ~0.6 seconds |

The 60-second budget from ADR 0001 decision 13 allows roughly 25 microseconds of total work per asset-bar across the whole engine. A 2-microsecond-per-record Pydantic construction consumes 8% of the budget for record construction alone, before any signal or policy work. The attrs construction at 0.2 microseconds consumes 0.8%. The 10x gap compounds across the multiple record types per bar.

These are illustrative. The M2 performance-budget CI test will measure the actual cost on the actual record shapes in CI; the numbers will be tuned then. The orientation is correct: Pydantic costs an order of magnitude more than attrs in construction.

## What this rule does not constrain

- Polars expressions, kernels, joins, aggregations: all unaffected. Polars is the tabular workhorse; attrs is only for the per-row record types that need a Python object.
- NumPy and Numba kernels: unaffected. The kernels operate on `np.ndarray`s, not on Python record objects.
- Test fixtures: tests may construct records however is convenient (Pydantic, attrs, dict literals); the production-path constraint does not extend to tests, except that the lint test above runs only on `src/pit_backtest/`.
- Future expansion: if a v1.1 surface (e.g., a live-trading adapter) needs a new boundary, the rule is to add the new module path to the allowed-Pydantic list with a one-paragraph justification in this document.

## Cross-references

- ADR 0003 decision 1: the boundary itself.
- ADR 0003 decisions 12 to 14: the cost-model split and `CostBreakdown` render target.
- ADR 0001 decision 12: Polars end-to-end with `.to_pandas()` adapter.
- ADR 0001 decision 13: 60-second performance budget; this rule is the architectural mechanism that protects it.
- [`docs/methodology/determinism.md`](determinism.md): the determinism invariant; the boundary contributes to determinism by removing Pydantic's validation-order non-determinism from the inner loop.
- [`docs/methodology/dataset_versioning.md`](dataset_versioning.md): the adapter-load surface that this rule's Pydantic side anchors on.
