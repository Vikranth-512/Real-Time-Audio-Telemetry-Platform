"""
analyzer_worker.py
------------------
QObject worker that consumes (timestamp, frame) tuples from the capture queue,
runs MetricsEngine, computes the FFT visualization buffer, and emits a Qt signal
with the combined result for the GUI to render.

Threading model:
  - This object is moved to a QThread by MainWindow at startup.
  - It never blocks the GUI thread.
  - It communicates with the GUI exclusively via Qt signals.
  - It communicates with StorageWorker by writing to a second queue.

Processing rate: ~46.875 frames/sec (48000 / 1024)
GUI signal rate: decoupled — the GUI timer decides how often to re-render.
"""

import queue
import logging
import time
import numpy as np
from typing import Optional

from PySide6.QtCore import QObject, Signal, Slot, QTimer

# Import the existing MetricsEngine without modifying its mathematics.
# If it has infrastructure-specific dependencies (Redis, async), those are
# isolated here in the adapter layer rather than changed in the engine itself.
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'backend'))
from processing.metrics_engine import MetricsEngine

from app.acquisition.audio_source import SAMPLE_RATE, PACKET_SIZE

logger = logging.getLogger(__name__)

# ── FFT configuration ─────────────────────────────────────────────────────────
FFT_SIZE      = 1_024   # must be <= PACKET_SIZE
FFT_BINS      = FFT_SIZE // 2
WINDOW        = np.hanning(FFT_SIZE).astype(np.float32)

# ── GUI update rates ──────────────────────────────────────────────────────────
# The worker processes every frame (~46.875 Hz). The GUI timer polls at these
# reduced rates to avoid a render backlog.
METRICS_GUI_HZ  = 15    # RMS / peak / BPM etc.
WAVEFORM_GUI_HZ = 30    # raw waveform
FFT_GUI_HZ      = 20    # FFT spectrum


class AnalyzerWorker(QObject):
    """
    Wraps MetricsEngine as a QObject worker, emitting signals for the GUI.

    Signals:
        new_metrics(dict)  — scalar metrics at METRICS_GUI_HZ
        new_waveform(np.ndarray)  — raw float32 frame at WAVEFORM_GUI_HZ
        new_fft(np.ndarray, np.ndarray)  — (freqs_hz, magnitudes) at FFT_GUI_HZ
    """

    new_metrics  = Signal(dict)
    new_waveform = Signal(object)        # np.ndarray passed as Python object
    new_fft      = Signal(object, object) # (freqs, magnitudes)
    drain_complete = Signal()
    reset_complete = Signal()
    error_occurred = Signal(str)

    def __init__(
        self,
        capture_queue: queue.Queue,
        storage_queue: Optional[queue.Queue] = None,
        sample_rate: int = SAMPLE_RATE,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._capture_queue = capture_queue
        self._storage_queue = storage_queue
        self._sample_rate   = int(sample_rate)

        # Adapter: instantiate the engine using its existing interface.
        # The engine's calculate_metrics() accepts a dict with 'samples' and
        # 'sample_rate' keys — we build that dict here rather than changing the engine.
        self._engine = MetricsEngine(sample_rate=self._sample_rate)

        # Waveform ring buffer for the GUI (~0.25 s at 48 kHz)
        self._WAVEFORM_BUF = 12_000
        self._waveform_buf  = np.zeros(self._WAVEFORM_BUF, dtype=np.float32)
        self._waveform_pos  = 0

        # GUI throttle timestamps
        self._last_metrics_emit  = 0.0
        self._last_waveform_emit = 0.0
        self._last_fft_emit      = 0.0

        # Freq axis for FFT (computed once, updated on set_sample_rate)
        self._freq_axis = np.fft.rfftfreq(FFT_SIZE, d=1.0 / self._sample_rate).astype(np.float32)

        # Poll timer — created in start_processing() on the worker thread.
        # DO NOT create it here: QTimer created in __init__ is owned by the
        # GUI thread and will never fire after moveToThread().
        self._poll_timer: Optional[QTimer] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @Slot(int)
    def set_sample_rate(self, sample_rate: int) -> None:
        """Configure sample rate. Invariant: worker must be stopped."""
        if self._poll_timer is not None and self._poll_timer.isActive():
            raise RuntimeError("Cannot change sample rate while AnalyzerWorker is actively processing.")
        self._sample_rate = int(sample_rate)
        self._freq_axis = np.fft.rfftfreq(FFT_SIZE, d=1.0 / self._sample_rate).astype(np.float32)
        self._engine.set_sample_rate(self._sample_rate)
        logger.info(f"AnalyzerWorker sample rate configured to {self._sample_rate} Hz")

    @Slot()
    def request_reset(self) -> None:
        """Thread-safe barrier: drains pending input queue, clears buffers, and resets engine."""
        try:
            while True:
                self._capture_queue.get_nowait()
        except queue.Empty:
            pass
        self._waveform_buf.fill(0.0)
        self._waveform_pos = 0
        self._engine.reset()
        self.reset_complete.emit()
        logger.debug("AnalyzerWorker reset completed")

    @Slot()
    def request_drain(self) -> None:
        """Processes all remaining in-flight frames, flushes to storage, and signals completion."""
        try:
            while True:
                capture_time, frame = self._capture_queue.get_nowait()
                try:
                    self._process_frame(capture_time, frame)
                except Exception as exc:
                    logger.exception(f"Error processing analyzer frame during drain at {capture_time}: {exc}")
                    self.error_occurred.emit(str(exc))
        except queue.Empty:
            pass
        self.drain_complete.emit()
        logger.debug("AnalyzerWorker drain completed")

    @Slot()
    def start_processing(self) -> None:
        """Called by the QThread after the worker has been moved to it."""
        # Create the QTimer HERE, on the worker thread, so its event loop
        # owns it and it fires correctly.
        self._poll_timer = QTimer()
        self._poll_timer.setInterval(1)  # 1 ms; real pace set by queue depth
        self._poll_timer.timeout.connect(self._process_pending)
        self._poll_timer.start()
        logger.info("AnalyzerWorker started")

    @Slot()
    def stop_processing(self) -> None:
        if self._poll_timer is not None:
            self._poll_timer.stop()
        logger.info("AnalyzerWorker stopped")

    # ── Processing loop ───────────────────────────────────────────────────────

    @Slot()
    def _process_pending(self) -> None:
        """Drain as many frames as are available without blocking."""
        try:
            while True:
                capture_time, frame = self._capture_queue.get_nowait()
                try:
                    self._process_frame(capture_time, frame)
                except Exception as exc:
                    logger.exception(f"Error processing analyzer frame at {capture_time}: {exc}")
                    self.error_occurred.emit(str(exc))
        except queue.Empty:
            pass

    def _process_frame(self, capture_time: float, frame: np.ndarray) -> None:
        if frame is None or len(frame) == 0:
            return

        now = time.monotonic()

        # ── 1. Metrics ────────────────────────────────────────────────────────
        # Adapter: engine expects (samples: List[float], timestamp: float).
        # We pass original frame unchanged to operate on the actual received frame.
        try:
            metrics: dict = self._engine.calculate_metrics(
                frame.tolist(), capture_time
            )
        except Exception as exc:
            logger.warning(f"MetricsEngine error: {exc}")
            metrics = {}

        # ── 2. FFT (reuse from engine if available, else compute here) ────────
        # If the engine already computed spectral data, prefer its result.
        fft_magnitudes = metrics.get("fft_magnitudes")
        freq_axis = self._freq_axis

        if fft_magnitudes is None:
            # Deterministic windowing: zero-pad short frames for FFT visualization only
            if len(frame) < FFT_SIZE:
                padded = np.pad(frame, (0, FFT_SIZE - len(frame)), mode="constant")
            else:
                padded = frame[:FFT_SIZE]
            windowed = padded * WINDOW
            spectrum = np.fft.rfft(windowed)
            fft_magnitudes = (np.abs(spectrum) / FFT_SIZE).astype(np.float32)

        # ── 3. Update waveform ring buffer ────────────────────────────────────
        n = len(frame)
        end = self._waveform_pos + n
        if end <= self._WAVEFORM_BUF:
            self._waveform_buf[self._waveform_pos:end] = frame
        else:
            first = self._WAVEFORM_BUF - self._waveform_pos
            self._waveform_buf[self._waveform_pos:] = frame[:first]
            self._waveform_buf[:n - first] = frame[first:]
        self._waveform_pos = end % self._WAVEFORM_BUF

        # ── 4. Forward to storage queue ───────────────────────────────────────
        if self._storage_queue is not None and metrics:
            try:
                self._storage_queue.put_nowait({
                    "timestamp": capture_time,
                    "metrics":   metrics,
                })
            except queue.Full:
                pass   # storage can lag without affecting processing

        # ── 5. Emit GUI signals at their respective throttled rates ───────────
        if now - self._last_metrics_emit >= 1.0 / METRICS_GUI_HZ:
            self._last_metrics_emit = now
            self.new_metrics.emit(metrics)

        if now - self._last_waveform_emit >= 1.0 / WAVEFORM_GUI_HZ:
            self._last_waveform_emit = now
            # Emit a contiguous view starting from the write head
            view = np.roll(self._waveform_buf, -self._waveform_pos).copy()
            self.new_waveform.emit(view)

        if now - self._last_fft_emit >= 1.0 / FFT_GUI_HZ:
            self._last_fft_emit = now
            self.new_fft.emit(freq_axis, fft_magnitudes)
