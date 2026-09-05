"""Daily market-data providers and validation helpers.

The analysis layer only depends on ``MarketDataProvider``.  Replacing Yahoo
Finance later therefore does not require changes to indicators or signals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
import time
from typing import Protocol, Sequence
from zoneinfo import ZoneInfo

import pandas as pd


REQUIRED_COLUMNS = ("price", "volume")


class MarketDataError(RuntimeError):
    """Raised when required market data cannot be obtained or validated."""


class MarketDataProvider(Protocol):
    """Interface implemented by swappable daily-data providers."""

    name: str

    def download(
        self,
        tickers: Sequence[str],
        start: date,
        end: date,
    ) -> "DownloadResult":
        """Download daily data. ``end`` is inclusive at this interface."""


@dataclass
class DownloadResult:
    data: dict[str, pd.DataFrame] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    provider: str = "unknown"


def _flatten_yfinance_columns(frame: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Normalize the different single-ticker column layouts from yfinance."""

    result = frame.copy()
    if not isinstance(result.columns, pd.MultiIndex):
        return result

    # Newer yfinance versions commonly return (field, ticker).
    for level in range(result.columns.nlevels):
        values = result.columns.get_level_values(level).astype(str)
        matches = [value.upper() == ticker.upper() for value in values]
        if any(matches):
            try:
                result = result.xs(ticker, axis=1, level=level, drop_level=True)
            except KeyError:
                matching_label = values[matches.index(True)]
                result = result.xs(matching_label, axis=1, level=level, drop_level=True)
            break

    while isinstance(result.columns, pd.MultiIndex) and result.columns.nlevels > 1:
        single_levels = [
            level
            for level in range(result.columns.nlevels)
            if result.columns.get_level_values(level).nunique() == 1
        ]
        if not single_levels:
            break
        result.columns = result.columns.droplevel(single_levels[0])

    if isinstance(result.columns, pd.MultiIndex):
        result.columns = ["_".join(map(str, column)).strip("_") for column in result.columns]
    return result


def normalize_daily_frame(frame: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Return a sorted, unique daily frame with adjusted ``price`` and ``volume``."""

    if frame is None or frame.empty:
        raise MarketDataError(f"{ticker}: 반환된 데이터가 비어 있습니다.")

    normalized = _flatten_yfinance_columns(frame, ticker)
    normalized.columns = [str(column).strip() for column in normalized.columns]

    price_column = None
    for candidate in ("Adj Close", "Close", "adj close", "close"):
        if candidate in normalized.columns and normalized[candidate].notna().any():
            price_column = candidate
            break
    volume_column = next(
        (candidate for candidate in ("Volume", "volume") if candidate in normalized.columns),
        None,
    )
    if price_column is None or volume_column is None:
        raise MarketDataError(
            f"{ticker}: 가격 또는 거래량 열이 없습니다. 열={list(normalized.columns)}"
        )

    result = normalized[[price_column, volume_column]].rename(
        columns={price_column: "price", volume_column: "volume"}
    )
    result.index = pd.to_datetime(result.index, errors="coerce")
    result = result.loc[~result.index.isna()].copy()
    if isinstance(result.index, pd.DatetimeIndex) and result.index.tz is not None:
        result.index = result.index.tz_localize(None)
    result.index = result.index.normalize()
    result.index.name = "date"
    result["price"] = pd.to_numeric(result["price"], errors="coerce")
    result["volume"] = pd.to_numeric(result["volume"], errors="coerce")
    result = result.dropna(subset=["price"]).sort_index()
    result = result.loc[~result.index.duplicated(keep="last")]
    result = result.loc[result["price"] > 0]
    if result.empty:
        raise MarketDataError(f"{ticker}: 유효한 양수 가격 데이터가 없습니다.")
    return result


class YFinanceProvider:
    """Free, no-key daily data provider backed by yfinance."""

    name = "yfinance (Yahoo Finance)"

    def __init__(
        self,
        retry_count: int = 3,
        retry_delay_seconds: float = 1.0,
        cache_dir: str | Path | None = None,
    ):
        self.retry_count = max(1, int(retry_count))
        self.retry_delay_seconds = max(0.0, float(retry_delay_seconds))
        self.cache_dir = Path(cache_dir).resolve() if cache_dir else None
        self._cache_configured = False

    def _download_one(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        try:
            import yfinance as yf
        except ImportError as exc:  # pragma: no cover - depends on local installation
            raise MarketDataError(
                "yfinance가 설치되지 않았습니다. 'pip install -r requirements.txt'를 실행하세요."
            ) from exc

        if self.cache_dir is not None and not self._cache_configured:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            # yfinance stores timezone, cookie, and ISIN SQLite databases here.
            # A project-local location also works in restricted Windows profiles.
            yf.set_tz_cache_location(str(self.cache_dir))
            self._cache_configured = True

        # yfinance treats end as exclusive, while our provider interface is inclusive.
        yahoo_end = end + timedelta(days=1)
        raw = yf.download(
            ticker,
            start=start.isoformat(),
            end=yahoo_end.isoformat(),
            interval="1d",
            auto_adjust=False,
            actions=False,
            progress=False,
            threads=False,
            timeout=20,
        )
        return normalize_daily_frame(raw, ticker)

    def download(
        self,
        tickers: Sequence[str],
        start: date,
        end: date,
    ) -> DownloadResult:
        result = DownloadResult(provider=self.name)
        for raw_ticker in dict.fromkeys(tickers):
            ticker = str(raw_ticker).strip().upper()
            if not ticker:
                continue
            last_error: Exception | None = None
            for attempt in range(1, self.retry_count + 1):
                try:
                    result.data[ticker] = self._download_one(ticker, start, end)
                    last_error = None
                    break
                except Exception as exc:  # isolate failures so other ETFs still work
                    last_error = exc
                    if attempt < self.retry_count and self.retry_delay_seconds:
                        time.sleep(self.retry_delay_seconds * attempt)
            if last_error is not None:
                result.errors[ticker] = str(last_error)
        return result


def create_provider(data_config: dict) -> MarketDataProvider:
    """Create the configured provider without leaking it into analysis code."""

    provider_name = str(data_config.get("provider", "yfinance")).lower()
    if provider_name != "yfinance":
        raise MarketDataError(
            f"지원하지 않는 데이터 공급자입니다: {provider_name}. 현재 MVP는 yfinance를 지원합니다."
        )
    return YFinanceProvider(
        retry_count=data_config.get("retry_count", 3),
        retry_delay_seconds=data_config.get("retry_delay_seconds", 1.0),
        cache_dir=data_config.get("resolved_cache_path") or data_config.get("cache_path"),
    )


def validate_download(
    download: DownloadResult,
    benchmark: str,
    expected_tickers: Sequence[str],
    min_required_rows: int,
    stale_after_calendar_days: int,
    as_of: date,
    exclude_incomplete_us_session: bool = True,
    market_close_grace_minutes: int = 15,
) -> DownloadResult:
    """Remove unusable ETF frames and add actionable warnings.

    The benchmark is mandatory.  Individual ETF failures are non-fatal and are
    kept in ``errors`` so the dashboard can explain the partial result.
    """

    benchmark = benchmark.upper()
    if benchmark not in download.data:
        detail = download.errors.get(benchmark, "데이터 없음")
        raise MarketDataError(f"기준지수 {benchmark} 다운로드 실패: {detail}")

    benchmark_frame = download.data[benchmark]

    if exclude_incomplete_us_session and not benchmark_frame.empty:
        now_new_york = datetime.now(ZoneInfo("America/New_York"))
        grace = max(0, int(market_close_grace_minutes))
        completed_after = datetime.combine(
            now_new_york.date(), clock_time(16, 0), tzinfo=now_new_york.tzinfo
        ) + timedelta(minutes=grace)
        latest_date = benchmark_frame.index.max().date()
        if latest_date == now_new_york.date() and now_new_york < completed_after:
            incomplete_day = pd.Timestamp(latest_date)
            for ticker, frame in list(download.data.items()):
                download.data[ticker] = frame.loc[frame.index < incomplete_day].copy()
            download.warnings.append(
                f"뉴욕장 종료 전 데이터({latest_date})를 미완성 일봉으로 보고 제외했습니다."
            )
            benchmark_frame = download.data[benchmark]

    if len(benchmark_frame) < min_required_rows:
        raise MarketDataError(
            f"기준지수 {benchmark} 데이터가 {len(benchmark_frame)}행뿐입니다. "
            f"최소 {min_required_rows}행이 필요합니다."
        )

    benchmark_last = benchmark_frame.index.max()
    age = (pd.Timestamp(as_of) - benchmark_last).days
    if age > stale_after_calendar_days:
        download.warnings.append(
            f"{benchmark} 최신 데이터가 {benchmark_last.date()}로 오래되었습니다({age}일 전)."
        )

    for raw_ticker in expected_tickers:
        ticker = raw_ticker.upper()
        if ticker == benchmark:
            continue
        if ticker not in download.data:
            download.errors.setdefault(
                ticker, "데이터 공급자가 해당 티커의 일봉을 반환하지 않았습니다."
            )
            continue
        frame = download.data[ticker]
        if len(frame) < min_required_rows:
            download.errors[ticker] = (
                f"데이터가 {len(frame)}행뿐입니다(최소 {min_required_rows}행 필요)."
            )
            del download.data[ticker]
            continue
        if benchmark_last not in frame.index:
            download.errors[ticker] = (
                f"기준일 {benchmark_last.date()} 데이터가 없어 순위에서 제외했습니다."
            )
            del download.data[ticker]

    return download
