import math

import pandas as pd

from sigma_data import (
    SigmaDataError,
    merge_sigma_snapshot,
    parse_sigma_snapshot,
    sigma_interpretation,
    sigma_status,
)


def sample_payload(session="CLOSED", session_date="2026-09-03"):
    return {
        "snapshot": {
            "session": session,
            "sessionDate": session_date,
            "generatedAt": "2026-09-04T07:05:42+09:00",
        },
        "quotes": [
            {
                "symbol": "QQQ",
                "price": 102,
                "previousClose": 101,
                "anchor": 100,
                "sigmaPercent": 2,
            }
        ],
        "sectorQuotes": [
            {
                "symbol": "AAA",
                "price": 105,
                "previousClose": 103,
                "anchor": 100,
                "sigmaPercent": 2.5,
            }
        ],
    }


def test_parse_snapshot_and_compute_normalized_sigma_position():
    snapshot = parse_sigma_snapshot(sample_payload(), "https://example.test/snapshot")
    records = snapshot.records.set_index("etf")
    assert snapshot.session_date == "2026-09-03"
    assert snapshot.session == "CLOSED"
    assert records.loc["QQQ", "sigma_zscore"] == 1.0
    assert records.loc["AAA", "sigma_zscore"] == 2.0
    assert records.loc["AAA", "sigma_status"] == "상단 큰 이탈"


def test_merge_requires_closed_matching_date_and_exact_ticker():
    current = pd.DataFrame(
        [
            {"etf": "AAA", "signal": "강한 주도"},
            {"etf": "BBB", "signal": "중립"},
        ]
    )
    snapshot = parse_sigma_snapshot(sample_payload(), "https://example.test/snapshot")
    merged = merge_sigma_snapshot(current, snapshot, "2026-09-03", "QQQ")

    assert merged.metadata["status"] == "정상"
    assert merged.metadata["coverage"] == 1
    assert merged.metadata["benchmark_zscore"] == 1.0
    assert bool(merged.current.loc[0, "sigma_available"])
    assert not bool(merged.current.loc[1, "sigma_available"])
    assert "중기 주도 강함" in merged.current.loc[0, "sigma_interpretation"]
    assert any("BBB" in warning for warning in merged.warnings)

    mismatched = merge_sigma_snapshot(current, snapshot, "2026-09-02", "QQQ")
    assert mismatched.metadata["status"] == "기준일 불일치"
    assert mismatched.current["sigma_available"].eq(False).all()

    open_snapshot = parse_sigma_snapshot(
        sample_payload(session="REGULAR"), "https://example.test/snapshot"
    )
    not_closed = merge_sigma_snapshot(current, open_snapshot, "2026-09-03", "QQQ")
    assert not_closed.metadata["status"] == "장 미완결"


def test_sigma_helpers_and_invalid_payload():
    assert sigma_status(1.6) == "상단 큰 이탈"
    assert sigma_status(-1.1) == "하단 이탈"
    assert sigma_status(math.nan) == "계산 불가"
    assert "단기 반등" in sigma_interpretation("약세", 0.2)

    try:
        parse_sigma_snapshot({}, "https://example.test/snapshot")
    except SigmaDataError as exc:
        assert "snapshot" in str(exc)
    else:
        raise AssertionError("invalid payload must raise SigmaDataError")
