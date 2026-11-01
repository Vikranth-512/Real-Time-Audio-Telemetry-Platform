"""
metrics_panel.py
----------------
Instrument-style numeric readout panel.

Cards are grouped into three logical sections:
  PRIMARY       RMS  |  PEAK  |  DOMINANT FREQUENCY
  SPECTRAL      CENTROID  |  ROLLOFF  |  FLATNESS
  TEMPORAL      ZCR  |  BPM

Design:
  - Distinct card background and subtle border to establish hierarchy.
  - Strong label (small uppercase), large bold value, small unit.
  - Left-aligned content within cards for a structured feel.
"""

import math
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QFrame, QSizePolicy, QGridLayout
)
from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QFont

# ── Palette ───────────────────────────────────────────────────────────────────
_LABEL    = "#8b9eb5"
_VALUE    = "#ffffff"
_UNIT     = "#5e7790"
_DIVIDER  = "#1a2535"
_GROUP_HDR = "#5e7790"

# ── Tooltip copy ──────────────────────────────────────────────────────────────
_TOOLTIPS: dict[str, str] = {
    "rms":
        "<b>RMS</b><br>Root Mean Square amplitude of the current analysis window. "
        "Represents the signal's continuous power level.",
    "peak":
        "<b>Peak Amplitude</b><br>Maximum absolute sample value in the current window. "
        "Indicates momentary signal ceiling.",
    "dominant_freq":
        "<b>Dominant Frequency</b><br>Frequency bin with the highest spectral magnitude. "
        "Reflects the primary tonal component of the signal.",
    "spectral_centroid":
        "<b>Spectral Centroid</b><br>Amplitude-weighted center of mass of the frequency spectrum. "
        "Higher values indicate a brighter, higher-frequency timbre.",
    "spectral_rolloff":
        "<b>Spectral Rolloff</b><br>Frequency below which 85% of the total spectral energy is contained. "
        "Describes spectral bandwidth.",
    "spectral_flatness":
        "<b>Spectral Flatness</b><br>Ratio of geometric to arithmetic mean of the spectrum (0–1). "
        "Values near 0 indicate a tonal signal; near 1 indicate noise-like content.",
    "zcr":
        "<b>Zero Crossing Rate</b><br>Number of sign changes per sample in the current window. "
        "Correlates with high-frequency content and noise.",
    "bpm":
        "<b>BPM</b><br>Estimated beats per minute derived from periodicity in the energy envelope. "
        "Requires sufficient rhythmic content for reliable detection.",
}

# ── Single readout card ───────────────────────────────────────────────────────

class _ReadoutCard(QFrame):
    """Single metric readout with background and border."""

    def __init__(
        self,
        label:   str,
        unit:    str = "",
        tooltip: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("metric-card")
        self.setMinimumWidth(110)
        self.setMinimumHeight(56)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(2)

        # Metric name label + unit on the same line
        title_text = label.upper()
        if unit:
            title_text += f" ({unit})"
            
        self._lbl = QLabel(title_text)
        self._lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._lbl.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        self._lbl.setStyleSheet(f"color: {_LABEL}; letter-spacing: 1px;")

        # Numeric value
        val_layout = QHBoxLayout()
        val_layout.setContentsMargins(0, 0, 0, 0)
        
        self._val = QLabel("—")
        self._val.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
        self._val.setFont(QFont("Consolas", 18, QFont.Weight.Bold))
        self._val.setStyleSheet(f"color: {_VALUE};")

        val_layout.addWidget(self._val)
        val_layout.addStretch()

        layout.addWidget(self._lbl)
        layout.addLayout(val_layout)

        if tooltip:
            self.setToolTip(tooltip)

    def set_value(self, value: float, decimals: int = 3) -> None:
        if value is None or math.isnan(value) or math.isinf(value):
            self._val.setText("—")
            return
        self._val.setText(f"{value:.{decimals}f}")


# ── Section divider ───────────────────────────────────────────────────────────

class _SectionHeader(QWidget):
    """A subtle section label with a horizontal rule."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 2)
        layout.setSpacing(8)

        lbl = QLabel(title.upper())
        lbl.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        lbl.setStyleSheet(f"color: {_GROUP_HDR}; letter-spacing: 1.5px;")
        lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setStyleSheet(f"color: {_DIVIDER};")

        layout.addWidget(lbl)
        layout.addWidget(rule)


# ── Panel ─────────────────────────────────────────────────────────────────────

class MetricsPanel(QWidget):
    """Three-section instrument readout: Primary / Spectral / Temporal."""

    def __init__(self, parent=None):
        super().__init__(parent)
        
        # We rely on layout sizeHint rather than a hardcoded maximum height
        # to ensure the cards are never squashed causing text overlap.

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 8)
        root.setSpacing(6)

        # ── PRIMARY ───────────────────────────────────────────────────────────
        self._rms   = _ReadoutCard("RMS",              "",   _TOOLTIPS["rms"])
        self._peak  = _ReadoutCard("Peak",             "",   _TOOLTIPS["peak"])
        self._dfreq = _ReadoutCard("Dominant Freq",    "Hz", _TOOLTIPS["dominant_freq"])

        # ── SPECTRAL ──────────────────────────────────────────────────────────
        self._centroid = _ReadoutCard("Centroid",  "Hz", _TOOLTIPS["spectral_centroid"])
        self._rolloff  = _ReadoutCard("Rolloff",   "Hz", _TOOLTIPS["spectral_rolloff"])
        self._flat     = _ReadoutCard("Flatness",  "",   _TOOLTIPS["spectral_flatness"])

        # ── TEMPORAL ──────────────────────────────────────────────────────────
        self._zcr  = _ReadoutCard("ZCR", "", _TOOLTIPS["zcr"])
        self._bpm  = _ReadoutCard("BPM", "", _TOOLTIPS["bpm"])

        # ── Layout ────────────────────────────────────────────────────────────
        root.addWidget(_SectionHeader("Primary"))
        root.addLayout(self._row([self._rms, self._peak, self._dfreq]))

        root.addSpacing(4)
        root.addWidget(_SectionHeader("Spectral"))
        root.addLayout(self._row([self._centroid, self._rolloff, self._flat]))

        root.addSpacing(4)
        root.addWidget(_SectionHeader("Temporal"))
        
        # Keep Temporal grouped to the left instead of floating awkwardly
        temp_row = QHBoxLayout()
        temp_row.setSpacing(6)
        temp_row.setContentsMargins(0, 0, 0, 0)
        temp_row.addWidget(self._zcr)
        temp_row.addWidget(self._bpm)
        temp_row.addStretch() # Ensures they don't stretch across the entire row
        root.addLayout(temp_row)
        
        root.addStretch()

    @staticmethod
    def _row(cards: list[_ReadoutCard]) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        row.setContentsMargins(0, 0, 0, 0)
        for c in cards:
            row.addWidget(c)
        return row

    # ── Slot connected to AnalyzerWorker.new_metrics ──────────────────────────

    @Slot(dict)
    def update_metrics(self, metrics: dict) -> None:
        if metrics is None:
            return
        # Primary
        self._rms.set_value(metrics.get("rms", 0.0), decimals=3)
        self._peak.set_value(metrics.get("peak", 0.0), decimals=3)
        self._dfreq.set_value(metrics.get("peak_frequency", 0.0), decimals=1)
        # Spectral
        self._centroid.set_value(metrics.get("spectral_centroid", 0.0), decimals=1)
        self._rolloff.set_value( metrics.get("spectral_rolloff",  0.0), decimals=1)
        self._flat.set_value(    metrics.get("spectral_flatness", 0.0), decimals=4)
        # Temporal
        self._zcr.set_value(metrics.get("zcr", 0.0), decimals=4)
        self._bpm.set_value(metrics.get("bpm", 0.0), decimals=1)
