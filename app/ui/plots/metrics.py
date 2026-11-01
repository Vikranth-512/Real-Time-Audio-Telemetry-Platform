"""
metrics.py
----------
METRIC HISTORY — Rolling multi-metric line chart.

Split into two stacked plots:
  - Top: RMS and Peak
  - Bottom: ZCR (with its own scaled Y-axis)

Receives a metrics dict from AnalyzerWorker.new_metrics and scrolls
the last N seconds.
"""

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Slot
from PySide6.QtWidgets import QWidget, QVBoxLayout
from collections import deque

BUFFER_SIZE = 300  # ~20 seconds at 15 Hz emission rate

# Instrument palette
_BG    = "#080b12"
_AXIS  = "#2d4060"
_LABEL = "#3d5570"
_RMS   = "#00b4d8"
_PEAK  = "#ff6b6b"
_ZCR   = "#7dd4a0"


class MetricsPlotWidget(QWidget):
    """Stacked scrolling history charts."""

    def __init__(self, parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # ── 1. RMS / Peak Plot (Top) ──────────────────────────────────────────
        self._plot_amp = pg.PlotWidget()
        self._plot_amp.setBackground(_BG)

        self._plot_amp.setLabel("left", "Amplitude", color=_LABEL)
        self._plot_amp.getAxis("left").setTextPen(_AXIS)
        self._plot_amp.getAxis("left").setPen(_AXIS)
        self._plot_amp.getAxis("bottom").setPen(_AXIS)
        self._plot_amp.showAxis("top", show=False)
        self._plot_amp.showAxis("right", show=False)
        self._plot_amp.getAxis("bottom").setStyle(showValues=False)

        self._plot_amp.setYRange(0, 1.0, padding=0.04)
        self._plot_amp.enableAutoRange(axis="y", enable=False)
        self._plot_amp.showGrid(x=False, y=True, alpha=0.08)

        legend_amp = self._plot_amp.addLegend(offset=(10, 6))
        legend_amp.setBrush(pg.mkBrush(8, 11, 18, 200))
        legend_amp.setPen(pg.mkPen("#1a2535"))

        # ── 2. ZCR Plot (Bottom) ──────────────────────────────────────────────
        self._plot_zcr = pg.PlotWidget()
        self._plot_zcr.setBackground(_BG)

        self._plot_zcr.setLabel("left", "ZCR", color=_LABEL)
        self._plot_zcr.getAxis("left").setTextPen(_AXIS)
        self._plot_zcr.getAxis("left").setPen(_AXIS)
        self._plot_zcr.getAxis("bottom").setPen(_AXIS)
        self._plot_zcr.showAxis("top", show=False)
        self._plot_zcr.showAxis("right", show=False)
        self._plot_zcr.getAxis("bottom").setStyle(showValues=False)

        # ZCR typically stays low (0.0 to 0.1 for most speech/music)
        # We allow it to auto-range slightly if it goes high, but start constrained
        self._plot_zcr.setYRange(0, 0.1, padding=0.04)
        self._plot_zcr.enableAutoRange(axis="y", enable=True)
        self._plot_zcr.showGrid(x=False, y=True, alpha=0.08)

        legend_zcr = self._plot_zcr.addLegend(offset=(10, 6))
        legend_zcr.setBrush(pg.mkBrush(8, 11, 18, 200))
        legend_zcr.setPen(pg.mkPen("#1a2535"))

        # ── Buffers ───────────────────────────────────────────────────────────
        self._rms_buf  = deque([0.0] * BUFFER_SIZE, maxlen=BUFFER_SIZE)
        self._peak_buf = deque([0.0] * BUFFER_SIZE, maxlen=BUFFER_SIZE)
        self._zcr_buf  = deque([0.0] * BUFFER_SIZE, maxlen=BUFFER_SIZE)
        self._x        = np.arange(BUFFER_SIZE, dtype=np.float32)

        # ── Curves ────────────────────────────────────────────────────────────
        self._rms_curve = self._plot_amp.plot(
            name="RMS",
            pen=pg.mkPen(_RMS, width=1.5),
            antialias=False,
        )
        self._peak_curve = self._plot_amp.plot(
            name="Peak",
            pen=pg.mkPen(_PEAK, width=1.2),
            antialias=False,
        )
        self._zcr_curve = self._plot_zcr.plot(
            name="ZCR",
            pen=pg.mkPen(_ZCR, width=1.2),
            antialias=False,
        )

        layout.addWidget(self._plot_amp, stretch=2)
        layout.addWidget(self._plot_zcr, stretch=1)

    # ── Slot connected to AnalyzerWorker.new_metrics ──────────────────────────

    @Slot(dict)
    def update_metrics(self, metrics: dict) -> None:
        if not metrics:
            return
        self._rms_buf.append(float(metrics.get("rms", 0.0)))
        self._peak_buf.append(float(metrics.get("peak", 0.0)))
        self._zcr_buf.append(float(metrics.get("zcr", 0.0)))

        self._rms_curve.setData( x=self._x, y=np.array(self._rms_buf,  dtype=np.float32))
        self._peak_curve.setData(x=self._x, y=np.array(self._peak_buf, dtype=np.float32))
        self._zcr_curve.setData( x=self._x, y=np.array(self._zcr_buf,  dtype=np.float32))
