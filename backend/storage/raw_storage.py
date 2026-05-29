import os
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import asyncio
import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PARQUET_DIR = Path(os.getenv("PARQUET_DIR", "data/parquet"))

class RawStorage:
    def __init__(self):
        self.active_buffers = {}
        self.active_writers = {}
        self.session_locks = {}
        
        self.max_buffer_size = 500
        self.flush_interval_seconds = 10.0
        
        # Ensure base dir exists
        PARQUET_DIR.mkdir(parents=True, exist_ok=True)

    def _get_lock(self, session_id):
        if session_id not in self.session_locks:
            self.session_locks[session_id] = asyncio.Lock()
        return self.session_locks[session_id]

    async def append(self, payload: dict):
        session_id = payload.get("session_id")
        if not session_id:
            return
            
        # Ensure we don't store 'samples'
        if "samples" in payload:
            del payload["samples"]

        lock = self._get_lock(session_id)
        async with lock:
            if session_id not in self.active_buffers:
                self.active_buffers[session_id] = {
                    "rows": [],
                    "last_flush": time.time(),
                    "row_count": 0
                }
                
            buf = self.active_buffers[session_id]
            buf["rows"].append(payload)
            buf["row_count"] += 1
            
        # After appending, try a flush
        await self.flush(session_id)

    async def flush(self, session_id: str, force: bool = False):
        lock = self._get_lock(session_id)
        
        async with lock:
            if session_id not in self.active_buffers:
                return
                
            buf = self.active_buffers[session_id]
            
            should_flush = force or (
                buf["row_count"] >= self.max_buffer_size or
                (time.time() - buf["last_flush"]) >= self.flush_interval_seconds
            )
            
            if not should_flush:
                return
                
            rows = buf["rows"]
            if not rows:
                return
                
            # Clear buffer
            buf["rows"] = []
            buf["row_count"] = 0
            buf["last_flush"] = time.time()
            
        # Write outside lock to not block append
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._write_parquet, session_id, rows)

    def _write_parquet(self, session_id, rows):
        try:
            df = pd.DataFrame(rows)
            table = pa.Table.from_pandas(df)
            
            file_path = PARQUET_DIR / f"{session_id}.parquet"
            
            if session_id not in self.active_writers:
                # Create a new writer if it doesn't exist
                writer = pq.ParquetWriter(file_path, table.schema)
                self.active_writers[session_id] = writer
            else:
                writer = self.active_writers[session_id]
                
            # Append row group
            writer.write_table(table)
            
        except Exception as e:
            logger.error(f"Error appending row group to parquet for {session_id}: {e}", exc_info=True)

    async def finalize_session(self, session_id: str):
        # Force flush remaining rows
        await self.flush(session_id, force=True)
        
        lock = self._get_lock(session_id)
        async with lock:
            if session_id in self.active_buffers:
                del self.active_buffers[session_id]

        # Close writer in executor
        if session_id in self.active_writers:
            writer = self.active_writers.pop(session_id)
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, writer.close)
            except Exception as e:
                logger.error(f"Error closing parquet writer for {session_id}: {e}", exc_info=True)
                
            # Log final file size
            file_path = PARQUET_DIR / f"{session_id}.parquet"
            try:
                if file_path.exists():
                    size_mb = os.path.getsize(file_path) / (1024 * 1024)
                    logger.info(f"[PARQUET] Finalized session={session_id} size={size_mb:.2f}MB")
            except Exception:
                pass
        
        # Cleanup lock safely (only if no one else is waiting on it, but here it's finalized)
        if session_id in self.session_locks:
            del self.session_locks[session_id]
