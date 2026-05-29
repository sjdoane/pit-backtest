"""Dormancy contract tests for Order.estimate_bps_at_submit (M2 PR C2).

Per ADR 0011 lock #1 and #7 the `Order.estimate_bps_at_submit` attribute
is a dormancy tripwire at M2. Reading it raises NotImplementedError
with a diagnostic pointing at ADR 0011 so a future M3 contributor must
deliberately delete the stub before populating the attribute.

Per ADR 0011 lock #7 the acceptance contract is dormancy (NotImplementedError
raised with documented message), NOT formula correctness. The formula's
symbolic exercise lives in `tests/integration/test_cost_estimate_vs_fill_tolerance.py`
(shipped in M2 PR B); that file is unchanged.

The @pytest.mark.dormant_until_m3 marker is registered in pyproject.toml
per ADR 0011 lock #7. The marker is reportable (`pytest -m dormant_until_m3`
lists every dormant contract) so M3 contributors must reckon with it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from pit_backtest.data.records import AssetId
from pit_backtest.execution.orders import FillPriceModel, Order


def _make_order() -> Order:
    return Order(
        order_id="o-001",
        asset_id=AssetId(0),
        quantity=Decimal("100"),
        fill_price_model=FillPriceModel.CLOSE,
        submit_dt=datetime(2024, 1, 2, 16, 0, 0),
    )


@pytest.mark.dormant_until_m3
def test_order_estimate_bps_at_submit_raises_not_implemented_per_adr_0011() -> None:
    """Per ADR 0011 lock #1 reading `Order.estimate_bps_at_submit` raises
    NotImplementedError with a diagnostic naming ADR 0011 and the M3
    activation gate (distinct policy-time vs matcher-time MarketStateLookup
    snapshots).

    Marked `dormant_until_m3` so M3 contributors who run
    `pytest -m dormant_until_m3` see exactly what they need to address
    before activating the tolerance contract.
    """
    order = _make_order()
    with pytest.raises(NotImplementedError, match="ADR 0011"):
        _ = order.estimate_bps_at_submit


@pytest.mark.dormant_until_m3
def test_order_estimate_bps_at_submit_diagnostic_names_activation_gate() -> None:
    """Per ADR 0011 lock #6 the activation gate is "distinct policy-time
    vs matcher-time MarketStateLookup snapshots", NOT `epsilon_bps > 0`.
    The verifier-corrected gate prevents a future ADR from flipping
    dormancy on a milestone that does not actually mid-sensitize the
    cost model. The exception message names this gate explicitly so a
    future M3 contributor cannot misread the dormancy as activatable
    by adding a spread proxy.
    """
    order = _make_order()
    try:
        _ = order.estimate_bps_at_submit
    except NotImplementedError as e:
        msg = str(e)
        assert "policy-time" in msg
        assert "matcher-time" in msg
        assert "MarketStateLookup" in msg
        assert "mid-insensitive" in msg
    else:
        pytest.fail(
            "Order.estimate_bps_at_submit should raise NotImplementedError "
            "per ADR 0011 dormancy contract"
        )


@pytest.mark.dormant_until_m3
def test_order_does_not_grow_mid_at_estimate_field_per_adr_0011_lock_2() -> None:
    """Per ADR 0011 lock #2 `Order.mid_at_estimate` is NOT added. The
    methodology doc's tolerance formula presupposes a mid the cost
    model does not consume; surfacing a Decimal field for an unused
    quantity would invite future contributors to populate it
    incorrectly.

    This test locks the negative-space invariant: the Order class
    surface does NOT include `mid_at_estimate` at M2. A future ADR
    that adds it must be deliberate.
    """
    order = _make_order()
    assert not hasattr(order, "mid_at_estimate")
    # The class itself also does not declare the attribute.
    assert "mid_at_estimate" not in Order.__slots__


@pytest.mark.dormant_until_m3
def test_no_cost_estimate_vs_fill_mismatch_error_class_per_adr_0011_lock_3() -> None:
    """Per ADR 0011 lock #3 `CostEstimateVsFillMismatchError` is NOT
    added at M2. The MatchingError hierarchy from PR B
    (UnsupportedFillPriceModelError, MultipleFillsPerBarError) is
    unchanged.

    This test locks the negative-space invariant by attempting an
    import; ImportError confirms the class is not in the matching
    module.
    """
    from pit_backtest.execution import matching

    assert not hasattr(matching, "CostEstimateVsFillMismatchError"), (
        "Per ADR 0011 lock #3 CostEstimateVsFillMismatchError must NOT "
        "exist at M2; if it has been added, ADR 0011 must be superseded"
    )


@pytest.mark.dormant_until_m3
def test_dormancy_marker_is_registered_in_pyproject_toml() -> None:
    """Per ADR 0011 lock #7 the @pytest.mark.dormant_until_m3 marker
    must be registered in `pyproject.toml` markers section so a
    future contributor running `pytest -m dormant_until_m3` lists
    every dormant contract.
    """
    import tomllib
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    pyproject_path = repo_root / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        pyproject = tomllib.load(f)

    markers = pyproject["tool"]["pytest"]["ini_options"]["markers"]
    dormant_marker_lines = [m for m in markers if m.startswith("dormant_until_m3")]
    assert len(dormant_marker_lines) == 1, (
        f"expected exactly one dormant_until_m3 marker registration in "
        f"pyproject.toml; found {len(dormant_marker_lines)}"
    )
    assert "ADR 0011" in dormant_marker_lines[0]


@pytest.mark.dormant_until_m3
def test_hasattr_propagates_not_implemented_per_post_impl_reviewer_finding_1() -> None:
    """Per post-impl reviewer Finding 1: `hasattr` propagates the
    NotImplementedError instead of returning True/False. This is the
    documented asymmetric contract; a future M3 contributor probing
    "is the field present" gets a CRASH (with the activation-gate
    diagnostic) rather than a silent False that would let them treat
    dormancy as absence.

    Callers that need a presence check should use
    `"estimate_bps_at_submit" in dir(type(order))` (which checks the
    class descriptor without invoking __get__) or catch
    NotImplementedError explicitly. This test locks the asymmetric
    contract so a future "fix" that converts the property to return
    None or NotImplemented (which hasattr would treat as True) is
    deliberately superseding ADR 0011.
    """
    order = _make_order()
    with pytest.raises(NotImplementedError, match="ADR 0011"):
        hasattr(order, "estimate_bps_at_submit")
    # The class-level descriptor IS discoverable via `dir`; the property
    # name appears in the type's namespace even though instance reads
    # raise.
    assert "estimate_bps_at_submit" in dir(type(order))


def test_adr_0011_file_exists() -> None:
    """Per ADR 0011 lock #7 the dormancy contract requires the ADR file
    itself to exist (and to be referenced from `Order.estimate_bps_at_submit`'s
    docstring + the NotImplementedError message + the README design
    pillar line). This test asserts the ADR is committed.
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    adr_path = repo_root / "docs" / "decisions" / "0011-tolerance-contract-dormancy-at-m2.md"
    assert adr_path.exists(), (
        f"ADR 0011 not found at {adr_path}; the dormancy contract requires "
        f"the ADR file to be committed in the same PR"
    )


def test_readme_design_pillar_names_adr_0011_dormancy() -> None:
    """Per ADR 0011 lock #9 (Growth's binding caveat) the README design
    pillars block must contain a line naming ADR 0011 and the dormancy
    state. Without this line a 10-minute recruiter cannot see the
    judgment Sam exercised in choosing dormancy over false enforcement.

    This test enforces the load-bearing precondition.
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    readme_path = repo_root / "README.md"
    readme_text = readme_path.read_text(encoding="utf-8")
    assert "ADR 0011" in readme_text, (
        "README.md must reference ADR 0011 per the binding precondition "
        "from the M2 PR C2 council Growth caveat; without this the "
        "dormancy collapses to a kill"
    )
    assert "dormant" in readme_text.lower(), (
        "README.md must use the word 'dormant' to surface the M2 design "
        "decision; per ADR 0011 the dormancy is intentional and must be "
        "discoverable from the first README screen"
    )


def test_methodology_doc_names_dormancy_per_adr_0011_lock_8() -> None:
    """Per ADR 0011 lock #8 `docs/methodology/cost_model_tolerance.md`
    must contain a "Dormancy at M2 (per ADR 0011)" section naming the
    Almgren-formula dimensional argument and the correct activation
    gate.
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    methodology_path = repo_root / "docs" / "methodology" / "cost_model_tolerance.md"
    text = methodology_path.read_text(encoding="utf-8")
    assert "Dormancy at M2" in text, (
        "cost_model_tolerance.md must contain a 'Dormancy at M2' section "
        "per ADR 0011 lock #8"
    )
    assert "ADR 0011" in text
    assert "mid-insensitive" in text
