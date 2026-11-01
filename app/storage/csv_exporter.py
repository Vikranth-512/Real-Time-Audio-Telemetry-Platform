"""
csv_exporter.py
---------------
Reads a completed session Parquet file and exports it as a CSV.
The CSV schema matches the existing App.jsx handleExportSession() column
layout so existing Tableau workbooks / spreadsheets remain compatible.

This is a pure utility function — no QThread needed since it is triggered
on demand (e.g. a menu action or button click) and runs in the GUI thread
for files small enough to process synchronously, or can be wrapped in a
QThread for large sessions.

CSV columns (per-second bucketed, matching existing export format):
    timestamp, rms, amplitude, bpm, frequency, zcr,
    rolling_mean_rms, rolling_variance, rolling_peak_amplitude
"""

import logging
from pathlib import Path
from typing import Union

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

ROLLING_WINDOW = 30  # seconds

def export_raw_session_csv(
    parquet_path: Union[str, Path], 
    output_path: Union[str, Path, None] = None,
    progress_callback=None
) -> Path:
    """
    Reads a finalized Parquet file in bounded row-group chunks and writes 
    complete unaggregated measurement records to CSV.
    Preserves exact stored column names, row values, timestamps, and row ordering 
    without resampling, bucketing, rolling averages, or recalculation.
    """
    parquet_path = Path(parquet_path)
    if not parquet_path.exists():
        raise FileNotFoundError(f"Parquet file not found: {parquet_path}")

    if output_path is None:
        output_path = parquet_path.with_suffix(".csv")
    output_path = Path(output_path)

    parquet_file = pq.ParquetFile(str(parquet_path))
    total_row_groups = parquet_file.num_row_groups
    
    if total_row_groups == 0:
        logger.warning(f"No data in {parquet_path.name} — CSV not written")
        if progress_callback:
            progress_callback(100)
        return output_path

    # Process row groups iteratively to bound memory
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        for i in range(total_row_groups):
            table = parquet_file.read_row_group(i)
            df = table.to_pandas()
            
            # Write header only for the first row group
            df.to_csv(f, index=False, header=(i == 0))
            
            if progress_callback:
                progress = int(((i + 1) / total_row_groups) * 100)
                progress_callback(progress)

    size_kb = output_path.stat().st_size / 1024
    logger.info(f"Raw CSV exported: {output_path.name} ({size_kb:.1f} KB)")
    return output_path



def export_session_csv(parquet_path: Union[str, Path], output_path: Union[str, Path, None] = None) -> Path:
    """
    Read a session Parquet file and write a CSV export.

    Args:
        parquet_path:  Path to the .parquet session file.
        output_path:   Destination CSV path. Defaults to same directory with .csv extension.

    Returns:
        Path to the written CSV file.
    """
    parquet_path = Path(parquet_path)
    if not parquet_path.exists():
        raise FileNotFoundError(f"Parquet file not found: {parquet_path}")

    if output_path is None:
        output_path = parquet_path.with_suffix(".csv")
    output_path = Path(output_path)

    # Load the Parquet file
    table = pq.read_table(str(parquet_path))
    df = table.to_pandas()

    if df.empty:
        logger.warning(f"No data in {parquet_path.name} — CSV not written")
        return output_path

    # ── Bucket into 1-second intervals (matching original App.jsx logic) ──────
    df["bucket"] = df["timestamp"].astype(int)
    per_sec = (
        df.groupby("bucket")
        .agg(
            rms=("rms", "mean"),
            amplitude=("peak", "mean"),
            bpm=("bpm", "mean"),
            frequency=("frequency", "mean"),
            zcr=("zcr", "mean"),
        )
        .reset_index()
        .rename(columns={"bucket": "timestamp"})
        .sort_values("timestamp")
    )

    # ── Rolling statistics (window = ROLLING_WINDOW rows = ~30 seconds) ───────
    per_sec["rolling_mean_rms"] = (
        per_sec["rms"].rolling(window=ROLLING_WINDOW, min_periods=1).mean()
    )
    per_sec["rolling_variance"] = (
        per_sec["rms"].rolling(window=ROLLING_WINDOW, min_periods=1).var().fillna(0.0)
    )
    per_sec["rolling_peak_amplitude"] = (
        per_sec["amplitude"].rolling(window=ROLLING_WINDOW, min_periods=1).max()
    )

    # ── Round to match original precision ─────────────────────────────────────
    per_sec = per_sec.round({
        "rms": 4, "amplitude": 4, "bpm": 1, "frequency": 2, "zcr": 4,
        "rolling_mean_rms": 4, "rolling_variance": 6, "rolling_peak_amplitude": 3,
    })

    # ── Write CSV ─────────────────────────────────────────────────────────────
    columns = [
        "timestamp", "rms", "amplitude", "bpm", "frequency", "zcr",
        "rolling_mean_rms", "rolling_variance", "rolling_peak_amplitude",
    ]
    per_sec[columns].to_csv(output_path, index=False)

    size_kb = output_path.stat().st_size / 1024
    logger.info(f"CSV exported: {output_path.name} ({size_kb:.1f} KB, {len(per_sec)} rows)")
    return output_path
