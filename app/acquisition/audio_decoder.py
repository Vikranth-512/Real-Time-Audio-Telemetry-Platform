"""
audio_decoder.py
----------------
Robust local audio file decoder powered by miniaudio.
Decodes MP3, WAV, FLAC, and OGG files into 32-bit float PCM buffers.
Provides frame-addressable seeking, partial final-chunk reading, and metadata.
"""

import logging
from pathlib import Path
from typing import Optional, Tuple
import numpy as np
import miniaudio

logger = logging.getLogger(__name__)


class AudioDecoderError(Exception):
    """Raised when an audio file cannot be opened or decoded."""
    pass


class AudioDecoder:
    """
    Decodes an audio file into normalized 32-bit float PCM array.
    Shape: (total_frames, channels), values in [-1.0, 1.0].
    """

    def __init__(self, file_path: Path):
        self._file_path = Path(file_path)
        if not self._file_path.exists():
            raise FileNotFoundError(f"Audio file not found: {self._file_path}")

        try:
            # Query native file properties to avoid unintended resampling
            info = miniaudio.get_file_info(str(self._file_path))
            native_channels = int(info.nchannels)
            native_rate = int(info.sample_rate)

            # Decode file directly to float32 samples at native rate and channels
            decoded = miniaudio.decode_file(
                str(self._file_path),
                output_format=miniaudio.SampleFormat.FLOAT32,
                nchannels=native_channels,
                sample_rate=native_rate,
            )
        except Exception as exc:
            raise AudioDecoderError(f"Failed to decode {self._file_path.name}: {exc}") from exc

        self._sample_rate = int(decoded.sample_rate)
        self._channels = int(decoded.nchannels)
        self._total_frames = int(decoded.num_frames)
        self._duration = float(self._total_frames / self._sample_rate) if self._sample_rate > 0 else 0.0

        # Reshape flat samples array into 2D (num_frames, channels)
        arr = np.frombuffer(decoded.samples, dtype=np.float32)
        if self._channels > 1:
            self._pcm_data = arr.reshape(-1, self._channels)
        else:
            self._pcm_data = arr.reshape(-1, 1)

        self._current_frame = 0
        self._format_name = self._file_path.suffix.lstrip(".").upper() or "AUDIO"

        logger.info(
            f"AudioDecoder opened '{self._file_path.name}': "
            f"{self._sample_rate} Hz, {self._channels} ch, "
            f"{self._duration:.2f}s ({self._total_frames} frames)"
        )

    @property
    def file_path(self) -> Path:
        return self._file_path

    @property
    def filename(self) -> str:
        return self._file_path.name

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def channels(self) -> int:
        return self._channels

    @property
    def duration(self) -> float:
        return self._duration

    @property
    def total_frames(self) -> int:
        return self._total_frames

    @property
    def format_name(self) -> str:
        return self._format_name

    @property
    def current_frame(self) -> int:
        return self._current_frame

    @property
    def current_seconds(self) -> float:
        return float(self._current_frame / self._sample_rate) if self._sample_rate > 0 else 0.0

    @property
    def is_eof(self) -> bool:
        return self._current_frame >= self._total_frames

    def read_frames(self, count: int) -> np.ndarray:
        """
        Read up to `count` frames from current position.
        Returns np.ndarray of shape (N, channels) where 0 <= N <= count.
        Returns shape (0, channels) when at EOF.
        """
        if self._pcm_data is None or self.is_eof:
            return np.empty((0, self._channels), dtype=np.float32)

        remaining = self._total_frames - self._current_frame
        n = max(0, min(int(count), remaining))
        if n == 0:
            return np.empty((0, self._channels), dtype=np.float32)

        chunk = self._pcm_data[self._current_frame: self._current_frame + n]
        self._current_frame += n
        return chunk

    def seek_seconds(self, target_seconds: float) -> float:
        """
        Seek to the specified time in seconds, clamped to [0.0, duration].
        Returns the actual resulting position in seconds.
        """
        clamped_sec = max(0.0, min(float(target_seconds), self._duration))
        target_frame = int(round(clamped_sec * self._sample_rate))
        self._current_frame = max(0, min(target_frame, self._total_frames))
        return self.current_seconds

    def close(self) -> None:
        """Release decoded sample memory."""
        self._pcm_data = None
