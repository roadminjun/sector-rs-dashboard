import pytest

from signals import classify_signal


def base_row(**overrides):
    row = {
        "etf": "TEST",
        "price": 120.0,
        "relative_return_5d": 0.01,
        "relative_return_20d": 0.02,
        "relative_return_60d": 0.03,
        "relative_return_20d_prev": 0.01,
        "rs_change_5d": 0.01,
        "rs_ratio": 0.5,
        "rs_ma_20": 0.48,
        "rs_high_20d": False,
        "ma_20": 110.0,
        "ma_50": 105.0,
        "above_ma_20": True,
        "above_ma_50": True,
        "underperform_days_5d": 1.0,
        "previously_strong": False,
    }
    row.update(overrides)
    return row


def assert_clean_reason(reason):
    assert reason
    assert "nan" not in reason.lower()
    assert "매수" not in reason
    assert "매도" not in reason
    assert "추천" not in reason


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (base_row(rs_high_20d=True), "강한 주도"),
        (
            base_row(relative_return_20d_prev=-0.001, relative_return_20d=0.001),
            "주도 후보",
        ),
        (
            base_row(
                relative_return_5d=-0.01,
                relative_return_20d=-0.03,
                relative_return_60d=0.01,
            ),
            "개선 중",
        ),
        (
            base_row(
                relative_return_5d=-0.01,
                relative_return_20d=-0.03,
                relative_return_60d=-0.04,
                above_ma_20=False,
                above_ma_50=False,
            ),
            "약세",
        ),
        (base_row(), "중립"),
    ],
)
def test_signal_categories(row, expected):
    signal, reason = classify_signal(row)
    assert signal == expected
    assert_clean_reason(reason)


def test_weakening_has_priority_over_current_strong_conditions():
    signal, reason = classify_signal(
        base_row(
            rs_high_20d=True,
            previously_strong=True,
            underperform_days_5d=4,
        )
    )
    assert signal == "약화"
    assert "4일" in reason


def test_candidate_requires_a_new_cross_and_positive_rs_change():
    signal, _ = classify_signal(
        base_row(relative_return_20d_prev=0.001, relative_return_20d=0.002)
    )
    assert signal == "중립"
    signal, _ = classify_signal(
        base_row(
            relative_return_20d_prev=-0.001,
            relative_return_20d=0.002,
            rs_change_5d=-0.001,
        )
    )
    assert signal == "중립"


def test_underperformance_does_not_mean_weakening_without_prior_strength():
    signal, _ = classify_signal(base_row(underperform_days_5d=5, previously_strong=False))
    assert signal == "중립"

