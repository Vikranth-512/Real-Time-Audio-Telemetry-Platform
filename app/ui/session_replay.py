"""
session_replay.py
-----------------
Interactive engineering inspection window for completed historical sessions.

Key Architecture:
  - ReplayController: Session-time playback engine driven by a 30 Hz QTimer
    and monotonic wall-clock delta calculation.
  - SessionReplayWindow: Professional desktop instrument window featuring
    stacked metric history curves, synchronized playhead, point-in-time readouts,
    and intentional empty states for non-stored signal data (waveform/FFT).
"""

import time
import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QObject, QTimer, Signal, Slot
from PySide6.QtGui import QFont, QKeyEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QSlider, QComboBox, QSplitter, QFrame, QSizePolicy, QMessageBox
)

from app.storage.parquet_reader import (
    ReplaySessionData, SessionDataReader, SessionReadError
)
from app.ui.widgets.metrics_panel import MetricsPanel

# ── Instrument Palette ─────────────────────────────────────────────────────────
_BG_DARK    = "#080b12"
_PANEL_BG   = "#111820"
_BORDER     = "#1a2535"
_AXIS       = "#2d4060"
_LABEL      = "#3d5570"
_MUTED      = "#5e7790"
_TEXT       = "#c9d8e8"
_RMS        = "#00b4d8"
_PEAK       = "#ff6b6b"
_ZCR        = "#7dd4a0"
_PLAYHEAD   = "#ffd166"


class ReplayState(Enum):
    PAUSED   = "PAUSED"
    PLAYING  = "PLAYING"
    COMPLETE = "COMPLETE"


class ReplayController(QObject):
    """
    Session-time playback controller.
    Maintains current position in session seconds, calculates delta using
    monotonic clock to prevent timer drift, and emits position/state updates.
    """

    position_changed = Signal(float, int)  # (position_seconds, nearest_index)
    state_changed    = Signal(object)      # ReplayState
    speed_changed    = Signal(float)       # speed multiplier
    finished         = Signal()

    def __init__(self, session_data: ReplaySessionData, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._data = session_data
        self._duration = max(0.0, float(session_data.session_duration))
        self._current_position = 0.0
        self._current_index = 0
        self._state = ReplayState.PAUSED
        self._speed = 1.0

        self._timer = QTimer(self)
        self._timer.setInterval(33)  # ~30 Hz update rate
        self._timer.timeout.connect(self._on_tick)
        self._last_tick_time = 0.0

    @property
    def duration(self) -> float:
        return self._duration

    @property
    def current_position(self) -> float:
        return self._current_position

    @property
    def current_index(self) -> int:
        return self._current_index

    @property
    def state(self) -> ReplayState:
        return self._state

    @property
    def speed(self) -> float:
        return self._speed

    @Slot()
    def play(self) -> None:
        """Start or resume playback."""
        if self._data.row_count <= 1 or self._duration <= 0.0:
            return

        # If already at the end, restart from beginning
        if self._state == ReplayState.COMPLETE or self._current_position >= self._duration:
            self._current_position = 0.0
            self._current_index = 0
            self.position_changed.emit(self._current_position, self._current_index)

        self._state = ReplayState.PLAYING
        self._last_tick_time = time.monotonic()
        self._timer.start()
        self.state_changed.emit(self._state)

    @Slot()
    def pause(self) -> None:
        """Pause playback."""
        if self._state == ReplayState.PLAYING:
            self._timer.stop()
            self._state = ReplayState.PAUSED
            self.state_changed.emit(self._state)

    @Slot()
    def toggle_play(self) -> None:
        """Toggle between play and pause."""
        if self._state == ReplayState.PLAYING:
            self.pause()
        else:
            self.play()

    @Slot(float)
    def seek(self, position_seconds: float) -> None:
        """
        Seek to a specific time in session seconds.
        Clamps cleanly within [0.0, duration] and updates the nearest measurement index.
        """
        clamped_pos = max(0.0, min(float(position_seconds), self._duration))
        self._current_position = clamped_pos
        self._current_index = SessionDataReader.find_nearest_index(
            self._data.timestamps_rel, clamped_pos
        )

        if self._state == ReplayState.COMPLETE and clamped_pos < self._duration:
            self._state = ReplayState.PAUSED
            self.state_changed.emit(self._state)

        self.position_changed.emit(self._current_position, self._current_index)

    @Slot()
    def step_forward(self) -> None:
        """Advance exactly one stored measurement."""
        if self._data.row_count <= 1:
            return
        new_idx = min(self._data.row_count - 1, self._current_index + 1)
        target_time = float(self._data.timestamps_rel[new_idx])
        self.seek(target_time)

    @Slot()
    def step_backward(self) -> None:
        """Step back exactly one stored measurement."""
        if self._data.row_count <= 1:
            return
        new_idx = max(0, self._current_index - 1)
        target_time = float(self._data.timestamps_rel[new_idx])
        self.seek(target_time)

    @Slot(float)
    def set_speed(self, speed: float) -> None:
        """Set playback speed multiplier."""
        self._speed = max(0.1, float(speed))
        self.speed_changed.emit(self._speed)

    def _on_tick(self) -> None:
        now = time.monotonic()
        elapsed_wall = now - self._last_tick_time
        self._last_tick_time = now

        new_pos = self._current_position + (elapsed_wall * self._speed)

        if new_pos >= self._duration:
            self._current_position = self._duration
            self._current_index = max(0, self._data.row_count - 1)
            self._timer.stop()
            self._state = ReplayState.COMPLETE
            self.position_changed.emit(self._current_position, self._current_index)
            self.state_changed.emit(self._state)
            self.finished.emit()
        else:
            self._current_position = new_pos
            self._current_index = SessionDataReader.find_nearest_index(
                self._data.timestamps_rel, new_pos
            )
            self.position_changed.emit(self._current_position, self._current_index)


# ── Empty State Plot Frame ────────────────────────────────────────────────────

def _create_empty_plot(title: str, main_msg: str, detail_msg: str) -> pg.PlotWidget:
    """Creates a styled PyQtGraph frame with clear instrument empty-state explanation."""
    pw = pg.PlotWidget()
    pw.setBackground(_BG_DARK)
    pw.setTitle(title, color=_LABEL, size="9pt")
    pw.getAxis("left").setPen(_AXIS)
    pw.getAxis("left").setTextPen(_AXIS)
    pw.getAxis("bottom").setPen(_AXIS)
    pw.getAxis("bottom").setTextPen(_AXIS)
    pw.showGrid(x=True, y=True, alpha=0.06)
    pw.showAxis("top", False)
    pw.showAxis("right", False)

    empty_text = pg.TextItem(
        html=(
            f'<div style="text-align:center;color:{_MUTED};font-family:Segoe UI,sans-serif;">'
            f'<div style="font-size:10pt;font-weight:bold;color:#7090b0;margin-bottom:4px;">{main_msg}</div>'
            f'<div style="font-size:8pt;color:{_MUTED};line-height:1.3;">{detail_msg}</div>'
            f'</div>'
        ),
        anchor=(0.5, 0.5),
    )
    pw.addItem(empty_text)
    return pw


# ── Main Session Replay Window ────────────────────────────────────────────────

class SessionReplayWindow(QWidget):
    """
    Top-level standalone window for interactive historical session replay.
    """

    def __init__(self, session_dir: Path, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        self._session_dir = Path(session_dir)
        self._data: ReplaySessionData = SessionDataReader.load_session(self._session_dir)
        self._controller = ReplayController(self._data, self)

        self.setWindowTitle(f"Audio Analyzer — Session Replay [{self._data.session_id}]")
        self.resize(1180, 800)
        self.setMinimumSize(950, 650)

        self._build_ui()
        self._apply_stylesheet()
        self._connect_signals()

        # Initialize to starting position
        self._update_readouts(0)
        self._update_playhead(0.0)
        self._update_time_label(0.0)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        # ── 1. Header Bar ─────────────────────────────────────────────────────
        header_frame = QFrame()
        header_frame.setObjectName("replay-header")
        header_layout = QHBoxLayout(header_frame)
        header_layout.setContentsMargins(12, 8, 12, 8)
        header_layout.setSpacing(16)

        # Title & Info
        title_box = QVBoxLayout()
        title_box.setSpacing(2)

        title_lbl = QLabel("SESSION REPLAY")
        title_lbl.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        title_lbl.setStyleSheet("color: #e6edf3; letter-spacing: 1.5px;")

        # Metadata format
        meta = self._data.metadata
        start_time_raw = meta.get("start_time", "")
        try:
            start_str = datetime.datetime.fromisoformat(start_time_raw).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            start_str = start_time_raw or "—"

        dur_s = self._data.session_duration
        dur_str = f"{int(dur_s // 60):02d}:{dur_s % 60:05.2f}"
        source_str = meta.get("source", "—")
        device_str = meta.get("device", "—")
        sr_khz = meta.get("sample_rate", 48000) / 1000.0
        ch_str = "Stereo" if meta.get("channels", 1) == 2 else "Mono"
        fpb_str = f"{meta.get('frames_per_buffer', 1024)} samples/frame"

        info_lbl = QLabel(
            f"{start_str}  │  {dur_str}  │  {source_str}  │  {device_str}  │  {sr_khz:.1f} kHz  │  {ch_str}  │  {fpb_str}"
        )
        info_lbl.setFont(QFont("Consolas", 8))
        info_lbl.setStyleSheet(f"color: {_MUTED};")

        title_box.addWidget(title_lbl)
        title_box.addWidget(info_lbl)
        header_layout.addLayout(title_box)

        header_layout.addStretch()

        # Status badge
        self._badge = QLabel("● HISTORICAL SESSION")
        self._badge.setObjectName("replay-badge")
        self._badge.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        header_layout.addWidget(self._badge)

        root.addWidget(header_frame)

        # ── 2. Top Visualizations (Waveform / Spectrum / Metrics Panel) ────────
        top_splitter = QSplitter(Qt.Orientation.Horizontal)
        top_splitter.setObjectName("top-splitter")

        # Waveform empty frame
        self._pw_waveform = _create_empty_plot(
            "TIME DOMAIN / HISTORICAL SIGNAL",
            "WAVEFORM DATA NOT STORED",
            "This session contains calculated measurements only.<br>Original PCM samples are unavailable for replay."
        )

        # Spectrum empty frame
        self._pw_spectrum = _create_empty_plot(
            "FREQUENCY / HISTORICAL SPECTRUM",
            "SPECTRUM DATA NOT STORED",
            "Original FFT bins were not persisted for this session."
        )

        top_splitter.addWidget(self._pw_waveform)
        top_splitter.addWidget(self._pw_spectrum)

        # Metrics readouts panel
        panel_container = QFrame()
        panel_container.setObjectName("panel-container")
        panel_layout = QVBoxLayout(panel_container)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        self._metrics_panel = MetricsPanel()
        panel_layout.addWidget(self._metrics_panel)
        top_splitter.addWidget(panel_container)

        top_splitter.setSizes([380, 380, 360])
        root.addWidget(top_splitter, stretch=3)

        # ── 3. Historical Metric Curves (Stacked Plots) ────────────────────────
        curves_frame = QFrame()
        curves_frame.setObjectName("curves-frame")
        curves_layout = QVBoxLayout(curves_frame)
        curves_layout.setContentsMargins(8, 6, 8, 6)
        curves_layout.setSpacing(4)

        # Top plot: Amplitude (RMS and Peak)
        self._plot_amp = pg.PlotWidget()
        self._plot_amp.setBackground(_BG_DARK)
        self._plot_amp.setLabel("left", "Amplitude", color=_LABEL)
        self._plot_amp.getAxis("left").setTextPen(_AXIS)
        self._plot_amp.getAxis("left").setPen(_AXIS)
        self._plot_amp.getAxis("bottom").setPen(_AXIS)
        self._plot_amp.showAxis("top", False)
        self._plot_amp.showAxis("right", False)
        self._plot_amp.getAxis("bottom").setStyle(showValues=False)
        self._plot_amp.showGrid(x=True, y=True, alpha=0.08)

        legend_amp = self._plot_amp.addLegend(offset=(10, 6))
        legend_amp.setBrush(pg.mkBrush(8, 11, 18, 200))
        legend_amp.setPen(pg.mkPen(_BORDER))

        # Bottom plot: ZCR
        self._plot_zcr = pg.PlotWidget()
        self._plot_zcr.setBackground(_BG_DARK)
        self._plot_zcr.setLabel("left", "ZCR", color=_LABEL)
        self._plot_zcr.setLabel("bottom", "Session Time (s)", color=_LABEL)
        self._plot_zcr.getAxis("left").setTextPen(_AXIS)
        self._plot_zcr.getAxis("left").setPen(_AXIS)
        self._plot_zcr.getAxis("bottom").setTextPen(_AXIS)
        self._plot_zcr.getAxis("bottom").setPen(_AXIS)
        self._plot_zcr.showAxis("top", False)
        self._plot_zcr.showAxis("right", False)
        self._plot_zcr.showGrid(x=True, y=True, alpha=0.08)

        legend_zcr = self._plot_zcr.addLegend(offset=(10, 6))
        legend_zcr.setBrush(pg.mkBrush(8, 11, 18, 200))
        legend_zcr.setPen(pg.mkPen(_BORDER))

        # Link X axes for synchronous zooming/panning
        self._plot_zcr.setXLink(self._plot_amp)

        # Populate static curves
        x_data = self._data.timestamps_rel
        self._curve_rms = self._plot_amp.plot(
            x=x_data, y=self._data.rms,
            name="RMS", pen=pg.mkPen(_RMS, width=1.4), antialias=False
        )
        self._curve_peak = self._plot_amp.plot(
            x=x_data, y=self._data.peak,
            name="Peak", pen=pg.mkPen(_PEAK, width=1.2), antialias=False
        )
        self._curve_zcr = self._plot_zcr.plot(
            x=x_data, y=self._data.zcr,
            name="ZCR", pen=pg.mkPen(_ZCR, width=1.2), antialias=False
        )

        # Set default view range bounded by session duration
        max_t = max(1.0, self._data.session_duration)
        self._plot_amp.setXRange(0, max_t, padding=0.01)
        self._plot_zcr.setXRange(0, max_t, padding=0.01)

        # Vertical playhead cursors
        self._playhead_amp = pg.InfiniteLine(
            pos=0.0, angle=90, movable=True,
            pen=pg.mkPen(_PLAYHEAD, width=1.5, style=Qt.PenStyle.SolidLine)
        )
        self._playhead_zcr = pg.InfiniteLine(
            pos=0.0, angle=90, movable=True,
            pen=pg.mkPen(_PLAYHEAD, width=1.5, style=Qt.PenStyle.SolidLine)
        )
        self._plot_amp.addItem(self._playhead_amp)
        self._plot_zcr.addItem(self._playhead_zcr)

        curves_layout.addWidget(self._plot_amp, stretch=2)
        curves_layout.addWidget(self._plot_zcr, stretch=1)

        root.addWidget(curves_frame, stretch=4)

        # ── 4. Timeline & Transport Control Bar ───────────────────────────────
        transport_frame = QFrame()
        transport_frame.setObjectName("transport-frame")
        transport_layout = QVBoxLayout(transport_frame)
        transport_layout.setContentsMargins(10, 6, 10, 8)
        transport_layout.setSpacing(6)

        # Timeline seek slider
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setObjectName("seek-slider")
        self._slider.setRange(0, 10000)
        self._slider.setValue(0)
        transport_layout.addWidget(self._slider)

        # Controls row
        ctrl_layout = QHBoxLayout()
        ctrl_layout.setContentsMargins(0, 0, 0, 0)
        ctrl_layout.setSpacing(8)

        self._btn_start = QPushButton("⏮")
        self._btn_start.setToolTip("Beginning (Home)")
        self._btn_step_back = QPushButton("◀")
        self._btn_step_back.setToolTip("Step Backward 1 Measurement (Left)")
        self._btn_play = QPushButton("▶")
        self._btn_play.setObjectName("btn-play")
        self._btn_play.setToolTip("Play / Pause (Space)")
        self._btn_step_fwd = QPushButton("▶")
        self._btn_step_fwd.setToolTip("Step Forward 1 Measurement (Right)")
        self._btn_end = QPushButton("⏭")
        self._btn_end.setToolTip("End (End)")

        ctrl_layout.addWidget(self._btn_start)
        ctrl_layout.addWidget(self._btn_step_back)
        ctrl_layout.addWidget(self._btn_play)
        ctrl_layout.addWidget(self._btn_step_fwd)
        ctrl_layout.addWidget(self._btn_end)

        ctrl_layout.addSpacing(12)

        self._time_lbl = QLabel("00:00.00 / 00:00.00")
        self._time_lbl.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        self._time_lbl.setStyleSheet("color: #e6edf3;")
        ctrl_layout.addWidget(self._time_lbl)

        ctrl_layout.addStretch()

        # Speed selector
        speed_lbl = QLabel("SPEED")
        speed_lbl.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        speed_lbl.setStyleSheet(f"color: {_MUTED};")
        ctrl_layout.addWidget(speed_lbl)

        self._speed_combo = QComboBox()
        self._speed_combo.setObjectName("speed-combo")
        for s in [0.25, 0.5, 1.0, 2.0, 4.0]:
            self._speed_combo.addItem(f"{s:g}×", userData=s)
        self._speed_combo.setCurrentIndex(2)  # 1.0×
        ctrl_layout.addWidget(self._speed_combo)

        transport_layout.addLayout(ctrl_layout)
        root.addWidget(transport_frame)

    def _connect_signals(self) -> None:
        # Controller signals
        self._controller.position_changed.connect(self._on_position_changed)
        self._controller.state_changed.connect(self._on_state_changed)

        # Transport buttons
        self._btn_start.clicked.connect(lambda: self._controller.seek(0.0))
        self._btn_step_back.clicked.connect(self._controller.step_backward)
        self._btn_play.clicked.connect(self._controller.toggle_play)
        self._btn_step_fwd.clicked.connect(self._controller.step_forward)
        self._btn_end.clicked.connect(lambda: self._controller.seek(self._controller.duration))

        # Speed change
        self._speed_combo.currentIndexChanged.connect(self._on_speed_combo_changed)

        # Slider seeking
        self._slider.sliderMoved.connect(self._on_slider_moved)
        self._slider.valueChanged.connect(self._on_slider_value_changed)

        # Playhead dragging
        self._playhead_amp.sigDragged.connect(self._on_playhead_dragged)
        self._playhead_zcr.sigDragged.connect(self._on_playhead_dragged)

    @Slot(float, int)
    def _on_position_changed(self, pos: float, index: int) -> None:
        self._update_playhead(pos)
        self._update_time_label(pos)
        self._update_readouts(index)

        # Update slider without triggering self-loop
        if self._controller.duration > 0:
            pct = int((pos / self._controller.duration) * 10000)
            self._slider.blockSignals(True)
            self._slider.setValue(pct)
            self._slider.blockSignals(False)

    @Slot(object)
    def _on_state_changed(self, state: ReplayState) -> None:
        if state == ReplayState.PLAYING:
            self._btn_play.setText("⏸")
            self._btn_play.setToolTip("Pause (Space)")
            self._badge.setText("● PLAYING HISTORICAL")
            self._badge.setStyleSheet("color: #7dd4a0; background-color: #11281e; border: 1px solid #1f4530;")
        elif state == ReplayState.COMPLETE:
            self._btn_play.setText("▶")
            self._btn_play.setToolTip("Restart from beginning (Space)")
            self._badge.setText("● COMPLETE")
            self._badge.setStyleSheet("color: #e3a830; background-color: #2b2210; border: 1px solid #4a3818;")
        else:
            self._btn_play.setText("▶")
            self._btn_play.setToolTip("Play (Space)")
            self._badge.setText("● HISTORICAL SESSION")
            self._badge.setStyleSheet("color: #7090b0; background-color: #141f2e; border: 1px solid #1f3045;")

    @Slot(int)
    def _on_slider_moved(self, val: int) -> None:
        pos = (val / 10000.0) * self._controller.duration
        self._controller.seek(pos)

    @Slot(int)
    def _on_slider_value_changed(self, val: int) -> None:
        if self._slider.isSliderDown():
            pos = (val / 10000.0) * self._controller.duration
            self._controller.seek(pos)

    def _on_playhead_dragged(self, line: pg.InfiniteLine) -> None:
        pos = line.value()
        self._controller.seek(pos)

    @Slot(int)
    def _on_speed_combo_changed(self, idx: int) -> None:
        speed = float(self._speed_combo.currentData())
        self._controller.set_speed(speed)

    def _update_playhead(self, pos: float) -> None:
        self._playhead_amp.blockSignals(True)
        self._playhead_zcr.blockSignals(True)
        self._playhead_amp.setValue(pos)
        self._playhead_zcr.setValue(pos)
        self._playhead_amp.blockSignals(False)
        self._playhead_zcr.blockSignals(False)

    def _update_time_label(self, pos: float) -> None:
        dur = self._controller.duration
        cur_m, cur_s = divmod(pos, 60.0)
        tot_m, tot_s = divmod(dur, 60.0)
        self._time_lbl.setText(f"{int(cur_m):02d}:{cur_s:05.2f} / {int(tot_m):02d}:{tot_s:05.2f}")

    def _update_readouts(self, index: int) -> None:
        """Feed current measurement values to MetricsPanel."""
        meas = SessionDataReader.get_measurement_dict(self._data, index)
        # Adapt to MetricsPanel expected dictionary format
        self._metrics_panel.update_metrics(meas)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_Space:
            self._controller.toggle_play()
            event.accept()
        elif key == Qt.Key.Key_Left:
            self._controller.step_backward()
            event.accept()
        elif key == Qt.Key.Key_Right:
            self._controller.step_forward()
            event.accept()
        elif key == Qt.Key.Key_Home:
            self._controller.seek(0.0)
            event.accept()
        elif key == Qt.Key.Key_End:
            self._controller.seek(self._controller.duration)
            event.accept()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self._controller.pause()
        super().closeEvent(event)

    def _apply_stylesheet(self) -> None:
        self.setStyleSheet(f"""
            QWidget {{
                background-color: #0d1117;
                color: {_TEXT};
                font-family: 'Segoe UI', sans-serif;
            }}
            QFrame#replay-header {{
                background-color: {_PANEL_BG};
                border: 1px solid {_BORDER};
                border-radius: 4px;
            }}
            QLabel#replay-badge {{
                background-color: #141f2e;
                color: #7090b0;
                border: 1px solid #1f3045;
                border-radius: 4px;
                padding: 4px 10px;
                font-family: Consolas, monospace;
            }}
            QFrame#panel-container {{
                background-color: {_PANEL_BG};
                border: 1px solid {_BORDER};
                border-radius: 4px;
            }}
            QFrame#curves-frame {{
                background-color: {_PANEL_BG};
                border: 1px solid {_BORDER};
                border-radius: 4px;
            }}
            QFrame#transport-frame {{
                background-color: {_PANEL_BG};
                border: 1px solid {_BORDER};
                border-radius: 4px;
            }}
            QPushButton {{
                background-color: #151d27;
                color: {_TEXT};
                border: 1px solid #2d4560;
                border-radius: 3px;
                padding: 4px 12px;
                font-size: 11px;
                min-width: 32px;
            }}
            QPushButton:hover {{
                background-color: #1f2b3b;
            }}
            QPushButton#btn-play {{
                font-size: 13px;
                padding: 4px 16px;
                font-weight: bold;
                background-color: #162438;
                border-color: #3b5f85;
            }}
            QPushButton#btn-play:hover {{
                background-color: #1e334f;
            }}
            QComboBox#speed-combo {{
                background-color: #151d27;
                color: {_TEXT};
                border: 1px solid #2d4560;
                border-radius: 3px;
                padding: 3px 8px;
                font-size: 10px;
                font-family: Consolas, monospace;
            }}
            QSlider::groove:horizontal {{
                height: 4px;
                background: #1a2535;
                border-radius: 2px;
            }}
            QSlider::sub-page:horizontal {{
                background: #00b4d8;
                border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                background: #ffffff;
                border: 1px solid #00b4d8;
                width: 12px;
                margin-top: -4px;
                margin-bottom: -4px;
                border-radius: 6px;
            }}
            QSlider::handle:horizontal:hover {{
                background: #ffd166;
                border-color: #ffd166;
            }}
        """)
