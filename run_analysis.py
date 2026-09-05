"""Command-line entry point suitable for Windows Task Scheduler."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import pandas as pd

from pipeline import DEFAULT_CONFIG_PATH, run_pipeline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="미국 ETF 섹터 상대강도를 계산하고 일별 CSV에 저장합니다."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="config.yaml 경로",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="계산만 하고 CSV에는 저장하지 않습니다.",
    )
    return parser.parse_args()


def main() -> int:
    # Keep Korean output readable in modern Windows terminals and redirected logs.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    args = parse_args()
    try:
        result = run_pipeline(args.config, save=not args.no_save)
    except Exception as exc:
        print(f"분석 실패: {exc}", file=sys.stderr)
        return 1

    columns = [
        "rank",
        "sector_name",
        "etf",
        "relative_return_5d",
        "relative_return_20d",
        "relative_return_60d",
        "sigma_zscore",
        "sigma_status",
        "signal",
    ]
    printable = result.current[columns].copy()
    for column in ("relative_return_5d", "relative_return_20d", "relative_return_60d"):
        printable[column] = printable[column].map(lambda value: f"{value:+.2%}")
    printable["sigma_zscore"] = printable["sigma_zscore"].map(
        lambda value: "N/A" if pd.isna(value) else f"{float(value):+.2f}σ"
    )
    print(f"분석 기준일: {result.analysis_date} | 공급자: {result.provider}")
    print(printable.to_string(index=False))
    sigma = result.sigma
    print(
        "1SIGMA 보조정보: "
        f"{sigma.get('status', '비활성')} "
        f"({sigma.get('coverage', 0)}/{sigma.get('expected', len(result.current))}개)"
    )
    if result.issues:
        print("\n제외/오류 종목:")
        for ticker, message in result.issues.items():
            print(f"- {ticker}: {message}")
    for warning in result.warnings:
        print(f"경고: {warning}", file=sys.stderr)
    if result.saved_path:
        print(f"\nCSV 저장: {result.saved_path}")
    if result.storage_error:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
