"""Transparent daily indicators for sector relative-strength analysis."""

from __future__ import annotations

from typing import Mapping

import numpy as np
import pandas as pd


class IndicatorError(ValueError):
    """Raised when aligned data is insufficient for requested indicators."""


def calculate_rsi(price: pd.Series, period: int = 14) -> pd.Series:
    """Calculate Wilder RSI, returning 50 when both average gain/loss are zero."""

    if period <= 0:
        raise IndicatorError("RSI 기간은 양수여야 합니다.")
    values = price.astype(float).to_numpy()
    result = np.full(len(values), np.nan, dtype=float)
    if len(values) <= period:
        return pd.Series(result, index=price.index, name="rsi")

    delta = np.diff(values)
    gains = np.clip(delta, 0.0, None)
    losses = np.clip(-delta, 0.0, None)
    average_gain = float(np.mean(gains[:period]))
    average_loss = float(np.mean(losses[:period]))

    def rsi_value(avg_gain: float, avg_loss: float) -> float:
        if avg_gain == 0.0 and avg_loss == 0.0:
            return 50.0
        if avg_loss == 0.0:
            return 100.0
        if avg_gain == 0.0:
            return 0.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    result[period] = rsi_value(average_gain, average_loss)
    for index in range(period + 1, len(values)):
        average_gain = ((average_gain * (period - 1)) + gains[index - 1]) / period
        average_loss = ((average_loss * (period - 1)) + losses[index - 1]) / period
        result[index] = rsi_value(average_gain, average_loss)
    return pd.Series(result, index=price.index, name="rsi")


def _rolling_high(series: pd.Series, period: int) -> pd.Series:
    rolling_max = series.rolling(period, min_periods=period).max()
    close_to_high = np.isclose(
        series.to_numpy(dtype=float),
        rolling_max.to_numpy(dtype=float),
        rtol=1e-10,
        atol=1e-12,
        equal_nan=False,
    )
    return pd.Series(close_to_high, index=series.index, dtype=bool)


def build_indicator_history(
    etf_data: pd.DataFrame,
    benchmark_data: pd.DataFrame,
    indicator_config: Mapping,
) -> pd.DataFrame:
    """Calculate a fully aligned indicator history for one ETF.

    Relative return is ETF return minus benchmark return (percentage-point
    difference). RS-ratio change is kept separately because the configuration
    explicitly requests both measures.
    """

    return_periods = [int(value) for value in indicator_config["return_periods"]]
    ma_periods = [int(value) for value in indicator_config["moving_average_periods"]]
    rs_high_periods = [int(value) for value in indicator_config["rs_high_periods"]]
    required_returns = {5, 20, 60}
    required_mas = {20, 50, 200}
    if not required_returns.issubset(return_periods):
        raise IndicatorError("return_periods에는 5, 20, 60이 모두 필요합니다.")
    if not required_mas.issubset(ma_periods):
        raise IndicatorError("moving_average_periods에는 20, 50, 200이 모두 필요합니다.")
    if not {20, 60}.issubset(rs_high_periods):
        raise IndicatorError("rs_high_periods에는 20, 60이 모두 필요합니다.")

    combined = pd.concat(
        [
            etf_data["price"].rename("price"),
            etf_data["volume"].rename("volume"),
            benchmark_data["price"].rename("benchmark_price"),
        ],
        axis=1,
        join="inner",
    ).sort_index()
    combined = combined.dropna(subset=["price", "benchmark_price"])
    minimum = max(max(ma_periods), max(return_periods), max(rs_high_periods)) + 1
    if len(combined) < minimum:
        raise IndicatorError(
            f"정렬 후 데이터가 {len(combined)}행뿐입니다. 최소 {minimum}행이 필요합니다."
        )

    history = combined.copy()
    history["rs_ratio"] = history["price"] / history["benchmark_price"]

    for period in return_periods:
        etf_return = history["price"].pct_change(periods=period, fill_method=None)
        benchmark_return = history["benchmark_price"].pct_change(
            periods=period, fill_method=None
        )
        history[f"return_{period}d"] = etf_return
        history[f"benchmark_return_{period}d"] = benchmark_return
        history[f"relative_return_{period}d"] = etf_return - benchmark_return
        history[f"rs_change_{period}d"] = history["rs_ratio"].pct_change(
            periods=period, fill_method=None
        )

    for period in rs_high_periods:
        history[f"rs_high_{period}d"] = _rolling_high(history["rs_ratio"], period)

    for period in ma_periods:
        history[f"ma_{period}"] = history["price"].rolling(
            period, min_periods=period
        ).mean()
        history[f"above_ma_{period}"] = history["price"] > history[f"ma_{period}"]

    history["ma_bullish_alignment"] = (
        (history["ma_20"] > history["ma_50"])
        & (history["ma_50"] > history["ma_200"])
    )

    rs_ma_period = int(indicator_config.get("rs_moving_average_period", 20))
    history[f"rs_ma_{rs_ma_period}"] = history["rs_ratio"].rolling(
        rs_ma_period, min_periods=rs_ma_period
    ).mean()
    # The signal rules use the 20-day RS moving average explicitly.
    if rs_ma_period != 20:
        history["rs_ma_20"] = history["rs_ratio"].rolling(20, min_periods=20).mean()

    volume_period = int(indicator_config.get("volume_average_period", 20))
    history[f"volume_ma_{volume_period}"] = history["volume"].rolling(
        volume_period, min_periods=volume_period
    ).mean()
    history["volume_ma"] = history[f"volume_ma_{volume_period}"]
    valid_volume_average = history[f"volume_ma_{volume_period}"].replace(0.0, np.nan)
    history["volume_ratio"] = history["volume"] / valid_volume_average

    etf_daily_return = history["price"].pct_change(fill_method=None)
    benchmark_daily_return = history["benchmark_price"].pct_change(fill_method=None)
    daily_relative_return = etf_daily_return - benchmark_daily_return
    # Treat machine-precision noise as a tie instead of a directional day.
    comparison_tolerance = 1e-12
    history["outperformed_benchmark"] = pd.Series(
        np.where(
            daily_relative_return.isna(),
            np.nan,
            (daily_relative_return > comparison_tolerance).astype(float),
        ),
        index=history.index,
        dtype=float,
    )
    history["underperformed_benchmark"] = pd.Series(
        np.where(
            daily_relative_return.isna(),
            np.nan,
            (daily_relative_return < -comparison_tolerance).astype(float),
        ),
        index=history.index,
        dtype=float,
    )
    history["outperform_days_5d"] = (
        history["outperformed_benchmark"].rolling(5, min_periods=5).sum()
    )
    history["underperform_days_5d"] = (
        history["underperformed_benchmark"].rolling(5, min_periods=5).sum()
    )

    rsi_period = int(indicator_config.get("rsi_period", 14))
    history["rsi"] = calculate_rsi(history["price"], rsi_period)
    history[f"rsi_{rsi_period}"] = history["rsi"]

    volatility_period = int(indicator_config.get("volatility_period", 20))
    annualization_days = int(indicator_config.get("volatility_annualization_days", 252))
    history["volatility"] = (
        etf_daily_return.rolling(volatility_period, min_periods=volatility_period).std()
        * np.sqrt(annualization_days)
    )
    history[f"volatility_{volatility_period}d"] = history["volatility"]

    # Inputs that make transition/weakening rules reproducible from one snapshot.
    history["relative_return_20d_prev"] = history["relative_return_20d"].shift(1)
    history["relative_return_60d_prev"] = history["relative_return_60d"].shift(1)
    strong_leader_history = (
        (history["relative_return_20d"] > 0)
        & (history["relative_return_60d"] > 0)
        & history["rs_high_20d"]
        & history["above_ma_20"]
        & history["above_ma_50"]
    )
    history["previously_strong"] = (
        strong_leader_history.shift(1).rolling(20, min_periods=1).max().fillna(0).astype(bool)
    )
    history["leadership_participant"] = (
        (history["relative_return_20d"] > 0)
        & (history["rs_ratio"] > history["rs_ma_20"])
        & history["above_ma_50"]
    )
    return history


def calculate_all_indicators(
    market_data: Mapping[str, pd.DataFrame],
    benchmark: str,
    etfs: Mapping[str, str],
    indicator_config: Mapping,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame], dict[str, str]]:
    """Return latest rows, full histories, and per-ETF calculation errors."""

    benchmark = benchmark.upper()
    benchmark_data = market_data[benchmark]
    rows: list[dict] = []
    histories: dict[str, pd.DataFrame] = {}
    errors: dict[str, str] = {}

    for raw_ticker, sector_name in etfs.items():
        ticker = raw_ticker.upper()
        if ticker not in market_data:
            continue
        try:
            history = build_indicator_history(
                market_data[ticker], benchmark_data, indicator_config
            )
            latest = history.iloc[-1].to_dict()
            latest.update(
                {
                    "date": history.index[-1].date().isoformat(),
                    "etf": ticker,
                    "sector_name": str(sector_name),
                    "benchmark": benchmark,
                    "rsi_period": int(indicator_config.get("rsi_period", 14)),
                    "volume_average_period": int(
                        indicator_config.get("volume_average_period", 20)
                    ),
                    "volatility_period": int(
                        indicator_config.get("volatility_period", 20)
                    ),
                }
            )
            rows.append(latest)
            histories[ticker] = history
        except Exception as exc:
            errors[ticker] = str(exc)

    if not rows:
        raise IndicatorError("계산 가능한 감시 ETF가 없습니다.")
    return pd.DataFrame(rows), histories, errors
