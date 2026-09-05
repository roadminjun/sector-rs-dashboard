"""End-to-end orchestration shared by the CLI and Streamlit dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import math
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
import yaml

from indicators import calculate_all_indicators
from market_data import (
    DownloadResult,
    MarketDataProvider,
    create_provider,
    validate_download,
)
from signals import apply_signals
from sigma_data import (
    SigmaDataError,
    SigmaSnapshotClient,
    disabled_sigma_result,
    merge_sigma_snapshot,
)
from storage import (
    StorageError,
    compare_signals,
    load_history,
    load_previous_snapshot,
    save_daily_results,
)


DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("config.yaml")


class ConfigurationError(ValueError):
    """Raised when config.yaml cannot support the requested analysis."""


@dataclass
class AnalysisResult:
    current: pd.DataFrame
    histories: dict[str, pd.DataFrame]
    changes: pd.DataFrame
    summary: dict[str, Any]
    analysis_date: str
    previous_date: str | None
    provider: str
    storage_path: Path
    saved_path: Path | None
    issues: dict[str, str]
    warnings: list[str]
    sigma: dict[str, Any]
    storage_error: str | None = None


def load_config(config_path: str | Path = DEFAULT_CONFIG_PATH) -> dict:
    path = Path(config_path).resolve()
    try:
        with path.open("r", encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file) or {}
    except FileNotFoundError as exc:
        raise ConfigurationError(f"설정 파일을 찾을 수 없습니다: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"YAML 형식 오류: {path} ({exc})") from exc

    required_sections = ("benchmark", "etfs", "indicators", "data", "storage")
    missing = [section for section in required_sections if section not in config]
    if missing:
        raise ConfigurationError(f"설정 섹션이 없습니다: {', '.join(missing)}")

    benchmark = config["benchmark"]
    if isinstance(benchmark, str):
        benchmark = {"ticker": benchmark, "name": benchmark}
        config["benchmark"] = benchmark
    if not isinstance(benchmark, Mapping) or not benchmark.get("ticker"):
        raise ConfigurationError("benchmark.ticker를 지정해야 합니다.")
    benchmark["ticker"] = str(benchmark["ticker"]).strip().upper()

    if not isinstance(config["etfs"], Mapping) or not config["etfs"]:
        raise ConfigurationError("etfs에는 한 개 이상의 '티커: 섹터명'이 필요합니다.")
    normalized_etfs: dict[str, str] = {}
    for ticker, name in config["etfs"].items():
        normalized = str(ticker).strip().upper()
        if not normalized:
            raise ConfigurationError("빈 ETF 티커는 사용할 수 없습니다.")
        if normalized == benchmark["ticker"]:
            raise ConfigurationError("기준지수 티커를 감시 ETF 목록에 중복할 수 없습니다.")
        normalized_etfs[normalized] = str(name).strip() or normalized
    config["etfs"] = normalized_etfs

    storage_value = config["storage"].get("results_path")
    if not storage_value:
        raise ConfigurationError("storage.results_path를 지정해야 합니다.")
    storage_path = Path(str(storage_value))
    if not storage_path.is_absolute():
        storage_path = path.parent / storage_path
    config["storage"]["resolved_results_path"] = storage_path.resolve()
    cache_value = config["data"].get("cache_path")
    if cache_value:
        cache_path = Path(str(cache_value))
        if not cache_path.is_absolute():
            cache_path = path.parent / cache_path
        config["data"]["resolved_cache_path"] = cache_path.resolve()
    config["config_path"] = path
    return config


def _truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    if pd.isna(value):
        return False
    return bool(value)


def build_summary(
    current: pd.DataFrame,
    previous: pd.DataFrame,
    previous_date: str | None,
    expected_current_count: int,
) -> dict[str, Any]:
    """Build dashboard facts without introducing a composite score."""

    top_three = [
        f"{row.sector_name} ({row.etf})"
        for row in current.head(3).itertuples(index=False)
    ]
    previous_signals = (
        previous.set_index("etf")["signal"].to_dict()
        if not previous.empty and {"etf", "signal"}.issubset(previous.columns)
        else {}
    )

    strengthening_rows = current.loc[current["signal"].isin(["주도 후보", "개선 중"])]
    strengthening = []
    for row in strengthening_rows.itertuples(index=False):
        prior = previous_signals.get(row.etf)
        if prior not in {"강한 주도", "주도 후보", "개선 중"}:
            strengthening.append(f"{row.sector_name} ({row.etf}, {row.signal})")
    if not strengthening and previous.empty:
        strengthening = [
            f"{row.sector_name} ({row.etf}, {row.signal})"
            for row in strengthening_rows.itertuples(index=False)
        ]

    weakening_tickers: set[str] = set(
        current.loc[current["signal"] == "약화", "etf"].astype(str)
    )
    if previous_signals:
        for row in current.itertuples(index=False):
            prior = previous_signals.get(row.etf)
            if prior in {"강한 주도", "주도 후보"} and row.signal in {
                "중립",
                "약화",
                "약세",
            }:
                weakening_tickers.add(row.etf)
    weakening = [
        f"{row.sector_name} ({row.etf}, {row.signal})"
        for row in current.itertuples(index=False)
        if row.etf in weakening_tickers
    ]

    leaders = current.loc[current["signal"] == "강한 주도", "etf"].astype(str).tolist()

    leadership = {
        "state": "비교 불가",
        "detail": "이전 저장 데이터가 없어 리더십 폭 변화를 계산할 수 없습니다.",
        "current_count": int(current["leadership_participant"].map(_truthy).sum()),
        "previous_count": None,
        "common_count": 0,
        "delta": None,
    }
    current_complete = len(current) == expected_current_count
    previous_expected: int | None = None
    if not previous.empty and "universe_size" in previous.columns:
        previous_sizes = pd.to_numeric(previous["universe_size"], errors="coerce").dropna()
        if not previous_sizes.empty and previous_sizes.nunique() == 1:
            previous_expected = int(previous_sizes.iloc[0])
    previous_complete = previous_expected is not None and len(previous) == previous_expected

    if not current_complete:
        leadership["detail"] = (
            f"현재 감시 ETF {expected_current_count}개 중 {len(current)}개만 분석돼 "
            "리더십 폭 비교를 생략했습니다."
        )
    elif not previous.empty and not previous_complete:
        expected_text = (
            f"{previous_expected}개" if previous_expected is not None else "확인 불가"
        )
        leadership["detail"] = (
            f"이전 스냅샷이 불완전합니다(저장 {len(previous)}개, 기대 {expected_text}). "
            "리더십 폭 비교를 생략했습니다."
        )
    elif not previous.empty and "leadership_participant" in previous.columns:
        current_by_etf = current.set_index("etf")
        previous_by_etf = previous.set_index("etf")
        common = sorted(set(current_by_etf.index) & set(previous_by_etf.index))
        if common:
            current_count = sum(
                _truthy(current_by_etf.loc[ticker, "leadership_participant"])
                for ticker in common
            )
            previous_count = sum(
                _truthy(previous_by_etf.loc[ticker, "leadership_participant"])
                for ticker in common
            )
            delta = current_count - previous_count
            threshold = max(1, math.ceil(len(common) * 0.15))
            if delta >= threshold:
                state = "확대"
            elif delta <= -threshold:
                state = "축소"
            else:
                state = "유지"
            leadership = {
                "state": state,
                "detail": (
                    f"공통 {len(common)}개 ETF 중 참여가 {previous_count}개에서 "
                    f"{current_count}개로 변했습니다({delta:+d}, 판정 기준 ±{threshold}개)."
                ),
                "current_count": current_count,
                "previous_count": previous_count,
                "common_count": len(common),
                "delta": delta,
            }

    return {
        "top_three": top_three,
        "strengthening": strengthening,
        "weakening": weakening,
        "leaders": leaders,
        "has_clear_leader": bool(leaders),
        "leadership": leadership,
        "previous_date": previous_date,
    }


def run_pipeline(
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    *,
    save: bool = True,
    provider: MarketDataProvider | None = None,
    sigma_client: SigmaSnapshotClient | None = None,
    as_of: date | None = None,
) -> AnalysisResult:
    """Download, calculate, classify, compare, and optionally persist one run."""

    config = load_config(config_path)
    benchmark = config["benchmark"]["ticker"]
    etfs = config["etfs"]
    data_config = config["data"]
    effective_as_of = as_of or date.today()
    lookback_days = int(data_config.get("lookback_calendar_days", 600))
    start = effective_as_of - timedelta(days=lookback_days)

    active_provider = provider or create_provider(data_config)
    tickers = [benchmark, *etfs.keys()]
    download: DownloadResult = active_provider.download(tickers, start, effective_as_of)
    download = validate_download(
        download,
        benchmark=benchmark,
        expected_tickers=tickers,
        min_required_rows=int(data_config.get("min_required_rows", 201)),
        stale_after_calendar_days=int(data_config.get("stale_after_calendar_days", 5)),
        as_of=effective_as_of,
        exclude_incomplete_us_session=bool(
            data_config.get("exclude_incomplete_us_session", True)
        ),
        market_close_grace_minutes=int(data_config.get("market_close_grace_minutes", 15)),
    )

    latest, histories, indicator_errors = calculate_all_indicators(
        download.data, benchmark, etfs, config["indicators"]
    )
    current, signal_errors = apply_signals(latest)
    current["provider"] = download.provider
    current["universe_size"] = len(etfs)
    analysis_date = str(current["date"].iloc[0])

    issues = {**download.errors, **indicator_errors, **signal_errors}
    warnings = list(download.warnings)

    # 1SIGMA is optional context. It cannot replace prices or alter the six
    # relative-strength classifications, and a remote failure never aborts the run.
    sigma_config = config.get("sigma") or {}
    if _truthy(sigma_config.get("enabled", False)):
        try:
            active_sigma_client = sigma_client or SigmaSnapshotClient(
                endpoint=str(sigma_config.get("endpoint", "")),
                timeout_seconds=float(sigma_config.get("timeout_seconds", 20)),
                retry_count=int(sigma_config.get("retry_count", 3)),
                retry_delay_seconds=float(sigma_config.get("retry_delay_seconds", 2)),
            )
            sigma_snapshot = active_sigma_client.fetch()
            sigma_merge = merge_sigma_snapshot(
                current, sigma_snapshot, analysis_date, benchmark
            )
        except (SigmaDataError, ValueError, TypeError) as exc:
            sigma_merge = disabled_sigma_result(current)
            sigma_merge.metadata.update(
                {"enabled": True, "status": "오류", "error": str(exc)}
            )
            sigma_merge.warnings.append(
                f"1SIGMA 보조 데이터를 사용하지 못했습니다. 기본 분석은 정상 유지됩니다: {exc}"
            )
    else:
        sigma_merge = disabled_sigma_result(current)
    current = sigma_merge.current
    warnings.extend(sigma_merge.warnings)
    storage_path: Path = config["storage"]["resolved_results_path"]
    storage_error: str | None = None
    try:
        history = load_history(storage_path)
    except StorageError as exc:
        history = pd.DataFrame()
        storage_error = str(exc)
        warnings.append(storage_error)

    previous_date, previous = load_previous_snapshot(history, analysis_date, benchmark)
    changes = compare_signals(current, previous)
    summary = build_summary(current, previous, previous_date, len(etfs))

    saved_path: Path | None = None
    if save and storage_error is None:
        try:
            saved_path = save_daily_results(current, storage_path, download.provider)
        except StorageError as exc:
            storage_error = str(exc)
            warnings.append(storage_error)

    return AnalysisResult(
        current=current,
        histories=histories,
        changes=changes,
        summary=summary,
        analysis_date=analysis_date,
        previous_date=previous_date,
        provider=download.provider,
        storage_path=storage_path,
        saved_path=saved_path,
        issues=issues,
        warnings=warnings,
        sigma=sigma_merge.metadata,
        storage_error=storage_error,
    )
