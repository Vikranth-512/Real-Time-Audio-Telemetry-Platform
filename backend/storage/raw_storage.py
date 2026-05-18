import os
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from datetime import datetime
import asyncio
import time
import logging

logger = logging.getLogger(__name__)

RAW_DATA_DIR = os.getenv("RAW_DATA_DIR", "/data/raw")

class RawStorage:
    def __init__(self):
        self.buffer = []
        self.last_flush_time = time.time()
        self.max_buffer_size = 5000
        self.flush_interval_seconds = 5.0
        self._lock = asyncio.Lock()

    async def append(self, payload: dict):
        async with self._lock:
            self.buffer.append(payload)
            
            should_flush = (
                len(self.buffer) >= self.max_buffer_size or
                (time.time() - self.last_flush_time) >= self.flush_interval_seconds
            )
            
        if should_flush:
            await self.flush()

    async def flush(self):
        async with self._lock:
            if not self.buffer:
                return

            records = self.buffer
            self.buffer = []
            self.last_flush_time = time.time()

        if records:
            # Run the IO operation in an executor to avoid blocking the event loop
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._write_parquet, records)

    def _write_parquet(self, records):
        try:
            df = pd.DataFrame(records)
            
            # Convert timestamp to a readable date for partitioning
            # Assuming timestamp is a unix timestamp
            df['datetime'] = pd.to_datetime(df['timestamp'], unit='s')
            df['date'] = df['datetime'].dt.strftime('%Y-%m-%d')
            
            # Group by device_id and date partition
            for (device_id, date), group_df in df.groupby(['device_id', 'date']):
                partition_dir = os.path.join(RAW_DATA_DIR, f"device_id={device_id}", f"date={date}")
                os.makedirs(partition_dir, exist_ok=True)
                
                # File naming strategy: DO NOT APPEND. Write a new file per flush.
                file_timestamp = int(time.time() * 1000)
                file_path = os.path.join(partition_dir, f"file_{file_timestamp}.parquet")
                
                # Drop partition columns before saving to save space if needed, 
                # but it's fine to keep them. We'll drop 'datetime' and 'date'.
                save_df = group_df.drop(columns=['datetime', 'date'])
                
                table = pa.Table.from_pandas(save_df)
                pq.write_table(table, file_path)
                
        except Exception as e:
            logger.error(f"Error flushing parquet data: {e}")
            raise
