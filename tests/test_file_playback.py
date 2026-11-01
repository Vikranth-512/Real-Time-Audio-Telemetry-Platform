"""
test_file_playback.py
---------------------
Unit and integration tests for audio file playback and real-time analysis pipeline:
  1. AudioDecoder decoding, metadata extraction, seeking, and EOF handling
  2. AudioOutput ring buffer backpressure, flushing, and hardware clock
  3. MetricsEngine multi-sample-rate calculation (44.1 kHz, 48 kHz, 32 kHz)
  4. AnalyzerWorker sample rate dynamic reconfiguration and barrier protocols (reset & drain)
  5. Golden pipeline equivalence test (synthetic sine tone reference)
  6. FilePlaybackController state transitions and fan-out
"""

import sys
import queue
import time
import wave
import tempfile
from pathlib import Path
import numpy as np

# Ensure project root & backend are in sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "backend"))

from PySide6.QtWidgets import QApplication

from app.acquisition.audio_decoder import AudioDecoder, AudioDecoderError
from app.acquisition.audio_output import AudioOutput
from app.acquisition.file_controller import FilePlaybackController, FilePlaybackState
from app.processing.analyzer_worker import AnalyzerWorker
from backend.processing.metrics_engine import MetricsEngine


def get_qapp():
    """Ensure a Qt Application instance exists for signal/timer testing."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def _create_synthetic_wav(
    path: Path,
    duration_s: float = 2.0,
    sample_rate: int = 44100,
    channels: int = 2,
    freq: float = 440.0
) -> None:
    """Generate a clean synthetic 16-bit PCM WAV file."""
    num_samples = int(duration_s * sample_rate)
    t = np.arange(num_samples) / sample_rate
    sine = (np.sin(2 * np.pi * freq * t) * 32767 * 0.6).astype(np.int16)
    
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        if channels == 1:
            wf.writeframes(sine.tobytes())
        else:
            stereo = np.column_stack((sine, sine)).flatten()
            wf.writeframes(stereo.tobytes())


class TestAudioDecoder:

    def test_open_and_metadata(self, tmp_path: Path):
        wav_path = tmp_path / "test_44100.wav"
        _create_synthetic_wav(wav_path, duration_s=1.5, sample_rate=44100, channels=2, freq=440.0)

        decoder = AudioDecoder(wav_path)
        assert decoder.sample_rate == 44100
        assert decoder.channels == 2
        assert abs(decoder.duration - 1.5) < 0.05
        assert decoder.total_frames == int(1.5 * 44100)
        assert not decoder.is_eof
        decoder.close()

    def test_read_frames_normalization(self, tmp_path: Path):
        wav_path = tmp_path / "test_norm.wav"
        _create_synthetic_wav(wav_path, duration_s=1.0, sample_rate=44100, channels=1, freq=1000.0)

        decoder = AudioDecoder(wav_path)
        chunk = decoder.read_frames(1024)
        assert len(chunk) == 1024
        assert chunk.shape == (1024, 1)
        assert chunk.dtype == np.float32
        # Amplitude was 0.6
        assert np.max(np.abs(chunk)) <= 0.65
        assert np.max(np.abs(chunk)) >= 0.55
        decoder.close()

    def test_read_until_eof(self, tmp_path: Path):
        wav_path = tmp_path / "test_eof.wav"
        # 2500 samples total -> two 1024 chunks + one 452 chunk + empty chunk
        _create_synthetic_wav(wav_path, duration_s=(2500 / 48000), sample_rate=48000, channels=2)

        decoder = AudioDecoder(wav_path)
        chunk1 = decoder.read_frames(1024)
        assert len(chunk1) == 1024
        assert not decoder.is_eof

        chunk2 = decoder.read_frames(1024)
        assert len(chunk2) == 1024
        assert not decoder.is_eof

        chunk3 = decoder.read_frames(1024)
        assert len(chunk3) == 452
        assert decoder.is_eof

        chunk4 = decoder.read_frames(1024)
        assert len(chunk4) == 0
        assert decoder.is_eof
        decoder.close()

    def test_seek_accuracy_and_bounds(self, tmp_path: Path):
        wav_path = tmp_path / "test_seek.wav"
        _create_synthetic_wav(wav_path, duration_s=3.0, sample_rate=44100, channels=2)

        decoder = AudioDecoder(wav_path)
        # Seek to 1.0s
        actual = decoder.seek_seconds(1.0)
        assert abs(actual - 1.0) < 0.001
        assert decoder.current_frame == 44100
        assert not decoder.is_eof

        # Seek beyond duration clamps to duration
        actual_end = decoder.seek_seconds(5.0)
        assert abs(actual_end - decoder.duration) < 0.001
        assert decoder.is_eof

        # Seek negative clamps to 0.0
        actual_start = decoder.seek_seconds(-2.0)
        assert actual_start == 0.0
        assert decoder.current_frame == 0
        assert not decoder.is_eof
        decoder.close()


class TestAudioOutput:

    def test_ring_buffer_and_backpressure(self):
        output = AudioOutput(sample_rate=44100, channels=2)
        # Bounded ring buffer: 44100 * 2 frames capacity
        assert not output.is_buffer_full()

        # Write small chunk
        test_data = np.zeros((1024, 2), dtype=np.float32)
        output.write(test_data)
        assert output.get_buffered_frames() == 1024

        # Flush clears buffer
        output.flush()
        assert output.get_buffered_frames() == 0
        output.close()

    def test_hardware_elapsed_clock(self):
        output = AudioOutput(sample_rate=48000, channels=1)
        assert output.get_elapsed_seconds() == 0.0

        # Simulate frame consumption
        output._consumed_frames = 96000
        assert abs(output.get_elapsed_seconds() - 2.0) < 0.001

        output.reset_elapsed()
        assert output.get_elapsed_seconds() == 0.0
        output.close()


class TestMetricsEngineMultiSampleRate:

    def test_dominant_frequency_detection_at_sample_rates(self):
        for sr in [32000, 44100, 48000]:
            engine = MetricsEngine(sample_rate=sr)
            assert engine.sample_rate == sr

            # 440 Hz test tone
            target_freq = 440.0
            t = np.arange(1024, dtype=np.float32) / sr
            tone = (np.sin(2 * np.pi * target_freq * t) * 0.5).astype(np.float32)

            metrics = engine.calculate_metrics(tone.tolist(), 0.0)
            assert "frequency" in metrics
            assert "rms" in metrics
            # Detected dominant frequency should be within FFT bin resolution (sr / 1024 ~ 43 Hz)
            detected_freq = metrics["frequency"]
            bin_width = sr / 1024.0
            assert abs(detected_freq - target_freq) <= bin_width * 1.5

    def test_dynamic_sample_rate_switch(self):
        engine = MetricsEngine(sample_rate=48000)
        assert engine.sample_rate == 48000

        engine.set_sample_rate(44100)
        assert engine.sample_rate == 44100
        engine.calculate_metrics([0.1] * 1024, 0.0)
        freqs, _ = engine._fft_magnitude(1024)
        assert freqs is not None
        assert freqs[-1] == 44100 / 2.0


class TestAnalyzerWorkerProtocols:

    def test_sample_rate_reconfiguration_and_barrier(self):
        _app = get_qapp()
        capture_q = queue.Queue()
        storage_q = queue.Queue()

        worker = AnalyzerWorker(capture_queue=capture_q, storage_queue=storage_q, sample_rate=48000)
        assert worker.sample_rate == 48000

        # Reconfigure while stopped
        worker.set_sample_rate(44100)
        assert worker.sample_rate == 44100

        # Test reset protocol
        reset_fired = []
        worker.reset_complete.connect(lambda: reset_fired.append(True))
        capture_q.put((0.0, np.zeros(1024, dtype=np.float32)))
        assert not capture_q.empty()

        worker.request_reset()
        assert capture_q.empty()
        assert len(reset_fired) == 1

        # Test drain protocol
        drain_fired = []
        worker.drain_complete.connect(lambda: drain_fired.append(True))
        worker.request_drain()
        assert len(drain_fired) == 1


class TestGoldenPipelineEquivalence:

    def test_sine_pipeline_equivalence(self):
        """
        Verify that feeding a 440 Hz sine signal yields accurate mathematical metrics
        matching analytical ground truth.
        """
        sr = 44100
        engine = MetricsEngine(sample_rate=sr)
        duration = 1.0
        num_samples = int(duration * sr)
        t = np.arange(num_samples) / sr
        freq = 440.0
        amplitude = 0.5
        signal = (np.sin(2 * np.pi * freq * t) * amplitude).astype(np.float32)

        # Process in chunks of 1024
        all_metrics = []
        for start_idx in range(0, num_samples - 1024, 1024):
            chunk = signal[start_idx:start_idx + 1024]
            timestamp = start_idx / sr
            m = engine.calculate_metrics(chunk.tolist(), timestamp)
            all_metrics.append(m)

        assert len(all_metrics) > 0
        # Peak of sine wave with amplitude 0.5 should be ~0.5
        peaks = [m["peak"] for m in all_metrics]
        assert np.allclose(peaks, 0.5, atol=0.01)

        # RMS of sine wave with amplitude 0.5 is 0.5 / sqrt(2) ~ 0.3535
        expected_rms = 0.5 / np.sqrt(2)
        rms_vals = [m["rms"] for m in all_metrics]
        assert np.allclose(rms_vals, expected_rms, atol=0.05)


class TestPartialFramesAndSignalIntegrity:

    def test_partial_frame_and_empty_frame_safety(self):
        _app = get_qapp()
        capture_q = queue.Queue()
        storage_q = queue.Queue()
        worker = AnalyzerWorker(capture_queue=capture_q, storage_queue=storage_q, sample_rate=48000)

        errors_emitted = []
        worker.error_occurred.connect(lambda msg: errors_emitted.append(msg))

        fft_results = []
        worker.new_fft.connect(lambda f, m: fft_results.append((f, m)))

        # 1. Empty frame should be safely ignored
        empty_frame = np.array([], dtype=np.float32)
        worker._process_frame(0.0, empty_frame)
        assert len(errors_emitted) == 0
        assert len(fft_results) == 0

        # 2. Partial frame (681 samples) should be processed with zero-padded FFT and no errors
        partial_frame = np.sin(np.linspace(0, 10 * np.pi, 681, dtype=np.float32)).astype(np.float32)
        original_copy = partial_frame.copy()

        worker._last_fft_emit = 0.0  # Force emit
        worker._process_frame(0.1, partial_frame)

        # Verify no error emitted
        assert len(errors_emitted) == 0

        # Verify Signal Integrity: input array was NOT modified or normalized/clamped in-place
        np.testing.assert_array_equal(partial_frame, original_copy)

        # Verify FFT was computed and emitted
        assert len(fft_results) == 1
        freqs, mags = fft_results[0]
        assert len(mags) == 513  # 1024 // 2 + 1 for rfft

    def test_signal_integrity_unmodified_pcm(self):
        """Verify that passing hot signals (> 1.0) through AnalyzerWorker leaves them unclipped."""
        _app = get_qapp()
        capture_q = queue.Queue()
        worker = AnalyzerWorker(capture_queue=capture_q, sample_rate=44100)

        hot_frame = np.array([1.25, -1.15, 0.5, -0.8, 1.4], dtype=np.float32)
        hot_copy = hot_frame.copy()

        worker._process_frame(0.0, hot_frame)

        # Input array must remain bit-for-bit identical
        np.testing.assert_array_equal(hot_frame, hot_copy)

    def test_eof_partial_frame_drain_sequence(self):
        """Verify that an EOF sequence with a partial frame completes drain acknowledgement cleanly."""
        _app = get_qapp()
        capture_q = queue.Queue()
        storage_q = queue.Queue()
        worker = AnalyzerWorker(capture_queue=capture_q, storage_queue=storage_q, sample_rate=48000)

        drain_acknowledged = []
        worker.drain_complete.connect(lambda: drain_acknowledged.append(True))

        errors = []
        worker.error_occurred.connect(lambda msg: errors.append(msg))

        # Enqueue: 1024 chunk, 1024 chunk, 681 partial chunk
        capture_q.put((0.0, np.ones(1024, dtype=np.float32) * 0.1))
        capture_q.put((1.0, np.ones(1024, dtype=np.float32) * 0.2))
        capture_q.put((2.0, np.ones(681, dtype=np.float32) * 0.3))

        # Request drain (must process remaining queue and emit drain_complete)
        worker.request_drain()

        assert len(drain_acknowledged) == 1
        assert len(errors) == 0
        assert capture_q.empty()


class TestWaveformAutoscaling:

    def test_waveform_dynamic_autoscaling_and_signal_integrity(self):
        _app = get_qapp()
        from app.ui.plots.waveform import WaveformWidget

        widget = WaveformWidget()
        assert widget._current_y_limit == 1.0

        # Feed hot samples (> 1.0)
        hot_samples = np.array([0.0, 0.5, 1.35, -1.2, 0.1], dtype=np.float32)
        hot_copy = hot_samples.copy()

        widget.update_waveform(hot_samples)

        # 1. Signal Integrity: input array was NOT mutated
        np.testing.assert_array_equal(hot_samples, hot_copy)

        # 2. Dynamic autoscaling: Y-limit expanded to accommodate 1.35 * 1.08
        expected_limit = 1.35 * 1.08
        assert abs(widget._current_y_limit - expected_limit) < 1e-4

        # 3. Clear/set_idle resets back to 1.0
        widget.clear()
        assert widget._current_y_limit == 1.0


class TestFilePlaybackController:

    def test_open_file_and_state_transitions(self, tmp_path: Path):
        _app = get_qapp()
        wav_path = tmp_path / "ctrl_test.wav"
        _create_synthetic_wav(wav_path, duration_s=1.0, sample_rate=44100, channels=2)

        capture_q = queue.Queue()
        controller = FilePlaybackController(capture_queue=capture_q, analyzer_worker=None)
        assert controller.state == FilePlaybackState.IDLE

        success = controller.open_file(wav_path)
        assert success
        assert controller.state == FilePlaybackState.READY
        assert controller.duration >= 0.95

        # Seeking updates position
        controller.seek(0.5)
        assert abs(controller.current_position - 0.5) < 0.05

        controller.stop()
        assert controller.state == FilePlaybackState.READY
        assert controller.current_position == 0.0

        controller.close()
        assert controller.state == FilePlaybackState.IDLE


def run_all_tests():
    print("Running Audio File Playback Test Suite...")

    # 1. TestAudioDecoder
    dec_test = TestAudioDecoder()
    with tempfile.TemporaryDirectory() as td:
        t_path = Path(td)
        dec_test.test_open_and_metadata(t_path)
        dec_test.test_read_frames_normalization(t_path)
        dec_test.test_read_until_eof(t_path)
        dec_test.test_seek_accuracy_and_bounds(t_path)
    print("  [PASS] TestAudioDecoder (4 tests)")

    # 2. TestAudioOutput
    out_test = TestAudioOutput()
    out_test.test_ring_buffer_and_backpressure()
    out_test.test_hardware_elapsed_clock()
    print("  [PASS] TestAudioOutput (2 tests)")

    # 3. TestMetricsEngineMultiSampleRate
    met_test = TestMetricsEngineMultiSampleRate()
    met_test.test_dominant_frequency_detection_at_sample_rates()
    met_test.test_dynamic_sample_rate_switch()
    print("  [PASS] TestMetricsEngineMultiSampleRate (2 tests)")

    # 4. TestAnalyzerWorkerProtocols
    work_test = TestAnalyzerWorkerProtocols()
    work_test.test_sample_rate_reconfiguration_and_barrier()
    print("  [PASS] TestAnalyzerWorkerProtocols (1 test)")

    # 5. TestGoldenPipelineEquivalence
    gold_test = TestGoldenPipelineEquivalence()
    gold_test.test_sine_pipeline_equivalence()
    print("  [PASS] TestGoldenPipelineEquivalence (1 test)")

    # 6. TestPartialFramesAndSignalIntegrity
    partial_test = TestPartialFramesAndSignalIntegrity()
    partial_test.test_partial_frame_and_empty_frame_safety()
    partial_test.test_signal_integrity_unmodified_pcm()
    partial_test.test_eof_partial_frame_drain_sequence()
    print("  [PASS] TestPartialFramesAndSignalIntegrity (3 tests)")

    # 7. TestWaveformAutoscaling
    wave_test = TestWaveformAutoscaling()
    wave_test.test_waveform_dynamic_autoscaling_and_signal_integrity()
    print("  [PASS] TestWaveformAutoscaling (1 test)")

    # 8. TestFilePlaybackController
    ctrl_test = TestFilePlaybackController()
    with tempfile.TemporaryDirectory() as td:
        t_path = Path(td)
        ctrl_test.test_open_file_and_state_transitions(t_path)
    print("  [PASS] TestFilePlaybackController (1 test)")

    print("\nAll 15 File Playback, Partial-Frame, and Signal-Integrity tests passed successfully!")


if __name__ == "__main__":
    run_all_tests()
