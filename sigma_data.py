"""Optional enrichment from the 1SIGMA public snapshot endpoint.

This module never replaces the primary daily-price provider.  It only adds the
weekly option-implied range context when the SIGMA snapshot date exactly matches
the yfinance analysis date.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import time
from typing import Any, Mapping

import numpy as np
import pandas as pd
import requests


SIGMA_COLUMNS = [
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
]


class SigmaDataError(RuntimeError):
    """Raised when a SIGMA response cannot be downloaded or trusted."""


@dataclass
class SigmaSnapshot:
    records: pd.DataFrame
    session_date: str
    session: str
    generated_at: str | None
    endpoint: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class SigmaMergeResult:
    current: pd.DataFrame
    metadata: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


def _finite_float(value: object) -> float:
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return np.nan
    return converted if math.isfinite(converted) else np.nan


def sigma_status(zscore: float) -> str:
    if not math.isfinite(zscore):
        return "계산 불가"
    if zscore >= 1.5:
        return "상단 큰 이탈"
    if zscore >= 1.0:
        return "상단 이탈"
    if zscore <= -1.5:
        return "하단 큰 이탈"
    if zscore <= -1.0:
        return "하단 이탈"
    return "정상 범위"


def sigma_interpretation(signal: str, zscore: float) -> str:
    """Describe the two independent dimensions without creating a score."""

    if not math.isfinite(zscore):
        return "SIGMA 위치를 계산할 수 없습니다."
    if signal == "강한 주도":
        if zscore >= 1.5:
            return "중기 주도 강함 · 단기 예상 범위를 크게 상회"
        if zscore >= 1.0:
            return "중기 주도 강함 · 주간 상단 이탈"
        if zscore < 0:
            return "중기 주도 강함 · 단기 위치는 주간 기준가 아래"
        return "중기 주도 강함 · 단기 위치는 예상 범위 안"
    if signal in {"주도 후보", "개선 중"}:
        if zscore >= 1.0:
            return "상대강도 개선 · 단기 가격 가속 동반"
        if zscore >= 0:
            return "상대강도 개선 · 주간 기준가 위"
        return "상대강도 개선 · 아직 주간 기준가 아래"
    if signal == "약화":
        if zscore >= 1.0:
            return "높은 SIGMA 위치에서 상대 모멘텀 약화"
        if zscore < 0:
            return "상대 모멘텀 약화 · 주간 기준가 아래"
        return "상대 모멘텀 약화 · 주간 예상 범위 안"
    if signal == "약세":
        if zscore > 0:
            return "중기 약세 속 단기 반등"
        if zscore <= -1.0:
            return "중기·단기 약세 동행"
        return "중기 약세 · 단기 위치는 예상 범위 안"
    if zscore >= 1.0:
        return "상대강도 중립 · 주간 상단 이탈"
    if zscore <= -1.0:
        return "상대강도 중립 · 주간 하단 이탈"
    return "상대강도 중립 · 주간 예상 범위 안"


class SigmaSnapshotClient:
    """Low-frequency HTTP client for the site-internal JSON snapshot."""

    def __init__(
        self,
        endpoint: str,
        timeout_seconds: float = 20,
        retry_count: int = 3,
        retry_delay_seconds: float = 2,
    ) -> None:
        self.endpoint = str(endpoint).strip()
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.retry_count = max(1, int(retry_count))
        self.retry_delay_seconds = max(0.0, float(retry_delay_seconds))

    def fetch(self) -> SigmaSnapshot:
        if not self.endpoint.startswith(("https://", "http://")):
            raise SigmaDataError("SIGMA endpoint는 http:// 또는 https:// 주소여야 합니다.")

        last_error: Exception | None = None
        payload: Mapping[str, Any] | None = None
        for attempt in range(1, self.retry_count + 1):
            try:
                response = requests.get(
                    self.endpoint,
                    timeout=self.timeout_seconds,
                    headers={"User-Agent": "sector-relative-strength-monitor/1.0"},
                )
                response.raise_for_status()
                parsed = response.json()
                if not isinstance(parsed, Mapping):
                    raise SigmaDataError("SIGMA 응답 최상위 값이 JSON 객체가 아닙니다.")
                payload = parsed
                break
            except (requests.RequestException, ValueError, SigmaDataError) as exc:
                last_error = exc
                if attempt < self.retry_count and self.retry_delay_seconds:
                    time.sleep(self.retry_delay_seconds * attempt)
        if payload is None:
            raise SigmaDataError(f"SIGMA 스냅샷 다운로드 실패: {last_error}")
        return parse_sigma_snapshot(payload, self.endpoint)


def parse_sigma_snapshot(payload: Mapping[str, Any], endpoint: str) -> SigmaSnapshot:
    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, Mapping):
        raise SigmaDataError("SIGMA 응답에 snapshot 객체가 없습니다.")

    session_date = str(snapshot.get("sessionDate") or "").strip()
    session = str(snapshot.get("session") or "").strip().upper()
    generated_at = snapshot.get("generatedAt")
    if not session_date:
        raise SigmaDataError("SIGMA snapshot.sessionDate가 없습니다.")
    try:
        normalized_date = pd.Timestamp(session_date).strftime("%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise SigmaDataError(f"SIGMA 기준일을 해석할 수 없습니다: {session_date}") from exc

    raw_quotes: list[Mapping[str, Any]] = []
    for collection_name in ("quotes", "sectorQuotes"):
        collection = payload.get(collection_name, [])
        if not isinstance(collection, list):
            raise SigmaDataError(f"SIGMA {collection_name} 값이 배열이 아닙니다.")
        raw_quotes.extend(item for item in collection if isinstance(item, Mapping))

    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    for quote in raw_quotes:
        symbol = str(quote.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        price = _finite_float(quote.get("price"))
        anchor = _finite_float(quote.get("anchor"))
        expected_move_percent = _finite_float(quote.get("sigmaPercent"))
        denominator = anchor * expected_move_percent / 100.0
        zscore = (
            (price - anchor) / denominator
            if math.isfinite(price)
            and math.isfinite(anchor)
            and math.isfinite(expected_move_percent)
            and denominator > 0
            else np.nan
        )
        if not math.isfinite(zscore):
            warnings.append(f"{symbol}: SIGMA 위치 계산에 필요한 값이 부족합니다.")
        rows.append(
            {
                "etf": symbol,
                "sigma_session_date": normalized_date,
                "sigma_generated_at": str(generated_at) if generated_at else pd.NA,
                "sigma_price": price,
                "sigma_previous_close": _finite_float(quote.get("previousClose")),
                "sigma_anchor": anchor,
                "sigma_percent": expected_move_percent,
                "sigma_zscore": zscore,
                "sigma_status": sigma_status(zscore),
            }
        )

    if not rows:
        raise SigmaDataError("SIGMA 응답에 사용 가능한 종목이 없습니다.")
    records = pd.DataFrame(rows).drop_duplicates(subset=["etf"], keep="last")
    return SigmaSnapshot(
        records=records.reset_index(drop=True),
        session_date=normalized_date,
        session=session,
        generated_at=str(generated_at) if generated_at else None,
        endpoint=endpoint,
        warnings=warnings,
    )


def merge_sigma_snapshot(
    current: pd.DataFrame,
    snapshot: SigmaSnapshot,
    analysis_date: str,
    benchmark: str,
) -> SigmaMergeResult:
    result = current.copy()
    # Explicit dtypes avoid pandas treating numeric enrichment fields as text.
    result["sigma_available"] = False
    for column in (
        "sigma_price",
        "sigma_previous_close",
        "sigma_anchor",
        "sigma_percent",
        "sigma_zscore",
    ):
        result[column] = np.nan
    for column in (
        "sigma_session_date",
        "sigma_generated_at",
        "sigma_status",
        "sigma_interpretation",
    ):
        result[column] = pd.NA

    metadata: dict[str, Any] = {
        "enabled": True,
        "status": "사용 불가",
        "endpoint": snapshot.endpoint,
        "session": snapshot.session,
        "session_date": snapshot.session_date,
        "generated_at": snapshot.generated_at,
        "coverage": 0,
        "expected": len(result),
        "benchmark_zscore": None,
    }
    warnings = list(snapshot.warnings)
    if snapshot.session != "CLOSED":
        warnings.append(
            f"SIGMA 장 상태가 CLOSED가 아니라 병합하지 않았습니다: {snapshot.session or 'UNKNOWN'}"
        )
        metadata["status"] = "장 미완결"
        return SigmaMergeResult(result, metadata, warnings)
    if snapshot.session_date != str(analysis_date):
        warnings.append(
            f"SIGMA 기준일({snapshot.session_date})과 주 분석 기준일({analysis_date})이 달라 "
            "병합하지 않았습니다."
        )
        metadata["status"] = "기준일 불일치"
        return SigmaMergeResult(result, metadata, warnings)

    records = snapshot.records.set_index("etf")
    for index, row in result.iterrows():
        ticker = str(row["etf"]).upper()
        if ticker not in records.index:
            continue
        sigma_row = records.loc[ticker]
        result.at[index, "sigma_available"] = True
        for column in SIGMA_COLUMNS:
            if column in {"sigma_available", "sigma_interpretation"}:
                continue
            result.at[index, column] = sigma_row.get(column, pd.NA)
        zscore = _finite_float(sigma_row.get("sigma_zscore"))
        result.at[index, "sigma_interpretation"] = sigma_interpretation(
            str(row["signal"]), zscore
        )

    benchmark = benchmark.upper()
    if benchmark in records.index:
        benchmark_z = _finite_float(records.loc[benchmark].get("sigma_zscore"))
        if math.isfinite(benchmark_z):
            metadata["benchmark_zscore"] = benchmark_z
    coverage = int(result["sigma_available"].fillna(False).astype(bool).sum())
    metadata["coverage"] = coverage
    metadata["status"] = "정상" if coverage else "일치 종목 없음"
    if coverage < len(result):
        missing = result.loc[~result["sigma_available"].astype(bool), "etf"].tolist()
        warnings.append(
            "SIGMA에 없는 감시 ETF는 보조 지표 없이 유지합니다: " + ", ".join(missing)
        )
    return SigmaMergeResult(result, metadata, warnings)


def disabled_sigma_result(current: pd.DataFrame) -> SigmaMergeResult:
    result = current.copy()
    result["sigma_available"] = False
    for column in (
        "sigma_price",
        "sigma_previous_close",
        "sigma_anchor",
        "sigma_percent",
        "sigma_zscore",
    ):
        result[column] = np.nan
    for column in (
        "sigma_session_date",
        "sigma_generated_at",
        "sigma_status",
        "sigma_interpretation",
    ):
        result[column] = pd.NA
    return SigmaMergeResult(
        current=result,
        metadata={
            "enabled": False,
            "status": "비활성",
            "coverage": 0,
            "expected": len(result),
            "session_date": None,
            "generated_at": None,
            "benchmark_zscore": None,
        },
    )
