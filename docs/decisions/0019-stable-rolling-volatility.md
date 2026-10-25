# ADR 0019: Center rolling returns before computing volatility

## Context

ADR 0005 final locked decision 8 and reviewer-response item 14 chose NumPy
convolution for rolling market-state statistics. Subtracting `mean(r)^2` from
`mean(r^2)` loses precision for constant
or nearly constant returns. Main CI run 40212165773 exposed a nonzero standard
deviation for constant returns; deterministic local regressions also show small
legitimate dispersion being rounded to zero. Clipping or relaxing the zero
tolerance would leave that second failure unresolved.

## Decision

Supersede only the daily-volatility convolution arithmetic in ADR 0005 final
locked decision 8 and reviewer-response item 14. Use pinned NumPy's centered
population standard deviation (`ddof=0`) over
zero-copy sliding windows. Process windows in batches so the centered-value
temporary holds at most 1,048,576 float64 values, or one full window if that
window alone exceeds the budget. Output and per-row reduction arrays are extra.

Keep NumPy 1.26.4, ADV convolution, input validation, population normalization
and the existing valid-window/lookahead-safe alignment unchanged. Do not clip
small positive volatility or change test tolerances to conceal cancellation.

## Consequences

Work remains O(T × window); centered reduction has a higher constant cost than
the two convolutions. Scratch does not grow as an unbounded T × window matrix.
Tests cover analytic constant/tiny-dispersion cases, independent population
references, window one/full-length/non-contiguous inputs and input preservation.
This numerical correction does not rebuild or revalidate any vendor study,
committed study artifact or financial result.
