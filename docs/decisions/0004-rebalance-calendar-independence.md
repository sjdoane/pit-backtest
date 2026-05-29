# ADR 0004: Rebalance calendar independence from backtest window

Status: Accepted.
Date: 2026-05-28.
Authors: Sam Doane (with skeptical-reviewer pass during M1 day 3 planning).

## Context

The M1 constant-weight monthly rebalance demo (ADR 0002 acceptance criterion 2) requires a rebalance-date convention. The initial design forced `start_dt` to be a rebalance date so the equity curve had a well-defined initial position. The skeptical-reviewer pass during M1 day 3 planning rejected this rule. This ADR captures the corrected decision and locks it for all v1 strategies that have a rebalance cadence.

## The reviewer's objection (verbatim)

> "Real practitioners run their backtests with the rebalance calendar fixed by fund policy (e.g., last NYSE day of month), and the backtest-window start_dt is independent. Forcing start_dt as a rebalance creates exactly the asymmetry the author asks about: a backtest starting 2010-01-04 has its first rebalance on 2010-01-04, but the same fund's true policy would have its first rebalance on 2010-01-29 (last NYSE day of January). The 2010-01-04 backtest is now reporting return on a different position trajectory than the 2010-01-29 backtest, and the difference is not the 'strategy edge', it is calendar artifact."

## Decision

The rebalance calendar for any v1 strategy is determined by fund policy (a deterministic rule over the NYSE trading calendar) and is independent of the backtest window's `start_dt` and `end_dt`.

Concretely, for the M1 constant-weight monthly demo:

- Rebalance dates are the last NYSE trading day of each calendar month, over a window wider than the backtest. The calendar is computed by `monthly_last_trading_day(trading_days)` and trimmed to `[start_dt, end_dt]` at consumption time.
- `start_dt` is not modified. The engine initializes the portfolio with `cash = initial_capital` and `positions = {}` on `start_dt`.
- The first rebalance executes on the first policy-determined date `d >= start_dt`. If `start_dt` happens to coincide with a policy-determined date (e.g., the last NYSE day of a month), the first rebalance is on `start_dt`; this is a calendar coincidence, not a forcing.
- Between `start_dt` and the first rebalance, the equity curve shows `nav = cash = initial_capital` (flat). The cash period reflects the real-world mechanics of a fund launching with cash on a non-rebalance day and waiting for the next scheduled rebalance.

The convention applies to every v1 strategy that has a periodic rebalance: constant-weight (M1), JT1993 momentum (M5), and any other v1+ strategy that defines a periodic rule.

## What this decision rules out

- **No "force first day to be a rebalance" rule** anywhere in the engine. The first rebalance is always the first policy date in `[start_dt, end_dt]`.
- **No "force last day to be a rebalance" rule**. The final NAV mark uses the close of `end_dt`, which may or may not coincide with a rebalance.
- **No window-dependent rebalance calendar**. Two backtests with overlapping windows produce identical rebalance dates on the overlap; the only difference is which subset of dates each consumes.

## What this decision allows

- Different fund-policy rules per strategy. The momentum study in M5 may use a different rule (e.g., last-trading-day-quarterly); each strategy's policy carries its own calendar generator.
- Run-time customization of the rule via the Policy's constructor argument. The Policy receives the pre-computed tuple of rebalance dates and treats it as opaque.

## Comparability invariant

Two backtests of the same strategy over windows `[a, b]` and `[c, d]` (with the same strategy parameters, same data snapshot, same initial capital) produce identical NAV and position trajectories on the overlap `[max(a, c), min(b, d)]`, conditional on the same initial-position state at `max(a, c)`. (The conditional is required because a backtest's history matters: a backtest starting earlier holds positions on `max(a, c)` while one starting later holds cash; comparability is on rebalance trajectory, not on absolute NAV.)

## Implementation requirements

- `engine/calendar.py` (or a similar location) exposes `monthly_last_trading_day(trading_days: tuple[date, ...]) -> tuple[date, ...]`. The function takes a complete trading-day calendar and returns the last NYSE trading day of each calendar month observed in the input. The result is sorted ascending and immutable. Tests verify the function on edge cases: month-end falling on a holiday (Good Friday in March), trading-day-only-once-in-month (rare; first/last bar of a window), and the empty input.
- The Policy receives the rebalance-date tuple as a `frozenset[date]` (for O(1) membership lookup in the per-bar `target_positions` check); the engine constructs the tuple once at `Backtest.__init__` and shares it with both the Policy and any reference function or test fixture.
- The `Backtest` constructor accepts `start_dt`, `end_dt`, `initial_capital`, `tickers`, and the strategy's `Policy` instance. It computes the rebalance tuple via the policy's helper, validates `start_dt <= end_dt`, and constructs the `TestClock`, `SharadarDataSource`, and the `BarLoop`.
- The `BarLoop` initializes `PortfolioState(cash=initial_capital, positions={}, initial_capital=initial_capital, realized_pnl=0.0)` on `start_dt`. On each subsequent bar, it (a) credits any dividends due, (b) marks-to-market, (c) calls `Policy.target_positions`, (d) constructs and submits any orders via the `MatchingEngine`, (e) applies fills, (f) records a snapshot.

## Status

This ADR is in **Accepted** status. It binds the M1 constant-weight demo, the M5 momentum study, and any v1 strategy with a rebalance cadence. It supersedes the implicit "force start_dt as rebalance" rule in the M1 day 3 plan.
