"""
parquet_writer.py
-----------------
QObject worker that batches incoming metrics dicts from a queue and writes
them to per-session Parquet files using PyArrow.

Design:
  - Runs on its own QThread — never touches the GUI thread.
  - Consumes from storage_queue: {"timestamp": float, "metrics": dict}
  - Buffers rows in memory (max_buffer_size) and flushes on a timer OR when
    the buffer is full, whichever comes first.
  - Preserves the same column schema as the existing raw_storage.py / PostgreSQL
    model so Parquet files from both old and new pipelines are compatible.
  - On stop(), performs a forced final flush before closing the writer.

Schema (matches models.py AudioMetric):
    timestamp, session_id, rms, peak, bpm, zcr, frequency,
    peak_frequency, spectral_centroid, spectral_rolloff, spectral_flatness
"""

import queue
import logging
import time
import uuid
from pathlib import Path
from typing import Optional

import pyarrow as pa
import pyarrow.parquet as pq
import pandas as pd

from PySide6.QtCore import QObject, Slot, QTimer

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────
DATA_DIR        = Path("data") / "sessions"
MAX_BUFFER_SIZE = 500        # rows before forced flush
FLUSH_INTERVAL  = 10_000     # ms between timed flushes

# ── PyArrow schema — matches existing PostgreSQL model column names ────────────
PARQUET_SCHEMA = pa.schema([
    pa.field("timestamp",         pa.float64()),
    pa.field("session_id",        pa.string()),
    pa.field("rms",               pa.float32()),
    pa.field("peak",              pa.float32()),
    pa.field("bpm",               pa.float32()),
    pa.field("zcr",               pa.float32()),
    pa.field("frequency",         pa.float32()),
    pa.field("peak_frequency",    pa.float32()),
    pa.field("spectral_centroid", pa.float32()),
    pa.field("spectral_rolloff",  pa.float32()),
    pa.field("spectral_flatness", pa.float32()),
])


class ParquetWriter(QObject):
    """
    Batching Parquet writer running on a dedicated QThread.

    The session_id is generated at start() and remains fixed for the lifetime
    of a single recording session. Call stop() to flush and close the file.
    """

    def __init__(
        self,
        storage_queue: queue.Queue,
        data_dir: Path = DATA_DIR,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._queue    = storage_queue
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)

        self._session_id: Optional[str]          = None
        self._file_path:  Optional[Path]         = None
        self._writer:     Optional[pq.ParquetWriter] = None
        self._buffer:     list[dict]             = []
        self._last_flush: float                  = 0.0

        # Timers are created inside start_writing() on the worker thread.
        # Creating them in __init__ (GUI thread) then calling start() on the
        # worker thread would cause them to never fire.
        self._poll_timer:  Optional[QTimer] = None
        self._flush_timer: Optional[QTimer] = None

    def set_session_context(self, session_id: str, directory: Path) -> None:
        """Inject the session identity before the worker thread starts."""
        self._session_id = session_id
        self._file_path = Path(directory) / "data.parquet"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @Slot()
    def start_writing(self) -> None:
        """Called by QThread.started — creates a new session file."""
        if not self._session_id or not self._file_path:
            # Fallback if set_session_context was not called (e.g., legacy tests)
            self._session_id = str(uuid.uuid4())
            self._file_path  = self._data_dir / f"session_{self._session_id}.parquet"
            
        self._writer     = pq.ParquetWriter(str(self._file_path), PARQUET_SCHEMA)
        self._buffer.clear()
        self._last_flush = time.monotonic()

        # Create timers HERE on the worker thread so their event loop owns them.
        self._poll_timer = QTimer()
        self._poll_timer.setInterval(200)  # drain every 200 ms
        self._poll_timer.timeout.connect(self._drain_queue)

        self._flush_timer = QTimer()
        self._flush_timer.setInterval(FLUSH_INTERVAL)
        self._flush_timer.timeout.connect(self._timed_flush)

        self._poll_timer.start()
        self._flush_timer.start()
        logger.info(f"ParquetWriter started — session={self._session_id}, file={self._file_path}")

    @Slot()
    def stop_writing(self) -> None:
        """Flush remaining rows and close the writer cleanly."""
        if self._poll_timer is not None:
            self._poll_timer.stop()
        if self._flush_timer is not None:
            self._flush_timer.stop()
        self._drain_queue()   # final drain
        self._flush(force=True)
        self._close_writer()
        logger.info(f"ParquetWriter stopped — {len(self._buffer)} rows remaining at close")

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def file_path(self) -> Optional[Path]:
        return self._file_path

    # ── Internal queue drain ──────────────────────────────────────────────────

    @Slot()
    def _drain_queue(self) -> None:
        try:
            while True:
                item = self._queue.get_nowait()
                self._buffer.append(self._normalize_row(item))
                if len(self._buffer) >= MAX_BUFFER_SIZE:
                    self._flush()
        except queue.Empty:
            pass

    @Slot()
    def _timed_flush(self) -> None:
        if self._buffer:
            self._flush()

    def _flush(self, force: bool = False) -> None:
        if not self._buffer or self._writer is None:
            return
        rows       = self._buffer
        self._buffer = []
        try:
            df    = pd.DataFrame(rows)
            table = pa.Table.from_pandas(df, schema=PARQUET_SCHEMA)
            self._writer.write_table(table)
            logger.debug(f"Parquet flush: {len(rows)} rows → {self._file_path.name}")
        except Exception as exc:
            logger.error(f"Parquet write error: {exc}", exc_info=True)
            # Put rows back to avoid data loss on transient errors
            self._buffer = rows + self._buffer

    def _close_writer(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                if self._file_path and self._file_path.exists():
                    size_mb = self._file_path.stat().st_size / (1024 * 1024)
                    logger.info(
                        f"Parquet file closed: {self._file_path.name} ({size_mb:.2f} MB)"
                    )
            except Exception as exc:
                logger.error(f"Failed to close ParquetWriter: {exc}")
            finally:
                self._writer = None

    # ── Row normalisation — maps MetricsEngine output to schema columns ───────

    def _normalize_row(self, item: dict) -> dict:
        metrics = item.get("metrics", {})
        return {
            "timestamp":         float(item.get("timestamp", 0.0)),
            "session_id":        self._session_id or "",
            "rms":               float(metrics.get("rms",               0.0)),
            "peak":              float(metrics.get("peak",              0.0)),
            "bpm":               float(metrics.get("bpm",               0.0)),
            "zcr":               float(metrics.get("zcr",               0.0)),
            "frequency":         float(metrics.get("frequency",         0.0)),
            "peak_frequency":    float(metrics.get("peak_frequency",    0.0)),
            "spectral_centroid": float(metrics.get("spectral_centroid", 0.0)),
            "spectral_rolloff":  float(metrics.get("spectral_rolloff",  0.0)),
            "spectral_flatness": float(metrics.get("spectral_flatness", 0.0)),
        }
