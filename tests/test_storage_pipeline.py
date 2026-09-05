from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from market_data import DownloadResult
from pipeline import build_summary, run_pipeline
from sigma_data import parse_sigma_snapshot
from storage import StorageError, load_history, load_previous_snapshot, save_daily_results


class FakeProvider:
    name = "synthetic-test-provider"

    def __init__(self, frames):
        self.frames = frames

    def download(self, tickers, start, end):
        return DownloadResult(
            data={ticker: self.frames[ticker].copy() for ticker in tickers},
            provider=self.name,
        )


class FakeSigmaClient:
    def fetch(self):
        return parse_sigma_snapshot(
            {
                "snapshot": {
                    "session": "CLOSED",
                    "sessionDate": "2026-09-03",
                    "generatedAt": "2026-09-04T07:00:00+09:00",
                },
                "quotes": [
                    {
                        "symbol": "QQQ",
                        "price": 101,
                        "previousClose": 100,
                        "anchor": 100,
                        "sigmaPercent": 2,
                    }
                ],
                "sectorQuotes": [
                    {
                        "symbol": "AAA",
                        "price": 103,
                        "previousClose": 102,
                        "anchor": 100,
                        "sigmaPercent": 2,
                    }
                ],
            },
            "https://example.test/snapshot",
        )


def make_frames():
    dates = pd.bdate_range(end="2026-09-03", periods=300)
    step = np.arange(len(dates))
    return {
        "QQQ": pd.DataFrame(
            {"price": 100 * np.power(1.001, step), "volume": 1_000_000}, index=dates
        ),
        "AAA": pd.DataFrame(
            {"price": 70 * np.power(1.002, step), "volume": 800_000 + step}, index=dates
        ),
        "BBB": pd.DataFrame(
            {"price": 90 * np.power(0.999, step), "volume": 600_000 + step}, index=dates
        ),
    }


def write_config(path: Path, results_path: Path):
    path.write_text(
        f"""
benchmark:
  ticker: QQQ
  name: Nasdaq-100
etfs:
  AAA: Alpha
  BBB: Beta
indicators:
  return_periods: [5, 20, 60]
  moving_average_periods: [20, 50, 200]
  rs_high_periods: [20, 60]
  rs_moving_average_period: 20
  volume_average_period: 20
  rsi_period: 14
  volatility_period: 20
  volatility_annualization_days: 252
data:
  provider: yfinance
  lookback_calendar_days: 600
  min_required_rows: 201
  retry_count: 1
  retry_delay_seconds: 0
  stale_after_calendar_days: 10
storage:
  results_path: {results_path.as_posix()}
""".strip(),
        encoding="utf-8",
    )


def test_pipeline_download_rank_signal_and_csv_upsert(tmp_path):
    config_path = tmp_path / "config.yaml"
    results_path = tmp_path / "daily.csv"
    write_config(config_path, results_path)
    provider = FakeProvider(make_frames())

    first = run_pipeline(
        config_path, save=True, provider=provider, as_of=date(2026, 9, 4)
    )
    assert first.analysis_date == "2026-09-03"
    assert first.current["etf"].tolist() == ["AAA", "BBB"]
    assert first.current["rank"].tolist() == [1, 2]
    assert set(first.current["signal"]) == {"강한 주도", "약세"}
    assert results_path.exists()
    stored = load_history(results_path)
    assert len(stored) == 2
    assert stored.sort_values("rank")["etf"].tolist() == ["AAA", "BBB"]
    assert stored["universe_size"].eq(2).all()
    assert stored["sigma_available"].eq(False).all()
    assert first.sigma["status"] == "비활성"

    second = run_pipeline(
        config_path, save=True, provider=provider, as_of=date(2026, 9, 4)
    )
    assert len(load_history(results_path)) == 2
    assert second.previous_date is None


def test_pipeline_optionally_merges_sigma_without_changing_signal(tmp_path):
    config_path = tmp_path / "config.yaml"
    results_path = tmp_path / "daily.csv"
    write_config(config_path, results_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "\nsigma:\n  enabled: true\n  endpoint: https://example.test/snapshot\n",
        encoding="utf-8",
    )

    result = run_pipeline(
        config_path,
        save=False,
        provider=FakeProvider(make_frames()),
        sigma_client=FakeSigmaClient(),
        as_of=date(2026, 9, 4),
    )
    rows = result.current.set_index("etf")
    assert result.sigma["coverage"] == 1
    assert rows.loc["AAA", "sigma_zscore"] == 1.5
    assert rows.loc["AAA", "signal"] == "강한 주도"
    assert not bool(rows.loc["BBB", "sigma_available"])


def test_storage_selects_one_latest_date_before_current(tmp_path):
    path = tmp_path / "history.csv"
    for day, signal in (("2026-09-01", "중립"), ("2026-09-02", "주도 후보")):
        save_daily_results(
            pd.DataFrame(
                [
                    {
                        "date": day,
                        "benchmark": "QQQ",
                        "etf": "AAA",
                        "sector_name": "Alpha",
                        "signal": signal,
                        "signal_reason": "테스트 이유",
                    }
                ]
            ),
            path,
            "test",
        )
    history = load_history(path)
    previous_date, snapshot = load_previous_snapshot(history, "2026-09-03", "QQQ")
    assert previous_date == "2026-09-02"
    assert snapshot.iloc[0]["signal"] == "주도 후보"


def test_partial_previous_snapshot_disables_leadership_breadth_comparison():
    current = pd.DataFrame(
        [
            {
                "rank": 1,
                "sector_name": "Alpha",
                "etf": "AAA",
                "signal": "중립",
                "leadership_participant": True,
            },
            {
                "rank": 2,
                "sector_name": "Beta",
                "etf": "BBB",
                "signal": "중립",
                "leadership_participant": False,
            },
        ]
    )
    previous = pd.DataFrame(
        [
            {
                "etf": "AAA",
                "signal": "중립",
                "leadership_participant": False,
                "universe_size": 2,
            }
        ]
    )
    summary = build_summary(current, previous, "2026-09-02", expected_current_count=2)
    assert summary["leadership"]["state"] == "비교 불가"
    assert "불완전" in summary["leadership"]["detail"]


def test_invalid_storage_parent_is_wrapped_as_storage_error(tmp_path):
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("blocker", encoding="utf-8")
    with pytest.raises(StorageError, match="CSV 저장 실패"):
        save_daily_results(
            pd.DataFrame(
                [
                    {
                        "date": "2026-09-03",
                        "benchmark": "QQQ",
                        "etf": "AAA",
                    }
                ]
            ),
            parent_file / "daily.csv",
            "test",
        )
