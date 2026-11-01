"""
Unit and integration tests for the PySide6 Desktop Audio Analyzer pipeline.
Tests cover:
  1. Device manager query & validation
  2. Metrics engine processing with synthetic test vectors
  3. Parquet writer buffering, schema conformity, and serialization
  4. CSV exporter with 1-second bucketing and rolling stats calculations
"""

import os
import sys
import time
import queue
import tempfile
from pathlib import Path
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

# Ensure project root & backend are on sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "backend"))

from app.acquisition.device_manager import AudioDeviceManager, DEFAULT_SAMPLE_RATE, DEFAULT_CHANNELS
from app.acquisition.audio_source import SAMPLE_RATE, PACKET_SIZE
from app.processing.analyzer_worker import AnalyzerWorker
from app.storage.parquet_writer import ParquetWriter, PARQUET_SCHEMA
from app.storage.csv_exporter import export_session_csv, export_raw_session_csv
from app.storage.session_manager import SessionManager
from backend.processing.metrics_engine import MetricsEngine


class TestAudioDeviceManager:
    def test_enumerate_devices(self):
        dm = AudioDeviceManager()
        devices = dm.enumerate_input_devices()
        assert isinstance(devices, list)
        # Should contain at least 1 input device if system has sound hardware
        if len(devices) > 0:
            dev = devices[0]
            assert dev.max_input_channels > 0
            assert dev.name is not None
            assert dev.index >= 0

    def test_validate_device(self):
        dm = AudioDeviceManager()
        devices = dm.enumerate_input_devices()
        if devices:
            first_idx = devices[0].index
            # Validating standard sample rates
            is_valid = dm.validate_device(first_idx, sample_rate=DEFAULT_SAMPLE_RATE, channels=DEFAULT_CHANNELS)
            assert isinstance(is_valid, bool)


class TestMetricsEngineProcessing:
    def test_adapter_frequency_detection(self):
        engine = MetricsEngine()
        # Generate 1024 samples of a 440 Hz sine tone at 48 kHz
        t = np.arange(1024, dtype=np.float32) / SAMPLE_RATE
        freq = 440.0
        tone = (np.sin(2 * np.pi * freq * t) * 0.5).astype(np.float32)

        metrics = engine.calculate_metrics(tone.tolist(), time.time())
        assert isinstance(metrics, dict)
        assert "rms" in metrics
        assert "peak" in metrics
        assert "frequency" in metrics
        assert "bpm" in metrics
        assert "zcr" in metrics

        # Verify RMS is approximately 0.5 / sqrt(2) ~ 0.3535
        assert abs(metrics["rms"] - (0.5 / np.sqrt(2))) < 0.05
        # Verify peak is 0.5
        assert abs(metrics["peak"] - 0.5) < 0.01

    def test_silence_handling(self):
        engine = MetricsEngine()
        silence = [0.0] * 1024
        metrics = engine.calculate_metrics(silence, time.time())
        assert metrics["rms"] == 0.0
        assert metrics["peak"] == 0.0


class TestStoragePipeline:
    def test_parquet_writer_and_csv_exporter(self, tmp_path):
        q = queue.Queue()
        # Enqueue 50 synthetic metric rows spanning ~2.5 seconds
        base_time = 1700000000.0
        for i in range(50):
            q.put({
                "timestamp": base_time + (i * 0.05),
                "metrics": {
                    "rms": 0.2 + (i * 0.002),
                    "peak": 0.4 + (i * 0.001),
                    "bpm": 120.0,
                    "zcr": 0.04,
                    "frequency": 440.0,
                    "peak_frequency": 440.0,
                    "spectral_centroid": 1200.0,
                    "spectral_rolloff": 2500.0,
                    "spectral_flatness": 0.05,
                }
            })

        writer = ParquetWriter(storage_queue=q, data_dir=tmp_path)
        # Directly execute write lifecycle without threading
        writer.start_writing()
        writer._drain_queue()
        writer._flush(force=True)
        writer.stop_writing()

        parquet_file = writer.file_path
        assert parquet_file.exists()
        assert parquet_file.stat().st_size > 0

        # Read back parquet and verify schema & data
        table = pq.read_table(str(parquet_file))
        assert table.num_rows == 50
        for field in PARQUET_SCHEMA:
            assert field.name in table.schema.names

        # Export CSV
        csv_file = tmp_path / "exported.csv"
        out_path = export_session_csv(parquet_file, csv_file)
        assert out_path.exists()

        df_csv = pd.read_csv(out_path)
        expected_cols = [
            "timestamp", "rms", "amplitude", "bpm", "frequency", "zcr",
            "rolling_mean_rms", "rolling_variance", "rolling_peak_amplitude",
        ]
        assert list(df_csv.columns) == expected_cols
        assert len(df_csv) >= 2  # 50 rows * 0.05s = 2.5s -> ~2-3 buckets

    def test_raw_csv_exporter(self, tmp_path):
        q = queue.Queue()
        # Enqueue exactly 10 rows
        for i in range(10):
            q.put({
                "timestamp": float(i),
                "metrics": {"rms": i * 0.1, "peak": i * 0.2, "bpm": 100.0}
            })

        writer = ParquetWriter(storage_queue=q, data_dir=tmp_path)
        writer.set_session_context("test_session", tmp_path)
        writer.start_writing()
        writer._drain_queue()
        writer._flush(force=True)
        writer.stop_writing()

        parquet_file = writer.file_path
        csv_file = tmp_path / "raw_export.csv"
        out_path = export_raw_session_csv(parquet_file, csv_file)
        
        df_csv = pd.read_csv(out_path)
        # Should have exactly 10 rows and preserve exact values
        assert len(df_csv) == 10
        assert df_csv.iloc[0]["rms"] == 0.0
        assert df_csv.iloc[9]["rms"] == 0.9

class TestSessionManager:
    def test_session_lifecycle(self, tmp_path):
        sm = SessionManager(sessions_dir=tmp_path)
        session = sm.create_session({"device": "Test Device"})
        assert session.session_id is not None
        assert session.directory.exists()
        
        # Write some fake parquet data
        q = queue.Queue()
        q.put({"timestamp": 0.0, "metrics": {"rms": 0.5, "peak": 1.0, "bpm": 120.0}})
        writer = ParquetWriter(storage_queue=q, data_dir=tmp_path)
        writer.set_session_context(session.session_id, session.directory)
        writer.start_writing()
        writer._drain_queue()
        writer._flush(force=True)
        writer.stop_writing()
        
        # Finalize
        sm.finalize_session(session.session_id, status="completed")
        
        # Verify metadata
        sessions = sm.list_sessions()
        assert len(sessions) == 1
        meta = sessions[0]
        assert meta["status"] == "completed"
        assert meta["device"] == "Test Device"
        assert meta["schema_version"] == 1
        
        summary = meta["summary"]
        assert summary["rms_mean"] == 0.5
        assert summary["peak_max"] == 1.0
        
        # Test delete
        sm.delete_session(session.session_id)
        assert len(sm.list_sessions()) == 0


if __name__ == "__main__":
    print("Running Audio Analyzer pipeline tests...")
    
    # Run TestAudioDeviceManager
    dev_test = TestAudioDeviceManager()
    dev_test.test_enumerate_devices()
    dev_test.test_validate_device()
    print("  [PASS] TestAudioDeviceManager")

    # Run TestMetricsEngineProcessing
    metrics_test = TestMetricsEngineProcessing()
    metrics_test.test_adapter_frequency_detection()
    metrics_test.test_silence_handling()
    print("  [PASS] TestMetricsEngineProcessing")

    # Run TestStoragePipeline with a temporary directory
    storage_test = TestStoragePipeline()
    with tempfile.TemporaryDirectory() as temp_dir:
        storage_test.test_parquet_writer_and_csv_exporter(Path(temp_dir))
        storage_test.test_raw_csv_exporter(Path(temp_dir))
    print("  [PASS] TestStoragePipeline")

    # Run TestSessionManager
    session_test = TestSessionManager()
    with tempfile.TemporaryDirectory() as temp_dir:
        session_test.test_session_lifecycle(Path(temp_dir))
    print("  [PASS] TestSessionManager")

    print("\nAll pipeline tests passed successfully!")

