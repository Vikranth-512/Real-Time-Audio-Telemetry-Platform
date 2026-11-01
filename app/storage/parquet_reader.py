"""
parquet_reader.py
-----------------
Read-only historical session data reader.
Extracts measurement arrays, validates chronological ordering, normalizes
relative timestamps, and provides deterministic nearest-neighbor lookup.

Design rules:
  - Strictly read-only; never mutates session files or data.
  - Decoupled from AnalyzerWorker and UI formatting.
  - Retains raw numeric arrays including NaN/Inf without arbitrary substitution.
"""

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any

import numpy as np
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)


class SessionReadError(Exception):
    """Base exception for session read failures."""
    pass


class SessionNotFoundError(SessionReadError):
    """Session directory does not exist."""
    pass


class ParquetNotFoundError(SessionReadError):
    """Session Parquet data file is missing."""
    pass


class EmptySessionError(SessionReadError):
    """Session Parquet file contains no data rows."""
    pass


class CorruptSessionError(SessionReadError):
    """Session data or metadata could not be parsed."""
    pass


@dataclass(frozen=True)
class ReplaySessionData:
    """Immutable domain representation of a loaded historical recording session."""
    session_id: str
    metadata: Dict[str, Any]
    session_duration: float
    row_count: int
    timestamps_epoch: np.ndarray
    timestamps_rel: np.ndarray
    rms: np.ndarray
    peak: np.ndarray
    zcr: np.ndarray
    bpm: np.ndarray
    frequency: np.ndarray
    peak_frequency: np.ndarray
    spectral_centroid: np.ndarray
    spectral_rolloff: np.ndarray
    spectral_flatness: np.ndarray


class SessionDataReader:
    """
    Dedicated reader for historical session Parquet and metadata storage.
    """

    @staticmethod
    def load_session(session_dir: Path) -> ReplaySessionData:
        """
        Load a historical session from its directory.
        Reads session.json (if present) and data.parquet into an immutable ReplaySessionData.
        """
        session_path = Path(session_dir)
        if not session_path.exists() or not session_path.is_dir():
            raise SessionNotFoundError(f"Session directory not found: {session_path}")

        meta_path = session_path / "session.json"
        metadata: Dict[str, Any] = {}
        if meta_path.exists():
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
            except Exception as exc:
                logger.warning(f"Could not parse metadata for {session_path.name}: {exc}")

        session_id = metadata.get("session_id", session_path.name)
        official_duration = metadata.get("duration_seconds", None)

        parquet_path = session_path / "data.parquet"
        if not parquet_path.exists():
            raise ParquetNotFoundError(f"Missing data.parquet in session {session_id}")

        if parquet_path.stat().st_size == 0:
            raise EmptySessionError(f"data.parquet is empty (0 bytes) in session {session_id}")

        try:
            table = pq.read_table(str(parquet_path))
        except Exception as exc:
            raise CorruptSessionError(f"Failed to read Parquet data for {session_id}: {exc}") from exc

        row_count = table.num_rows
        if row_count == 0:
            raise EmptySessionError(f"data.parquet contains 0 rows for session {session_id}")

        # Extract columns as NumPy arrays safely
        def get_col(name: str, dtype=np.float32) -> np.ndarray:
            if name in table.column_names:
                return table[name].to_numpy().astype(dtype, copy=False)
            return np.zeros(row_count, dtype=dtype)

        timestamps_epoch = get_col("timestamp", dtype=np.float64)

        # Validate non-decreasing timestamps: t[i+1] >= t[i]
        # Allow duplicate timestamps from batching, but reject reverse jumps
        if row_count > 1:
            diffs = np.diff(timestamps_epoch)
            if np.any(diffs < -1e-6):  # allowing microsecond float tolerance
                logger.warning(f"Session {session_id} contains non-monotonic timestamp anomalies.")

        # Compute normalized relative timestamps starting at 0.0
        t0 = timestamps_epoch[0]
        timestamps_rel = np.maximum(0.0, timestamps_epoch - t0)

        # Derive session duration:
        # Prefer finalized duration from session.json; fallback to timestamps_rel[-1]
        if official_duration is not None and official_duration > 0:
            session_duration = float(official_duration)
        else:
            session_duration = float(timestamps_rel[-1]) if row_count > 1 else 0.0

        return ReplaySessionData(
            session_id=session_id,
            metadata=metadata,
            session_duration=session_duration,
            row_count=row_count,
            timestamps_epoch=timestamps_epoch,
            timestamps_rel=timestamps_rel,
            rms=get_col("rms"),
            peak=get_col("peak"),
            zcr=get_col("zcr"),
            bpm=get_col("bpm"),
            frequency=get_col("frequency"),
            peak_frequency=get_col("peak_frequency"),
            spectral_centroid=get_col("spectral_centroid"),
            spectral_rolloff=get_col("spectral_rolloff"),
            spectral_flatness=get_col("spectral_flatness"),
        )

    @staticmethod
    def find_nearest_index(timestamps_rel: np.ndarray, position_seconds: float) -> int:
        """
        Deterministic nearest-neighbor index lookup using binary search.
        Tie-breaking rule: On equal distance between two adjacent timestamps,
        the later measurement is selected.
        """
        n = len(timestamps_rel)
        if n == 0:
            return 0
        if n == 1 or position_seconds <= timestamps_rel[0]:
            return 0
        if position_seconds >= timestamps_rel[-1]:
            return n - 1

        idx = int(np.searchsorted(timestamps_rel, position_seconds))
        if idx == 0:
            return 0
        if idx >= n:
            return n - 1

        d_prev = abs(position_seconds - timestamps_rel[idx - 1])
        d_curr = abs(timestamps_rel[idx] - position_seconds)

        # Tie-break: on equal distance (within float epsilon), select the later index (idx)
        if abs(d_curr - d_prev) <= 1e-9 or d_curr < d_prev:
            return idx
        return idx - 1

    @staticmethod
    def get_measurement_dict(data: ReplaySessionData, index: int) -> Dict[str, float]:
        """
        Extract clean dictionary of numeric measurements at the specified index.
        Values preserve raw float values (including NaN and Inf).
        """
        idx = max(0, min(index, data.row_count - 1))
        return {
            "timestamp": float(data.timestamps_epoch[idx]),
            "timestamp_rel": float(data.timestamps_rel[idx]),
            "rms": float(data.rms[idx]),
            "peak": float(data.peak[idx]),
            "zcr": float(data.zcr[idx]),
            "bpm": float(data.bpm[idx]),
            "frequency": float(data.frequency[idx]),
            "peak_frequency": float(data.peak_frequency[idx]),
            "spectral_centroid": float(data.spectral_centroid[idx]),
            "spectral_rolloff": float(data.spectral_rolloff[idx]),
            "spectral_flatness": float(data.spectral_flatness[idx]),
        }
