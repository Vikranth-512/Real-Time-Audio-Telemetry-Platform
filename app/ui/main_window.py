"""
main_window.py
--------------
Application main window. Owns the thread/worker lifecycle and wires
Qt signals from AnalyzerWorker or FilePlaybackController to the visualization widgets.

UI structure:
  ┌─ Acquisition / Playback Control Bar ────────────────────────────────┐
  │  [⚡ LIVE] [🎵 AUDIO FILE] │ Controls (Live or File) │ ● IDLE      │
  ├─ Session Info Strip ────────────────────────────────────────────────┤
  │  48.0 kHz  │  Mono  │  1024 samples/frame  │  float32               │
  ├─ TIME DOMAIN ───────────────────────────────────────────────────────┤
  │  (waveform — x-axis in ms)                                          │
  ├─ FREQUENCY SPECTRUM ──────────────┬─ METRIC HISTORY ────────────────┤
  │  (FFT — clean freq labels)        │  (RMS / Peak / ZCR rolling)     │
  ├─ PRIMARY / SPECTRAL / TEMPORAL ─────────────────────────────────────┤
  │  Dominant Freq  |  RMS  |  Peak  |  ZCR  |  ...                     │
  └─ Status bar: subtle, technical readout ─────────────────────────────┘

Thread ownership:
  _worker_thread   QThread running AnalyzerWorker
  _storage_thread  QThread running ParquetWriter
  _feeder_thread   threading.Thread in FilePlaybackController (when file mode)
  GUI thread       renders widgets, handles user input
"""

import queue
import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore  import Qt, QThread, QTimer, Slot
from PySide6.QtGui   import QFont
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QComboBox, QLabel, QSplitter, QStatusBar,
    QFileDialog, QFrame, QStackedWidget, QSlider,
)

from app.acquisition.audio_source   import AudioSource, SAMPLE_RATE, PACKET_SIZE
from app.acquisition.device_manager import AudioDeviceManager, AudioDevice, SerialDeviceInfo
from app.acquisition.esp32_audio_source import Esp32AudioSource, Esp32State
from app.acquisition.file_controller import FilePlaybackController, FilePlaybackState
from app.processing.analyzer_worker import AnalyzerWorker
from app.ui.plots.waveform          import WaveformWidget
from app.ui.plots.spectrum          import SpectrumWidget
from app.ui.plots.metrics           import MetricsPlotWidget
from app.ui.widgets.metrics_panel   import MetricsPanel
from app.storage.parquet_writer     import ParquetWriter
from app.storage.csv_exporter       import export_session_csv
from app.storage.session_manager    import SessionManager

logger = logging.getLogger(__name__)

CAPTURE_QUEUE_MAXSIZE = 64
STORAGE_QUEUE_MAXSIZE = 256


# ── Clickable Seek Slider ─────────────────────────────────────────────────────

class _ClickableSlider(QSlider):
    """QSlider that jumps directly to the clicked location on mouse press."""

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            span = self.width()
            pos = event.position().x()
            if span > 0:
                fraction = max(0.0, min(1.0, pos / span))
                val = int(self.minimum() + fraction * (self.maximum() - self.minimum()))
                self.setValue(val)
        super().mousePressEvent(event)


# ── Status indicator widget ───────────────────────────────────────────────────

class _StatusIndicator(QWidget):
    """Colored dot · state text · elapsed time."""

    _COL_IDLE    = "#5e7790"
    _COL_RUN     = "#00cc88"
    _COL_WARN    = "#e3a830"
    _COL_ERR     = "#e35050"
    _COL_PAUSE   = "#38bdf8"
    _COL_DONE    = "#a78bfa"

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._dot = QLabel("●")
        self._dot.setFont(QFont("Arial", 11))

        self._state = QLabel("IDLE")
        self._state.setFont(QFont("Consolas", 9, QFont.Weight.Bold))

        self._elapsed = QLabel("")
        self._elapsed.setFont(QFont("Consolas", 9))
        self._elapsed.setVisible(False)

        layout.addWidget(self._dot)
        layout.addWidget(self._state)
        layout.addWidget(self._elapsed)
        layout.addStretch()

        self._apply("IDLE", self._COL_IDLE, show_elapsed=False)

    def _apply(self, text: str, color: str, show_elapsed: bool) -> None:
        css = f"color: {color};"
        self._dot.setStyleSheet(css)
        self._state.setStyleSheet(css)
        self._state.setText(text)
        self._elapsed.setStyleSheet(f"color: {color};")
        self._elapsed.setVisible(show_elapsed)

    def set_idle(self)     -> None: self._apply("IDLE",        self._COL_IDLE, False)
    def set_running(self)  -> None: self._apply("ACQUIRING",   self._COL_RUN,  True)
    def set_playing(self)  -> None: self._apply("PLAYING",     self._COL_RUN,  False)
    def set_paused(self)   -> None: self._apply("PAUSED",      self._COL_PAUSE, False)
    def set_complete(self) -> None: self._apply("COMPLETE",    self._COL_DONE, False)
    def set_stopping(self) -> None: self._apply("STOPPING",    self._COL_WARN, False)
    def set_error(self, msg: str = "AUDIO ERROR") -> None:
        self._apply(msg, self._COL_ERR, False)

    def update_elapsed(self, seconds: int) -> None:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        self._elapsed.setText(f"  {h:02d}:{m:02d}:{s:02d}")


def _format_time(seconds: float) -> str:
    s = max(0, int(seconds))
    m = s // 60
    sec = s % 60
    return f"{m:02d}:{sec:02d}"


# ── Main window ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Audio Analyzer")
        self.resize(1360, 860)

        # ── Internal state ────────────────────────────────────────────────────
        self._device_manager   = AudioDeviceManager()
        self._input_devices: list[AudioDevice] = []
        self._output_devices: list[AudioDevice] = []
        self._serial_devices: list[SerialDeviceInfo] = []
        self._capture_queue    = queue.Queue(maxsize=CAPTURE_QUEUE_MAXSIZE)
        self._storage_queue    = queue.Queue(maxsize=STORAGE_QUEUE_MAXSIZE)
        self._audio_source: AudioSource | None   = None
        self._esp32_source: Esp32AudioSource | None = None
        self._esp32_thread: QThread | None = None
        self._worker_thread:   QThread | None    = None
        self._worker:          AnalyzerWorker | None = None
        self._storage_thread:  QThread | None    = None
        self._parquet_writer:  ParquetWriter | None  = None
        self._session_manager  = SessionManager()
        self._current_session  = None
        self._last_parquet_path = None
        self._running = False
        self._elapsed_seconds = 0

        # Current source mode: "live" or "file"
        self._current_mode = "live"

        # File playback controller
        self._file_controller = FilePlaybackController(
            capture_queue=self._capture_queue,
            analyzer_worker=None,
            parent=self
        )
        self._file_controller.playback_position.connect(self._on_file_position)
        self._file_controller.state_changed.connect(self._on_file_state_changed)
        self._file_controller.file_loaded.connect(self._on_file_loaded)
        self._file_controller.error.connect(self._on_file_error)

        self._file_duration = 0.0
        self._is_scrubbing = False

        # Elapsed-time ticker for live mode (GUI thread, 1 Hz)
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)

        # ── Build UI ──────────────────────────────────────────────────────────
        self._apply_stylesheet()
        self._build_ui()
        self._populate_devices()
        self._build_menu()

    # ─────────────────────────────────────────────────────────────────────────
    # UI construction
    # ─────────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setCentralWidget(central)

        root.addWidget(self._build_control_bar())
        root.addWidget(self._build_session_strip())
        root.addWidget(self._build_plot_area(), stretch=1)
        root.addWidget(self._build_metric_panel())
        self._build_status_bar()

    # ── Control bar ───────────────────────────────────────────────────────────

    def _build_control_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("control-bar")
        bar.setFixedHeight(56)
        outer = QHBoxLayout(bar)
        outer.setContentsMargins(16, 6, 16, 6)
        outer.setSpacing(16)

        # Mode toggle buttons
        mode_box = QHBoxLayout()
        mode_box.setSpacing(4)
        self._mode_live_btn = QPushButton("LIVE")
        self._mode_live_btn.setObjectName("mode-tab-btn")
        self._mode_live_btn.setCheckable(True)
        self._mode_live_btn.setChecked(True)
        self._mode_live_btn.setToolTip("Real-time live audio analysis (microphone or loopback)")
        self._mode_live_btn.clicked.connect(lambda: self._on_mode_switched("live"))

        self._mode_file_btn = QPushButton("AUDIO FILE")
        self._mode_file_btn.setObjectName("mode-tab-btn")
        self._mode_file_btn.setCheckable(True)
        self._mode_file_btn.setChecked(False)
        self._mode_file_btn.setToolTip("Decode, play, and analyze MP3 / WAV / FLAC / OGG audio files")
        self._mode_file_btn.clicked.connect(lambda: self._on_mode_switched("file"))

        mode_box.addWidget(self._mode_live_btn)
        mode_box.addWidget(self._mode_file_btn)
        outer.addLayout(mode_box)

        # Separator
        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.VLine)
        sep1.setStyleSheet("color: #1a2535;")
        outer.addWidget(sep1)

        # QStackedWidget for Live vs File controls
        self._mode_stack = QStackedWidget()
        self._mode_stack.addWidget(self._build_live_controls())
        self._mode_stack.addWidget(self._build_file_controls())
        outer.addWidget(self._mode_stack, stretch=1)

        # Separator
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.VLine)
        sep2.setStyleSheet("color: #1a2535;")
        outer.addWidget(sep2)

        # Status indicator (right-aligned)
        self._status_indicator = _StatusIndicator()
        outer.addWidget(self._status_indicator)

        return bar

    def _build_live_controls(self) -> QWidget:
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        # SOURCE selector
        src_group = QVBoxLayout()
        src_group.setSpacing(2)
        src_lbl = QLabel("SOURCE")
        src_lbl.setObjectName("ctrl-label")
        self._source_combo = QComboBox()
        self._source_combo.setObjectName("ctrl-combo")
        self._source_combo.setMinimumWidth(140)
        self._source_combo.addItem("System Output",  userData="System Output")
        self._source_combo.addItem("Microphone",     userData="Microphone")
        self._source_combo.addItem("ESP32 (Serial)", userData="ESP32")
        self._source_combo.setToolTip(
            "<b>System Output</b> — captures digital audio rendered to speakers.<br>"
            "<b>Microphone</b> — captures acoustic input from a physical device.<br>"
            "<b>ESP32 (Serial)</b> — ingests raw PCM audio stream over USB Serial."
        )
        self._source_combo.currentIndexChanged.connect(self._on_source_type_changed)
        src_group.addWidget(src_lbl)
        src_group.addWidget(self._source_combo)
        layout.addLayout(src_group)

        # LIVE DEVICE STACK: Page 0 = Audio Device, Page 1 = ESP32 Controls
        self._live_dev_stack = QStackedWidget()

        # Page 0: Standard Audio Device selector
        audio_dev_page = QWidget()
        audio_dev_layout = QVBoxLayout(audio_dev_page)
        audio_dev_layout.setContentsMargins(0, 0, 0, 0)
        audio_dev_layout.setSpacing(2)
        dev_lbl = QLabel("INPUT DEVICE")
        dev_lbl.setObjectName("ctrl-label")
        self._device_combo = QComboBox()
        self._device_combo.setObjectName("ctrl-combo")
        self._device_combo.setMinimumWidth(240)
        audio_dev_layout.addWidget(dev_lbl)
        audio_dev_layout.addWidget(self._device_combo)
        self._live_dev_stack.addWidget(audio_dev_page)

        # Page 1: ESP32 Device Configuration
        esp32_page = QWidget()
        esp32_layout = QHBoxLayout(esp32_page)
        esp32_layout.setContentsMargins(0, 0, 0, 0)
        esp32_layout.setSpacing(10)

        # Port selector + Refresh
        port_group = QVBoxLayout()
        port_group.setSpacing(2)
        port_lbl = QLabel("ESP32 PORT")
        port_lbl.setObjectName("ctrl-label")
        port_row = QHBoxLayout()
        port_row.setSpacing(4)
        self._esp32_port_combo = QComboBox()
        self._esp32_port_combo.setObjectName("ctrl-combo")
        self._esp32_port_combo.setMinimumWidth(180)
        self._esp32_refresh_btn = QPushButton("↻")
        self._esp32_refresh_btn.setObjectName("refresh-btn")
        self._esp32_refresh_btn.setToolTip("Rescan connected serial / COM ports")
        self._esp32_refresh_btn.setFixedSize(26, 26)
        self._esp32_refresh_btn.clicked.connect(self._refresh_serial_ports)
        port_row.addWidget(self._esp32_port_combo)
        port_row.addWidget(self._esp32_refresh_btn)
        port_group.addWidget(port_lbl)
        port_group.addLayout(port_row)
        esp32_layout.addLayout(port_group)

        # Baud Rate
        baud_group = QVBoxLayout()
        baud_group.setSpacing(2)
        baud_lbl = QLabel("BAUD")
        baud_lbl.setObjectName("ctrl-label")
        self._esp32_baud_combo = QComboBox()
        self._esp32_baud_combo.setObjectName("ctrl-combo")
        self._esp32_baud_combo.setMinimumWidth(90)
        for b in (115200, 230400, 460800, 921600):
            self._esp32_baud_combo.addItem(str(b), userData=b)
        self._esp32_baud_combo.setCurrentIndex(0)
        baud_group.addWidget(baud_lbl)
        baud_group.addWidget(self._esp32_baud_combo)
        esp32_layout.addLayout(baud_group)

        # Sample Rate Interpretation
        sr_group = QVBoxLayout()
        sr_group.setSpacing(2)
        sr_lbl = QLabel("SAMPLE RATE")
        sr_lbl.setObjectName("ctrl-label")
        self._esp32_sr_combo = QComboBox()
        self._esp32_sr_combo.setObjectName("ctrl-combo")
        self._esp32_sr_combo.setMinimumWidth(100)
        for r in (48000, 44100, 24000, 16000, 8000):
            self._esp32_sr_combo.addItem(f"{r} Hz", userData=r)
        self._esp32_sr_combo.setCurrentIndex(0)
        self._esp32_sr_combo.setToolTip("External interpretation parameter: incoming PCM samples are analyzed at this rate")
        self._esp32_sr_combo.currentIndexChanged.connect(self._on_esp32_sample_rate_changed)
        sr_group.addWidget(sr_lbl)
        sr_group.addWidget(self._esp32_sr_combo)
        esp32_layout.addLayout(sr_group)

        # Channels
        ch_group = QVBoxLayout()
        ch_group.setSpacing(2)
        ch_lbl = QLabel("CHANNELS")
        ch_lbl.setObjectName("ctrl-label")
        self._esp32_ch_lbl = QLabel("1 (Mono)")
        self._esp32_ch_lbl.setObjectName("ctrl-label")
        self._esp32_ch_lbl.setStyleSheet("color: #8aa8c8; font-size: 11px; padding: 4px 0;")
        ch_group.addWidget(ch_lbl)
        ch_group.addWidget(self._esp32_ch_lbl)
        esp32_layout.addLayout(ch_group)

        # ESP32 Status
        st_group = QVBoxLayout()
        st_group.setSpacing(2)
        st_lbl = QLabel("STATUS")
        st_lbl.setObjectName("ctrl-label")
        self._esp32_status_lbl = QLabel("● Disconnected")
        self._esp32_status_lbl.setObjectName("esp32-status")
        self._esp32_status_lbl.setStyleSheet("color: #5e7790; font-size: 10px; font-weight: bold; padding: 4px 0;")
        st_group.addWidget(st_lbl)
        st_group.addWidget(self._esp32_status_lbl)
        esp32_layout.addLayout(st_group)

        self._live_dev_stack.addWidget(esp32_page)
        layout.addWidget(self._live_dev_stack)

        # START / STOP buttons
        btn_group = QHBoxLayout()
        btn_group.setSpacing(8)
        self._start_btn = QPushButton("▶  START")
        self._start_btn.setObjectName("start-btn")
        self._start_btn.setFixedWidth(105)
        self._start_btn.clicked.connect(self._on_start)

        self._stop_btn = QPushButton("■  STOP")
        self._stop_btn.setObjectName("stop-btn")
        self._stop_btn.setFixedWidth(105)
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop)

        btn_group.addWidget(self._start_btn)
        btn_group.addWidget(self._stop_btn)

        btn_wrapper = QVBoxLayout()
        btn_wrapper.addStretch()
        btn_wrapper.addLayout(btn_group)
        layout.addLayout(btn_wrapper)
        layout.addStretch()

        return w

    def _build_file_controls(self) -> QWidget:
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        # Open File Button
        open_group = QVBoxLayout()
        open_group.addStretch()
        self._file_open_btn = QPushButton("Open File…")
        self._file_open_btn.setObjectName("file-open-btn")
        self._file_open_btn.setFixedHeight(28)
        self._file_open_btn.setToolTip("Open MP3, WAV, FLAC, or OGG file")
        self._file_open_btn.clicked.connect(self._on_open_file)
        open_group.addWidget(self._file_open_btn)
        layout.addLayout(open_group)

        # Output Device selector
        out_group = QVBoxLayout()
        out_group.setSpacing(2)
        out_lbl = QLabel("PLAYBACK OUTPUT")
        out_lbl.setObjectName("ctrl-label")
        self._file_output_combo = QComboBox()
        self._file_output_combo.setObjectName("ctrl-combo")
        self._file_output_combo.setMinimumWidth(190)
        self._file_output_combo.currentIndexChanged.connect(self._on_file_output_changed)
        out_group.addWidget(out_lbl)
        out_group.addWidget(self._file_output_combo)
        layout.addLayout(out_group)

        # Transport controls: [] [▶ PLAY] [■ STOP]
        trans_group = QHBoxLayout()
        trans_group.setSpacing(6)

        self._file_prev_btn = QPushButton("<<")
        self._file_prev_btn.setObjectName("file-ctrl-btn")
        self._file_prev_btn.setFixedWidth(34)
        self._file_prev_btn.setFixedHeight(28)
        self._file_prev_btn.setEnabled(False)
        self._file_prev_btn.setToolTip("Restart from beginning")
        self._file_prev_btn.clicked.connect(self._on_file_rewind_clicked)

        self._file_play_btn = QPushButton("▶  PLAY")
        self._file_play_btn.setObjectName("start-btn")
        self._file_play_btn.setFixedWidth(95)
        self._file_play_btn.setFixedHeight(28)
        self._file_play_btn.setEnabled(False)
        self._file_play_btn.clicked.connect(self._on_file_play_clicked)

        self._file_stop_btn = QPushButton("■  STOP")
        self._file_stop_btn.setObjectName("stop-btn")
        self._file_stop_btn.setFixedWidth(85)
        self._file_stop_btn.setFixedHeight(28)
        self._file_stop_btn.setEnabled(False)
        self._file_stop_btn.clicked.connect(self._on_file_stop_clicked)

        trans_group.addWidget(self._file_prev_btn)
        trans_group.addWidget(self._file_play_btn)
        trans_group.addWidget(self._file_stop_btn)

        trans_wrapper = QVBoxLayout()
        trans_wrapper.addStretch()
        trans_wrapper.addLayout(trans_group)
        layout.addLayout(trans_wrapper)

        # Timeline Scrubber: Time display + Slider
        scrub_group = QVBoxLayout()
        scrub_group.setSpacing(2)

        scrub_header = QHBoxLayout()
        time_title = QLabel("TIMELINE")
        time_title.setObjectName("ctrl-label")
        self._file_time_lbl = QLabel("00:00 / 00:00")
        self._file_time_lbl.setObjectName("time-display")
        scrub_header.addWidget(time_title)
        scrub_header.addStretch()
        scrub_header.addWidget(self._file_time_lbl)

        self._file_slider = _ClickableSlider(Qt.Orientation.Horizontal)
        self._file_slider.setObjectName("file-slider")
        self._file_slider.setRange(0, 1000)
        self._file_slider.setValue(0)
        self._file_slider.setEnabled(False)
        self._file_slider.sliderPressed.connect(self._on_slider_pressed)
        self._file_slider.sliderReleased.connect(self._on_slider_released)
        self._file_slider.sliderMoved.connect(self._on_slider_moved)

        scrub_group.addLayout(scrub_header)
        scrub_group.addWidget(self._file_slider)
        layout.addLayout(scrub_group, stretch=1)

        # File chip
        chip_wrapper = QVBoxLayout()
        chip_wrapper.addStretch()
        self._file_chip_lbl = QLabel("No file loaded")
        self._file_chip_lbl.setObjectName("file-chip")
        self._file_chip_lbl.setToolTip("Currently loaded audio file")
        chip_wrapper.addWidget(self._file_chip_lbl)
        layout.addLayout(chip_wrapper)

        return w

    # ── Session info strip ────────────────────────────────────────────────────

    def _build_session_strip(self) -> QWidget:
        strip = QWidget()
        strip.setObjectName("session-strip")
        strip.setFixedHeight(20)
        self._strip_layout = QHBoxLayout(strip)
        self._strip_layout.setContentsMargins(16, 0, 16, 0)
        self._strip_layout.setSpacing(0)

        self._strip_sr_lbl = QLabel(f"{SAMPLE_RATE / 1000:.1f} kHz")
        self._strip_sr_lbl.setObjectName("strip-value")
        self._strip_ch_lbl = QLabel("MONO")
        self._strip_ch_lbl.setObjectName("strip-value")
        self._strip_buf_lbl = QLabel(f"{PACKET_SIZE} SAMPLES/FRAME")
        self._strip_buf_lbl.setObjectName("strip-value")
        self._strip_fmt_lbl = QLabel("FLOAT32")
        self._strip_fmt_lbl.setObjectName("strip-value")
        self._strip_extra_lbl = QLabel("")
        self._strip_extra_lbl.setObjectName("strip-value")
        self._strip_extra_lbl.setVisible(False)

        def _make_sep():
            sep = QLabel("│")
            sep.setObjectName("strip-sep")
            return sep

        self._strip_layout.addWidget(self._strip_sr_lbl)
        self._strip_layout.addWidget(_make_sep())
        self._strip_layout.addWidget(self._strip_ch_lbl)
        self._strip_layout.addWidget(_make_sep())
        self._strip_layout.addWidget(self._strip_buf_lbl)
        self._strip_layout.addWidget(_make_sep())
        self._strip_layout.addWidget(self._strip_fmt_lbl)
        self._strip_sep_extra = _make_sep()
        self._strip_sep_extra.setVisible(False)
        self._strip_layout.addWidget(self._strip_sep_extra)
        self._strip_layout.addWidget(self._strip_extra_lbl)
        self._strip_layout.addStretch()
        return strip

    def _update_session_strip(self, sample_rate: int, channels: int = 1, extra: str = "") -> None:
        self._strip_sr_lbl.setText(f"{sample_rate / 1000:.1f} kHz")
        if self._current_mode == "file" and channels > 1:
            self._strip_ch_lbl.setText("MONO (downmixed)")
        else:
            self._strip_ch_lbl.setText("MONO")
        if extra:
            self._strip_sep_extra.setVisible(True)
            self._strip_extra_lbl.setVisible(True)
            self._strip_extra_lbl.setText(extra)
        else:
            self._strip_sep_extra.setVisible(False)
            self._strip_extra_lbl.setVisible(False)

        self._sb_tech.setText(
            f"  {sample_rate / 1000:.1f} kHz  │  Mono  │  {PACKET_SIZE} samples/frame  │  float32  "
        )

    # ── Plot area ─────────────────────────────────────────────────────────────

    def _build_plot_area(self) -> QWidget:
        container = QWidget()
        container.setObjectName("plot-area")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        splitter_v = QSplitter(Qt.Orientation.Vertical)
        splitter_v.setHandleWidth(3)
        splitter_v.setStyleSheet(
            "QSplitter::handle { background: #0f1620; }"
        )

        # Top: TIME DOMAIN (waveform)
        wave_wrapper = QWidget()
        wave_wrapper.setObjectName("plot-panel")
        wl = QVBoxLayout(wave_wrapper)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(0)
        wave_hdr = self._panel_header("TIME DOMAIN")
        wl.addWidget(wave_hdr)
        self._waveform_widget = WaveformWidget()
        wl.addWidget(self._waveform_widget)
        splitter_v.addWidget(wave_wrapper)

        # Bottom split: FREQUENCY SPECTRUM + METRIC HISTORY
        splitter_h = QSplitter(Qt.Orientation.Horizontal)
        splitter_h.setHandleWidth(3)
        splitter_h.setStyleSheet(
            "QSplitter::handle { background: #0f1620; }"
        )

        spec_wrapper = QWidget()
        spec_wrapper.setObjectName("plot-panel")
        sl = QVBoxLayout(spec_wrapper)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        sl.addWidget(self._panel_header("FREQUENCY SPECTRUM"))
        self._spectrum_widget = SpectrumWidget()
        sl.addWidget(self._spectrum_widget)
        splitter_h.addWidget(spec_wrapper)

        met_wrapper = QWidget()
        met_wrapper.setObjectName("plot-panel")
        ml = QVBoxLayout(met_wrapper)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(0)
        ml.addWidget(self._panel_header("METRIC HISTORY"))
        self._metrics_plot = MetricsPlotWidget()
        ml.addWidget(self._metrics_plot)
        splitter_h.addWidget(met_wrapper)

        splitter_h.setSizes([650, 400])
        splitter_v.addWidget(splitter_h)
        splitter_v.setSizes([340, 280])

        layout.addWidget(splitter_v)
        return container

    @staticmethod
    def _panel_header(title: str) -> QLabel:
        """Subtle panel title — small uppercase, very low contrast."""
        lbl = QLabel(title)
        lbl.setObjectName("panel-header")
        lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        lbl.setFixedHeight(20)
        return lbl

    # ── Metric panel ──────────────────────────────────────────────────────────

    def _build_metric_panel(self) -> QWidget:
        wrapper = QWidget()
        wrapper.setObjectName("metric-area")
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._metrics_panel = MetricsPanel()
        layout.addWidget(self._metrics_panel)
        return wrapper

    # ── Status bar ────────────────────────────────────────────────────────────

    def _build_status_bar(self) -> None:
        sb = QStatusBar()
        sb.setObjectName("main-status-bar")
        self.setStatusBar(sb)

        # Permanent right widget — clean technical readout
        self._sb_tech = QLabel(f"  {SAMPLE_RATE / 1000:.1f} kHz  │  Mono  │  {PACKET_SIZE} samples/frame  │  float32  ")
        self._sb_tech.setObjectName("sb-tech")
        sb.addPermanentWidget(self._sb_tech)
        sb.showMessage("Ready")

    # ── Menu ──────────────────────────────────────────────────────────────────

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        
        self._history_action = file_menu.addAction("Session History…")
        self._history_action.triggered.connect(self._on_show_history)
        file_menu.addSeparator()

        self._export_csv_action = file_menu.addAction("Export Current Session…")
        self._export_csv_action.setEnabled(False)
        self._export_csv_action.triggered.connect(self._on_export_csv)
        file_menu.addSeparator()
        
        file_menu.addAction("E&xit", self.close)

    # ─────────────────────────────────────────────────────────────────────────
    # Device population
    # ─────────────────────────────────────────────────────────────────────────

    def _populate_devices(self) -> None:
        """Load input, output, and serial devices and populate the combos."""
        self._input_devices = self._device_manager.enumerate_input_devices()
        self._refresh_device_combo()

        self._output_devices = self._device_manager.enumerate_output_devices()
        self._refresh_output_combo()

        self._refresh_serial_ports()

    def _refresh_device_combo(self) -> None:
        """Filter input device combo to match the selected source type."""
        source_type = self._source_combo.currentData()
        self._device_combo.blockSignals(True)
        self._device_combo.clear()

        filtered = [d for d in self._input_devices if d.source_type == source_type]
        for d in filtered:
            self._device_combo.addItem(d.display_name, userData=d.index)

        # For System Output, prefer the WASAPI loopback device
        if source_type == "System Output":
            for i, d in enumerate(filtered):
                if d.is_loopback and "wasapi" in d.host_api_name.lower():
                    self._device_combo.setCurrentIndex(i)
                    break

        self._device_combo.blockSignals(False)

        if not filtered:
            self._device_combo.addItem("No devices found", userData=None)

    def _refresh_serial_ports(self) -> None:
        """Enumerate connected serial ports and populate _esp32_port_combo."""
        self._serial_devices = self._device_manager.enumerate_serial_ports()
        self._esp32_port_combo.blockSignals(True)
        self._esp32_port_combo.clear()

        esp32_idx = -1
        for idx, dev in enumerate(self._serial_devices):
            self._esp32_port_combo.addItem(dev.display_name, userData=dev.port)
            if dev.is_esp32_hint and esp32_idx == -1:
                esp32_idx = idx

        if esp32_idx >= 0:
            self._esp32_port_combo.setCurrentIndex(esp32_idx)

        self._esp32_port_combo.blockSignals(False)

        if not self._serial_devices:
            self._esp32_port_combo.addItem("No COM ports found", userData=None)

    def _refresh_output_combo(self) -> None:
        """Populate the playback output devices combo."""
        self._file_output_combo.blockSignals(True)
        self._file_output_combo.clear()

        default_out = self._device_manager.get_default_output_device()
        default_idx = -1

        for idx, d in enumerate(self._output_devices):
            self._file_output_combo.addItem(d.display_name, userData=d.index)
            if default_out and d.index == default_out.index:
                default_idx = idx

        if default_idx >= 0:
            self._file_output_combo.setCurrentIndex(default_idx)

        self._file_output_combo.blockSignals(False)

        if not self._output_devices:
            self._file_output_combo.addItem("Default Output Device", userData=None)

    @Slot()
    def _on_source_type_changed(self) -> None:
        source_type = self._source_combo.currentData()
        if source_type == "ESP32":
            self._live_dev_stack.setCurrentIndex(1)
            self._refresh_serial_ports()
            sr = int(self._esp32_sr_combo.currentData() or SAMPLE_RATE)
            self._update_session_strip(sr, 1, "ESP32 USB Serial")
        else:
            self._live_dev_stack.setCurrentIndex(0)
            self._refresh_device_combo()
            self._update_session_strip(SAMPLE_RATE, 1, "")

    @Slot()
    def _on_esp32_sample_rate_changed(self) -> None:
        if self._source_combo.currentData() == "ESP32":
            sr = int(self._esp32_sr_combo.currentData() or SAMPLE_RATE)
            self._update_session_strip(sr, 1, "ESP32 USB Serial")

    @Slot(str)
    def _on_esp32_state_changed(self, state_str: str) -> None:
        if state_str == Esp32State.CONNECTED.value:
            self._esp32_status_lbl.setText("● Connected")
            self._esp32_status_lbl.setStyleSheet("color: #38bdf8; font-size: 10px; font-weight: bold; padding: 4px 0;")
        elif state_str == Esp32State.RUNNING.value:
            sr = int(self._esp32_sr_combo.currentData() or SAMPLE_RATE)
            self._esp32_status_lbl.setText(f"● Receiving ({sr/1000:.1f} kHz)")
            self._esp32_status_lbl.setStyleSheet("color: #00e096; font-size: 10px; font-weight: bold; padding: 4px 0;")
            self.statusBar().showMessage(f"ESP32: Receiving PCM at {sr} Hz")
        elif state_str == Esp32State.ERROR.value:
            self._esp32_status_lbl.setText("● Error")
            self._esp32_status_lbl.setStyleSheet("color: #e35050; font-size: 10px; font-weight: bold; padding: 4px 0;")
        elif state_str == Esp32State.STOPPING.value:
            self._esp32_status_lbl.setText("● Stopping")
            self._esp32_status_lbl.setStyleSheet("color: #e3a830; font-size: 10px; font-weight: bold; padding: 4px 0;")
        elif state_str == Esp32State.DISCONNECTED.value:
            self._esp32_status_lbl.setText("● Disconnected")
            self._esp32_status_lbl.setStyleSheet("color: #5e7790; font-size: 10px; font-weight: bold; padding: 4px 0;")

    @Slot(str)
    def _on_esp32_status_message(self, msg: str) -> None:
        logger.info(f"ESP32 status: {msg}")
        self.statusBar().showMessage(f"ESP32: {msg}")

    @Slot(dict)
    def _on_esp32_stats_updated(self, stats: dict) -> None:
        tip = (
            f"Port: {stats.get('port')}\n"
            f"Packets Decoded: {stats.get('packets_received', 0)}\n"
            f"Samples Decoded: {stats.get('samples_received', 0)}\n"
            f"Bytes Received: {stats.get('bytes_received', 0)}\n"
            f"Queue Drops: {stats.get('queue_drops', 0)}\n"
            f"CRC Failures: {stats.get('crc_failures', 0)}\n"
            f"Resync Events: {stats.get('resync_events', 0)}"
        )
        self._esp32_status_lbl.setToolTip(tip)

    @Slot(str)
    def _on_esp32_error(self, err_msg: str) -> None:
        logger.error(f"ESP32 source error: {err_msg}")
        self._status_indicator.set_error("SERIAL ERROR")
        self.statusBar().showMessage(f"ESP32 Serial Error: {err_msg}")
        self._stop_live()

    @Slot()
    def _on_file_output_changed(self) -> None:
        dev_idx = self._file_output_combo.currentData()
        self._file_controller.set_output_device_index(dev_idx)

    # ─────────────────────────────────────────────────────────────────────────
    # Mode Switching
    # ─────────────────────────────────────────────────────────────────────────

    @Slot()
    def _on_mode_switched(self, mode: str) -> None:
        if mode == self._current_mode:
            self._mode_live_btn.setChecked(mode == "live")
            self._mode_file_btn.setChecked(mode == "file")
            return

        # Stop whatever is currently active
        self._stop()

        self._current_mode = mode
        if mode == "live":
            self._mode_live_btn.setChecked(True)
            self._mode_file_btn.setChecked(False)
            self._mode_stack.setCurrentIndex(0)
            self._waveform_widget.set_sample_rate(SAMPLE_RATE)
            self._spectrum_widget.set_sample_rate(SAMPLE_RATE)
            self._waveform_widget.clear()
            self._spectrum_widget.clear()
            self._update_session_strip(SAMPLE_RATE, 1, "")
            self.statusBar().showMessage("Ready — Live Acquisition")
        else:
            self._mode_live_btn.setChecked(False)
            self._mode_file_btn.setChecked(True)
            self._mode_stack.setCurrentIndex(1)
            if self._file_controller.decoder:
                sr = self._file_controller.decoder.sample_rate
                self._waveform_widget.set_sample_rate(sr)
                self._spectrum_widget.set_sample_rate(sr)
                self._update_session_strip(sr, self._file_controller.decoder.channels, self._file_controller.decoder.filename)
                self.statusBar().showMessage(f"Ready — {self._file_controller.decoder.filename}")
            else:
                self.statusBar().showMessage("Ready — Select an audio file to analyze")

    # ─────────────────────────────────────────────────────────────────────────
    # Live Acquisition Lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    @Slot()
    def _on_start(self) -> None:
        self._start_live()

    @Slot()
    def _on_stop(self) -> None:
        self._stop_live()

    def _start_live(self) -> None:
        source_type = self._source_combo.currentData()

        # ── ESP32 Serial Path ────────────────────────────────────────────────
        if source_type == "ESP32":
            port = self._esp32_port_combo.currentData()
            if not port:
                self.statusBar().showMessage("No serial COM port selected for ESP32.")
                return

            baud_rate = int(self._esp32_baud_combo.currentData() or 115200)
            sample_rate = int(self._esp32_sr_combo.currentData() or SAMPLE_RATE)

            _drain(self._capture_queue)

            runtime_metadata = {
                "source_type": "live",
                "source": "ESP32",
                "transport": "USB Serial",
                "port": port,
                "baud_rate": baud_rate,
                "device": f"ESP32 ({port})",
                "sample_rate": sample_rate,
                "channels": 1,
                "frames_per_buffer": PACKET_SIZE,
                "sample_format": "float32"
            }
            self._current_session = self._session_manager.create_session(runtime_metadata)

            self._waveform_widget.set_sample_rate(sample_rate)
            self._spectrum_widget.set_sample_rate(sample_rate)
            self._waveform_widget.clear()
            self._spectrum_widget.clear()
            self._update_session_strip(sample_rate, 1, f"ESP32 · {port}")

            # AnalyzerWorker on dedicated QThread
            self._worker = AnalyzerWorker(
                capture_queue=self._capture_queue,
                storage_queue=self._storage_queue,
                sample_rate=sample_rate,
            )
            self._worker_thread = QThread()
            self._worker.moveToThread(self._worker_thread)
            self._worker_thread.started.connect(self._worker.start_processing)
            self._worker.new_waveform.connect(self._waveform_widget.update_waveform)
            self._worker.new_fft.connect(self._spectrum_widget.update_spectrum)
            self._worker.new_metrics.connect(self._metrics_plot.update_metrics)
            self._worker.new_metrics.connect(self._metrics_panel.update_metrics)
            self._worker_thread.start()

            # ParquetWriter on dedicated QThread
            self._parquet_writer = ParquetWriter(storage_queue=self._storage_queue)
            self._parquet_writer.set_session_context(
                self._current_session.session_id,
                self._current_session.directory
            )
            self._storage_thread = QThread()
            self._parquet_writer.moveToThread(self._storage_thread)
            self._storage_thread.started.connect(self._parquet_writer.start_writing)
            self._storage_thread.start()

            # Esp32AudioSource on dedicated worker QThread owned by MainWindow
            self._esp32_source = Esp32AudioSource(
                out_queue=self._capture_queue,
                port=port,
                baud_rate=baud_rate,
                sample_rate=sample_rate,
                channels=1,
                analyzer_frame_size=PACKET_SIZE,
                use_crc=True,
            )
            self._esp32_thread = QThread()
            self._esp32_source.moveToThread(self._esp32_thread)
            self._esp32_thread.started.connect(self._esp32_source.run)

            self._esp32_source.state_changed.connect(self._on_esp32_state_changed)
            self._esp32_source.status_message.connect(self._on_esp32_status_message)
            self._esp32_source.stats_updated.connect(self._on_esp32_stats_updated)
            self._esp32_source.error_occurred.connect(self._on_esp32_error)

            self._esp32_thread.start()

            self._running = True
            self._elapsed_seconds = 0
            self._elapsed_timer.start()

            self._start_btn.setEnabled(False)
            self._stop_btn.setEnabled(True)
            self._source_combo.setEnabled(False)
            self._esp32_port_combo.setEnabled(False)
            self._esp32_baud_combo.setEnabled(False)
            self._esp32_sr_combo.setEnabled(False)
            self._esp32_refresh_btn.setEnabled(False)
            self._mode_file_btn.setEnabled(False)
            self._export_csv_action.setEnabled(False)
            self._status_indicator.set_running()
            self.statusBar().showMessage(f"Acquiring — ESP32 · {port} ({baud_rate} baud, {sample_rate} Hz)")
            logger.info(f"ESP32 pipeline started — port={port}, baud={baud_rate}, sr={sample_rate}")
            return

        # ── Audio Device (System Output / Microphone) Path ───────────────────
        device_index = self._device_combo.currentData()
        if device_index is None:
            self.statusBar().showMessage("No device selected.")
            return

        if not self._device_manager.validate_device(device_index):
            self._status_indicator.set_error("DEVICE ERROR")
            self.statusBar().showMessage(
                f"Device [{device_index}] cannot be opened at {SAMPLE_RATE} Hz — "
                "try a different device."
            )
            return

        # Clear stale frames
        _drain(self._capture_queue)

        # ── Create Session Metadata ───────────────────────────────────────────
        runtime_metadata = {
            "source_type": "live",
            "source": self._source_combo.currentText(),
            "device": self._device_combo.currentText(),
            "sample_rate": SAMPLE_RATE,
            "channels": 1,
            "frames_per_buffer": PACKET_SIZE,
            "sample_format": "float32"
        }
        self._current_session = self._session_manager.create_session(runtime_metadata)

        # ── AnalyzerWorker on its own QThread ─────────────────────────────────
        self._worker = AnalyzerWorker(
            capture_queue=self._capture_queue,
            storage_queue=self._storage_queue,
            sample_rate=SAMPLE_RATE,
        )
        self._worker_thread = QThread()
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.start_processing)
        self._worker.new_waveform.connect(self._waveform_widget.update_waveform)
        self._worker.new_fft.connect(self._spectrum_widget.update_spectrum)
        self._worker.new_metrics.connect(self._metrics_plot.update_metrics)
        self._worker.new_metrics.connect(self._metrics_panel.update_metrics)
        self._worker_thread.start()

        # ── ParquetWriter on its own QThread ──────────────────────────────────
        self._parquet_writer = ParquetWriter(storage_queue=self._storage_queue)
        self._parquet_writer.set_session_context(
            self._current_session.session_id, 
            self._current_session.directory
        )
        self._storage_thread = QThread()
        self._parquet_writer.moveToThread(self._storage_thread)
        self._storage_thread.started.connect(self._parquet_writer.start_writing)
        self._storage_thread.start()

        # ── Audio capture (PortAudio thread) ──────────────────────────────────
        self._audio_source = AudioSource(
            out_queue=self._capture_queue,
            device_index=device_index,
        )
        try:
            self._audio_source.start()
        except Exception as exc:
            self._status_indicator.set_error("AUDIO ERROR")
            self.statusBar().showMessage(f"Failed to open audio stream: {exc}")
            logger.error(f"AudioSource.start() failed: {exc}")
            
            self._stop_background_threads()
            if self._current_session:
                self._session_manager.finalize_session(
                    self._current_session.session_id,
                    status="incomplete",
                    error_msg=str(exc)
                )
                self._current_session = None
            return

        self._running = True
        self._elapsed_seconds = 0
        self._elapsed_timer.start()

        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._source_combo.setEnabled(False)
        self._device_combo.setEnabled(False)
        self._mode_file_btn.setEnabled(False)
        self._export_csv_action.setEnabled(False)
        self._status_indicator.set_running()
        self.statusBar().showMessage(
            f"Acquiring — {self._source_combo.currentText()}  "
            f"· {self._device_combo.currentText()}"
        )
        logger.info(f"Live pipeline started — device_index={device_index}")

    def _stop_live(self) -> None:
        self._status_indicator.set_stopping()
        self._elapsed_timer.stop()

        if self._audio_source:
            self._audio_source.stop()
            self._audio_source = None

        if self._esp32_source:
            self._esp32_source.stop()
        if self._esp32_thread:
            self._esp32_thread.quit()
            self._esp32_thread.wait(3000)
            self._esp32_thread = None
        self._esp32_source = None

        self._stop_background_threads()
        
        if self._current_session:
            self._session_manager.finalize_session(
                self._current_session.session_id,
                status="completed"
            )
            self._current_session = None

        # Auto-refresh history if open
        if hasattr(self, "_history_window") and self._history_window and self._history_window.isVisible():
            self._history_window.refresh()

        # Reset plots to idle state
        self._waveform_widget.set_idle()
        self._spectrum_widget.set_idle()

        self._running = False
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._source_combo.setEnabled(True)
        self._device_combo.setEnabled(True)
        self._esp32_port_combo.setEnabled(True)
        self._esp32_baud_combo.setEnabled(True)
        self._esp32_sr_combo.setEnabled(True)
        self._esp32_refresh_btn.setEnabled(True)
        self._esp32_status_lbl.setText("● Disconnected")
        self._esp32_status_lbl.setStyleSheet("color: #5e7790; font-size: 10px; font-weight: bold; padding: 4px 0;")
        self._mode_file_btn.setEnabled(True)
        self._status_indicator.set_idle()

        if self._last_parquet_path:
            self._export_csv_action.setEnabled(True)
            self.statusBar().showMessage(
                f"Session saved — {self._last_parquet_path.name}"
            )
        else:
            self.statusBar().showMessage("Stopped")
        logger.info("Live pipeline stopped")

    # ─────────────────────────────────────────────────────────────────────────
    # File Playback Lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    @Slot()
    def _on_open_file(self) -> None:
        if self._current_session is not None:
            self._stop_file_playback()

        file_path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Open Audio File",
            "",
            "Audio Files (*.mp3 *.wav *.flac *.ogg);;MP3 Files (*.mp3);;WAV Files (*.wav);;FLAC Files (*.flac);;OGG Files (*.ogg);;All Files (*.*)"
        )
        if not file_path_str:
            return

        dev_idx = self._file_output_combo.currentData()
        success = self._file_controller.open_file(Path(file_path_str), output_device_index=dev_idx)
        if not success:
            return

    @Slot(dict)
    def _on_file_loaded(self, meta: dict) -> None:
        ch_text = "Stereo" if meta["channels"] == 2 else ("Mono" if meta["channels"] == 1 else f"{meta['channels']} Ch")
        dur_text = _format_time(meta["duration"])
        self._file_chip_lbl.setText(f"{meta['filename']}  │  {meta['sample_rate']/1000:.1f} kHz  │  {ch_text}  │  {dur_text}")
        self._file_duration = meta["duration"]
        self._file_time_lbl.setText(f"00:00 / {dur_text}")
        self._file_slider.setValue(0)
        self._file_slider.setEnabled(True)
        self._file_play_btn.setEnabled(True)
        self._file_prev_btn.setEnabled(True)

        self._waveform_widget.set_sample_rate(meta["sample_rate"])
        self._spectrum_widget.set_sample_rate(meta["sample_rate"])
        self._waveform_widget.clear()
        self._spectrum_widget.clear()
        self._update_session_strip(meta["sample_rate"], meta["channels"], meta["filename"])
        self.statusBar().showMessage(f"Loaded {meta['filename']} ({dur_text})")

    @Slot()
    def _on_file_play_clicked(self) -> None:
        if self._file_controller.state == FilePlaybackState.PLAYING:
            self._file_controller.pause()
        else:
            if self._current_session is None:
                self._start_file_session()
            self._file_controller.play()

    @Slot()
    def _on_file_stop_clicked(self) -> None:
        self._stop_file_playback()

    @Slot()
    def _on_file_rewind_clicked(self) -> None:
        self._file_controller.seek(0.0)

    @Slot()
    def _on_slider_pressed(self) -> None:
        self._is_scrubbing = True

    @Slot(int)
    def _on_slider_moved(self, value: int) -> None:
        if self._file_duration > 0:
            target = (value / 1000.0) * self._file_duration
            self._file_time_lbl.setText(f"{_format_time(target)} / {_format_time(self._file_duration)}")

    @Slot()
    def _on_slider_released(self) -> None:
        if self._file_duration > 0:
            target = (self._file_slider.value() / 1000.0) * self._file_duration
            self._file_controller.seek(target)
        self._is_scrubbing = False

    @Slot(float, float)
    def _on_file_position(self, current: float, total: float) -> None:
        self._file_duration = total
        if not self._is_scrubbing:
            self._file_time_lbl.setText(f"{_format_time(current)} / {_format_time(total)}")
            if total > 0:
                slider_val = int((current / total) * 1000)
                self._file_slider.blockSignals(True)
                self._file_slider.setValue(max(0, min(1000, slider_val)))
                self._file_slider.blockSignals(False)

    @Slot(object)
    def _on_file_state_changed(self, state: FilePlaybackState) -> None:
        if state == FilePlaybackState.PLAYING:
            self._file_play_btn.setText("⏸  PAUSE")
            self._file_play_btn.setObjectName("pause-btn")
            self._file_play_btn.setStyleSheet("")
            self._file_stop_btn.setEnabled(True)
            self._mode_live_btn.setEnabled(False)
            self._file_open_btn.setEnabled(False)
            self._status_indicator.set_playing()
            name = self._file_controller.decoder.filename if self._file_controller.decoder else ""
            self.statusBar().showMessage(f"Playing — {name}")
        elif state == FilePlaybackState.PAUSED:
            self._file_play_btn.setText("▶  RESUME")
            self._file_play_btn.setObjectName("start-btn")
            self._file_play_btn.setStyleSheet("")
            self._status_indicator.set_paused()
            self.statusBar().showMessage("Playback paused")
        elif state == FilePlaybackState.COMPLETE:
            self._file_play_btn.setText("▶  PLAY")
            self._file_play_btn.setObjectName("start-btn")
            self._file_play_btn.setStyleSheet("")
            self._status_indicator.set_complete()
            self._stop_file_session(completed=True)
            self.statusBar().showMessage("File playback completed")
        elif state == FilePlaybackState.READY:
            self._file_play_btn.setText("▶  PLAY")
            self._file_play_btn.setObjectName("start-btn")
            self._file_play_btn.setStyleSheet("")
            self._status_indicator.set_idle()
        elif state == FilePlaybackState.ERROR:
            self._status_indicator.set_error()
            self._stop_file_session(completed=False)

    @Slot(str)
    def _on_file_error(self, err_msg: str) -> None:
        self.statusBar().showMessage(f"File Error: {err_msg}")
        self._status_indicator.set_error("FILE ERROR")

    def _start_file_session(self) -> None:
        decoder = self._file_controller.decoder
        if not decoder:
            return

        _drain(self._capture_queue)

        runtime_metadata = {
            "source_type": "file",
            "source": "Audio File",
            "file_path": str(decoder.file_path),
            "source_filename": decoder.filename,
            "sample_rate": decoder.sample_rate,
            "channels": 1,
            "original_channels": decoder.channels,
            "frames_per_buffer": PACKET_SIZE,
            "sample_format": "float32"
        }
        self._current_session = self._session_manager.create_session(runtime_metadata)

        # Start AnalyzerWorker on dedicated QThread
        self._worker = AnalyzerWorker(
            capture_queue=self._capture_queue,
            storage_queue=self._storage_queue,
            sample_rate=decoder.sample_rate,
        )
        self._worker_thread = QThread()
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.start_processing)
        self._worker.new_waveform.connect(self._waveform_widget.update_waveform)
        self._worker.new_fft.connect(self._spectrum_widget.update_spectrum)
        self._worker.new_metrics.connect(self._metrics_plot.update_metrics)
        self._worker.new_metrics.connect(self._metrics_panel.update_metrics)
        self._worker_thread.start()

        self._file_controller.set_worker(self._worker)

        # Start ParquetWriter on dedicated QThread
        self._parquet_writer = ParquetWriter(storage_queue=self._storage_queue)
        self._parquet_writer.set_session_context(
            self._current_session.session_id, 
            self._current_session.directory
        )
        self._storage_thread = QThread()
        self._parquet_writer.moveToThread(self._storage_thread)
        self._storage_thread.started.connect(self._parquet_writer.start_writing)
        self._storage_thread.start()

        self._file_stop_btn.setEnabled(True)
        self._export_csv_action.setEnabled(False)
        logger.info(f"File analysis session started for {decoder.filename}")

    def _stop_file_session(self, completed: bool = True) -> None:
        self._file_controller.set_worker(None)
        self._stop_background_threads()

        if self._current_session:
            self._session_manager.finalize_session(
                self._current_session.session_id,
                status="completed" if completed else "incomplete"
            )
            self._current_session = None

        if hasattr(self, "_history_window") and self._history_window and self._history_window.isVisible():
            self._history_window.refresh()

        self._waveform_widget.set_idle()
        self._spectrum_widget.set_idle()

        self._file_stop_btn.setEnabled(False)
        self._mode_live_btn.setEnabled(True)
        self._file_open_btn.setEnabled(True)

        if self._last_parquet_path:
            self._export_csv_action.setEnabled(True)
            self.statusBar().showMessage(
                f"Session saved — {self._last_parquet_path.name}"
            )

    def _stop_file_playback(self) -> None:
        self._file_controller.stop()
        self._stop_file_session(completed=True)

    # ─────────────────────────────────────────────────────────────────────────
    # Shared Control / Cleanup
    # ─────────────────────────────────────────────────────────────────────────

    def _stop_background_threads(self) -> None:
        """Stops the worker and storage threads cleanly."""
        if self._esp32_source and self._esp32_thread:
            self._esp32_source.stop()
            self._esp32_thread.quit()
            self._esp32_thread.wait(3000)
            self._esp32_thread = None
            self._esp32_source = None

        if self._worker and self._worker_thread:
            self._worker.stop_processing()
            self._worker_thread.quit()
            self._worker_thread.wait(3000)
            self._worker_thread = None
            self._worker = None

        if self._parquet_writer and self._storage_thread:
            self._last_parquet_path = self._parquet_writer.file_path
            self._parquet_writer.stop_writing()
            self._storage_thread.quit()
            self._storage_thread.wait(5000)
            self._storage_thread = None
            self._parquet_writer = None

    def _stop(self) -> None:
        """Dispatches stop to the currently active subsystem."""
        if self._current_mode == "file":
            if self._file_controller.state in (FilePlaybackState.PLAYING, FilePlaybackState.PAUSED) or self._current_session is not None:
                self._stop_file_playback()
        else:
            if self._running:
                self._stop_live()

    @Slot()
    def _tick_elapsed(self) -> None:
        if self._running:
            self._elapsed_seconds += 1
            self._status_indicator.update_elapsed(self._elapsed_seconds)

    # ─────────────────────────────────────────────────────────────────────────
    # History & CSV export
    # ─────────────────────────────────────────────────────────────────────────

    @Slot()
    def _on_show_history(self) -> None:
        from app.ui.session_history import SessionHistoryWindow
        if not hasattr(self, "_history_window") or self._history_window is None:
            self._history_window = SessionHistoryWindow(self._session_manager, parent=self)
            self._history_window.setWindowFlags(Qt.WindowType.Window)
        
        self._history_window.show()
        self._history_window.raise_()
        self._history_window.refresh()

    @Slot()
    def _on_export_csv(self) -> None:
        if not self._last_parquet_path or not self._last_parquet_path.exists():
            self.statusBar().showMessage("No session file available for export.")
            return
        default = str(self._last_parquet_path.with_suffix(".csv"))
        dest, _ = QFileDialog.getSaveFileName(
            self, "Export Session CSV", default, "CSV Files (*.csv)"
        )
        if not dest:
            return
        try:
            out = export_session_csv(self._last_parquet_path, dest)
            self.statusBar().showMessage(f"Exported — {out.name}")
        except Exception as exc:
            self.statusBar().showMessage(f"Export failed: {exc}")

    # ─────────────────────────────────────────────────────────────────────────
    # Clean shutdown
    # ─────────────────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        self._stop()
        self._file_controller.close()
        super().closeEvent(event)

    # ─────────────────────────────────────────────────────────────────────────
    # Stylesheet — instrument dark theme
    # ─────────────────────────────────────────────────────────────────────────

    def _apply_stylesheet(self) -> None:
        self.setStyleSheet("""
            /* ── Tooltips ─────────────────────────────────────────────────── */
            QToolTip {
                background-color: rgba(17, 24, 32, 230);
                color: #c9d8e8;
                border: 1px solid #2d4560;
                padding: 4px;
                border-radius: 2px;
                font-family: 'Segoe UI', sans-serif;
                font-size: 11px;
            }

            /* ── Base ─────────────────────────────────────────────────────── */
            QMainWindow, QWidget {
                background-color: #0d1117;
                color: #c9d8e8;
                font-family: 'Segoe UI', sans-serif;
                font-size: 11px;
            }
            
            QLabel {
                background-color: transparent;
            }

            /* ── Menu bar ─────────────────────────────────────────────────── */
            QMenuBar {
                background-color: #0d1117;
                color: #8aa8c8;
                border-bottom: 1px solid #1a2535;
                spacing: 2px;
                padding: 2px 0;
            }
            QMenuBar::item:selected { background-color: #1a2535; color: #ffffff; }
            QMenu {
                background-color: #111820;
                color: #c9d8e8;
                border: 1px solid #1a2535;
            }
            QMenu::item:selected { background-color: #1a2535; }

            /* ── Control bar ──────────────────────────────────────────────── */
            QWidget#control-bar {
                background-color: #111820;
                border-bottom: 1px solid #1a2535;
            }

            QPushButton#mode-tab-btn {
                background-color: #151d27;
                color: #5e7790;
                border: 1px solid #1a2535;
                border-radius: 3px;
                padding: 6px 13px;
                font-size: 10px;
                font-weight: bold;
                letter-spacing: 1px;
            }
            QPushButton#mode-tab-btn:hover {
                color: #c9d8e8;
                border-color: #2d4560;
            }
            QPushButton#mode-tab-btn:checked {
                background-color: #0b2233;
                color: #00e096;
                border: 1px solid #009966;
            }

            QLabel#ctrl-label {
                color: #5e7790;
                font-size: 9px;
                letter-spacing: 1px;
                font-weight: bold;
                font-family: 'Segoe UI', sans-serif;
                text-transform: uppercase;
            }

            QComboBox#ctrl-combo {
                background-color: #151d27;
                color: #ffffff;
                border: 1px solid #1a2535;
                border-radius: 3px;
                padding: 4px 10px;
                min-height: 26px;
                selection-background-color: #1f2b3b;
            }
            QComboBox#ctrl-combo::drop-down {
                border: none;
                width: 20px;
            }
            QComboBox#ctrl-combo QAbstractItemView {
                background-color: #151d27;
                color: #ffffff;
                border: 1px solid #1a2535;
                selection-background-color: #1f2b3b;
            }

            QPushButton#start-btn {
                background-color: #092e42;
                color: #00e096;
                border: 1px solid #009966;
                border-radius: 3px;
                padding: 6px 14px;
                font-size: 11px;
                font-weight: bold;
                letter-spacing: 1px;
            }
            QPushButton#start-btn:hover   { background-color: #0b3d56; border-color: #00cc88; }
            QPushButton#start-btn:pressed { background-color: #061f2c; }
            QPushButton#start-btn:disabled{ background-color: #111820; color: #3d5570; border-color: #1a2535; }

            QPushButton#refresh-btn {
                background-color: #151d27;
                color: #8aa8c8;
                border: 1px solid #1a2535;
                border-radius: 3px;
                padding: 2px 4px;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton#refresh-btn:hover {
                background-color: #1f2b3b;
                color: #00e096;
                border-color: #2d4560;
            }
            QPushButton#refresh-btn:disabled {
                color: #3d5570;
                background-color: #111820;
            }

            QLabel#esp32-status {
                font-family: 'Consolas', monospace;
            }

            QPushButton#pause-btn {
                background-color: #0c2738;
                color: #38bdf8;
                border: 1px solid #0284c7;
                border-radius: 3px;
                padding: 6px 14px;
                font-size: 11px;
                font-weight: bold;
                letter-spacing: 1px;
            }
            QPushButton#pause-btn:hover   { background-color: #113852; border-color: #38bdf8; }
            QPushButton#pause-btn:pressed { background-color: #081a26; }

            QPushButton#stop-btn {
                background-color: #111820;
                color: #3d5570;
                border: 1px solid #1a2535;
                border-radius: 3px;
                padding: 6px 14px;
                font-size: 11px;
                font-weight: bold;
                letter-spacing: 1px;
            }
            QPushButton#stop-btn:hover   { background-color: #2b1111; color: #ff6b6b; border-color: #aa2020; }
            QPushButton#stop-btn:pressed { background-color: #1a0808; }
            QPushButton#stop-btn:disabled{ background-color: #111820; color: #3d5570; border-color: #1a2535; }

            QPushButton#file-open-btn {
                background-color: #151d27;
                color: #c9d8e8;
                border: 1px solid #1e293b;
                border-radius: 3px;
                padding: 5px 12px;
                font-size: 11px;
                font-weight: bold;
            }
            QPushButton#file-open-btn:hover {
                background-color: #1f2b3b;
                border-color: #2d4560;
                color: #ffffff;
            }

            QPushButton#file-ctrl-btn {
                background-color: #151d27;
                color: #8aa8c8;
                border: 1px solid #1a2535;
                border-radius: 3px;
                padding: 4px 8px;
                font-size: 12px;
            }
            QPushButton#file-ctrl-btn:hover {
                background-color: #1f2b3b;
                color: #ffffff;
                border-color: #2d4560;
            }
            QPushButton#file-ctrl-btn:disabled {
                background-color: #111820;
                color: #3d5570;
                border-color: #1a2535;
            }

            QLabel#time-display {
                color: #8aa8c8;
                font-family: 'Consolas', monospace;
                font-size: 10px;
                letter-spacing: 1px;
            }

            QLabel#file-chip {
                background-color: #151d27;
                color: #8aa8c8;
                border: 1px solid #1a2535;
                border-radius: 3px;
                padding: 4px 10px;
                font-family: 'Consolas', monospace;
                font-size: 10px;
            }

            /* Timeline Seek Slider */
            QSlider#file-slider {
                min-height: 18px;
            }
            QSlider#file-slider::groove:horizontal {
                border: 1px solid #1a2535;
                background: #151d27;
                height: 4px;
                border-radius: 2px;
            }
            QSlider#file-slider::sub-page:horizontal {
                background: #00cc88;
                border-radius: 2px;
            }
            QSlider#file-slider::handle:horizontal {
                background: #ffffff;
                border: 1px solid #00cc88;
                width: 10px;
                margin-top: -3px;
                margin-bottom: -3px;
                border-radius: 5px;
            }
            QSlider#file-slider::handle:horizontal:hover {
                background: #00e096;
            }

            /* ── Session info strip ───────────────────────────────────────── */
            QWidget#session-strip {
                background-color: #0d1117;
                border-bottom: 1px solid #151d27;
            }
            QLabel#strip-value {
                color: #5e7790;
                font-family: 'Consolas', monospace;
                font-size: 10px;
                letter-spacing: 1px;
                padding: 0 8px;
            }
            QLabel#strip-sep {
                color: #1a2535;
                padding: 0 2px;
            }

            /* ── Plot panels ─────────────────────────────────────────────── */
            QWidget#plot-area, QWidget#plot-panel {
                background-color: #0d1117;
            }

            QLabel#panel-header {
                background-color: #0d1117;
                color: #5e7790;
                font-weight: bold;
                font-family: 'Segoe UI', sans-serif;
                font-size: 9px;
                letter-spacing: 1px;
                padding-left: 12px;
                border-bottom: 1px solid #151d27;
            }

            /* ── Metric panel ─────────────────────────────────────────────── */
            QWidget#metric-area {
                background-color: #0d1117;
                border-top: 1px solid #151d27;
            }
            
            QFrame#metric-card {
                background-color: #151d27;
                border: 1px solid #1e293b;
                border-radius: 4px;
            }
            QFrame#metric-card:hover {
                border-color: #2d4560;
            }

            /* ── Status bar ──────────────────────────────────────────────── */
            QStatusBar#main-status-bar {
                background-color: #0d1117;
                color: #5e7790;
                font-size: 10px;
                border-top: 1px solid #151d27;
            }
            QLabel#sb-tech {
                color: #5e7790;
                font-family: 'Consolas', monospace;
                font-size: 10px;
            }

            /* ── Splitter handles ─────────────────────────────────────────── */
            QSplitter::handle { background-color: #151d27; }
        """)


# ── Utility ───────────────────────────────────────────────────────────────────

def _drain(q: queue.Queue) -> None:
    """Non-blocking queue drain."""
    while not q.empty():
        try:
            q.get_nowait()
        except queue.Empty:
            break
