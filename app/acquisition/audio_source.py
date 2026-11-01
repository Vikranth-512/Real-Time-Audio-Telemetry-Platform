"""
audio_source.py
---------------
Captures system audio via sounddevice callback API and puts frames into a
thread-safe queue for downstream consumption by AnalyzerWorker.

Design rules enforced here:
  - The sounddevice callback runs on a PortAudio internal thread.
  - The callback MUST NOT perform any of: FFT, file I/O, Qt calls, logging,
    pandas/pyarrow operations, or any expensive computation.
  - It does exactly: cast to float32 → copy into numpy array → queue.put_nowait.

Usage:
    q = queue.Queue(maxsize=64)
    src = AudioSource(device_index=0, queue=q)
    src.start()
    # ... processing loop reads from q ...
    src.stop()
"""

import queue
import logging
import threading
import numpy as np
import pyaudiowpatch as pyaudio
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ── Application-level constants (not user-configurable) ───────────────────────
SAMPLE_RATE  = 48_000   # Hz — must match downstream MetricsEngine expectations
PACKET_SIZE  = 1_024    # samples per callback frame (~21.3 ms)
CHANNELS     = 1        # mono capture


class AudioSource:
    """
    Wraps a sounddevice InputStream with a callback-based capture loop.

    The captured frames are placed into `out_queue` as (timestamp, np.ndarray)
    tuples. The array dtype is always float32, values normalised to [-1.0, 1.0].

    Thread safety: start() and stop() are safe to call from any thread.
    The callback itself runs on PortAudio's internal thread.
    """

    def __init__(self, out_queue: queue.Queue, device_index: Optional[int] = None):
        self._queue        = out_queue
        self._device_index = device_index
        self._pa           = pyaudio.PyAudio()
        self._stream       = None
        self._lock         = threading.Lock()
        self._running      = False

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        with self._lock:
            if self._running:
                logger.warning("AudioSource.start() called while already running")
                return
            
            # WASAPI Loopback requires specific channel count, usually 2 for stereo output.
            # We will open with mono if possible, but fallback to stereo logic in the callback.
            try:
                self._stream = self._pa.open(
                    format=pyaudio.paFloat32,
                    channels=CHANNELS,
                    rate=SAMPLE_RATE,
                    input=True,
                    input_device_index=self._device_index,
                    frames_per_buffer=PACKET_SIZE,
                    stream_callback=self._callback
                )
            except Exception as e:
                # If mono fails (e.g. for WASAPI stereo devices), try stereo
                logger.warning(f"Opening with {CHANNELS} channels failed, trying 2 channels: {e}")
                self._stream = self._pa.open(
                    format=pyaudio.paFloat32,
                    channels=2,
                    rate=SAMPLE_RATE,
                    input=True,
                    input_device_index=self._device_index,
                    frames_per_buffer=PACKET_SIZE,
                    stream_callback=self._callback
                )

            self._stream.start_stream()
            self._running = True
            logger.info(
                f"AudioSource started — device={self._device_index}, "
                f"{SAMPLE_RATE}Hz, {PACKET_SIZE} samples/frame"
            )

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            if self._stream is not None:
                self._stream.stop_stream()
                self._stream.close()
                self._stream = None
            self._running = False
            logger.info("AudioSource stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    # ── PortAudio callback (runs on audio thread — keep fast) ─────────────────

    def _callback(
        self,
        in_data,
        frame_count,
        time_info,
        status
    ):
        if status:
            # Log overflow/underflow without raising — never block the audio thread
            logger.debug(f"AudioSource status: {status}")

        if in_data is None:
            return (None, pyaudio.paContinue)

        # Decode raw bytes to float32
        frame = np.frombuffer(in_data, dtype=np.float32)
        
        # If it's stereo (or more), we only take the first channel
        actual_channels = len(frame) // frame_count
        if actual_channels > 1:
            frame = frame[::actual_channels].copy()
        else:
            frame = frame.copy()

        # capture_time is the timestamp of the *first sample* in this frame
        capture_time: float = time_info.get('input_buffer_adc_time', time.time())

        try:
            self._queue.put_nowait((capture_time, frame))
        except queue.Full:
            # Drop the frame rather than blocking the audio callback
            logger.debug("AudioSource: queue full, frame dropped")
            
        return (None, pyaudio.paContinue)
