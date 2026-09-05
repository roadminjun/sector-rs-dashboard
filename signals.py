"""Simple, auditable sector-state classification and Korean explanations."""

from __future__ import annotations

import math
from typing import Mapping

import pandas as pd


SIGNAL_ORDER = {
    "강한 주도": 0,
    "주도 후보": 1,
    "개선 중": 2,
    "중립": 3,
    "약화": 4,
    "약세": 5,
}

REQUIRED_SIGNAL_FIELDS = (
    "price",
    "relative_return_5d",
    "relative_return_20d",
    "relative_return_60d",
    "relative_return_20d_prev",
    "rs_change_5d",
    "rs_ratio",
    "rs_ma_20",
    "rs_high_20d",
    "ma_20",
    "ma_50",
    "above_ma_20",
    "above_ma_50",
    "underperform_days_5d",
    "previously_strong",
)


class SignalError(ValueError):
    """Raised when a signal cannot be evaluated from the supplied indicators."""


def _finite_number(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _as_bool(value: object) -> bool:
    if pd.isna(value):
        return False
    return bool(value)


def _percent(value: float) -> str:
    return f"{float(value):+.1%}"


def _price(value: float) -> str:
    return f"{float(value):,.2f}"


def classify_signal(row: Mapping) -> tuple[str, str]:
    """Classify one latest indicator row using fixed priority rules.

    Priority is: weakening -> strong leader -> bearish -> candidate ->
    improving -> neutral.  All comparisons use unrounded values.
    """

    missing = [field for field in REQUIRED_SIGNAL_FIELDS if field not in row]
    non_numeric_fields = {
        "rs_high_20d",
        "above_ma_20",
        "above_ma_50",
        "previously_strong",
    }
    invalid = [
        field
        for field in REQUIRED_SIGNAL_FIELDS
        if field not in non_numeric_fields and not _finite_number(row.get(field))
    ]
    if missing or invalid:
        fields = ", ".join(sorted(set(missing + invalid)))
        raise SignalError(f"신호 판정에 필요한 값이 없습니다: {fields}")

    ticker = str(row.get("etf", "ETF"))
    rel_5 = float(row["relative_return_5d"])
    rel_20 = float(row["relative_return_20d"])
    rel_60 = float(row["relative_return_60d"])
    rel_20_prev = float(row["relative_return_20d_prev"])
    rs_change_5 = float(row["rs_change_5d"])
    rs_ratio = float(row["rs_ratio"])
    rs_ma_20 = float(row["rs_ma_20"])
    price = float(row["price"])
    ma_20 = float(row["ma_20"])
    ma_50 = float(row["ma_50"])
    underperform_days = int(float(row["underperform_days_5d"]))

    above_20 = _as_bool(row["above_ma_20"])
    above_50 = _as_bool(row["above_ma_50"])
    rs_high_20 = _as_bool(row["rs_high_20d"])
    previously_strong = _as_bool(row["previously_strong"])

    rs_below_average = rs_ratio < rs_ma_20
    frequent_underperformance = underperform_days >= 4
    if previously_strong and (rs_below_average or frequent_underperformance):
        if rs_below_average and frequent_underperformance:
            trigger = (
                "RS 비율이 20일선 아래로 내려갔고, 최근 5거래일 중 "
                f"{underperform_days}일 QQQ를 언더퍼폼했기 때문에"
            )
        elif rs_below_average:
            trigger = "RS 비율이 20일선 아래로 내려갔기 때문에"
        else:
            trigger = (
                f"최근 5거래일 중 {underperform_days}일 QQQ를 언더퍼폼했기 때문에"
            )
        return (
            "약화",
            f"{ticker}는 최근 20거래일 안에 강한 주도 조건을 보였지만, "
            f"현재 {trigger} 모멘텀 약화로 판정됐습니다.",
        )

    strong_leader = (
        rel_20 > 0
        and rel_60 > 0
        and rs_high_20
        and above_20
        and above_50
    )
    if strong_leader:
        return (
            "강한 주도",
            f"{ticker}는 QQQ 대비 20일 상대수익률이 {_percent(rel_20)}, "
            f"60일 상대수익률이 {_percent(rel_60)}이고, RS 비율이 20일 신고가이며 "
            "가격이 20일선과 50일선 위라 강한 주도로 판정됐습니다.",
        )

    bearish = rel_20 < 0 and rel_60 < 0 and not above_20 and not above_50
    if bearish:
        return (
            "약세",
            f"{ticker}는 QQQ 대비 20일 상대수익률이 {_percent(rel_20)}, "
            f"60일 상대수익률이 {_percent(rel_60)}이고, 현재 가격 "
            f"{_price(price)}가 20일선({_price(ma_20)})과 50일선({_price(ma_50)}) "
            "아래라 약세로 판정됐습니다.",
        )

    candidate = rel_20_prev <= 0 < rel_20 and rs_change_5 > 0
    if candidate:
        return (
            "주도 후보",
            f"{ticker}의 QQQ 대비 20일 상대수익률이 전일 "
            f"{_percent(rel_20_prev)}에서 {_percent(rel_20)}로 양수 전환했고, "
            f"RS 비율의 5일 변화율도 {_percent(rs_change_5)}로 상승해 "
            "주도 후보로 판정됐습니다.",
        )

    improving = rel_20 < 0 and rel_5 > rel_20
    if improving:
        return (
            "개선 중",
            f"{ticker}는 QQQ 대비 20일 상대수익률이 아직 {_percent(rel_20)}이지만, "
            f"5일 상대수익률이 {_percent(rel_5)}로 중기 값보다 빠르게 개선돼 "
            "개선 중으로 판정됐습니다.",
        )

    return (
        "중립",
        f"{ticker}의 QQQ 대비 상대수익률은 5일 {_percent(rel_5)}, "
        f"20일 {_percent(rel_20)}, 60일 {_percent(rel_60)}이며, "
        "현재는 다른 명시적 조건을 충족하지 않아 중립으로 판정됐습니다.",
    )


def moving_average_state(row: Mapping) -> str:
    positions = [
        f"{period}일선 {'위' if _as_bool(row.get(f'above_ma_{period}')) else '아래'}"
        for period in (20, 50, 200)
    ]
    alignment = "정배열" if _as_bool(row.get("ma_bullish_alignment")) else "정배열 아님"
    return f"{alignment} · " + "/".join(positions)


def apply_signals(latest: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, str]]:
    """Apply signals, exclude invalid rows, and assign deterministic ranks."""

    valid_rows: list[dict] = []
    errors: dict[str, str] = {}
    for _, series in latest.iterrows():
        row = series.to_dict()
        ticker = str(row.get("etf", "unknown"))
        try:
            signal, reason = classify_signal(row)
            row["signal"] = signal
            row["signal_reason"] = reason.replace("\n", " ").strip()
            row["moving_average_state"] = moving_average_state(row)
            valid_rows.append(row)
        except SignalError as exc:
            errors[ticker] = str(exc)

    if not valid_rows:
        raise SignalError("신호를 판정할 수 있는 ETF가 없습니다.")

    result = pd.DataFrame(valid_rows)
    result = result.sort_values(
        ["relative_return_20d", "relative_return_60d", "relative_return_5d", "etf"],
        ascending=[False, False, False, True],
        na_position="last",
    ).reset_index(drop=True)
    result.insert(0, "rank", range(1, len(result) + 1))
    return result, errors
