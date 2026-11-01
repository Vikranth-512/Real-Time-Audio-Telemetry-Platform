"""
spectrum.py
-----------
Real-time FREQUENCY SPECTRUM display using a filled PyQtGraph PlotCurveItem.

Improvements over previous version:
  - Custom tick marks at engineering-standard frequencies (20 Hz → 20 kHz).
  - Human-readable labels: 50, 100, 200, 500, 1k, 2k, 5k, 10k, 20k.
  - No scientific notation in axis labels.
  - Dominant frequency marker: vertical dashed line + frequency annotation.
  - Thicker, higher-contrast spectrum line.
  - Idle state overlay.

No signal processing in this widget. Purely a renderer.
"""

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import QWidget, QVBoxLayout

# Instrument palette
_BG       = "#080b12"
_AXIS     = "#2d4060"
_LABEL    = "#3d5570"
_CURVE    = "#00d4aa"
_FILL     = (0, 212, 170, 25)
_MARKER   = "#ff6b35"

# Engineering-standard frequency tick positions and labels
# For log mode the tick values are passed as actual Hz — pyqtgraph converts to log space
_FREQ_TICKS: list[tuple[float, str]] = [
    (20,    "20"),
    (50,    "50"),
    (100,   "100"),
    (200,   "200"),
    (500,   "500"),
    (1_000, "1k"),
    (2_000, "2k"),
    (5_000, "5k"),
    (10_000,"10k"),
    (20_000,"20k"),
]


def _fmt_freq(hz: float) -> str:
    """Return a human-readable frequency string."""
    if hz >= 1000:
        return f"{hz / 1000:.1f} kHz"
    return f"{hz:.0f} Hz"


class SpectrumWidget(QWidget):
    """FFT spectrum — log-frequency axis with clean engineering tick marks."""

    def __init__(self, parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # ── Plot widget ───────────────────────────────────────────────────────
        self._plot = pg.PlotWidget()
        self._plot.setBackground(_BG)

        # Axes
        self._plot.setLabel("left",   "Amplitude", color=_LABEL)
        self._plot.setLabel("bottom", "Frequency  (Hz)", color=_LABEL)
        self._plot.getAxis("left").setTextPen(_AXIS)
        self._plot.getAxis("left").setPen(_AXIS)
        self._plot.getAxis("bottom").setTextPen(_AXIS)
        self._plot.getAxis("bottom").setPen(_AXIS)
        self._plot.showAxis("top",   show=False)
        self._plot.showAxis("right", show=False)

        # Log x-axis
        self._plot.setLogMode(x=True, y=False)

        # Custom tick marks in log space
        # In pyqtgraph log mode, AxisItem.setTicks() takes log10-space values
        log_major = [(np.log10(f), label) for f, label in _FREQ_TICKS]
        self._plot.getAxis("bottom").setTicks([log_major, []])
        self._plot.getAxis("bottom").setStyle(tickTextOffset=4)

        # Fixed display range: 20 Hz – 22 kHz in log10 space
        self._plot.setXRange(np.log10(20), np.log10(22_000), padding=0)
        self._plot.setYRange(0, 0.5, padding=0.04)
        self._plot.enableAutoRange(axis="x", enable=False)
        self._plot.enableAutoRange(axis="y", enable=False)

        # Subtle grid
        self._plot.showGrid(x=True, y=True, alpha=0.07)

        # Spectrum curve — slightly thicker, smooth fill
        self._curve = self._plot.plot(
            pen=pg.mkPen(_CURVE, width=1.5),
            fillLevel=0,
            brush=pg.mkBrush(_FILL),
            antialias=False,
        )

        # Dominant frequency marker — vertical dashed line
        self._marker_line = pg.InfiniteLine(
            angle=90,
            movable=False,
            pen=pg.mkPen(_MARKER, width=1, style=Qt.PenStyle.DashLine),
        )
        self._marker_line.setVisible(False)
        self._plot.addItem(self._marker_line)

        # Dominant frequency text label
        self._marker_label = pg.TextItem(
            text="",
            anchor=(0.5, 1.1),
            color=_MARKER,
        )
        self._marker_label.setVisible(False)
        self._plot.addItem(self._marker_label)

        # Idle state overlay
        self._idle_text = pg.TextItem(
            html='<div style="color:#2d4060;font-size:11pt;text-align:center">'
                 'No Signal<br>'
                 '<span style="font-size:8pt">Select a source and press Start</span>'
                 '</div>',
            anchor=(0.5, 0.5),
        )
        self._idle_text.setPos(np.log10(1_000), 0.25)
        self._plot.addItem(self._idle_text)
        self._has_data = False

        layout.addWidget(self._plot)

    # ── Slot connected to AnalyzerWorker.new_fft ──────────────────────────────

    @Slot(object, object)
    def update_spectrum(self, freqs: np.ndarray, magnitudes: np.ndarray) -> None:
        if freqs is None or magnitudes is None or len(freqs) < 2:
            return

        # Skip DC bin (index 0) — log10(0) is undefined
        f = freqs[1:]
        m = magnitudes[1:]

        if not self._has_data:
            self._idle_text.setVisible(False)
            self._has_data = True

        self._curve.setData(x=f, y=m)

        # ── Update dominant frequency marker ──────────────────────────────────
        valid = (f >= 20) & (f <= 22_000)
        if np.any(valid):
            m_valid = m[valid]
            f_valid = f[valid]
            peak_mag = float(m_valid.max())
            if peak_mag > 5e-4:  # only annotate meaningful signal
                peak_freq = float(f_valid[m_valid.argmax()])
                log_pos   = np.log10(peak_freq)
                self._marker_line.setValue(log_pos)
                self._marker_label.setPos(log_pos, peak_mag)
                self._marker_label.setText(_fmt_freq(peak_freq))
                self._marker_line.setVisible(True)
                self._marker_label.setVisible(True)
                return
        self._marker_line.setVisible(False)
        self._marker_label.setVisible(False)

    def set_sample_rate(self, sample_rate: int) -> None:
        """Update spectrum display range to Nyquist frequency."""
        self._sample_rate = int(sample_rate)
        nyquist = self._sample_rate / 2.0
        self._plot.setXRange(np.log10(20), np.log10(nyquist), padding=0)

    def set_idle(self) -> None:
        """Show idle state when acquisition is stopped."""
        self._has_data = False
        self._curve.setData([], [])
        self._marker_line.setVisible(False)
        self._marker_label.setVisible(False)
        self._idle_text.setVisible(True)

    def clear(self) -> None:
        """Clear the current spectrum curve."""
        self._curve.setData([], [])
        self._marker_line.setVisible(False)
        self._marker_label.setVisible(False)
