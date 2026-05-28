"""User-facing CLI config; Pydantic per the boundary contract.

See docs/methodology/pydantic_polars_boundary.md for the rationale on
using Pydantic at the CLI surface.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BacktestConfig(BaseModel):
    """Backtest configuration parsed from CLI flags or a YAML file.

    Parsed once at engine start; the config object is then read repeatedly
    in the inner loop without re-validation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    start_dt: date
    end_dt: date
    universe_id: str
    snapshot_bundle: str  # e.g., "sharadar_2026-05-28"
    impact_model: Literal[
        "square_root", "linear", "fixed_bps", "no_impact", "bouchaud"
    ] = "square_root"
    eta: Decimal = Field(default=Decimal("0.142"), ge=Decimal("0.0"))
    seed: int = 0
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
