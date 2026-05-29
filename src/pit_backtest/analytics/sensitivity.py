"""SensitivityBand attrs container and from_run_sweep factory.

Per ADR 0005 step 14 the eta-sweep rendering surface is a `SensitivityBand`
attrs.frozen container, explicitly NOT a `BacktestPathDistribution`
(parameter uncertainty is not statistical uncertainty per the locked
distinction). Per ADR 0010 lock #1 the `Runner.run_sweep` returns the
raw `list[ConstantWeightDemoResult]`; `SensitivityBand.from_run_sweep`
is the analytics-layer factory that wraps the results with validation.

Per ADR 0010 lock #3 the container rejects `ConfidenceTier !=
SWEEP_SELECTED_NO_CORRECTION` at construction so a future caller cannot
accidentally use the wrong container (CPCV-corrected paths use
`BacktestPathDistribution` per ADR 0005).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import attrs
import polars as pl

from pit_backtest.engine.constant_weight_result import ConstantWeightDemoResult
from pit_backtest.validation.confidence_tier import ConfidenceTier


@attrs.frozen(slots=True)
class SensitivityBand:
    """Container for the per-parameter-value equity curves from a sweep.

    The container's invariants per ADR 0010 lock #3 and #5:
    - parameter_values is sorted ascending and is non-empty.
    - central_value is in parameter_values.
    - per_parameter_equity keys equal set(parameter_values).
    - per_parameter_final_pnl keys equal set(parameter_values).
    - confidence_tier is exactly SWEEP_SELECTED_NO_CORRECTION; any other
      tier raises ValueError at construction because the band is the
      wrong container for CPCV-corrected results (those use
      BacktestPathDistribution per ADR 0005).

    The class is attrs.frozen + slots so the container is immutable once
    constructed; callers that want to modify a band must construct a new
    one.
    """

    parameter_name: str
    parameter_values: tuple[Decimal, ...]
    per_parameter_equity: dict[Decimal, pl.DataFrame]
    per_parameter_final_pnl: dict[Decimal, float]
    central_value: Decimal
    confidence_tier: ConfidenceTier
    tickers: tuple[str, ...]
    start_dt: date
    end_dt: date
    initial_capital: float
    sharadar_bundle: str

    def __attrs_post_init__(self) -> None:
        if self.confidence_tier != ConfidenceTier.SWEEP_SELECTED_NO_CORRECTION:
            raise ValueError(
                "SensitivityBand is for sweep results; CPCV-corrected "
                "paths use BacktestPathDistribution per ADR 0005. Got "
                f"confidence_tier={self.confidence_tier}."
            )
        if not self.parameter_values:
            raise ValueError("parameter_values is empty")
        if not self.parameter_name:
            raise ValueError("parameter_name is empty")
        for prev, curr in zip(self.parameter_values, self.parameter_values[1:]):
            if not prev < curr:
                raise ValueError(
                    f"parameter_values must be sorted ascending; got "
                    f"{self.parameter_values}"
                )
        if self.central_value not in self.parameter_values:
            raise ValueError(
                f"central_value {self.central_value} not in parameter_values "
                f"{self.parameter_values}"
            )
        equity_keys = set(self.per_parameter_equity.keys())
        if equity_keys != set(self.parameter_values):
            raise ValueError(
                f"per_parameter_equity keys {sorted(equity_keys)} do not "
                f"match parameter_values {sorted(self.parameter_values)}"
            )
        pnl_keys = set(self.per_parameter_final_pnl.keys())
        if pnl_keys != set(self.parameter_values):
            raise ValueError(
                f"per_parameter_final_pnl keys {sorted(pnl_keys)} do not "
                f"match parameter_values {sorted(self.parameter_values)}"
            )

    @classmethod
    def from_run_sweep(
        cls,
        results: list[ConstantWeightDemoResult],
        parameter_name: str,
        parameter_values: tuple[Decimal, ...],
        central_value: Decimal,
    ) -> "SensitivityBand":
        """Build a SensitivityBand from a Runner.run_sweep results list.

        Per ADR 0010 lock #2 this is the analytics-layer wrapping site.
        Results are taken in param_grid order (Runner.run_sweep contract);
        the i-th result corresponds to parameter_values[i].

        Raises ValueError if len(results) does not equal len(parameter_values)
        (Runner.run_sweep promises one-to-one alignment so a mismatch
        indicates a caller bug).
        """
        if len(results) != len(parameter_values):
            raise ValueError(
                f"results length {len(results)} does not match "
                f"parameter_values length {len(parameter_values)}; "
                f"Runner.run_sweep returns results in param_grid order"
            )
        per_param_equity: dict[Decimal, pl.DataFrame] = {}
        per_param_pnl: dict[Decimal, float] = {}
        for param_val, result in zip(parameter_values, results):
            per_param_equity[param_val] = result.equity_curve
            per_param_pnl[param_val] = result.final_pnl

        # Cross-result invariants: all results share the same fixture-level
        # config (tickers, window, initial_capital, bundle).
        first = results[0]
        for result in results[1:]:
            if result.tickers != first.tickers:
                raise ValueError(
                    f"results carry inconsistent tickers; expected "
                    f"{first.tickers}, got {result.tickers} from a sweep"
                )
            if result.start_dt != first.start_dt or result.end_dt != first.end_dt:
                raise ValueError(
                    f"results carry inconsistent window; expected "
                    f"[{first.start_dt}..{first.end_dt}], got "
                    f"[{result.start_dt}..{result.end_dt}]"
                )
            if result.initial_capital != first.initial_capital:
                raise ValueError(
                    f"results carry inconsistent initial_capital; expected "
                    f"{first.initial_capital}, got {result.initial_capital}"
                )
            if result.sharadar_bundle != first.sharadar_bundle:
                raise ValueError(
                    f"results carry inconsistent sharadar_bundle; expected "
                    f"{first.sharadar_bundle}, got {result.sharadar_bundle}"
                )

        return cls(
            parameter_name=parameter_name,
            parameter_values=parameter_values,
            per_parameter_equity=per_param_equity,
            per_parameter_final_pnl=per_param_pnl,
            central_value=central_value,
            confidence_tier=ConfidenceTier.SWEEP_SELECTED_NO_CORRECTION,
            tickers=first.tickers,
            start_dt=first.start_dt,
            end_dt=first.end_dt,
            initial_capital=first.initial_capital,
            sharadar_bundle=first.sharadar_bundle,
        )

    def equity_curve_at(self, parameter_value: Decimal) -> pl.DataFrame:
        """Return the equity curve for a specific parameter value.

        Raises KeyError if the parameter value is not in the band.
        """
        if parameter_value not in self.per_parameter_equity:
            raise KeyError(
                f"parameter_value {parameter_value} not in band; "
                f"available: {sorted(self.parameter_values)}"
            )
        return self.per_parameter_equity[parameter_value]

    def to_plot_frame(self) -> pl.DataFrame:
        """Return a long-form Polars frame for downstream plotting.

        Columns: parameter_value (Float64), dt (Date), nav (Float64).
        One row per (parameter_value, dt) pair across all parameter values.
        Sorted by (parameter_value ascending, dt ascending) for determinism.
        """
        frames: list[pl.DataFrame] = []
        for param_val in self.parameter_values:
            equity = self.per_parameter_equity[param_val]
            frame = equity.select(
                pl.lit(float(param_val)).alias("parameter_value"),
                pl.col("dt"),
                pl.col("nav"),
            )
            frames.append(frame)
        return pl.concat(frames).sort("parameter_value", "dt")

    def render_summary_line(self) -> str:
        """One-line summary for logs and PR descriptions.

        Format:
        sensitivity_band: parameter=eta, values=[0.05, 0.10, 0.142, 0.20, 0.30],
        central=0.142, central_pnl=$+12,345.67, range_pnl=[$+11,000.00, $+13,000.00],
        tickers=SPY, window=2005-01-04..2024-12-31, snapshot=sharadar_2026-05-29
        """
        values_str = ", ".join(str(v) for v in self.parameter_values)
        central_pnl = self.per_parameter_final_pnl[self.central_value]
        all_pnls = [self.per_parameter_final_pnl[v] for v in self.parameter_values]
        min_pnl = min(all_pnls)
        max_pnl = max(all_pnls)
        return (
            f"sensitivity_band: parameter={self.parameter_name}, "
            f"values=[{values_str}], "
            f"central={self.central_value}, "
            f"central_pnl=${central_pnl:+,.2f}, "
            f"range_pnl=[${min_pnl:+,.2f}, ${max_pnl:+,.2f}], "
            f"tickers={','.join(self.tickers)}, "
            f"window={self.start_dt}..{self.end_dt}, "
            f"snapshot={self.sharadar_bundle}"
        )

    def render_band_table(self) -> str:
        """Markdown table with one row per parameter value.

        Columns: parameter_value | final_pnl | delta_bps_vs_central
        delta_bps_vs_central is (final_pnl - central_final_pnl) /
        initial_capital * 10_000.
        """
        central_pnl = self.per_parameter_final_pnl[self.central_value]
        rows: list[str] = []
        rows.append(f"| {self.parameter_name} | final_pnl | delta_bps_vs_central |")
        rows.append("|---|---|---|")
        for param_val in self.parameter_values:
            pnl = self.per_parameter_final_pnl[param_val]
            delta_bps = (pnl - central_pnl) / self.initial_capital * 10_000.0
            rows.append(f"| {param_val} | ${pnl:+,.2f} | {delta_bps:+.2f} |")
        return "\n".join(rows)


def render_optional_plot(band: SensitivityBand, output_path: str) -> None:
    """Optional matplotlib plot of the sensitivity band.

    Matplotlib is NOT pinned in pyproject.toml; this helper raises
    ImportError with a message pointing at `pip install matplotlib`
    if matplotlib is not available. The plot is auxiliary; the band
    table is the canonical artifact per ADR 0002 M2 criterion 2.

    The plot emits one curve per parameter value. The central curve is
    drawn with a thicker line so the central estimate is visually
    distinguished from the band edges.
    """
    try:
        import matplotlib.pyplot as plt
    except ImportError as e:
        raise ImportError(
            "render_optional_plot requires matplotlib; install it with "
            "`pip install matplotlib`. The plot is auxiliary; the band "
            "table from render_band_table is the canonical artifact."
        ) from e

    fig, ax = plt.subplots(figsize=(10, 6))
    for param_val in band.parameter_values:
        equity = band.per_parameter_equity[param_val]
        dts = equity["dt"].to_list()
        navs = equity["nav"].to_list()
        is_central = param_val == band.central_value
        linewidth = 2.5 if is_central else 1.0
        label = f"{band.parameter_name}={param_val}"
        if is_central:
            label += " (central)"
        ax.plot(dts, navs, label=label, linewidth=linewidth)
    ax.set_xlabel("Date")
    ax.set_ylabel("NAV ($)")
    ax.set_title(
        f"Sensitivity band: {band.parameter_name} sweep "
        f"({band.tickers[0] if len(band.tickers) == 1 else ','.join(band.tickers)}) "
        f"{band.start_dt}..{band.end_dt}"
    )
    ax.legend(loc="best")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_path)
    plt.close(fig)
