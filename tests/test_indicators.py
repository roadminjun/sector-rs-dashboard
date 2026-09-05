import numpy as np
import pandas as pd
import pytest

from indicators import build_indicator_history, calculate_rsi


CONFIG = {
    "return_periods": [5, 20, 60],
    "moving_average_periods": [20, 50, 200],
    "rs_high_periods": [20, 60],
    "rs_moving_average_period": 20,
    "volume_average_period": 20,
    "rsi_period": 14,
    "volatility_period": 20,
    "volatility_annualization_days": 252,
}


def frames(length=260):
    dates = pd.bdate_range("2025-01-02", periods=length)
    benchmark_price = 100 * np.power(1.001, np.arange(length))
    etf_price = 80 * np.power(1.0015, np.arange(length))
    benchmark = pd.DataFrame(
        {"price": benchmark_price, "volume": np.full(length, 1_000_000)}, index=dates
    )
    etf = pd.DataFrame(
        {"price": etf_price, "volume": np.arange(length) + 1_000_000}, index=dates
    )
    return etf, benchmark


def test_returns_relative_returns_and_rs_change_are_separate():
    etf, benchmark = frames()
    history = build_indicator_history(etf, benchmark, CONFIG)
    last = history.iloc[-1]
    expected_etf = etf["price"].iloc[-1] / etf["price"].iloc[-21] - 1
    expected_benchmark = benchmark["price"].iloc[-1] / benchmark["price"].iloc[-21] - 1
    expected_relative = expected_etf - expected_benchmark
    expected_rs_change = (
        (etf["price"].iloc[-1] / benchmark["price"].iloc[-1])
        / (etf["price"].iloc[-21] / benchmark["price"].iloc[-21])
        - 1
    )
    assert last["return_20d"] == pytest.approx(expected_etf)
    assert last["relative_return_20d"] == pytest.approx(expected_relative)
    assert last["rs_change_20d"] == pytest.approx(expected_rs_change)
    assert last["relative_return_20d"] != pytest.approx(last["rs_change_20d"])


def test_high_moving_averages_volume_rsi_and_outperform_counts():
    etf, benchmark = frames()
    history = build_indicator_history(etf, benchmark, CONFIG)
    last = history.iloc[-1]
    assert bool(last["rs_high_20d"])
    assert bool(last["rs_high_60d"])
    assert last["ma_20"] == pytest.approx(etf["price"].iloc[-20:].mean())
    assert last["volume_ratio"] == pytest.approx(
        etf["volume"].iloc[-1] / etf["volume"].iloc[-20:].mean()
    )
    assert last["outperform_days_5d"] == 5
    assert last["underperform_days_5d"] == 0
    assert last["rsi_14"] == pytest.approx(100.0)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (np.arange(1.0, 31.0), 100.0),
        (np.arange(30.0, 0.0, -1.0), 0.0),
        (np.full(30, 10.0), 50.0),
    ],
)
def test_wilder_rsi_edge_cases(values, expected):
    rsi = calculate_rsi(pd.Series(values), 14)
    assert rsi.iloc[-1] == pytest.approx(expected)


def test_equal_daily_returns_are_neither_outperformance_nor_underperformance():
    etf, benchmark = frames()
    etf["price"] = benchmark["price"] * 0.8
    history = build_indicator_history(etf, benchmark, CONFIG)
    assert history["outperform_days_5d"].iloc[-1] == 0
    assert history["underperform_days_5d"].iloc[-1] == 0


def test_configurable_auxiliary_periods_keep_canonical_columns():
    etf, benchmark = frames()
    config = {
        **CONFIG,
        "rsi_period": 10,
        "volume_average_period": 15,
        "volatility_period": 12,
    }
    history = build_indicator_history(etf, benchmark, config)
    assert history["rsi"].equals(history["rsi_10"])
    assert history["volume_ma"].equals(history["volume_ma_15"])
    assert history["volatility"].equals(history["volatility_12d"])
