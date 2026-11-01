"""
test_esp32_audio_source.py
--------------------------
Unit and pipeline tests for ESP32 USB-Serial audio ingestion.
Tests cover:
  1. Pure parser framing, CRC16 validation, fragmented reads, and resynchronization.
  2. Edge cases: split magic, split header, split payload, multi-packets, garbage prefix.
  3. Corruption handling: CRC mismatch, bad sample count, corrupted magic.
  4. Real-time backpressure: drop oldest frame on queue full.
  5. Serial disconnection handling without application crash.
  6. Golden signal test: 48 kHz 440 Hz sine wave encoded into serial packets,
     parsed, accumulated, and analyzed via MetricsEngine.
"""

import math
import queue
import struct
import time
from pathlib import Path
import unittest.mock as mock

import numpy as np
import pytest
import serial

from app.acquisition.esp32_audio_source import (
    Esp32PacketParser,
    Esp32AudioSource,
    Esp32State,
    MAGIC,
    WIRE_MAGIC_BYTES,
    HEADER_SIZE,
    TRAILER_SIZE,
    ANALYZER_FRAME_SIZE,
    compute_crc16,
    encode_esp32_packet,
)
from backend.processing.metrics_engine import MetricsEngine


class TestEsp32PacketParser:
    """Tests for the pure Python Esp32PacketParser without physical hardware."""

    def test_crc16_known_vector(self):
        # Standard ASCII test vector "123456789" -> CRC16-CCITT = 0x29B1
        assert compute_crc16(b"123456789") == 0x29B1

    def test_valid_packet_decode(self):
        parser = Esp32PacketParser(use_crc=True)
        # Create 128 int16 samples with known ramp
        raw_samples = np.linspace(-10000, 10000, 128, dtype=np.int16)
        packet = encode_esp32_packet(raw_samples, use_crc=True)

        decoded = parser.feed(packet)
        assert len(decoded) == 1
        assert parser.packets_decoded == 1
        assert parser.samples_decoded == 128
        assert parser.crc_failures == 0
        assert parser.invalid_packets == 0

        expected_float = raw_samples.astype(np.float32) / 32768.0
        np.testing.assert_allclose(decoded[0], expected_float, rtol=1e-5)

    def test_split_header(self):
        parser = Esp32PacketParser(use_crc=True)
        samples = np.array([1000, -1000, 2000, -2000], dtype=np.int16)
        packet = encode_esp32_packet(samples, use_crc=True)

        # Feed byte 0..1 (MAGIC only), then 2..end (SAMPLE_COUNT + payload + CRC)
        chunk1 = packet[:2]
        chunk2 = packet[2:]

        out1 = parser.feed(chunk1)
        assert len(out1) == 0  # Header not complete yet

        out2 = parser.feed(chunk2)
        assert len(out2) == 1
        assert len(out2[0]) == 4
        expected = samples.astype(np.float32) / 32768.0
        np.testing.assert_allclose(out2[0], expected, rtol=1e-5)

    def test_split_magic_byte_boundary(self):
        parser = Esp32PacketParser(use_crc=True)
        samples = np.array([500, -500], dtype=np.int16)
        packet = encode_esp32_packet(samples, use_crc=True)

        # Split precisely on the two magic bytes (0x55, then 0xAA...)
        assert packet[0:2] == WIRE_MAGIC_BYTES
        chunk1 = packet[:1]  # only 0x55
        chunk2 = packet[1:]  # 0xAA + rest

        out1 = parser.feed(chunk1)
        assert len(out1) == 0

        out2 = parser.feed(chunk2)
        assert len(out2) == 1
        assert parser.packets_decoded == 1

    def test_split_payload(self):
        parser = Esp32PacketParser(use_crc=True)
        samples = np.arange(200, dtype=np.int16)
        packet = encode_esp32_packet(samples, use_crc=True)

        # Feed across 5 chunks
        chunk_size = len(packet) // 5
        decoded = []
        for i in range(5):
            part = packet[i * chunk_size : (i + 1) * chunk_size if i < 4 else None]
            decoded.extend(parser.feed(part))

        assert len(decoded) == 1
        assert len(decoded[0]) == 200
        np.testing.assert_allclose(decoded[0], samples.astype(np.float32) / 32768.0, rtol=1e-5)

    def test_multiple_packets_in_single_feed(self):
        parser = Esp32PacketParser(use_crc=True)
        pkt1 = encode_esp32_packet(np.array([100, 200], dtype=np.int16))
        pkt2 = encode_esp32_packet(np.array([300, 400, 500], dtype=np.int16))
        pkt3 = encode_esp32_packet(np.array([600], dtype=np.int16))

        combined = pkt1 + pkt2 + pkt3
        decoded = parser.feed(combined)

        assert len(decoded) == 3
        assert parser.packets_decoded == 3
        assert parser.samples_decoded == 6
        assert len(decoded[0]) == 2
        assert len(decoded[1]) == 3
        assert len(decoded[2]) == 1

    def test_garbage_resynchronization(self):
        parser = Esp32PacketParser(use_crc=True)
        garbage = b"\x00\xFF\x12\x34\xFE\xDC\xBA\x98"
        samples = np.array([1234, -5678], dtype=np.int16)
        packet = encode_esp32_packet(samples)

        decoded = parser.feed(garbage + packet)
        assert len(decoded) == 1
        assert parser.resync_events >= 1
        np.testing.assert_allclose(decoded[0], samples.astype(np.float32) / 32768.0)

    def test_partial_packet_plus_garbage_plus_valid_packet(self):
        parser = Esp32PacketParser(use_crc=True)
        # Construct a truncated packet (header only, missing payload)
        truncated = struct.pack('<HH', MAGIC, 100) + b"\x01\x02\x03\x04"
        garbage = b"\xde\xad\xbe\xef"
        valid_samples = np.array([999, -999], dtype=np.int16)
        valid_packet = encode_esp32_packet(valid_samples)

        # Feed truncated packet first
        out1 = parser.feed(truncated)
        assert len(out1) == 0

        # Now stream has garbage and then a real packet
        out2 = parser.feed(garbage + valid_packet)
        # Parser should resynchronize and recover valid_packet
        assert len(out2) == 1
        np.testing.assert_allclose(out2[0], valid_samples.astype(np.float32) / 32768.0)

    def test_valid_packet_plus_partial_next_packet(self):
        parser = Esp32PacketParser(use_crc=True)
        pkt1 = encode_esp32_packet(np.array([10, 20], dtype=np.int16))
        pkt2 = encode_esp32_packet(np.array([30, 40, 50], dtype=np.int16))

        # Feed pkt1 + half of pkt2
        out1 = parser.feed(pkt1 + pkt2[:6])
        assert len(out1) == 1
        assert len(out1[0]) == 2

        # Feed remainder of pkt2
        out2 = parser.feed(pkt2[6:])
        assert len(out2) == 1
        assert len(out2[0]) == 3

    def test_corrupted_crc_rejection(self):
        parser = Esp32PacketParser(use_crc=True)
        samples = np.array([100, 200, 300], dtype=np.int16)
        packet = bytearray(encode_esp32_packet(samples, use_crc=True))

        # Corrupt one payload byte
        packet[6] ^= 0xFF

        valid_samples = np.array([777], dtype=np.int16)
        valid_packet = encode_esp32_packet(valid_samples, use_crc=True)

        decoded = parser.feed(bytes(packet) + valid_packet)
        # Corrupted packet rejected due to CRC mismatch; valid packet parsed
        assert parser.crc_failures == 1
        assert len(decoded) == 1
        assert len(decoded[0]) == 1
        assert decoded[0][0] == pytest.approx(777.0 / 32768.0, rel=1e-4)

    def test_invalid_sample_count(self):
        parser = Esp32PacketParser(use_crc=True)
        # Sample count 0 (empty)
        bad_header_0 = struct.pack('<HH', MAGIC, 0) + b"\x00\x00"
        # Sample count 99999 (exceeds MAX_SAMPLE_COUNT 4096)
        bad_header_giant = struct.pack('<HH', MAGIC, 9999) + b"\x00\x00"

        valid = encode_esp32_packet(np.array([55], dtype=np.int16))

        decoded = parser.feed(bad_header_0 + bad_header_giant + valid)
        assert parser.invalid_packets >= 2
        assert len(decoded) == 1
        assert decoded[0][0] == pytest.approx(55.0 / 32768.0, rel=1e-4)


class TestEsp32AudioSource:
    """Tests for Esp32AudioSource queue, state lifecycle, and backpressure."""

    def test_backpressure_drops_oldest(self):
        q = queue.Queue(maxsize=2)
        source = Esp32AudioSource(
            out_queue=q,
            port="COM99",
            sample_rate=48000,
            analyzer_frame_size=4,  # small frame size for testing
        )

        # Enqueue 2 frames to fill queue
        q.put_nowait((1.0, np.zeros(4, dtype=np.float32)))
        q.put_nowait((2.0, np.ones(4, dtype=np.float32)))
        assert q.full()

        # Feed 1 packet with 4 samples through mock serial
        packet = encode_esp32_packet(np.array([100, 200, 300, 400], dtype=np.int16))

        with mock.patch("serial.Serial") as mock_serial_cls:
            mock_inst = mock.MagicMock()
            mock_inst.read.side_effect = [packet, b""]
            mock_inst.is_open = True
            mock_serial_cls.return_value = mock_inst

            # Run 1 iteration of worker
            def stop_after_read(*args, **kwargs):
                source.stop()
                return packet

            mock_inst.read = stop_after_read
            source.run()

        # Queue should still have maxsize 2, but oldest frame (timestamp 1.0) dropped
        assert q.qsize() == 2
        first_remaining_time, _ = q.get_nowait()
        assert first_remaining_time == 2.0  # oldest was dropped
        second_remaining_time, frame = q.get_nowait()
        assert frame[0] == pytest.approx(100.0 / 32768.0, rel=1e-4)
        assert source._queue_drops >= 1

    def test_serial_disconnect_transitions_to_error_without_crash(self):
        q = queue.Queue(maxsize=16)
        source = Esp32AudioSource(
            out_queue=q,
            port="COM_DISCONNECT_TEST",
            sample_rate=48000,
        )

        errors = []
        source.error_occurred.connect(errors.append)

        with mock.patch("serial.Serial") as mock_serial_cls:
            mock_inst = mock.MagicMock()
            # Simulate SerialException (e.g. device unplugged during read)
            mock_inst.read.side_effect = serial.SerialException("Device unplugged")
            mock_inst.is_open = True
            mock_serial_cls.return_value = mock_inst

            source.run()

        assert source.state == Esp32State.ERROR
        assert len(errors) == 1
        assert "Device unplugged" in errors[0]
        assert not source.is_running


class TestGoldenSignalPipeline:
    """
    Deterministic golden signal test:
    48 kHz, mono, 440 Hz sine tone with amplitude 0.5.
    Encodes samples into wire packets -> decodes via Esp32PacketParser ->
    accumulates into 1024-sample frames -> evaluates via MetricsEngine.
    """

    def test_golden_sine_metrics(self):
        sample_rate = 48000
        frequency = 440.0
        amplitude = 0.5
        duration = 1.0  # 1 second of audio = 48,000 samples
        total_samples = int(sample_rate * duration)

        # Generate exact mathematical 440 Hz sine wave
        t = np.arange(total_samples, dtype=np.float32) / sample_rate
        synthetic_signal = (amplitude * np.sin(2.0 * np.pi * frequency * t)).astype(np.float32)

        # Slice into ESP32 wire packets of 256 samples each
        packet_sample_size = 256
        wire_stream = bytearray()
        for i in range(0, total_samples, packet_sample_size):
            chunk = synthetic_signal[i : i + packet_sample_size]
            wire_stream.extend(encode_esp32_packet(chunk, use_crc=True))

        # Stream bytes through Esp32PacketParser
        parser = Esp32PacketParser(use_crc=True)
        # Simulate realistic partial serial reads of 500 bytes each
        decoded_frames = []
        read_chunk_size = 500
        for offset in range(0, len(wire_stream), read_chunk_size):
            decoded_frames.extend(parser.feed(bytes(wire_stream[offset : offset + read_chunk_size])))

        assert parser.crc_failures == 0
        assert parser.invalid_packets == 0
        assert parser.samples_decoded == total_samples

        # Accumulate decoded samples into 1024-sample analyzer frames
        flat_decoded = np.concatenate(decoded_frames)
        assert len(flat_decoded) == total_samples

        # Verify int16 -> float32 conversion fidelity
        np.testing.assert_allclose(flat_decoded, synthetic_signal, atol=1e-4)

        # Feed frames through MetricsEngine
        engine = MetricsEngine(sample_rate=sample_rate)
        engine.set_sample_rate(sample_rate)

        frame_size = ANALYZER_FRAME_SIZE
        last_metrics = {}
        for i in range(0, total_samples - frame_size + 1, frame_size):
            frame = flat_decoded[i : i + frame_size]
            last_metrics = engine.calculate_metrics(frame.tolist(), timestamp=i / sample_rate)

        # Verify produced metrics against expected theoretical values:
        # Expected Peak = amplitude = 0.5
        assert abs(last_metrics["peak"] - 0.5) < 0.01

        # Expected RMS = amplitude / sqrt(2) ≈ 0.35355
        expected_rms = amplitude / math.sqrt(2.0)
        assert abs(last_metrics["rms"] - expected_rms) < 0.03

        # Expected Dominant Frequency ≈ 440 Hz (within 1024-pt FFT bin resolution of 46.875 Hz)
        assert abs(last_metrics["frequency"] - 440.0) <= 25.0
        assert abs(last_metrics["peak_frequency"] - 440.0) <= 25.0

        # Expected Zero Crossing Rate (ZCR):
        # 440 Hz wave crosses zero 880 times/second -> 880 / 48000 ≈ 0.0183 crossings/sample
        expected_zcr = (2.0 * frequency) / sample_rate
        assert abs(last_metrics["zcr"] - expected_zcr) < 0.005
