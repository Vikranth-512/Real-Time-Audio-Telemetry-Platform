"""
file_controller.py
------------------
Orchestrator for audio file playback and real-time analysis.

Coordinates:
  - AudioDecoder: progressive/indexed float32 PCM decoding
  - AudioOutput: PortAudio stream playback with hardware clock tracking
  - AnalyzerWorker: canonical analysis pipeline
  - Feeder worker thread: bounded buffering, fan-out, seeking, and EOF draining
"""

import logging
import queue
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any

import numpy as np
from PySide6.QtCore import QObject, Signal, Slot, QTimer

from app.acquisition.audio_decoder import AudioDecoder, AudioDecoderError
from app.acquisition.audio_output import AudioOutput, AudioOutputError
from app.processing.analyzer_worker import AnalyzerWorker

logger = logging.getLogger(__name__)


class FilePlaybackState(Enum):
    IDLE              = "IDLE"
    READY             = "READY"
    PLAYING           = "PLAYING"
    PAUSED            = "PAUSED"
    ANALYZER_DRAINING = "ANALYZER_DRAINING"
    COMPLETE          = "COMPLETE"
    ERROR             = "ERROR"


class FilePlaybackController(QObject):
    """
    Manages audio file playback orchestration and synchronization with AnalyzerWorker.
    """

    playback_position = Signal(float, float)  # (current_seconds, total_duration)
    state_changed     = Signal(object)        # FilePlaybackState
    file_loaded       = Signal(dict)          # metadata dict
    error             = Signal(str)
    session_finalized = Signal(dict)
    _eof_signal       = Signal()

    def __init__(
        self,
        capture_queue: queue.Queue,
        analyzer_worker: Optional[AnalyzerWorker] = None,
        parent: Optional[QObject] = None
    ):
        super().__init__(parent)
        self._capture_queue = capture_queue
        self._worker = None

        self._decoder: Optional[AudioDecoder] = None
        self._audio_output: Optional[AudioOutput] = None
        self._output_device_index: Optional[int] = None

        self._state = FilePlaybackState.IDLE
        self._seek_origin_seconds = 0.0

        # Synchronization events
        self._reset_event = threading.Event()
        self._drain_event = threading.Event()
        self._eof_signal.connect(self._on_eof_main_thread)

        if analyzer_worker is not None:
            self.set_worker(analyzer_worker)

        # Feeder thread
        self._feeder_thread: Optional[threading.Thread] = None
        self._feeder_stop_req = threading.Event()

        # GUI timer for physical playback clock updates (~30 Hz)
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(33)
        self._clock_timer.timeout.connect(self._on_clock_tick)

    def set_worker(self, worker: Optional[AnalyzerWorker]) -> None:
        """Attach or detach an AnalyzerWorker instance."""
        if self._worker is not None:
            try:
                self._worker.reset_complete.disconnect(self._on_worker_reset_complete)
                self._worker.drain_complete.disconnect(self._on_worker_drain_complete)
            except Exception:
                pass
        self._worker = worker
        if self._worker is not None:
            self._worker.reset_complete.connect(self._on_worker_reset_complete)
            self._worker.drain_complete.connect(self._on_worker_drain_complete)
            if self._decoder:
                try:
                    self._worker.set_sample_rate(self._decoder.sample_rate)
                except Exception as exc:
                    logger.warning(f"Could not reconfigure analyzer sample rate: {exc}")

    @property
    def state(self) -> FilePlaybackState:
        return self._state

    @property
    def decoder(self) -> Optional[AudioDecoder]:
        return self._decoder

    @property
    def duration(self) -> float:
        return self._decoder.duration if self._decoder else 0.0

    @property
    def current_position(self) -> float:
        if not self._decoder:
            return 0.0
        if self._state == FilePlaybackState.COMPLETE:
            return self._decoder.duration
        if self._audio_output:
            elapsed = self._audio_output.get_elapsed_seconds()
            return min(self._decoder.duration, self._seek_origin_seconds + elapsed)
        return self._seek_origin_seconds

    def set_output_device_index(self, index: Optional[int]) -> None:
        """Update selected output device for subsequent playback."""
        self._output_device_index = index
        if self._audio_output and not self._audio_output.is_active:
            # Recreate AudioOutput with new device index
            self._audio_output.close()
            self._audio_output = AudioOutput(
                sample_rate=self._decoder.sample_rate,
                channels=self._decoder.channels,
                device_index=self._output_device_index
            )

    def open_file(self, file_path: Path, output_device_index: Optional[int] = None) -> bool:
        """
        Open and load an audio file. If a previous file is active, stops it first.
        """
        self.stop()

        if output_device_index is not None:
            self._output_device_index = output_device_index

        try:
            self._decoder = AudioDecoder(file_path)
        except Exception as exc:
            self._state = FilePlaybackState.ERROR
            self.state_changed.emit(self._state)
            self.error.emit(f"Failed to open audio file: {exc}")
            return False

        # Configure AnalyzerWorker to file's native sample rate while stopped
        if self._worker:
            try:
                self._worker.set_sample_rate(self._decoder.sample_rate)
            except Exception as exc:
                logger.warning(f"Could not reconfigure analyzer sample rate: {exc}")

        # Initialize AudioOutput
        if self._audio_output:
            self._audio_output.close()

        try:
            self._audio_output = AudioOutput(
                sample_rate=self._decoder.sample_rate,
                channels=self._decoder.channels,
                device_index=self._output_device_index
            )
        except Exception as exc:
            self._state = FilePlaybackState.ERROR
            self.state_changed.emit(self._state)
            self.error.emit(f"Failed to initialize audio output device: {exc}")
            return False

        self._seek_origin_seconds = 0.0
        self._state = FilePlaybackState.READY
        self.state_changed.emit(self._state)

        meta = {
            "filename": self._decoder.filename,
            "file_path": str(self._decoder.file_path),
            "format": self._decoder.format_name,
            "sample_rate": self._decoder.sample_rate,
            "channels": self._decoder.channels,
            "duration": self._decoder.duration,
            "total_frames": self._decoder.total_frames,
        }
        self.file_loaded.emit(meta)
        self.playback_position.emit(0.0, self._decoder.duration)
        return True

    @Slot()
    def play(self) -> None:
        """Start or resume audio file playback and real-time analysis."""
        if not self._decoder or not self._audio_output:
            return

        if self._state == FilePlaybackState.COMPLETE:
            # Restart from beginning
            self.seek(0.0)

        if self._state in (FilePlaybackState.READY, FilePlaybackState.PAUSED):
            if not self._audio_output.is_active:
                try:
                    self._audio_output.start()
                except Exception as exc:
                    self._state = FilePlaybackState.ERROR
                    self.state_changed.emit(self._state)
                    self.error.emit(f"Output playback error: {exc}")
                    return
            else:
                self._audio_output.resume()

            self._state = FilePlaybackState.PLAYING
            self.state_changed.emit(self._state)

            # Start feeder worker thread if not already running
            if self._feeder_thread is None or not self._feeder_thread.is_alive():
                self._feeder_stop_req.clear()
                self._feeder_thread = threading.Thread(
                    target=self._feeder_loop,
                    name="FilePlaybackFeeder",
                    daemon=True
                )
                self._feeder_thread.start()

            self._clock_timer.start()

    @Slot()
    def pause(self) -> None:
        """Pause playback without finalizing the session."""
        if self._state == FilePlaybackState.PLAYING:
            if self._audio_output:
                self._audio_output.pause()
            self._clock_timer.stop()
            self._state = FilePlaybackState.PAUSED
            self.state_changed.emit(self._state)

    @Slot()
    def toggle_play(self) -> None:
        if self._state == FilePlaybackState.PLAYING:
            self.pause()
        else:
            self.play()

    @Slot()
    def stop(self) -> None:
        """Stop playback, trigger drain, and finalize active session."""
        if self._state in (FilePlaybackState.IDLE,):
            return

        was_active = (self._state == FilePlaybackState.PLAYING)

        # Stop feeder thread cleanly
        self._feeder_stop_req.set()
        if self._feeder_thread and self._feeder_thread.is_alive():
            self._feeder_thread.join(timeout=0.3)
        self._feeder_thread = None

        self._clock_timer.stop()

        if self._audio_output:
            self._audio_output.stop()
            self._audio_output.flush()

        # Execute synchronized drain
        if was_active and self._worker:
            self._drain_event.clear()
            self._worker.request_drain()
            self._drain_event.wait(timeout=0.5)

        if self._decoder:
            self._decoder.seek_seconds(0.0)
            self._seek_origin_seconds = 0.0
            self._state = FilePlaybackState.READY
            self.playback_position.emit(0.0, self._decoder.duration)
        else:
            self._state = FilePlaybackState.IDLE

        self.state_changed.emit(self._state)

    @Slot(float)
    def seek(self, target_seconds: float) -> None:
        """
        Synchronized seek barrier protocol:
        Pause feeder & output -> flush output buffer -> reset analyzer state ->
        seek decoder -> reset output clock offset -> resume if previously playing.
        """
        if not self._decoder:
            return

        was_playing = (self._state == FilePlaybackState.PLAYING)

        # 1. Pause output & clock
        if self._audio_output:
            self._audio_output.pause()
        self._clock_timer.stop()

        # 2. Flush pending unplayed output buffer
        if self._audio_output:
            self._audio_output.flush()

        # 3. Synchronous analyzer reset barrier
        if self._worker:
            self._reset_event.clear()
            self._worker.request_reset()
            self._reset_event.wait(timeout=0.5)

        # 4. Seek decoder
        actual_pos = self._decoder.seek_seconds(target_seconds)
        self._seek_origin_seconds = actual_pos

        # 5. Reset hardware output clock reference
        if self._audio_output:
            self._audio_output.reset_elapsed()

        # 6. Update position
        self.playback_position.emit(actual_pos, self._decoder.duration)

        # 7. Resume if playing
        if was_playing:
            if self._audio_output:
                self._audio_output.resume()
            self._clock_timer.start()
            self._state = FilePlaybackState.PLAYING
            self.state_changed.emit(self._state)
        elif self._state == FilePlaybackState.COMPLETE:
            self._state = FilePlaybackState.PAUSED
            self.state_changed.emit(self._state)

    def close(self) -> None:
        """Clean shutdown of all file and audio resources."""
        self.stop()
        if self._audio_output:
            self._audio_output.close()
            self._audio_output = None
        if self._decoder:
            self._decoder.close()
            self._decoder = None
        self._state = FilePlaybackState.IDLE

    # ── Internal Worker Feeder Loop ───────────────────────────────────────────

    def _feeder_loop(self) -> None:
        """
        Background producer feeding AudioOutput and AnalyzerWorker capture queue.
        Implements bounded backpressure against the hardware output buffer.
        """
        logger.debug("FilePlaybackController feeder loop started")

        while not self._feeder_stop_req.is_set():
            if self._state != FilePlaybackState.PLAYING:
                time.sleep(0.01)
                continue

            # Backpressure: wait if output buffer is full
            if self._audio_output and self._audio_output.is_buffer_full():
                time.sleep(0.008)
                continue

            # Read next 1024 frames from decoder
            chunk = self._decoder.read_frames(1024)
            if len(chunk) == 0:
                # Reached EOF! Wait until audio output buffer finishes playing physically
                while self._audio_output and self._audio_output.get_buffered_frames() > 0:
                    if self._feeder_stop_req.is_set() or self._state != FilePlaybackState.PLAYING:
                        break
                    time.sleep(0.02)

                if not self._feeder_stop_req.is_set() and self._state == FilePlaybackState.PLAYING:
                    self._handle_eof()
                break

            # 1. Output multi-channel PCM to speakers
            if self._audio_output:
                self._audio_output.write(chunk)

            # 2. Downmix to mono for AnalyzerWorker
            if chunk.shape[1] > 1:
                mono_frame = np.mean(chunk, axis=1).astype(np.float32)
            else:
                mono_frame = chunk[:, 0].astype(np.float32)

            frame_start_index = self._decoder.current_frame - len(chunk)
            file_timestamp = float(frame_start_index / self._decoder.sample_rate)

            # Enqueue to analyzer capture queue: (timestamp, mono_frame)
            try:
                self._capture_queue.put((file_timestamp, mono_frame), timeout=0.1)
            except queue.Full:
                logger.warning("Capture queue full during file playback — analysis overrun detected")

        logger.debug("FilePlaybackController feeder loop ended")

    def _handle_eof(self) -> None:
        """Execute the EOF drain protocol from feeder thread."""
        self._state = FilePlaybackState.ANALYZER_DRAINING
        self.state_changed.emit(self._state)

        # Trigger drain in AnalyzerWorker if attached
        if self._worker:
            self._drain_event.clear()
            self._worker.request_drain()
            self._drain_event.wait(timeout=1.0)

        # Notify GUI thread to stop clock timer and mark complete
        self._eof_signal.emit()

    @Slot()
    def _on_eof_main_thread(self) -> None:
        self._clock_timer.stop()
        self._state = FilePlaybackState.COMPLETE
        self.state_changed.emit(self._state)
        if self._decoder:
            self.playback_position.emit(self._decoder.duration, self._decoder.duration)

    @Slot()
    def _on_clock_tick(self) -> None:
        if self._state == FilePlaybackState.PLAYING and self._decoder and self._audio_output:
            elapsed = self._audio_output.get_elapsed_seconds()
            pos = min(self._decoder.duration, self._seek_origin_seconds + elapsed)
            self.playback_position.emit(pos, self._decoder.duration)

    @Slot()
    def _on_worker_reset_complete(self) -> None:
        self._reset_event.set()

    @Slot()
    def _on_worker_drain_complete(self) -> None:
        self._drain_event.set()
