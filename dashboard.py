"""Streamlit dashboard for the sector relative-strength MVP."""

from __future__ import annotations

from datetime import timedelta
import os
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from pipeline import DEFAULT_CONFIG_PATH, AnalysisResult, load_config, run_pipeline
from storage import StorageError, load_history, save_daily_results


st.set_page_config(
    page_title="미국 섹터 상대강도 신호 탐지기",
    page_icon="📊",
    layout="wide",
)


@st.cache_data(ttl=3600, show_spinner=False)
def load_analysis_cached(config_path: str, config_modified_ns: int) -> AnalysisResult:
    # config_modified_ns intentionally participates in the cache key.
    del config_modified_ns
    return run_pipeline(config_path, save=False)


def format_percent(value: object, digits: int = 1) -> str:
    return "N/A" if pd.isna(value) else f"{float(value):+.{digits}%}"


def format_number(value: object, digits: int = 2) -> str:
    return "N/A" if pd.isna(value) else f"{float(value):,.{digits}f}"


def dashboard_read_only() -> bool:
    """Return true on cloud deployments where repository files are immutable history."""

    raw_value: object = os.getenv("SECTOR_RS_DASHBOARD_READ_ONLY", "")
    if not str(raw_value).strip():
        try:
            raw_value = st.secrets.get("SECTOR_RS_DASHBOARD_READ_ONLY", False)
        except (FileNotFoundError, KeyError, TypeError):
            raw_value = False
    return str(raw_value).strip().lower() in {"1", "true", "yes", "on"}


def format_sigma_z(value: object) -> str:
    return "N/A" if pd.isna(value) else f"{float(value):+.2f}σ"


def ranking_table(current: pd.DataFrame) -> pd.DataFrame:
    table = pd.DataFrame(
        {
            "순위": current["rank"].astype(int),
            "섹터명": current["sector_name"],
            "ETF": current["etf"],
            "현재 가격": current["price"].map(format_number),
            "5일 상대수익률": current["relative_return_5d"].map(format_percent),
            "20일 상대수익률": current["relative_return_20d"].map(format_percent),
            "60일 상대수익률": current["relative_return_60d"].map(format_percent),
            "RS 20일 신고가": current["rs_high_20d"].map(
                lambda value: "예" if bool(value) else "아니오"
            ),
            "이동평균 상태": current["moving_average_state"],
            "거래량 비율": current["volume_ratio"].map(
                lambda value: "N/A" if pd.isna(value) else f"{float(value):.2f}x"
            ),
            "RSI": current["rsi"].map(lambda value: format_number(value, 1)),
            "SIGMA 위치": current["sigma_zscore"].map(format_sigma_z),
            "예상 변동폭": current["sigma_percent"].map(
                lambda value: "N/A" if pd.isna(value) else f"±{float(value):.2f}%"
            ),
            "SIGMA 상태": current["sigma_status"].fillna("미제공"),
            "신호": current["signal"],
            "신호 발생 이유": current["signal_reason"],
            "상대강도 × SIGMA 관찰": current["sigma_interpretation"].fillna(
                "보조 데이터 없음"
            ),
        }
    )
    return table


def render_summary(result: AnalysisResult) -> None:
    summary = result.summary
    st.subheader("핵심 요약")
    columns = st.columns(5)
    columns[0].metric("분석 ETF", f"{len(result.current)}개")
    columns[1].metric(
        "강한 주도",
        f"{len(summary['leaders'])}개",
        ", ".join(summary["leaders"]) if summary["leaders"] else "없음",
    )
    leadership = summary["leadership"]
    columns[2].metric(
        "리더십 폭",
        leadership["state"],
        None if leadership["delta"] is None else f"{leadership['delta']:+d}개",
    )
    columns[3].metric(
        "비교 기준",
        summary["previous_date"] or "첫 실행",
        "최근 저장 거래일" if summary["previous_date"] else "전일 데이터 없음",
    )
    sigma = result.sigma
    columns[4].metric(
        "1SIGMA 보조정보",
        f"{sigma.get('coverage', 0)}/{sigma.get('expected', len(result.current))}개",
        str(sigma.get("status", "비활성")),
    )

    left, right = st.columns(2)
    with left:
        st.markdown(
            "**현재 가장 강한 3개**  \n"
            + (" · ".join(summary["top_three"]) if summary["top_three"] else "없음")
        )
        st.markdown(
            "**새롭게 강해지는 섹터**  \n"
            + (" · ".join(summary["strengthening"]) if summary["strengthening"] else "없음")
        )
    with right:
        st.markdown(
            "**기존 대비 약화되는 섹터**  \n"
            + (" · ".join(summary["weakening"]) if summary["weakening"] else "없음")
        )
        if summary["has_clear_leader"]:
            st.markdown(
                "**명확한 주도 섹터**  \n있음 — " + ", ".join(summary["leaders"])
            )
        else:
            st.markdown("**명확한 주도 섹터**  \n없음")
    st.caption(
        "리더십 참여 = 20일 상대수익률 > 0, RS > RS 20일선, 가격 > 50일선. "
        + leadership["detail"]
    )
    if sigma.get("enabled"):
        st.caption(
            "1SIGMA는 같은 기준일의 CLOSED 스냅샷만 결합하는 보조 관찰값입니다. "
            "기존 상대강도 순위와 신호 판정에는 사용하지 않습니다. 금요일 종가에는 "
            "새 주간 밴드 기준가가 설정되어 위치가 0.00σ로 표시될 수 있습니다."
        )


def make_etf_chart(history: pd.DataFrame, ticker: str, sector_name: str) -> go.Figure:
    figure = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.035,
        row_heights=[0.40, 0.23, 0.18, 0.19],
        subplot_titles=("조정 가격과 이동평균", "ETF / QQQ 상대강도", "거래량", "RSI"),
    )
    figure.add_trace(
        go.Scatter(x=history.index, y=history["price"], name=ticker, line=dict(width=2)),
        row=1,
        col=1,
    )
    for period, color in ((20, "#f59e0b"), (50, "#2563eb"), (200, "#7c3aed")):
        figure.add_trace(
            go.Scatter(
                x=history.index,
                y=history[f"ma_{period}"],
                name=f"MA {period}",
                line=dict(width=1.3, color=color),
            ),
            row=1,
            col=1,
        )
    figure.add_trace(
        go.Scatter(
            x=history.index,
            y=history["rs_ratio"],
            name="RS 비율",
            line=dict(width=2, color="#0f766e"),
        ),
        row=2,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=history.index,
            y=history["rs_ma_20"],
            name="RS MA 20",
            line=dict(width=1.3, color="#dc2626", dash="dash"),
        ),
        row=2,
        col=1,
    )
    figure.add_trace(
        go.Bar(
            x=history.index,
            y=history["volume"],
            name="거래량",
            marker_color="#94a3b8",
        ),
        row=3,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=history.index,
            y=history["volume_ma"],
            name="거래량 이동평균",
            line=dict(width=1.2, color="#334155"),
        ),
        row=3,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=history.index,
            y=history["rsi"],
            name="RSI",
            line=dict(width=1.8, color="#7c3aed"),
        ),
        row=4,
        col=1,
    )
    figure.add_hline(y=70, line_dash="dot", line_color="#dc2626", row=4, col=1)
    figure.add_hline(y=30, line_dash="dot", line_color="#2563eb", row=4, col=1)
    figure.update_yaxes(title_text="가격", row=1, col=1)
    figure.update_yaxes(title_text="RS", row=2, col=1)
    figure.update_yaxes(title_text="거래량", row=3, col=1)
    figure.update_yaxes(title_text="RSI", range=[0, 100], row=4, col=1)
    latest = history.index.max()
    figure.update_xaxes(range=[latest - timedelta(days=365), latest], row=4, col=1)
    figure.update_layout(
        title=f"{sector_name} ({ticker})",
        height=900,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        margin=dict(l=40, r=30, t=90, b=40),
        bargap=0.1,
    )
    return figure


def render_saved_fallback(config_path: Path, message: str) -> None:
    st.error(f"새 분석을 완료하지 못했습니다: {message}")
    try:
        config = load_config(config_path)
        history = load_history(config["storage"]["resolved_results_path"])
    except Exception as exc:
        st.info(f"표시할 저장 결과도 없습니다: {exc}")
        return
    if history.empty:
        st.info("표시할 저장 결과가 없습니다.")
        return
    latest_date = history["date"].max()
    snapshot = history.loc[history["date"] == latest_date].copy()
    st.warning(f"대신 저장된 최신 스냅샷({latest_date})을 표시합니다. 차트는 제공되지 않습니다.")
    visible = [
        column
        for column in ("sector_name", "etf", "relative_return_20d", "signal", "signal_reason")
        if column in snapshot.columns
    ]
    st.dataframe(snapshot[visible], hide_index=True, width="stretch")


def main() -> None:
    st.title("미국 섹터 상대강도 신호 탐지기")
    st.caption(
        "매일 일봉으로 QQQ 대비 상대강도와 추세를 관찰합니다. "
        "이 화면은 신호와 근거만 제공하며 매수·매도 지시가 아닙니다."
    )

    config_path = DEFAULT_CONFIG_PATH
    with st.sidebar:
        st.header("분석 설정")
        st.code(str(config_path), language=None)
        refresh = st.button("데이터 새로고침 및 재분석", type="primary", width="stretch")
        st.caption("ETF 목록과 계산 기간은 config.yaml에서 수정합니다.")
        st.divider()
        st.markdown(
            "**순위 기준**  \n20일 상대수익률 → 60일 → 5일 → ETF 코드"
        )
        st.caption("복합 점수는 사용하지 않습니다.")

    read_only = dashboard_read_only()
    if read_only:
        st.sidebar.info(
            "클라우드 읽기 전용 모드: CSV 갱신은 GitHub Actions가 담당합니다."
        )

    if refresh:
        load_analysis_cached.clear()
        st.session_state.pop("saved_snapshot_key", None)

    try:
        modified_ns = config_path.stat().st_mtime_ns
        with st.spinner("일봉 데이터를 내려받고 상대강도를 계산하는 중입니다..."):
            result = load_analysis_cached(str(config_path), modified_ns)
    except Exception as exc:
        render_saved_fallback(config_path, str(exc))
        return

    # Keep CSV writes outside the cached download/calculation function. A session
    # guard avoids writes caused only by selectbox or chart interactions.
    snapshot_key = f"{result.analysis_date}|{modified_ns}"
    if (
        not read_only
        and st.session_state.get("saved_snapshot_key") != snapshot_key
        and not result.storage_error
    ):
        try:
            result.saved_path = save_daily_results(
                result.current, result.storage_path, result.provider
            )
            st.session_state["saved_snapshot_key"] = snapshot_key
        except StorageError as exc:
            result.storage_error = str(exc)

    header_left, header_right = st.columns([3, 2])
    expected_count = int(result.current["universe_size"].iloc[0])
    header_left.info(
        f"분석 기준 거래일: **{result.analysis_date}** · 데이터: **{result.provider}** · "
        f"성공: **{len(result.current)}/{expected_count}개**"
    )
    if read_only:
        header_right.info("클라우드 읽기 전용 · 저장은 GitHub Actions 담당")
    elif result.saved_path:
        header_right.success(f"CSV 저장 완료: {result.saved_path}")
    elif result.storage_error:
        header_right.error(result.storage_error)
    else:
        header_right.caption("이 세션에서 동일 스냅샷을 이미 저장했습니다.")

    for warning in result.warnings:
        st.warning(warning)
    if result.issues:
        with st.expander(f"제외 또는 오류 종목 {len(result.issues)}개"):
            for ticker, issue in result.issues.items():
                st.write(f"- {ticker}: {issue}")

    render_summary(result)

    st.subheader("섹터 상대강도 순위표")
    st.caption(
        "상대수익률은 ETF 수익률에서 QQQ 수익률을 뺀 값이며, 모든 판정은 표시 전 원값으로 계산합니다."
    )
    st.dataframe(
        ranking_table(result.current),
        hide_index=True,
        width="stretch",
        height=min(650, 80 + len(result.current) * 36),
        column_config={
            "신호 발생 이유": st.column_config.TextColumn(width="large"),
            "상대강도 × SIGMA 관찰": st.column_config.TextColumn(width="large"),
            "이동평균 상태": st.column_config.TextColumn(width="medium"),
        },
    )

    csv_bytes = result.current.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "현재 스냅샷 CSV 다운로드",
        data=csv_bytes,
        file_name=f"sector_rs_{result.analysis_date}.csv",
        mime="text/csv",
    )

    st.subheader("전일 대비 신호 변화")
    if result.previous_date:
        st.caption(f"비교 대상은 저장된 가장 최근 과거 거래일 {result.previous_date}입니다.")
    else:
        st.info("첫 저장 실행이므로 비교할 과거 신호가 없습니다.")
    change_table = result.changes[["sector_name", "etf", "signal_change"]].rename(
        columns={"sector_name": "섹터명", "etf": "ETF", "signal_change": "신호 변화"}
    )
    st.dataframe(change_table, hide_index=True, width="stretch")

    st.subheader("ETF 상세 차트")
    labels = {
        f"{row.sector_name} ({row.etf})": row.etf
        for row in result.current.itertuples(index=False)
    }
    selected_label = st.selectbox("ETF 선택", options=list(labels))
    selected_ticker = labels[selected_label]
    selected_sector = str(
        result.current.loc[result.current["etf"] == selected_ticker, "sector_name"].iloc[0]
    )
    selected_row = result.current.loc[
        result.current["etf"] == selected_ticker
    ].iloc[0]
    if bool(selected_row.get("sigma_available", False)):
        sigma_columns = st.columns(4)
        sigma_columns[0].metric(
            "1SIGMA 기준가", format_number(selected_row["sigma_anchor"])
        )
        sigma_columns[1].metric(
            "예상 변동폭", f"±{float(selected_row['sigma_percent']):.2f}%"
        )
        sigma_columns[2].metric(
            "현재 위치", format_sigma_z(selected_row["sigma_zscore"])
        )
        sigma_columns[3].metric("단기 상태", str(selected_row["sigma_status"]))
        st.info(str(selected_row["sigma_interpretation"]))
    else:
        st.caption(f"{selected_ticker}는 현재 1SIGMA 스냅샷에 없어 보조 정보가 없습니다.")
    st.plotly_chart(
        make_etf_chart(result.histories[selected_ticker], selected_ticker, selected_sector),
        width="stretch",
    )
    st.caption("가격과 이동평균은 분할·배당을 반영한 조정종가 기준입니다.")

    with st.expander("MVP 판정 정의와 제한사항"):
        st.markdown(
            """
- **강한 주도:** 20·60일 상대수익률 양수, RS 20일 신고가, 가격이 MA20·MA50 위
- **주도 후보:** 전일 20일 상대수익률이 0 이하이고 당일 양수 전환, RS 5일 변화율 양수
- **개선 중:** 20일 상대수익률은 음수지만 5일 상대수익률이 20일 값보다 큼
- **약화:** 최근 20거래일 내 강한 주도 이력이 있고 RS가 20일선 아래이거나 최근 5일 중 4일 이상 언더퍼폼
- **약세:** 20·60일 상대수익률 음수, 가격이 MA20·MA50 아래
- **중립:** 위 조건에 해당하지 않음
- **1SIGMA 상태:** |위치| < 1 정상 범위, ±1 이상 상·하단 이탈, ±1.5 이상 큰 이탈

이 MVP는 일봉 기반 관찰 도구입니다. 점수 모델, 자동주문, 매매전략, 백테스트,
자금 배분, 옵션 GEX, 구성종목 기반 시장 폭은 포함하지 않습니다. 1SIGMA 값은
별도의 주간 예상 범위 맥락이며 기존 신호를 변경하지 않습니다.
            """
        )


if __name__ == "__main__":
    main()
