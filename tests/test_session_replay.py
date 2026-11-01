"""
test_session_replay.py
-----------------------
Unit and Qt integration tests for Session Replay:
  1. SessionDataReader loading, timestamp normalization, and schema mapping
  2. Non-decreasing timestamp validation (permitting batch duplicates)
  3. Deterministic nearest-neighbor position selection with later-index tie breaking
  4. Single-measurement edge case handling
  5. NaN/Inf preservation (no zero substitution)
  6. Error handling for missing files, empty parquet, and corrupt sessions
  7. Cryptographic SHA-256 read-only verification
  8. ReplayController state transitions, seeking bounds, speed scaling, and restart
"""

import hashlib
import json
import math
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from PySide6.QtWidgets import QApplication

# Ensure project root & backend are in sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "backend"))

from app.storage.parquet_writer import PARQUET_SCHEMA
from app.storage.parquet_reader import (
    SessionDataReader, ReplaySessionData,
    SessionNotFoundError, ParquetNotFoundError,
    EmptySessionError, CorruptSessionError
)
from app.ui.session_replay import ReplayController, ReplayState


def get_qapp():
    """Ensure a Qt Application instance exists for signal/timer testing."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def create_synthetic_session(
    session_dir: Path,
    session_id: str = "test_session_001",
    num_rows: int = 50,
    interval_s: float = 0.05,
    official_duration: float = 2.5,
    duplicate_timestamps: bool = False,
    include_nan: bool = False,
) -> Path:
    """Helper to generate a valid test session on disk."""
    session_dir.mkdir(parents=True, exist_ok=True)

    base_time = 1700000000.0
    rows = []
    for i in range(num_rows):
        t_offset = i * interval_s
        if duplicate_timestamps and i in (10, 20):
            # Duplicate previous timestamp
            t_offset = (i - 1) * interval_s

        flatness_val = float("nan") if (include_nan and i == 5) else (0.01 + (i * 0.001))

        rows.append({
            "timestamp": base_time + t_offset,
            "session_id": session_id,
            "rms": 0.1 + (i * 0.005),
            "peak": 0.2 + (i * 0.005),
            "bpm": 120.0,
            "zcr": 0.02,
            "frequency": 440.0,
            "peak_frequency": 440.0,
            "spectral_centroid": 1000.0 + (i * 10),
            "spectral_rolloff": 2000.0,
            "spectral_flatness": flatness_val,
        })

    # Write Parquet
    table = pa.Table.from_pylist(rows, schema=PARQUET_SCHEMA)
    parquet_path = session_dir / "data.parquet"
    pq.write_table(table, str(parquet_path))

    # Write session.json metadata
    meta = {
        "session_id": session_id,
        "schema_version": 1,
        "status": "completed",
        "start_time": "2026-09-04T10:00:00.000",
        "end_time": "2026-09-04T10:00:02.500",
        "duration_seconds": official_duration,
        "source": "System Output",
        "device": "Test Audio Device",
        "sample_rate": 48000,
        "channels": 2,
        "frames_per_buffer": 1024,
    }
    meta_path = session_dir / "session.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    return session_dir


# ─────────────────────────────────────────────────────────────────────────────
# 1. SessionDataReader Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSessionDataReader:

    def test_load_valid_session(self, tmp_path: Path):
        session_dir = tmp_path / "valid_session"
        create_synthetic_session(session_dir, num_rows=40, interval_s=0.1, official_duration=4.0)

        data = SessionDataReader.load_session(session_dir)

        assert isinstance(data, ReplaySessionData)
        assert data.session_id == "test_session_001"
        assert data.row_count == 40
        assert data.session_duration == 4.0
        assert len(data.timestamps_rel) == 40
        assert data.timestamps_rel[0] == 0.0
        assert abs(data.timestamps_rel[-1] - 3.9) < 0.001
        assert len(data.rms) == 40
        assert len(data.peak) == 40
        assert len(data.zcr) == 40
        assert len(data.frequency) == 40
        assert len(data.peak_frequency) == 40

    def test_non_decreasing_timestamps(self, tmp_path: Path):
        """Verify batches with duplicate timestamps t[i+1] == t[i] load cleanly."""
        session_dir = tmp_path / "dup_timestamps_session"
        create_synthetic_session(session_dir, num_rows=30, duplicate_timestamps=True)

        data = SessionDataReader.load_session(session_dir)
        assert data.row_count == 30
        diffs = np.diff(data.timestamps_rel)
        assert np.all(diffs >= 0.0)

    def test_deterministic_nearest_neighbor_and_tie_breaking(self):
        """
        Verify searchsorted nearest-neighbor calculation.
        On equal distance, the later measurement must be chosen.
        """
        timestamps = np.array([0.0, 0.1, 0.2, 0.3], dtype=np.float64)

        # Standard closest
        assert SessionDataReader.find_nearest_index(timestamps, 0.0) == 0
        assert SessionDataReader.find_nearest_index(timestamps, 0.04) == 0
        assert SessionDataReader.find_nearest_index(timestamps, 0.08) == 1
        assert SessionDataReader.find_nearest_index(timestamps, 0.18) == 2
        assert SessionDataReader.find_nearest_index(timestamps, 0.29) == 3

        # Clamping bounds
        assert SessionDataReader.find_nearest_index(timestamps, -5.0) == 0
        assert SessionDataReader.find_nearest_index(timestamps, 10.0) == 3

        # Exact tie: 0.15 is equidistant from 0.1 (idx 1) and 0.2 (idx 2).
        # Requirement: On equal distance, select the later measurement.
        assert SessionDataReader.find_nearest_index(timestamps, 0.15) == 2
        # 0.05 is equidistant from 0.0 (idx 0) and 0.1 (idx 1) -> select 1
        assert SessionDataReader.find_nearest_index(timestamps, 0.05) == 1

    def test_single_measurement_session(self, tmp_path: Path):
        """Sessions with only 1 measurement row must not crash and set duration 0.0."""
        session_dir = tmp_path / "single_row_session"
        create_synthetic_session(session_dir, num_rows=1, official_duration=0.0)

        data = SessionDataReader.load_session(session_dir)
        assert data.row_count == 1
        assert data.session_duration == 0.0
        assert len(data.timestamps_rel) == 1
        assert data.timestamps_rel[0] == 0.0

        idx = SessionDataReader.find_nearest_index(data.timestamps_rel, 1.5)
        assert idx == 0

        meas = SessionDataReader.get_measurement_dict(data, 0)
        assert meas["rms"] > 0

    def test_nan_preservation(self, tmp_path: Path):
        """Verify NaN values are preserved in arrays and not replaced with 0.0."""
        session_dir = tmp_path / "nan_session"
        create_synthetic_session(session_dir, num_rows=10, include_nan=True)

        data = SessionDataReader.load_session(session_dir)
        assert math.isnan(data.spectral_flatness[5])

        meas = SessionDataReader.get_measurement_dict(data, 5)
        assert math.isnan(meas["spectral_flatness"])

    def test_missing_and_corrupt_session_errors(self, tmp_path: Path):
        # 1. Non-existent directory
        try:
            SessionDataReader.load_session(tmp_path / "does_not_exist")
            assert False, "Should raise SessionNotFoundError"
        except SessionNotFoundError:
            pass

        # 2. Missing data.parquet
        no_parquet_dir = tmp_path / "no_parquet"
        no_parquet_dir.mkdir()
        try:
            SessionDataReader.load_session(no_parquet_dir)
            assert False, "Should raise ParquetNotFoundError"
        except ParquetNotFoundError:
            pass

        # 3. Empty data.parquet (0 bytes)
        empty_file_dir = tmp_path / "empty_file"
        empty_file_dir.mkdir()
        (empty_file_dir / "data.parquet").touch()
        try:
            SessionDataReader.load_session(empty_file_dir)
            assert False, "Should raise EmptySessionError"
        except EmptySessionError:
            pass

        # 4. Corrupt parquet file
        corrupt_dir = tmp_path / "corrupt_parquet"
        corrupt_dir.mkdir()
        (corrupt_dir / "data.parquet").write_bytes(b"NOT A REAL PARQUET FILE")
        try:
            SessionDataReader.load_session(corrupt_dir)
            assert False, "Should raise CorruptSessionError"
        except CorruptSessionError:
            pass

    def test_missing_metadata_fallback(self, tmp_path: Path):
        """If session.json is missing, derive duration and id gracefully from Parquet."""
        session_dir = tmp_path / "no_meta_session"
        create_synthetic_session(session_dir, num_rows=20, interval_s=0.1)
        (session_dir / "session.json").unlink()

        data = SessionDataReader.load_session(session_dir)
        assert data.session_id == "no_meta_session"
        assert abs(data.session_duration - 1.9) < 0.02
        assert data.row_count == 20

    def test_readonly_sha256_verification(self, tmp_path: Path):
        """Verify session.json and data.parquet hashes are 100% unchanged after read."""
        session_dir = tmp_path / "readonly_session"
        create_synthetic_session(session_dir, num_rows=25)

        p_file = session_dir / "data.parquet"
        j_file = session_dir / "session.json"

        h_parquet_before = hashlib.sha256(p_file.read_bytes()).hexdigest()
        h_json_before = hashlib.sha256(j_file.read_bytes()).hexdigest()

        # Perform reader loading and multiple queries
        data = SessionDataReader.load_session(session_dir)
        for t in [0.0, 0.5, 1.0, 1.5]:
            idx = SessionDataReader.find_nearest_index(data.timestamps_rel, t)
            _ = SessionDataReader.get_measurement_dict(data, idx)

        h_parquet_after = hashlib.sha256(p_file.read_bytes()).hexdigest()
        h_json_after = hashlib.sha256(j_file.read_bytes()).hexdigest()

        assert h_parquet_before == h_parquet_after, "data.parquet was modified by replay reader!"
        assert h_json_before == h_json_after, "session.json was modified by replay reader!"


# ─────────────────────────────────────────────────────────────────────────────
# 2. ReplayController Qt Integration Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestReplayController:

    def __init__(self):
        self.app = get_qapp()

    def test_controller_initial_state(self, tmp_path: Path):
        session_dir = tmp_path / "ctrl_session"
        create_synthetic_session(session_dir, num_rows=50, interval_s=0.1, official_duration=5.0)
        data = SessionDataReader.load_session(session_dir)

        controller = ReplayController(data)
        assert controller.state == ReplayState.PAUSED
        assert controller.current_position == 0.0
        assert controller.current_index == 0
        assert controller.duration == 5.0
        assert controller.speed == 1.0

    def test_controller_play_and_pause_signals(self, tmp_path: Path):
        session_dir = tmp_path / "ctrl_play_session"
        create_synthetic_session(session_dir, num_rows=20, official_duration=2.0)
        data = SessionDataReader.load_session(session_dir)

        controller = ReplayController(data)

        states = []
        controller.state_changed.connect(states.append)

        controller.play()
        assert controller.state == ReplayState.PLAYING
        assert states[-1] == ReplayState.PLAYING

        controller.pause()
        assert controller.state == ReplayState.PAUSED
        assert states[-1] == ReplayState.PAUSED

    def test_controller_seeking_and_bounds_clamping(self, tmp_path: Path):
        session_dir = tmp_path / "ctrl_seek_session"
        create_synthetic_session(session_dir, num_rows=50, interval_s=0.1, official_duration=5.0)
        data = SessionDataReader.load_session(session_dir)

        controller = ReplayController(data)

        positions = []
        controller.position_changed.connect(lambda pos, idx: positions.append((pos, idx)))

        # Seek within valid range
        controller.seek(2.5)
        assert controller.current_position == 2.5
        assert controller.current_index == SessionDataReader.find_nearest_index(data.timestamps_rel, 2.5)
        assert positions[-1][0] == 2.5

        # Seek below 0 (clamp to 0.0)
        controller.seek(-10.0)
        assert controller.current_position == 0.0
        assert controller.current_index == 0

        # Seek beyond duration (clamp to 5.0)
        controller.seek(99.0)
        assert controller.current_position == 5.0
        assert controller.current_index == data.row_count - 1

    def test_controller_step_forward_and_backward(self, tmp_path: Path):
        session_dir = tmp_path / "ctrl_step_session"
        create_synthetic_session(session_dir, num_rows=10, interval_s=0.2, official_duration=2.0)
        data = SessionDataReader.load_session(session_dir)

        controller = ReplayController(data)
        assert controller.current_index == 0

        controller.step_forward()
        assert controller.current_index == 1
        assert abs(controller.current_position - 0.2) < 0.01

        controller.step_forward()
        assert controller.current_index == 2

        controller.step_backward()
        assert controller.current_index == 1

        controller.step_backward()
        assert controller.current_index == 0

        # Clamp at zero
        controller.step_backward()
        assert controller.current_index == 0

    def test_controller_speed_scaling(self, tmp_path: Path):
        session_dir = tmp_path / "ctrl_speed_session"
        create_synthetic_session(session_dir)
        data = SessionDataReader.load_session(session_dir)

        controller = ReplayController(data)

        speeds = []
        controller.speed_changed.connect(speeds.append)

        controller.set_speed(2.0)
        assert controller.speed == 2.0
        assert speeds[-1] == 2.0

        controller.set_speed(0.5)
        assert controller.speed == 0.5
        assert speeds[-1] == 0.5

    def test_controller_end_completion_and_restart(self, tmp_path: Path):
        session_dir = tmp_path / "ctrl_end_session"
        create_synthetic_session(session_dir, num_rows=10, interval_s=0.1, official_duration=1.0)
        data = SessionDataReader.load_session(session_dir)

        controller = ReplayController(data)

        finished_called = []
        controller.finished.connect(lambda: finished_called.append(True))

        # Seek directly to end
        controller.seek(1.0)
        # Simulate timer tick reaching or exceeding duration
        controller._last_tick_time = 0.0
        controller._on_tick()

        assert controller.state == ReplayState.COMPLETE
        assert len(finished_called) == 1

        # Pressing play when in COMPLETE state restarts from 0.0
        controller.play()
        assert controller.current_position == 0.0
        assert controller.state == ReplayState.PLAYING
        controller.pause()

    def test_single_measurement_cannot_play(self, tmp_path: Path):
        session_dir = tmp_path / "ctrl_single_session"
        create_synthetic_session(session_dir, num_rows=1, official_duration=0.0)
        data = SessionDataReader.load_session(session_dir)

        controller = ReplayController(data)
        controller.play()
        # Should remain PAUSED because duration is 0
        assert controller.state == ReplayState.PAUSED


def run_all_tests():
    print("Running Session Replay Test Suite...")

    # 1. TestSessionDataReader
    reader_test = TestSessionDataReader()
    with tempfile.TemporaryDirectory() as td:
        t_path = Path(td)
        reader_test.test_load_valid_session(t_path)
        reader_test.test_non_decreasing_timestamps(t_path)
        reader_test.test_deterministic_nearest_neighbor_and_tie_breaking()
        reader_test.test_single_measurement_session(t_path)
        reader_test.test_nan_preservation(t_path)
        reader_test.test_missing_and_corrupt_session_errors(t_path)
        reader_test.test_missing_metadata_fallback(t_path)
        reader_test.test_readonly_sha256_verification(t_path)
    print("  [PASS] TestSessionDataReader (8 tests)")

    # 2. TestReplayController
    ctrl_test = TestReplayController()
    with tempfile.TemporaryDirectory() as td:
        t_path = Path(td)
        ctrl_test.test_controller_initial_state(t_path)
        ctrl_test.test_controller_play_and_pause_signals(t_path)
        ctrl_test.test_controller_seeking_and_bounds_clamping(t_path)
        ctrl_test.test_controller_step_forward_and_backward(t_path)
        ctrl_test.test_controller_speed_scaling(t_path)
        ctrl_test.test_controller_end_completion_and_restart(t_path)
        ctrl_test.test_single_measurement_cannot_play(t_path)
    print("  [PASS] TestReplayController (7 tests)")

    print("\nAll 15 Session Replay tests passed successfully!")


if __name__ == "__main__":
    run_all_tests()
