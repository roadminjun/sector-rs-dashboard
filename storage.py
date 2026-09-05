"""Atomic CSV history storage and previous-snapshot comparisons."""

from __future__ import annotations

from datetime import date, datetime, timezone
import os
from pathlib import Path
import tempfile

import pandas as pd


KEY_COLUMNS = ["date", "benchmark", "etf"]
STORAGE_COLUMNS = [
    "date",
    "benchmark",
    "etf",
    "sector_name",
    "provider",
    "rank",
    "universe_size",
    "price",
    "return_5d",
    "return_20d",
    "return_60d",
    "benchmark_return_5d",
    "benchmark_return_20d",
    "benchmark_return_60d",
    "relative_return_5d",
    "relative_return_20d",
    "relative_return_60d",
    "rs_ratio",
    "rs_change_5d",
    "rs_change_20d",
    "rs_high_20d",
    "rs_high_60d",
    "rs_ma_20",
    "ma_20",
    "ma_50",
    "ma_200",
    "above_ma_20",
    "above_ma_50",
    "above_ma_200",
    "ma_bullish_alignment",
    "moving_average_state",
    "volume",
    "volume_average_period",
    "volume_ma",
    "volume_ma_20",
    "volume_ratio",
    "outperform_days_5d",
    "underperform_days_5d",
    "rsi_period",
    "rsi",
    "rsi_14",
    "volatility_period",
    "volatility",
    "volatility_20d",
    "leadership_participant",
    "sigma_available",
    "sigma_session_date",
    "sigma_generated_at",
    "sigma_price",
    "sigma_previous_close",
    "sigma_anchor",
    "sigma_percent",
    "sigma_zscore",
    "sigma_status",
    "sigma_interpretation",
    "signal",
    "signal_reason",
    "generated_at_utc",
]


class StorageError(RuntimeError):
    """Raised when existing history cannot be trusted or updated safely."""


def _normalize_dates(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    if "date" in result.columns:
        parsed = pd.to_datetime(result["date"], errors="coerce")
        if parsed.isna().any():
            raise StorageError("CSV의 date 열에 해석할 수 없는 값이 있습니다.")
        result["date"] = parsed.dt.strftime("%Y-%m-%d")
    return result


def load_history(csv_path: str | Path) -> pd.DataFrame:
    path = Path(csv_path)
    if not path.exists():
        return pd.DataFrame(columns=STORAGE_COLUMNS)
    try:
        history = pd.read_csv(path, encoding="utf-8-sig")
    except Exception as exc:
        raise StorageError(
            f"기존 CSV를 읽을 수 없어 안전을 위해 덮어쓰지 않았습니다: {path} ({exc})"
        ) from exc
    if history.empty:
        return pd.DataFrame(columns=STORAGE_COLUMNS)
    missing = [column for column in KEY_COLUMNS if column not in history.columns]
    if missing:
        raise StorageError(f"기존 CSV에 키 열이 없습니다: {', '.join(missing)}")
    return _normalize_dates(history)


def prepare_storage_rows(results: pd.DataFrame, provider: str) -> pd.DataFrame:
    prepared = results.copy()
    prepared["provider"] = provider
    prepared["generated_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    missing = [column for column in KEY_COLUMNS if column not in prepared.columns]
    if missing:
        raise StorageError(f"저장 결과에 필수 열이 없습니다: {', '.join(missing)}")
    prepared = _normalize_dates(prepared)
    for column in STORAGE_COLUMNS:
        if column not in prepared.columns:
            prepared[column] = pd.NA
    return prepared[STORAGE_COLUMNS]


def save_daily_results(
    results: pd.DataFrame,
    csv_path: str | Path,
    provider: str,
) -> Path:
    """Upsert a daily snapshot and atomically replace the history CSV."""

    path = Path(csv_path)
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = load_history(path)
        prepared = prepare_storage_rows(results, provider)

        combined = (
            prepared.reset_index(drop=True)
            if existing.empty
            else pd.concat([existing, prepared], ignore_index=True, sort=False)
        )
        combined = combined.drop_duplicates(subset=KEY_COLUMNS, keep="last")
        combined = combined.sort_values(KEY_COLUMNS).reset_index(drop=True)
        for column in STORAGE_COLUMNS:
            if column not in combined.columns:
                combined[column] = pd.NA
        # Keep known fields first while preserving future-compatible extra fields.
        extra_columns = [column for column in combined.columns if column not in STORAGE_COLUMNS]
        combined = combined[STORAGE_COLUMNS + extra_columns]

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".tmp",
            prefix=f".{path.stem}-",
            dir=path.parent,
            delete=False,
            encoding="utf-8-sig",
            newline="",
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            combined.to_csv(temporary_file, index=False)
        os.replace(temporary_path, path)
    except StorageError:
        raise
    except PermissionError as exc:
        raise StorageError(
            f"CSV를 갱신할 수 없습니다. Excel 등에서 파일을 닫고 다시 실행하세요: {path}"
        ) from exc
    except (OSError, ValueError, TypeError) as exc:
        raise StorageError(f"CSV 저장 실패: {path} ({exc})") from exc
    finally:
        if temporary_path is not None and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass
    return path


def load_previous_snapshot(
    history: pd.DataFrame,
    before_date: str | date | pd.Timestamp,
    benchmark: str,
) -> tuple[str | None, pd.DataFrame]:
    """Return the one latest complete stored date before ``before_date``."""

    if history.empty:
        return None, pd.DataFrame()
    normalized = _normalize_dates(history)
    target = pd.Timestamp(before_date).strftime("%Y-%m-%d")
    subset = normalized.loc[
        (normalized["benchmark"].astype(str).str.upper() == benchmark.upper())
        & (normalized["date"] < target)
    ]
    if subset.empty:
        return None, pd.DataFrame()
    previous_date = str(subset["date"].max())
    snapshot = subset.loc[subset["date"] == previous_date].copy()
    snapshot = snapshot.drop_duplicates(subset=["etf"], keep="last")
    return previous_date, snapshot.reset_index(drop=True)


def compare_signals(current: pd.DataFrame, previous: pd.DataFrame) -> pd.DataFrame:
    """Create user-facing transition text for current ETF rows."""

    previous_signals = (
        previous.set_index("etf")["signal"].to_dict()
        if not previous.empty and {"etf", "signal"}.issubset(previous.columns)
        else {}
    )
    rows: list[dict] = []
    for _, row in current.iterrows():
        ticker = str(row["etf"])
        current_signal = str(row["signal"])
        previous_signal = previous_signals.get(ticker)
        if previous_signal is None or pd.isna(previous_signal):
            change = "신규 감시 (비교 없음)"
            changed = False
        elif str(previous_signal) == current_signal:
            change = f"{current_signal} 유지"
            changed = False
        else:
            change = f"{previous_signal} → {current_signal}"
            changed = True
        rows.append(
            {
                "rank": int(row["rank"]),
                "sector_name": row["sector_name"],
                "etf": ticker,
                "previous_signal": previous_signal,
                "current_signal": current_signal,
                "signal_change": change,
                "changed": changed,
            }
        )
    return pd.DataFrame(rows).sort_values(
        ["changed", "rank"], ascending=[False, True]
    ).reset_index(drop=True)
