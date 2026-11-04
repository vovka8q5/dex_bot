import pytest

from dexbot.sizing import size_order


def test_sizes_one_percent_of_equity():
    sized = size_order(
        equity=10_000,
        cash=10_000,
        entry=100,
        stop=95,
        fee_rate=0.001,
        max_risk_pct=0.01,
    )
    assert sized.ok
    assert sized.risk_quote == pytest.approx(100)
    assert sized.qty_quote == pytest.approx(2000)
    assert sized.qty_base == pytest.approx(20)


def test_cash_cap_does_not_increase_risk():
    sized = size_order(
        equity=10_000,
        cash=500,
        entry=100,
        stop=95,
        fee_rate=0.001,
        max_risk_pct=0.01,
    )
    assert sized.ok
    assert sized.qty_quote < 500
    assert sized.risk_quote < 100
    assert sized.qty_quote * 0.001 + sized.qty_quote <= 500 + 1e-6


def test_invalid_stop_and_empty_cash():
    assert not size_order(
        equity=10_000, cash=10_000, entry=100, stop=100, fee_rate=0.001, max_risk_pct=0.01
    ).ok
    assert not size_order(
        equity=10_000, cash=10_000, entry=100, stop=110, fee_rate=0.001, max_risk_pct=0.01
    ).ok
    assert size_order(
        equity=10_000, cash=0, entry=100, stop=95, fee_rate=0.001, max_risk_pct=0.01
    ).reason == "no_cash"
