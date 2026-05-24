import asyncio
import os
import json
import uuid
import logging
import time

import redis.asyncio as aioredis
from redis.exceptions import ResponseError, ConnectionError

from processing.metrics_engine import MetricsEngine
from storage.db import batch_insert_metrics
from storage.raw_storage import RawStorage

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "WARNING").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL))
logger = logging.getLogger(__name__)
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

REDIS_URL          = os.getenv("REDIS_URL", "redis://localhost:6379/0")
GROUP_NAME         = "audio_group"
ACTIVE_SESSIONS_KEY = "active_sessions"

# Bounded concurrency: max parallel session tasks
MAX_SESSION_TASKS = 64

# ─── Per-session consumer task ────────────────────────────────────────────────

class SessionConsumer:
    """
    Owns one Redis consumer-group reader for a single session stream
    audio_stream:{session_id}.  Runs as an asyncio task until the session
    is marked stopped or the task is cancelled.
    """

    def __init__(self, session_id: str, redis_client, raw_storage: RawStorage,
                 consumer_name: str, stopped_cache: set):
        self.session_id     = session_id
        self.redis          = redis_client
        self.raw_storage    = raw_storage
        self.consumer_name  = consumer_name
        self.stopped_cache  = stopped_cache  # shared reference
        self.engine         = MetricsEngine()
        self.stream_key     = f"audio_stream:{session_id}"
        self.metrics_stream = f"audio_metrics_stream:{session_id}"

    async def ensure_consumer_group(self):
        while True:
            try:
                await self.redis.xgroup_create(
                    name=self.stream_key,
                    groupname=GROUP_NAME,
                    id="0",
                    mkstream=True,
                )
                return
            except ResponseError as e:
                if "BUSYGROUP" in str(e):
                    return
                raise
            except ConnectionError:
                logger.warning(f"[{self.session_id}] Redis not ready, retrying...")
                await asyncio.sleep(2)

    async def process_messages(self, messages: list) -> list:
        """Process a batch of messages. Returns list of ack IDs."""
        metrics_batch = []
        raw_payloads  = []
        ack_ids       = []

        for message_id, data in messages:
            try:
                device_id  = data.get("device_id")
                timestamp  = float(data.get("timestamp"))
                session_id = data.get("session_id")
                samples    = json.loads(data.get("samples", "[]"))

                if session_id in self.stopped_cache:
                    ack_ids.append(message_id)
                    continue

                raw_payloads.append({
                    "device_id": device_id,
                    "timestamp": timestamp,
                    "session_id": session_id,
                    "samples": samples,
                })

                metrics = self.engine.calculate_metrics(samples, timestamp)

                db_metric = {
                    "timestamp":     timestamp,
                    "device_id":     device_id,
                    "session_id":    session_id,
                    "bpm":           float(metrics.get("bpm",       0.0)),
                    "avg_amplitude": float(metrics.get("peak",      0.0)),
                    "rms_energy":    float(metrics.get("rms",       0.0)),
                    "zcr":           float(metrics.get("zcr",       0.0)),
                    "frequency":     float(metrics.get("frequency", 0.0)),
                }
                metrics_batch.append(db_metric)

                # Publish to partitioned metrics stream for broadcaster
                stream_metric = dict(db_metric)
                stream_metric["full_metrics"] = json.dumps(metrics)
                stream_metric["samples"]      = json.dumps(samples)
                await self.redis.xadd(
                    self.metrics_stream, stream_metric,
                    maxlen=50, approximate=True
                )

                ack_ids.append(message_id)

            except Exception as e:
                logger.error(f"[{self.session_id}] Error on msg {message_id}: {e}")

        # Batch DB insert
        if metrics_batch:
            try:
                await batch_insert_metrics(metrics_batch)
            except Exception as e:
                logger.error(f"[{self.session_id}] DB insert failed: {e}")
                return []  # don't ack — allow retry

        # Raw storage (best-effort)
        for payload in raw_payloads:
            try:
                await self.raw_storage.append(payload)
            except Exception as e:
                logger.error(f"[{self.session_id}] Parquet append failed: {e}")

        return ack_ids

    async def run(self):
        await self.ensure_consumer_group()
        last_claim_time = time.time()
        last_diag_time = time.time()

        logger.info(f"[{self.session_id}] SessionConsumer started on {self.stream_key}")

        while True:
            try:
                # Exit cleanly if session was stopped
                if self.session_id in self.stopped_cache:
                    logger.info(f"[{self.session_id}] Session stopped, consumer exiting")
                    return

                now = time.time()

                # Reclaim stale messages every 10s
                if now - last_claim_time > 10:
                    try:
                        pending = await self.redis.xpending(self.stream_key, GROUP_NAME)
                        if pending and pending.get("pending", 0) > 0:
                            claimed = await self.redis.xautoclaim(
                                name=self.stream_key,
                                groupname=GROUP_NAME,
                                consumername=self.consumer_name,
                                min_idle_time=30000,
                                start_id="0-0",
                                count=100,
                            )
                            if claimed and claimed[1]:
                                ack_ids = await self.process_messages(claimed[1])
                                if ack_ids:
                                    await self.redis.xack(self.stream_key, GROUP_NAME, *ack_ids)
                    except Exception as e:
                        logger.error(f"[{self.session_id}] Reclaim failed: {e}")
                    last_claim_time = now

                # Real-time lag diagnostics
                if now - last_diag_time > 5:
                    try:
                        xlen = await self.redis.xlen(self.stream_key)
                        pending = await self.redis.xpending(self.stream_key, GROUP_NAME)
                        pending_count = pending.get("pending", 0) if pending else 0
                        logger.info(f"[{self.session_id}] DIAGNOSTICS | STREAM DEPTH: {xlen} | PENDING: {pending_count}")
                    except Exception as e:
                        pass
                    last_diag_time = now

                # Read new messages
                results = await self.redis.xreadgroup(
                    groupname=GROUP_NAME,
                    consumername=self.consumer_name,
                    streams={self.stream_key: ">"},
                    count=100,
                    block=25,
                )

                if results:
                    for _, messages in results:
                        if messages:
                            ack_ids = await self.process_messages(messages)
                            if ack_ids:
                                try:
                                    await self.redis.xack(self.stream_key, GROUP_NAME, *ack_ids)
                                except Exception as e:
                                    logger.error(f"[{self.session_id}] XACK failed: {e}")

                await self.raw_storage.flush()

            except asyncio.CancelledError:
                logger.info(f"[{self.session_id}] Consumer task cancelled")
                return

            except ConnectionError:
                logger.error(f"[{self.session_id}] Redis connection lost, retrying...")
                await asyncio.sleep(2)

            except Exception as e:
                logger.error(f"[{self.session_id}] Consumer loop error: {e}", exc_info=True)
                await asyncio.sleep(1)


# ─── Master worker ────────────────────────────────────────────────────────────

class StreamWorker:
    """
    Manages a pool of SessionConsumer asyncio tasks, one per active session.
    Discovers sessions via the `active_sessions` Redis set (O(1), written by
    the stream producer on every ingest).
    """

    def __init__(self):
        self.redis          = aioredis.from_url(
            REDIS_URL,
            decode_responses=True,
            health_check_interval=30,
        )
        self.raw_storage    = RawStorage()
        self.consumer_name  = f"worker-{uuid.uuid4()}"
        self.stopped_cache: set  = set()
        self.last_stopped_check  = 0.0
        self._session_tasks: dict[str, asyncio.Task] = {}  # session_id -> Task
        self._running = True

    async def _refresh_stopped_sessions(self):
        now = time.time()
        if now - self.last_stopped_check > 5:
            try:
                self.stopped_cache = await self.redis.smembers("stopped_sessions")
                self.last_stopped_check = now
            except Exception as e:
                logger.error(f"Failed to refresh stopped sessions: {e}")

    async def _discover_and_spawn(self):
        """
        Reads active_sessions set; spawns a SessionConsumer task for any
        session that doesn't already have one.  Cancels tasks for stopped sessions.
        Bounded to MAX_SESSION_TASKS concurrent tasks.
        """
        try:
            active = await self.redis.smembers(ACTIVE_SESSIONS_KEY)
        except Exception as e:
            logger.error(f"Failed to read active_sessions: {e}")
            return

        await self._refresh_stopped_sessions()

        # Cancel tasks for stopped sessions
        for sid in list(self._session_tasks.keys()):
            if sid in self.stopped_cache:
                task = self._session_tasks.pop(sid)
                if not task.done():
                    task.cancel()

        # Spawn tasks for new sessions (bounded)
        for sid in active:
            if sid in self.stopped_cache:
                continue
            if sid in self._session_tasks:
                # Already running — reuse if not done
                if not self._session_tasks[sid].done():
                    continue
                # Task died unexpectedly — clean up
                del self._session_tasks[sid]

            if len(self._session_tasks) >= MAX_SESSION_TASKS:
                logger.warning(f"MAX_SESSION_TASKS ({MAX_SESSION_TASKS}) reached, skipping {sid}")
                break

            consumer = SessionConsumer(
                session_id=sid,
                redis_client=self.redis,
                raw_storage=self.raw_storage,
                consumer_name=self.consumer_name,
                stopped_cache=self.stopped_cache,
            )
            self._session_tasks[sid] = asyncio.create_task(
                consumer.run(),
                name=f"consumer-{sid}",
            )
            logger.info(f"Spawned consumer task for session {sid}")

    async def run(self):
        logger.info(f"{self.consumer_name} master worker starting")

        # Graceful shutdown on SIGTERM
        import signal
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._shutdown)
            except NotImplementedError:
                pass  # Windows fallback

        while self._running:
            try:
                await self._discover_and_spawn()
            except Exception as e:
                logger.error(f"Discovery loop error: {e}", exc_info=True)

            # Refresh every 2 seconds
            await asyncio.sleep(2)

        # Graceful shutdown: cancel all tasks
        for task in self._session_tasks.values():
            task.cancel()
        if self._session_tasks:
            await asyncio.gather(*self._session_tasks.values(), return_exceptions=True)

        await self.redis.aclose()
        logger.info("Worker shutdown complete")

    def _shutdown(self):
        logger.info("Shutdown signal received")
        self._running = False


if __name__ == "__main__":
    worker = StreamWorker()
    asyncio.run(worker.run())
