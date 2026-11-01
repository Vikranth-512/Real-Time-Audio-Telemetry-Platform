"""
session_manager.py
------------------
Centralized manager for local Session History.
Handles session ID generation, filesystem metadata, and summary calculations
from the raw Parquet storage.
"""

import json
import logging
import shutil
import datetime
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

SESSIONS_DIR = Path("data") / "sessions"

@dataclass
class SessionContext:
    session_id: str
    directory: Path
    start_time: str
    metadata: dict


class SessionManager:
    """Manages creation, finalization, and listing of recording sessions."""

    def __init__(self, sessions_dir: Path = SESSIONS_DIR):
        self._dir = Path(sessions_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def create_session(self, runtime_metadata: dict) -> SessionContext:
        """
        Create a new session directory and return its context.
        Uses timestamp with milliseconds to avoid collisions.
        """
        now = datetime.datetime.now()
        # Format: 20260903_141732_418
        session_id = now.strftime("%Y%m%d_%H%M%S") + f"_{now.microsecond // 1000:03d}"
        
        session_dir = self._dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        start_time_iso = now.isoformat(timespec='milliseconds')
        
        metadata = {
            "session_id": session_id,
            "schema_version": 1,
            "status": "initializing",
            "start_time": start_time_iso,
            **runtime_metadata
        }

        # Write initial metadata to disk in case of catastrophic crash
        self._write_metadata(session_dir, metadata)

        logger.info(f"Session created: {session_id}")
        return SessionContext(
            session_id=session_id,
            directory=session_dir,
            start_time=start_time_iso,
            metadata=metadata
        )

    def finalize_session(
        self, 
        session_id: str, 
        status: str = "completed", 
        error_msg: Optional[str] = None
    ) -> None:
        """
        Finalize a session, calculate duration and explicit summary stats from Parquet.
        """
        session_dir = self._dir / session_id
        meta_path = session_dir / "session.json"
        
        if not meta_path.exists():
            logger.error(f"Cannot finalize session {session_id}: missing session.json")
            return

        with open(meta_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)

        now = datetime.datetime.now()
        end_time_iso = now.isoformat(timespec='milliseconds')
        
        try:
            start_dt = datetime.datetime.fromisoformat(metadata["start_time"])
            duration_seconds = (now - start_dt).total_seconds()
        except Exception:
            duration_seconds = 0.0

        metadata["end_time"] = end_time_iso
        metadata["duration_seconds"] = round(duration_seconds, 2)
        metadata["status"] = status
        
        if error_msg:
            metadata["error"] = error_msg

        # Compute summary statistics safely from Parquet
        parquet_path = session_dir / "data.parquet"
        summary = self._compute_summary(parquet_path)
        if summary:
            metadata["summary"] = summary
        elif status == "completed":
            metadata["status"] = "incomplete"
            metadata["error"] = "No Parquet data recorded."

        self._write_metadata(session_dir, metadata)
        logger.info(f"Session finalized: {session_id} [{metadata['status']}]")

    def _compute_summary(self, parquet_path: Path) -> Optional[dict]:
        """Read final Parquet table and compute single-source-of-truth summaries."""
        if not parquet_path.exists() or parquet_path.stat().st_size == 0:
            return None

        try:
            # Load the entire table for summary math. 
            # PyArrow computes aggregations extremely fast in C++.
            table = pq.read_table(str(parquet_path))
            if table.num_rows == 0:
                return None
            
            # Helper to safely compute mean and max
            import pyarrow.compute as pc
            
            def get_stat(col_name: str, func: str) -> float:
                if col_name not in table.column_names:
                    return 0.0
                arr = table[col_name]
                try:
                    if func == "mean":
                        return float(pc.mean(arr).as_py() or 0.0)
                    elif func == "max":
                        return float(pc.max(arr).as_py() or 0.0)
                except Exception:
                    pass
                return 0.0

            return {
                "rms_mean": round(get_stat("rms", "mean"), 4),
                "rms_max": round(get_stat("rms", "max"), 4),
                "peak_max": round(get_stat("peak", "max"), 4),
                "dominant_frequency_mean": round(get_stat("peak_frequency", "mean"), 1),
                "dominant_frequency_max": round(get_stat("peak_frequency", "max"), 1),
                "centroid_mean": round(get_stat("spectral_centroid", "mean"), 1),
                "rolloff_mean": round(get_stat("spectral_rolloff", "mean"), 1),
                "flatness_mean": round(get_stat("spectral_flatness", "mean"), 4),
                "zcr_mean": round(get_stat("zcr", "mean"), 4),
                "bpm_mean": round(get_stat("bpm", "mean"), 1),
                "data_size_mb": round(parquet_path.stat().st_size / (1024 * 1024), 2)
            }
        except Exception as exc:
            logger.error(f"Failed to compute session summary: {exc}")
            return None

    def list_sessions(self) -> list[dict]:
        """List all valid sessions in descending chronological order."""
        sessions = []
        if not self._dir.exists():
            return sessions

        for session_dir in self._dir.iterdir():
            if not session_dir.is_dir():
                continue
            meta_path = session_dir / "session.json"
            if meta_path.exists():
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                        
                    # Add path info so UI can locate it easily
                    meta["_directory"] = str(session_dir.absolute())
                    sessions.append(meta)
                except Exception as exc:
                    logger.warning(f"Corrupt session metadata {meta_path}: {exc}")

        # Sort descending by start time (newest first)
        sessions.sort(key=lambda x: x.get("start_time", ""), reverse=True)
        return sessions

    def delete_session(self, session_id: str) -> bool:
        """Completely remove a session and all its data."""
        session_dir = self._dir / session_id
        if session_dir.exists() and session_dir.is_dir():
            try:
                shutil.rmtree(session_dir)
                logger.info(f"Deleted session {session_id}")
                return True
            except Exception as exc:
                logger.error(f"Failed to delete session {session_id}: {exc}")
        return False

    def get_session_path(self, session_id: str) -> Optional[Path]:
        """Return the path to a session's directory if it exists."""
        session_dir = self._dir / session_id
        return session_dir if session_dir.exists() else None

    def _write_metadata(self, directory: Path, data: dict) -> None:
        """Atomic write of session metadata."""
        target = directory / "session.json"
        temp = target.with_suffix(".tmp")
        try:
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
                f.flush()
            temp.replace(target)
        except Exception as exc:
            logger.error(f"Failed to write metadata for {directory.name}: {exc}")
            if temp.exists():
                temp.unlink()
