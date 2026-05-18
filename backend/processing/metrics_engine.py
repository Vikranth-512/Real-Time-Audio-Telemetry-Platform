import math
import numpy as np
from typing import List, Dict
from collections import deque


class MetricsEngine:

    SAMPLE_RATE = 48000
    # Demo sender uses 12-bit style samples centered at 2048 (0..4095).
    # Use OFFSET=2048 so silence (baseline) becomes zero after normalization
    # and SCALE maps to [-1, 1] range approximately.
    OFFSET = 2048
    SCALE = 1.0 / 2048.0

    PACKET_SIZE = 1024
    ENVELOPE_RATE = SAMPLE_RATE / PACKET_SIZE

    SILENCE_THRESHOLD = 0.005

    def __init__(self):

        self.sample_buffer = deque(maxlen=48000)
        self.energy_buffer = deque(maxlen=400)
        self.beat_intervals = deque(maxlen=10)

    def _hann_window(self, n: int) -> np.ndarray:
        if n <= 1:
            return np.ones((max(n, 1),), dtype=float)
        i = np.arange(n, dtype=float)
        return 0.5 - 0.5 * np.cos(2.0 * np.pi * i / (n - 1))

    def _fft_magnitude(self, n: int = 1024):

        if len(self.sample_buffer) < n:
            return None, None

        data = np.array(list(self.sample_buffer)[-n:], dtype=float)

        data = (data - self.OFFSET) * self.SCALE

        data = data - np.mean(data)

        data = np.concatenate(([0], np.diff(data)))

        data = data * self._hann_window(n)

        spec = np.fft.rfft(data)
        mags = np.abs(spec)
        freqs = np.fft.rfftfreq(n, 1 / self.SAMPLE_RATE)

        return freqs, mags

    def compute_peak_frequency(self, freqs: np.ndarray, mags: np.ndarray) -> float:
        if freqs is None or mags is None or len(mags) == 0:
            return 0.0

        valid = np.where((freqs >= 20) & (freqs <= 5000))[0]

        if len(valid) == 0:
            return 0.0

        if np.max(mags[valid]) < 1e-6:
            return 0.0

        idx = valid[int(np.argmax(mags[valid]))]

        return float(freqs[idx])

    def compute_spectral_centroid(self, freqs: np.ndarray, mags: np.ndarray) -> float:
        if freqs is None or mags is None or len(mags) == 0:
            return 0.0

        valid = np.where(freqs >= 20)[0]

        if len(valid) == 0:
            return 0.0

        m = mags[valid] ** 2
        s = float(np.sum(m))

        if s <= 1e-12:
            return 0.0

        return float(np.sum(freqs[valid] * m) / s)

    def compute_spectral_rolloff(self, freqs: np.ndarray, mags: np.ndarray, pct: float = 0.85) -> float:
        if freqs is None or mags is None or len(mags) == 0:
            return 0.0

        valid = np.where(freqs >= 20)[0]

        if len(valid) == 0:
            return 0.0

        m = mags[valid] ** 2
        total = float(np.sum(m))

        if total <= 1e-12:
            return 0.0

        target = total * pct
        cumsum = np.cumsum(m)

        idx = int(np.searchsorted(cumsum, target))
        idx = min(max(idx, 0), len(valid) - 1)

        return float(freqs[valid][idx])

    def compute_spectral_flatness(self, freqs: np.ndarray, mags: np.ndarray) -> float:
        if freqs is None or mags is None or len(mags) == 0:
            return 0.0

        valid = np.where(freqs >= 20)[0]

        if len(valid) == 0:
            return 0.0

        m = mags[valid].astype(float) + 1e-12

        geo = float(np.exp(np.mean(np.log(m))))
        arith = float(np.mean(m))

        if arith <= 1e-12:
            return 0.0

        return float(geo / arith)

    def _normalize(self, samples: List[int]):
        return [(s - self.OFFSET) * self.SCALE for s in samples]

    def calculate_rms(self, normalized):

        if not normalized:
            return 0.0

        total = 0.0

        for v in normalized:
            total += v * v

        return math.sqrt(total / len(normalized))

    def calculate_peak(self, normalized):

        peak = 0.0

        for v in normalized:
            a = abs(v)
            if a > peak:
                peak = a

        return peak

    def calculate_zcr(self, normalized):
        if not normalized or len(normalized) < 2:
            return 0.0
        
        arr = np.array(normalized)
        crossings = np.nonzero(np.diff(arr > 0))[0]
        zcr = len(crossings) / (len(normalized) - 1)
        return float(zcr)

    def estimate_frequency(self, normalized):

        freqs, mags = self._fft_magnitude(1024)

        return self.compute_peak_frequency(freqs, mags)

    def update_energy_envelope(self, normalized):

        if not normalized:
            return

        energy = 0.0

        for v in normalized:
            energy += v * v

        energy /= len(normalized)

        if energy < self.SILENCE_THRESHOLD:
            return

        self.energy_buffer.append(energy)

    def autocorrelation_bpm(self):

        if len(self.energy_buffer) < 60:
            return 0.0

        energies = np.array(self.energy_buffer, dtype=float)

        kernel_size = 5
        kernel = np.ones(kernel_size) / kernel_size
        smooth = np.convolve(energies, kernel, mode='same')

        mean_energy = np.mean(smooth)
        std_energy = np.std(smooth)

        threshold = mean_energy + 0.5 * std_energy

        peaks = []

        for i in range(1, len(smooth) - 1):
            if smooth[i] > threshold and smooth[i] > smooth[i - 1] and smooth[i] > smooth[i + 1]:
                peaks.append(i)

        if len(peaks) < 3:
            return 0.0

        env_rate = self.ENVELOPE_RATE
        intervals = []

        for i in range(1, len(peaks)):
            interval = (peaks[i] - peaks[i - 1]) / env_rate

            if 0.3 < interval < 1.5:
                intervals.append(interval)

        if len(intervals) < 2:
            return 0.0

        avg_interval = sum(intervals) / len(intervals)
        bpm = 60.0 / avg_interval

        if bpm < 40 or bpm > 200:
            return 0.0

        return round(bpm, 1)

    def calculate_metrics(self, samples: List[int], timestamp: float) -> Dict[str, float]:

        if not samples:
            return {
                "rms": 0.0,
                "peak": 0.0,
                "frequency": 0.0,
                "bpm": 0.0,
                "zcr": 0.0,
                "peak_frequency": 0.0,
                "spectral_centroid": 0.0,
                "spectral_rolloff": 0.0,
                "spectral_flatness": 0.0,
            }

        normalized = self._normalize(samples)

        rms = self.calculate_rms(normalized)

        if rms < self.SILENCE_THRESHOLD:
            return {
                "rms": 0.0,
                "peak": 0.0,
                "frequency": 0.0,
                "bpm": 0.0,
                "zcr": 0.0,
                "peak_frequency": 0.0,
                "spectral_centroid": 0.0,
                "spectral_rolloff": 0.0,
                "spectral_flatness": 0.0,
            }

        self.sample_buffer.extend(samples)

        peak = self.calculate_peak(normalized)
        frequency = self.estimate_frequency(normalized)
        zcr = self.calculate_zcr(normalized)

        self.update_energy_envelope(normalized)
        bpm = self.autocorrelation_bpm()

        freqs, mags = self._fft_magnitude(1024)

        peak_frequency = self.compute_peak_frequency(freqs, mags)
        spectral_centroid = self.compute_spectral_centroid(freqs, mags)
        spectral_rolloff = self.compute_spectral_rolloff(freqs, mags, pct=0.85)
        spectral_flatness = self.compute_spectral_flatness(freqs, mags)

        # apply no artificial gain by default
        gain = 1
        rms *= gain
        peak *= gain

        rms = min(rms, 1.0)
        peak = min(peak, 1.0)

        return {
            # report normalized rms/peak in range ~0..1 (no *10 scaling)
            "rms": round(rms, 3),
            "peak": round(peak, 3),
            "frequency": round(frequency, 2),
            "bpm": bpm,
            "zcr": round(zcr, 4),
            "peak_frequency": round(peak_frequency, 2),
            "spectral_centroid": round(spectral_centroid, 2),
            "spectral_rolloff": round(spectral_rolloff, 2),
            "spectral_flatness": round(spectral_flatness, 4),
        }

    def get_session_summary(self):

        if not self.sample_buffer:
            return {
                "avg_rms": 0.0,
                "max_amplitude": 0.0,
                "avg_bpm": 0.0,
            }

        normalized = [(s - self.OFFSET) * self.SCALE for s in self.sample_buffer]

        rms = self.calculate_rms(normalized)
        peak = self.calculate_peak(normalized)

        avg_bpm = 0.0

        if len(self.beat_intervals) >= 2:
            avg_interval = sum(self.beat_intervals) / len(self.beat_intervals)
            avg_bpm = 60.0 / avg_interval

        return {
            "avg_rms": round(rms, 3),
            "max_amplitude": round(peak, 3),
            "avg_bpm": round(avg_bpm, 1),
        }

    def reset(self):

        self.sample_buffer.clear()
        self.energy_buffer.clear()
        self.beat_intervals.clear()