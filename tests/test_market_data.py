from datetime import date, datetime

import numpy as np
import pandas as pd

import market_data
from market_data import DownloadResult, normalize_daily_frame, validate_download


def test_normalize_yfinance_multiindex_layout():
    dates = pd.to_datetime(["2026-09-02", "2026-09-03"])
    columns = pd.MultiIndex.from_tuples(
        [("Adj Close", "QQQ"), ("Volume", "QQQ")], names=["Price", "Ticker"]
    )
    raw = pd.DataFrame([[100.0, 1_000], [101.0, 1_100]], index=dates, columns=columns)
    normalized = normalize_daily_frame(raw, "QQQ")
    assert normalized.columns.tolist() == ["price", "volume"]
    assert normalized["price"].tolist() == [100.0, 101.0]
    assert normalized["volume"].tolist() == [1_000, 1_100]


def test_validate_removes_current_new_york_session_before_close(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 4, 10, 30, tzinfo=tz)

    monkeypatch.setattr(market_data, "datetime", FixedDatetime)
    dates = pd.bdate_range(end="2026-09-04", periods=202)
    frame = pd.DataFrame(
        {"price": np.linspace(100, 120, len(dates)), "volume": 1_000_000},
        index=dates,
    )
    result = DownloadResult(
        data={"QQQ": frame.copy(), "AAA": frame.copy()}, provider="test"
    )
    validated = validate_download(
        result,
        benchmark="QQQ",
        expected_tickers=["QQQ", "AAA"],
        min_required_rows=201,
        stale_after_calendar_days=5,
        as_of=date(2026, 9, 4),
        exclude_incomplete_us_session=True,
        market_close_grace_minutes=15,
    )
    assert validated.data["QQQ"].index.max().date() == date(2026, 9, 3)
    assert validated.data["AAA"].index.max().date() == date(2026, 9, 3)
    assert any("미완성 일봉" in warning for warning in validated.warnings)


def test_validate_records_silently_missing_expected_etf():
    dates = pd.bdate_range(end="2026-09-03", periods=201)
    frame = pd.DataFrame(
        {"price": np.linspace(100, 120, len(dates)), "volume": 1_000_000},
        index=dates,
    )
    result = DownloadResult(data={"QQQ": frame}, provider="test")
    validated = validate_download(
        result,
        benchmark="QQQ",
        expected_tickers=["QQQ", "MISSING"],
        min_required_rows=201,
        stale_after_calendar_days=30,
        as_of=date(2026, 9, 4),
        exclude_incomplete_us_session=False,
    )
    assert "MISSING" in validated.errors
    assert "반환하지 않았습니다" in validated.errors["MISSING"]
