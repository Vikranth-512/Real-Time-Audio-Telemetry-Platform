"""
waveform.py
-----------
Real-time TIME DOMAIN display using PyQtGraph PlotCurveItem.

X-axis is calibrated in milliseconds from the audio constants so the user
reads time directly instead of sample indices. Grid is subtle: very faint
major lines, prominent zero reference, no minor decoration.

Design rules:
  - One persistent PlotCurveItem and one InfiniteLine — never recreated.
  - PlotCurveItem.setData() is the only per-frame call.
  - No signal processing in this widget.
"""

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import QWidget, QVBoxLayout

# Read real constants from the acquisition layer — no magic numbers here.
from app.acquisition.audio_source import SAMPLE_RATE, PACKET_SIZE

# Buffer size must match AnalyzerWorker._WAVEFORM_BUF
_WAVEFORM_SAMPLES = 12_000
# Duration in milliseconds that one full buffer represents
_DURATION_MS = (_WAVEFORM_SAMPLES / SAMPLE_RATE) * 1000.0  # 250.0 ms at 48 kHz

# Instrument palette
_BG      = "#080b12"
_AXIS    = "#2d4060"
_CURVE   = "#00b4d8"
_ZERO    = "#1c3050"
_LABEL   = "#3d5570"


class WaveformWidget(QWidget):
    """Scrolling oscilloscope trace — x-axis in milliseconds."""

    def __init__(self, parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # ── Plot widget ───────────────────────────────────────────────────────
        self._plot = pg.PlotWidget()
        self._plot.setBackground(_BG)

        # Title
        title_item = pg.LabelItem(
            "TIME DOMAIN",
            color="#3d5570",
            size="9pt",
        )
        self._plot.addItem(title_item)

        # Axes
        self._plot.setLabel("left",   "Amplitude", color=_LABEL)
        self._plot.setLabel("bottom", "Time (ms)", color=_LABEL)
        self._plot.getAxis("left").setTextPen(_AXIS)
        self._plot.getAxis("left").setPen(_AXIS)
        self._plot.getAxis("bottom").setTextPen(_AXIS)
        self._plot.getAxis("bottom").setPen(_AXIS)

        # Dynamic y-range tracker: nominal [-1, 1], adapts with headroom for hot signals
        self._current_y_limit = 1.0
        self._plot.setYRange(-1.0, 1.0, padding=0.04)
        self._plot.enableAutoRange(axis="y", enable=False)

        # Fixed x-range in milliseconds
        self._plot.setXRange(0.0, _DURATION_MS, padding=0)
        self._plot.enableAutoRange(axis="x", enable=False)

        # Subtle major grid only — no minor grid
        self._plot.showGrid(x=True, y=True, alpha=0.08)

        # Disable right/top axis clutter
        self._plot.showAxis("top",   show=False)
        self._plot.showAxis("right", show=False)

        # Zero reference line (slightly brighter than grid)
        zero_line = pg.InfiniteLine(
            pos=0, angle=0,
            pen=pg.mkPen(_ZERO, width=1),
            movable=False,
        )
        self._plot.addItem(zero_line)

        # Waveform curve — one persistent item
        self._curve = self._plot.plot(
            pen=pg.mkPen(_CURVE, width=1),
            antialias=False,
        )

        # Pre-compute time axis in ms — never reallocated
        self._x_ms = np.linspace(0.0, _DURATION_MS, _WAVEFORM_SAMPLES, dtype=np.float32)

        # Idle-state overlay
        self._idle_text = pg.TextItem(
            html='<div style="color:#2d4060;font-size:11pt;text-align:center">'
                 'No Signal<br>'
                 '<span style="font-size:8pt">Select a source and press Start</span>'
                 '</div>',
            anchor=(0.5, 0.5),
        )
        self._idle_text.setPos(_DURATION_MS / 2, 0)
        self._plot.addItem(self._idle_text)
        self._has_data = False

        layout.addWidget(self._plot)

    # ── Slot connected to AnalyzerWorker.new_waveform ─────────────────────────

    @Slot(object)
    def update_waveform(self, samples: np.ndarray) -> None:
        if samples is None or len(samples) == 0:
            return
        if not self._has_data:
            self._idle_text.setVisible(False)
            self._has_data = True
        n = min(len(samples), _WAVEFORM_SAMPLES)

        # Display-only dynamic autoscaling: calculate peak without modifying `samples`
        display_peak = float(np.max(np.abs(samples[:n]))) if n > 0 else 0.0
        target_limit = max(1.0, display_peak * 1.08)

        if target_limit > self._current_y_limit:
            # Fast attack: immediately expand bounds to prevent visual clipping
            self._current_y_limit = target_limit
            self._plot.setYRange(-self._current_y_limit, self._current_y_limit, padding=0.0)
        elif self._current_y_limit > 1.0:
            # Slow release: gently decay back toward normal limits without breathing
            self._current_y_limit = max(target_limit, self._current_y_limit * 0.96)
            self._plot.setYRange(-self._current_y_limit, self._current_y_limit, padding=0.0)

        self._curve.setData(x=self._x_ms[:n], y=samples[:n])

    def set_sample_rate(self, sample_rate: int) -> None:
        """Update waveform time axis based on actual sample rate."""
        self._sample_rate = int(sample_rate)
        # Exact mathematical sample time in ms: t[i] = (i / sample_rate) * 1000.0
        self._x_ms = (np.arange(_WAVEFORM_SAMPLES, dtype=np.float32) / self._sample_rate) * 1000.0
        dur_ms = (_WAVEFORM_SAMPLES / self._sample_rate) * 1000.0
        self._plot.setXRange(0.0, dur_ms, padding=0)
        self._idle_text.setPos(dur_ms / 2, 0)

    def set_idle(self) -> None:
        """Show idle state when acquisition is stopped."""
        self._has_data = False
        self._current_y_limit = 1.0
        self._plot.setYRange(-1.0, 1.0, padding=0.04)
        self._curve.setData([], [])
        self._idle_text.setVisible(True)

    def clear(self) -> None:
        """Clear the current waveform trace."""
        self._current_y_limit = 1.0
        self._plot.setYRange(-1.0, 1.0, padding=0.04)
        self._curve.setData([], [])
