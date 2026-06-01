"""Tests for policy.top_quintile.TopQuintileLongPolicy (M5; ADR 0002 dec 20)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import attrs

from pit_backtest.data.records import AssetId
from pit_backtest.policy.top_quintile import TopQuintileLongPolicy

_RB = date(2010, 1, 29)
_DT = datetime(2010, 1, 29, 16, 0)


class _DummyCost:
    """Unused by the equal-weight target_positions body; present for the protocol."""

    def estimate(self, asset_id, shares, direction, dt):  # noqa: ANN001
        return Decimal("0")


def _run(
    scores: dict[AssetId, float],
    prices: dict[AssetId, float],
    *,
    cash: float = 10_000.0,
    positions: dict[AssetId, float] | None = None,
    rebalance: bool = True,
):
    rebalance_dates = frozenset({_RB}) if rebalance else frozenset()
    policy = TopQuintileLongPolicy(
        rebalance_dates=rebalance_dates,
        price_lookup=lambda aid, when: prices.get(aid),
    )
    portfolio = SimpleNamespace(cash=cash, positions=positions or {})
    return policy.target_positions(scores, portfolio, _DummyCost(), _DT)


def _scores(n: int) -> dict[AssetId, float]:
    # Distinct descending scores: AssetId i gets score (n - i) / n.
    return {AssetId(i): (n - i) / n for i in range(1, n + 1)}


def _prices(n: int, price: float = 100.0) -> dict[AssetId, float]:
    return {AssetId(i): price for i in range(1, n + 1)}


def test_quintile_cut_is_ceil_n_over_5() -> None:
    # n=10 -> ceil(10/5)=2; n=11 -> 3; n=5 -> 1.
    assert len(_run(_scores(10), _prices(10)).targets) == 2
    assert len(_run(_scores(11), _prices(11)).targets) == 3
    assert len(_run(_scores(5), _prices(5)).targets) == 1


def test_tiny_universe_longs_single_best() -> None:
    result = _run(_scores(3), _prices(3))
    # ceil(3/5) = 1: only the highest-scoring name (AssetId 1, score 2/3).
    assert list(result.targets.keys()) == [AssetId(1)]


def test_selects_the_top_scorers() -> None:
    result = _run(_scores(10), _prices(10))
    # Top 2 by score are AssetId 1 (0.9) and AssetId 2 (0.8).
    assert set(result.targets.keys()) == {AssetId(1), AssetId(2)}


def test_ties_resolved_by_asset_id_deterministic() -> None:
    # cut=2; AssetId 1 highest; AssetId 2 and 8 tie for 2nd at exactly 0.5.
    scores = {AssetId(i): 0.1 for i in range(1, 11)}
    scores[AssetId(1)] = 0.9
    scores[AssetId(2)] = 0.5
    scores[AssetId(8)] = 0.5
    prices = _prices(10)
    result1 = _run(scores, prices)
    result2 = _run(scores, prices)
    # Tie-break by AssetId ascending: AssetId 2 is selected, AssetId 8 is not.
    assert set(result1.targets.keys()) == {AssetId(1), AssetId(2)}
    assert AssetId(8) not in result1.targets
    assert result1.targets == result2.targets  # deterministic


def test_empty_signal_returns_empty_targets() -> None:
    assert _run({}, {}).targets == {}


def test_non_rebalance_date_returns_empty_targets() -> None:
    assert _run(_scores(10), _prices(10), rebalance=False).targets == {}


def test_weights_renormalize_to_equal_dollars_over_priced_selected() -> None:
    # cut=2 over NAV=10000 (all cash, no positions) -> 5000 each.
    result = _run(_scores(10), _prices(10), cash=10_000.0)
    assert result.targets == {
        AssetId(1): Decimal(repr(5000.0)),
        AssetId(2): Decimal(repr(5000.0)),
    }


def test_selected_but_unpriced_name_drops_and_other_takes_full_book() -> None:
    # cut=2 selects AssetId 1 + 2, but AssetId 1 has no price; AssetId 2 gets all.
    prices = _prices(10)
    del prices[AssetId(1)]
    result = _run(_scores(10), prices, cash=10_000.0)
    assert result.targets == {AssetId(2): Decimal(repr(10_000.0))}


def test_all_negative_scores_still_longs_top_cut_equal_weight() -> None:
    # All-negative momentum: longs the least-negative top quintile, equal-weight,
    # with strictly positive AND equal dollar targets (the equal-weight-not-
    # score-weight decision; momentum scores can be negative).
    scores = {AssetId(i): -0.1 * i for i in range(1, 11)}  # -0.1 .. -1.0
    result = _run(scores, _prices(10), cash=10_000.0)
    # cut=2: least-negative are AssetId 1 (-0.1) and AssetId 2 (-0.2).
    assert set(result.targets.keys()) == {AssetId(1), AssetId(2)}
    values = list(result.targets.values())
    assert all(v > Decimal("0") for v in values)  # long-only, no negatives
    assert values[0] == values[1]  # equal-weight, not score-weight
    assert values[0] == Decimal(repr(5000.0))


def test_long_only_no_negative_targets() -> None:
    scores = {AssetId(i): (5 - i) for i in range(1, 11)}  # mix of pos and neg
    result = _run(scores, _prices(10), cash=10_000.0)
    assert all(v >= Decimal("0") for v in result.targets.values())


def test_top_quintile_policy_is_attrs_frozen() -> None:
    assert attrs.has(TopQuintileLongPolicy)
    policy = TopQuintileLongPolicy(
        rebalance_dates=frozenset({_RB}), price_lookup=lambda aid, when: None
    )
    import pytest

    with pytest.raises(attrs.exceptions.FrozenInstanceError):
        policy.rebalance_dates = frozenset()  # type: ignore[misc]
