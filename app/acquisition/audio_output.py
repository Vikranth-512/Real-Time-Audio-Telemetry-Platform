"""
audio_output.py
---------------
Non-blocking audio output stream wrapper using pyaudiowpatch.
Maintains a bounded playback buffer, feeds PortAudio via callback,
and provides physical consumed-frame tracking as the authoritative hardware clock.
"""

import logging
import threading
from collections import deque
from typing import Optional

import numpy as np
import pyaudiowpatch as pyaudio

logger = logging.getLogger(__name__)


class AudioOutputError(Exception):
    """Raised when an audio output stream cannot be initialized."""
    pass


class AudioOutput:
    """
    Manages non-blocking audio playback to a selected Windows output device.
    """

    def __init__(
        self,
        sample_rate: int,
        channels: int,
        device_index: Optional[int] = None,
        buffer_duration_seconds: float = 1.5
    ):
        self._sample_rate = int(sample_rate)
        self._channels = max(1, int(channels))
        self._device_index = device_index
        self._max_buffer_frames = int(self._sample_rate * buffer_duration_seconds)

        self._pa = pyaudio.PyAudio()
        self._stream: Optional[pyaudio.Stream] = None
        self._lock = threading.Lock()

        self._buffer: deque[np.ndarray] = deque()
        self._buffered_frames = 0
        self._consumed_frames = 0
        self._is_active = False
        self._is_paused = False

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def channels(self) -> int:
        return self._channels

    @property
    def is_active(self) -> bool:
        return self._is_active

    @property
    def is_paused(self) -> bool:
        return self._is_paused

    def is_buffer_full(self) -> bool:
        """Returns True if the output ring buffer has reached high watermark."""
        with self._lock:
            return self._buffered_frames >= self._max_buffer_frames

    def get_buffered_frames(self) -> int:
        with self._lock:
            return self._buffered_frames

    def get_elapsed_seconds(self) -> float:
        """
        Authoritative hardware clock: physical frames consumed by PortAudio callback
        divided by sample rate.
        """
        with self._lock:
            return float(self._consumed_frames / self._sample_rate) if self._sample_rate > 0 else 0.0

    def reset_elapsed(self) -> None:
        """Reset the consumed frame counter (e.g. after a seek)."""
        with self._lock:
            self._consumed_frames = 0

    def write(self, pcm: np.ndarray) -> int:
        """
        Write a block of PCM frames into the playback buffer.
        pcm must have shape (N, channels) or (N,) for mono.
        Returns the number of frames accepted.
        """
        if pcm is None or len(pcm) == 0:
            return 0

        # Ensure 2D (frames, channels)
        if pcm.ndim == 1:
            pcm = pcm.reshape(-1, 1)

        # Enforce channels match stream
        n_frames, n_ch = pcm.shape
        if n_ch != self._channels:
            if n_ch == 1 and self._channels == 2:
                # Duplicate mono to stereo
                pcm = np.repeat(pcm, 2, axis=1)
            elif n_ch > 2 and self._channels == 2:
                pcm = pcm[:, :2]
            elif n_ch == 2 and self._channels == 1:
                pcm = np.mean(pcm, axis=1, keepdims=True)

        pcm_contiguous = np.ascontiguousarray(pcm, dtype=np.float32)

        with self._lock:
            self._buffer.append(pcm_contiguous)
            self._buffered_frames += n_frames

        return n_frames

    def flush(self) -> None:
        """Flush all pending unplayed frames from the buffer."""
        with self._lock:
            self._buffer.clear()
            self._buffered_frames = 0

    def start(self) -> None:
        """Open and start the PortAudio output stream."""
        with self._lock:
            if self._stream is not None:
                return

            try:
                self._stream = self._pa.open(
                    format=pyaudio.paFloat32,
                    channels=self._channels,
                    rate=self._sample_rate,
                    output=True,
                    output_device_index=self._device_index,
                    frames_per_buffer=1024,
                    stream_callback=self._output_callback
                )
                self._stream.start_stream()
                self._is_active = True
                self._is_paused = False
                logger.info(
                    f"AudioOutput stream started — device={self._device_index}, "
                    f"{self._sample_rate} Hz, {self._channels} ch"
                )
            except Exception as exc:
                raise AudioOutputError(f"Failed to open audio output stream: {exc}") from exc

    def pause(self) -> None:
        """Pause playback."""
        with self._lock:
            self._is_paused = True

    def resume(self) -> None:
        """Resume playback."""
        with self._lock:
            self._is_paused = False

    def stop(self) -> None:
        """Stop and close the output stream cleanly."""
        with self._lock:
            self._is_active = False
            self._is_paused = False
            if self._stream is not None:
                try:
                    self._stream.stop_stream()
                    self._stream.close()
                except Exception as exc:
                    logger.warning(f"Error stopping output stream: {exc}")
                finally:
                    self._stream = None

    def close(self) -> None:
        """Terminate stream and PortAudio instance."""
        self.stop()
        self.flush()
        try:
            self._pa.terminate()
        except Exception:
            pass

    def _output_callback(self, in_data, frame_count, time_info, status):
        """
        PortAudio callback running on high-priority audio thread.
        Consumes frames from the ring buffer or generates silence on underrun/pause.
        """
        needed_frames = frame_count
        output_chunks = []
        frames_obtained = 0

        with self._lock:
            if not self._is_paused and self._buffer:
                while self._buffer and frames_obtained < needed_frames:
                    chunk = self._buffer[0]
                    chunk_frames = len(chunk)
                    needed = needed_frames - frames_obtained

                    if chunk_frames <= needed:
                        output_chunks.append(chunk)
                        frames_obtained += chunk_frames
                        self._buffer.popleft()
                        self._buffered_frames -= chunk_frames
                    else:
                        output_chunks.append(chunk[:needed])
                        self._buffer[0] = chunk[needed:]
                        frames_obtained += needed
                        self._buffered_frames -= needed

            self._consumed_frames += frames_obtained

        if frames_obtained > 0:
            obtained_pcm = np.concatenate(output_chunks, axis=0) if len(output_chunks) > 1 else output_chunks[0]
            if frames_obtained < needed_frames:
                # Pad remainder with silence
                silence_frames = needed_frames - frames_obtained
                silence = np.zeros((silence_frames, self._channels), dtype=np.float32)
                full_pcm = np.concatenate([obtained_pcm, silence], axis=0)
            else:
                full_pcm = obtained_pcm
        else:
            # Full silence
            full_pcm = np.zeros((needed_frames, self._channels), dtype=np.float32)

        return (full_pcm.tobytes(), pyaudio.paContinue)
