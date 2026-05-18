import json
import os
import redis.asyncio as aioredis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
ACTIVE_SESSIONS_KEY = "active_sessions"

class StreamProducer:
    def __init__(self):
        self.redis: aioredis.Redis = aioredis.from_url(
            REDIS_URL,
            decode_responses=True,
            health_check_interval=30,
        )

    async def push_to_stream(self, payload: dict):
        """
        Pushes a validated audio payload to the session-partitioned Redis Stream.
        Stream key: audio_stream:{session_id}  (one stream per session).
        Also registers the session in the active_sessions set so the worker
        and broadcaster can discover it without a KEYS scan.
        """
        session_id = str(payload.get("session_id"))
        stream_key = f"audio_stream:{session_id}"

        message = {
            "device_id": str(payload.get("device_id")),
            "timestamp": str(payload.get("timestamp")),
            "session_id": session_id,
            "samples": json.dumps(payload.get("samples", []))
        }

        # MAXLEN keeps per-session memory bounded (~2000 packets max)
        await self.redis.xadd(stream_key, message, maxlen=2000, approximate=True)

        # Register session for discovery (cheap SADD is idempotent)
        await self.redis.sadd(ACTIVE_SESSIONS_KEY, session_id)

    async def close(self):
        await self.redis.aclose()
