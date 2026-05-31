import asyncio
import json
import logging
import os
import secrets
import time
import uuid
from typing import Dict, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from pydantic import ValidationError
from sqlalchemy import select, desc, func

from ingestion.schemas import AudioPayload
from ingestion.stream_producer import StreamProducer, ACTIVE_SESSIONS_KEY
from storage.db import async_session, AudioMetric, init_db
import redis.asyncio as aioredis

# ─── Global Agent Tracking ────────────────────────────────────────────────────
# Map agent_instance_id -> active WebSocket to kill zombies actively
agent_sockets: Dict[str, WebSocket] = {}

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL  = os.getenv("LOG_LEVEL", "WARNING").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL))
logger     = logging.getLogger(__name__)
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

REDIS_URL  = os.getenv("REDIS_URL")
SESSION_TTL = 86_400  # 24 h


# ─── CORS origin parsing ─────────────────────────────────────────────────────
def _parse_cors_origins() -> list[str]:
    """
    Read allowed origins from CORS_ORIGINS (comma-separated).
    Normalises trailing slashes, rejects wildcards, and validates
    that every entry starts with http:// or https://.
    Returns an empty list if nothing is configured (caller decides
    whether to fail).
    """
    raw = os.getenv("CORS_ORIGINS", "").strip()
    if not raw:
        return []

    origins: list[str] = []
    for entry in raw.split(","):
        origin = entry.strip().rstrip("/")
        if not origin or origin == "*":
            continue
        if not origin.startswith(("http://", "https://")):
            raise RuntimeError(
                f"Invalid CORS origin '{origin}': must start with "
                f"http:// or https://"
            )
        origins.append(origin)

    # Deduplicate, preserve order
    seen: set[str] = set()
    unique: list[str] = []
    for o in origins:
        if o not in seen:
            seen.add(o)
            unique.append(o)
    return unique


ALLOWED_ORIGINS = _parse_cors_origins()


# ─── App ─────────────────────────────────────────────────────────────────────
app = FastAPI(title="Audio Waveform Analyzer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS else ["http://invalid.local"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent
DIST_DIR = BASE_DIR.parent / "dashboard" / "dist"
if DIST_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(DIST_DIR)), name="static")

stream_producer = StreamProducer()
redis_client    = aioredis.from_url(REDIS_URL, decode_responses=True, health_check_interval=30)

# ─── Session-scoped WebSocket manager ────────────────────────────────────────
class SessionConnectionManager:
    """
    Maintains a per-session set of dashboard WebSocket connections.
    Dashboards subscribe to sessions they want to watch.
    Publishers are separate connections and are not stored here.
    """

    def __init__(self):
        # session_id -> set of watching dashboards
        self._subs: Dict[str, Set[WebSocket]] = {}
        # all dashboard connections (receive global events like session_discovered)
        self._global: Set[WebSocket] = set()

    async def connect_dashboard(self, ws: WebSocket):
        await ws.accept()
        self._global.add(ws)

    def disconnect_dashboard(self, ws: WebSocket):
        self._global.discard(ws)
        # Remove from all session sets
        for subs in self._subs.values():
            subs.discard(ws)
        # Clean up empty sets
        empty = [sid for sid, s in self._subs.items() if not s]
        for sid in empty:
            del self._subs[sid]

    def subscribe(self, session_id: str, ws: WebSocket):
        self._subs.setdefault(session_id, set()).add(ws)

    def unsubscribe(self, session_id: str, ws: WebSocket):
        if session_id in self._subs:
            self._subs[session_id].discard(ws)
            if not self._subs[session_id]:
                del self._subs[session_id]

    async def broadcast_to_session(self, session_id: str, message: str):
        """Send to all dashboards subscribed to this session."""
        for ws in list(self._subs.get(session_id, set())):
            try:
                await ws.send_text(message)
            except Exception:
                self.disconnect_dashboard(ws)

    async def broadcast_global(self, message: str):
        """Send to all connected dashboards (session discovery events)."""
        for ws in list(self._global):
            try:
                await ws.send_text(message)
            except Exception:
                self._global.discard(ws)

    def subscriber_count(self, session_id: str) -> int:
        return len(self._subs.get(session_id, set()))

    def dashboard_count(self) -> int:
        return len(self._global)


manager = SessionConnectionManager()

# ─── Background broadcaster (reads partitioned metrics streams) ───────────────
async def broadcast_metrics_task():
    """
    Reads from audio_metrics_stream:{session_id} for all active sessions.
    Uses the active_sessions Redis set for O(1) discovery — no KEYS/SCAN in
    the hot path.  Refreshes the session list every 2 s.
    """
    logger.info("Broadcaster starting")
    cursors: Dict[str, str] = {}  # stream_key -> last_id
    last_session_refresh = 0.0

    while True:
        try:
            now = time.time()

            # Refresh session list every 2 seconds
            if now - last_session_refresh > 2:
                active = await redis_client.smembers(ACTIVE_SESSIONS_KEY)
                for sid in active:
                    key = f"audio_metrics_stream:{sid}"
                    if key not in cursors:
                        cursors[key] = "$"  # only new messages for live dashboards
                last_session_refresh = now

            if not cursors:
                await asyncio.sleep(0.5)
                continue

            # Single XREAD across all active metric streams (non-blocking poll)
            response = await redis_client.xread(
                streams=cursors,
                count=50,
                block=200,
            )

            if not response:
                continue

            for stream_key, messages in response:
                # stream_key is bytes or str depending on decode_responses
                key_str = stream_key if isinstance(stream_key, str) else stream_key.decode()
                session_id = key_str.split(":")[-1]

                for msg_id, data in messages:
                    cursors[key_str] = msg_id

                    # Skip stopped sessions
                    if await redis_client.sismember("stopped_sessions", session_id):
                        continue

                    timestamp  = float(data.get("timestamp", 0))
                    metrics_str = data.get("full_metrics", "{}")
                    samples_str = data.get("samples",      "[]")
                    sample_count = data.get("sample_count", "0")
                    packet_sequence = data.get("packet_sequence", "0")
                    capture_timestamp = data.get("capture_timestamp", str(timestamp))

                    # Fast-path JSON assembly avoids parsing and re-stringifying 
                    # huge float arrays on the backend's main event loop.
                    audio_msg = (
                        f'{{"type":"audio_update","session_id":"{session_id}",'
                        f'"timestamp":{timestamp},"sample_count":{sample_count},'
                        f'"packet_sequence":{packet_sequence},"capture_timestamp":{capture_timestamp},'
                        f'"metrics":{metrics_str},"samples":{samples_str}}}'
                    )
                    await manager.broadcast_to_session(session_id, audio_msg)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Broadcaster error: {e}")
            await asyncio.sleep(1)


# ─── Idle session cleanup ─────────────────────────────────────────────────────
async def cleanup_task():
    """
    Every 5 minutes: remove sessions from active_sessions whose Redis
    session key has expired (TTL gone) or that are in stopped_sessions.
    """
    while True:
        await asyncio.sleep(300)
        try:
            active  = await redis_client.smembers(ACTIVE_SESSIONS_KEY)
            stopped = await redis_client.smembers("stopped_sessions")
            stale   = active & stopped
            if stale:
                await redis_client.srem(ACTIVE_SESSIONS_KEY, *stale)
                logger.info(f"Cleanup: removed {len(stale)} stale sessions")
        except Exception as e:
            logger.error(f"Cleanup task error: {e}")


# ─── Startup / shutdown ───────────────────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    if not ALLOWED_ORIGINS:
        raise RuntimeError(
            "CORS_ORIGINS must be set to at least one explicit origin "
            "(e.g. CORS_ORIGINS=https://example.com). "
            "Wildcard '*' is not accepted."
        )
    logger.info(f"CORS allowed origins: {ALLOWED_ORIGINS}")

    await init_db()
    asyncio.create_task(broadcast_metrics_task())
    asyncio.create_task(cleanup_task())


@app.on_event("shutdown")
async def shutdown_event():
    await stream_producer.close()
    await redis_client.aclose()


# ─── Static frontend ──────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    try:
        with open("../dashboard/dist/index.html") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Dashboard missing. Run npm run build.</h1>")


# ─── HTTP ingest (REST fallback) ──────────────────────────────────────────────
@app.post("/ingest")
async def ingest_audio(payload: AudioPayload):
    if not payload.timestamp:
        payload.timestamp = time.time()
    await stream_producer.push_to_stream(payload.dict())
    return {"status": "ok"}


ALLOWED_WS_ORIGINS: set[str] = set()  # populated at startup from ALLOWED_ORIGINS

# ─── Device WebSocket (publisher) ────────────────────────────────────────────
@app.websocket("/ws/stream")
@app.websocket("/ws/esp32")
async def websocket_ingest(websocket: WebSocket):
    """
    Publisher endpoint — generates session_id + owner_token.
    Token is returned to the device; dashboards do NOT need a token (public watch).
    """
    origin = websocket.headers.get("origin")
    if origin and origin.rstrip("/") not in ALLOWED_ORIGINS:
        await websocket.close(code=1008)
        return

    await websocket.accept()

    session_id    = str(uuid.uuid4())
    owner_token   = secrets.token_hex(16)
    device_id     = "ws-device"

    # Store session metadata in Redis with 24h TTL
    session_key = f"session:{session_id}"
    await redis_client.hset(session_key, mapping={
        "owner_token": owner_token,
        "device_id":   device_id,
        "created_at":  str(time.time()),
    })
    await redis_client.expire(session_key, SESSION_TTL)
    await redis_client.sadd(ACTIVE_SESSIONS_KEY, session_id)

    # Notify all dashboards that a new session appeared
    await manager.broadcast_global(json.dumps({
        "type":       "session_discovered",
        "session_id": session_id,
        "device_id":  device_id,
    }))

    await websocket.send_text(json.dumps({
        "type":       "session_created",
        "session_id": session_id,
        "token":      owner_token,
    }))

    logger.info(f"Device connected: session={session_id}")

    last_heartbeat = time.time()
    active_agent_instance = None

    try:
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
                last_heartbeat = time.time()
            except asyncio.TimeoutError:
                logger.warning(f"Publisher heartbeat timeout: session={session_id}")
                break

            try:
                data    = json.loads(message)
                msg_type = data.get("type")
                
                if msg_type == "hello":
                    agent_version = data.get("agent_version")
                    agent_id = data.get("agent_instance_id")
                    stream_id = data.get("stream_instance_id")
                    
                    if active_agent_instance is None:
                        active_agent_instance = agent_id
                        
                        # Active Zombie Killer: if another socket exists for this agent, kill it.
                        if agent_id in agent_sockets:
                            old_ws = agent_sockets[agent_id]
                            if old_ws != websocket:
                                logger.warning(f"Duplicate agent {agent_id} detected. Closing old zombie connection.")
                                try:
                                    await old_ws.close(code=4009)
                                except Exception:
                                    pass
                        
                        agent_sockets[agent_id] = websocket
                        
                        # Store current active agent for duplicate checking
                        await redis_client.hset(session_key, "agent_instance_id", agent_id)
                        logger.info(f"Agent handshake: v{agent_version}, instance={agent_id}, stream={stream_id}")
                    elif active_agent_instance != agent_id:
                        logger.error("Duplicate agent session detected in hello (mismatched ID).")
                        await websocket.close(code=4009)
                        return
                    continue
                    
                if msg_type == "heartbeat":
                    continue

                samples = data.get("samples", [])

                if samples:
                    # Validate publisher token (device must echo token)
                    provided_token = data.get("token", "")
                    if provided_token and provided_token != owner_token:
                        await websocket.close(code=4403)
                        return
                        
                    # Check duplicate protection
                    agent_id = data.get("agent_instance_id")
                    if agent_id and active_agent_instance and agent_id != active_agent_instance:
                        logger.error("Duplicate agent session detected in audio frame.")
                        await websocket.close(code=4009)
                        return

                    timestamp = data.get("timestamp", time.time())
                    payload   = AudioPayload(
                        device_id=data.get("device_id", device_id),
                        timestamp=timestamp,
                        session_id=session_id,
                        samples=samples,
                        packet_sequence=data.get("packet_sequence", 0),
                        capture_timestamp=data.get("capture_timestamp", timestamp),
                    )
                    await stream_producer.push_to_stream(payload.dict())

            except ValidationError as ve:
                logger.warning(f"Validation error: {ve}")
            except json.JSONDecodeError:
                pass

    except WebSocketDisconnect:
        logger.info(f"Device disconnected: session={session_id}")
        
        # Clean up agent socket map
        if active_agent_instance and agent_sockets.get(active_agent_instance) == websocket:
            del agent_sockets[active_agent_instance]

        # Mark stopped and notify dashboards
        await redis_client.sadd("stopped_sessions", session_id)
        await redis_client.srem(ACTIVE_SESSIONS_KEY, session_id)
        await manager.broadcast_global(json.dumps({
            "type":       "session_ended",
            "session_id": session_id,
        }))


# ─── Dashboard WebSocket (viewer — public watch) ──────────────────────────────
@app.websocket("/ws/audio")
async def websocket_dashboard(websocket: WebSocket):
    """
    Single multiplexed dashboard connection.
    Messages FROM dashboard:
      {"type": "subscribe",   "session_id": "abc123"}
      {"type": "unsubscribe", "session_id": "abc123"}
    Messages TO dashboard:
      {"type": "active_sessions", "sessions": [...]}
      {"type": "session_discovered", "session_id": ..., "device_id": ...}
      {"type": "session_ended",      "session_id": ...}
      {"type": "audio_update",       "session_id": ..., "metrics": ..., ...}
    """
    await manager.connect_dashboard(websocket)

    # On connect: send current active sessions so frontend can bootstrap
    try:
        active_sids = await redis_client.smembers(ACTIVE_SESSIONS_KEY)
        stopped     = await redis_client.smembers("stopped_sessions")
        live        = list(active_sids - stopped)

        # Enrich with device_id from Redis session metadata
        sessions_list = []
        for sid in live:
            info = await redis_client.hgetall(f"session:{sid}")
            sessions_list.append({
                "session_id": sid,
                "device_id":  info.get("device_id", "unknown"),
                "created_at": info.get("created_at"),
            })

        await websocket.send_text(json.dumps({
            "type":     "active_sessions",
            "sessions": sessions_list,
        }))
    except Exception as e:
        logger.error(f"Failed to send active sessions on connect: {e}")

    try:
        while True:
            message = await websocket.receive_text()
            try:
                data     = json.loads(message)
                msg_type = data.get("type")

                if msg_type == "subscribe":
                    sid = data.get("session_id")
                    if sid:
                        manager.subscribe(sid, websocket)
                        await websocket.send_text(json.dumps({
                            "type": "subscribed", "session_id": sid
                        }))

                elif msg_type == "unsubscribe":
                    sid = data.get("session_id")
                    if sid:
                        manager.unsubscribe(sid, websocket)

                # Native ping/pong is handled at the WS protocol level.
                # We only handle application-level ping for older clients.
                elif msg_type == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))

            except json.JSONDecodeError:
                pass

    except WebSocketDisconnect:
        manager.disconnect_dashboard(websocket)


# ─── REST APIs ────────────────────────────────────────────────────────────────

def _validate_session_id(session_id: str) -> str:
    """Validate that a session_id is a well-formed UUID. Raises 400 if not."""
    try:
        uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid session ID: {session_id}")
    return session_id

def _compute_averages_from_rows(rows):
    if not rows:
        return {"avg_rms": 0.0, "avg_peak": 0.0, "avg_frequency": 0.0, "avg_bpm": 0.0}
    rms_sum = amp_sum = bpm_sum = 0
    count   = len(rows)
    for r in rows:
        rms_sum += (r.rms_energy    or 0.0)
        amp_sum += (r.avg_amplitude or 0.0)
        bpm_sum += (r.bpm           or 0.0)
    avg_rms = round(rms_sum / count, 4)
    avg_amp = round(amp_sum / count, 4)
    avg_bpm = round(bpm_sum / count, 4)
    return {
        "avg_rms": avg_rms, "avg_peak": avg_amp,
        "avg_frequency": 0.0, "avg_bpm": avg_bpm,
        "rms": avg_rms, "peak": avg_amp, "bpm": avg_bpm, "frequency": 0.0,
    }


@app.get("/api/sessions/live")
async def list_live_sessions():
    """Returns sessions currently in active_sessions (not stopped)."""
    active  = await redis_client.smembers(ACTIVE_SESSIONS_KEY)
    stopped = await redis_client.smembers("stopped_sessions")
    live    = active - stopped
    result  = []
    for sid in live:
        info = await redis_client.hgetall(f"session:{sid}")
        result.append({
            "session_id": sid,
            "device_id":  info.get("device_id", "unknown"),
            "created_at": info.get("created_at"),
        })
    return {"sessions": result}


@app.get("/api/sessions")
async def list_sessions():
    async with async_session() as session:
        stmt = (
            select(AudioMetric.session_id, func.max(AudioMetric.timestamp).label("last_timestamp"))
            .group_by(AudioMetric.session_id)
            .order_by(desc("last_timestamp"))
            .limit(100)
        )
        result   = await session.execute(stmt)
        sessions = [
            {"session_id": sid, "timestamp": ts}
            for sid, ts in result.fetchall()
        ]
        return {"sessions": sessions}


@app.get("/api/session/{session_id}")
async def get_session(session_id: str):
    _validate_session_id(session_id)
    async with async_session() as session:
        stmt   = select(AudioMetric).where(AudioMetric.session_id == session_id).order_by(AudioMetric.timestamp)
        result = await session.execute(stmt)
        rows   = result.scalars().all()
        if not rows:
            raise HTTPException(status_code=404, detail="Session not found")
        metrics = [{"timestamp": r.timestamp, "metrics": {
            "bpm": r.bpm, "rms": r.rms_energy, "peak": r.avg_amplitude,
            "frequency": r.frequency, "zcr": r.zcr,
        }} for r in rows]
        return {"session_id": session_id, "averages": _compute_averages_from_rows(rows), "metrics": metrics}


@app.get("/api/session/{session_id}/averages")
async def get_session_averages(session_id: str, mode: str = Query(default="wave", pattern="^(wave|fft)$")):
    _validate_session_id(session_id)
    async with async_session() as session:
        stmt   = select(AudioMetric).where(AudioMetric.session_id == session_id)
        result = await session.execute(stmt)
        rows   = result.scalars().all()
        if not rows:
            raise HTTPException(status_code=404, detail="Session not found")
        return {"session_id": session_id, **_compute_averages_from_rows(rows)}


@app.get("/api/session/{session_id}/export")
@app.get("/api/sessions/{session_id}/export")
@app.get("/api/session/{session_id}/metrics")
async def export_session_metrics(session_id: str, mode: str = Query(default="wave", pattern="^(wave|fft)$")):
    _validate_session_id(session_id)
    async with async_session() as session:
        stmt   = select(AudioMetric).where(AudioMetric.session_id == session_id).order_by(AudioMetric.timestamp)
        result = await session.execute(stmt)
        rows   = result.scalars().all()
        if not rows:
            raise HTTPException(status_code=404, detail="Session not found")
        metrics = [{"timestamp": r.timestamp, "metrics": {
            "bpm": r.bpm, "rms": r.rms_energy, "peak": r.avg_amplitude,
            "frequency": r.frequency, "zcr": r.zcr,
        }} for r in rows]
        return {
            "session_id":  session_id,
            "averages":    _compute_averages_from_rows(rows),
            "full_metrics": metrics,
        }


@app.get("/api/metrics/current")
async def get_current_metrics():
    active = await redis_client.smembers(ACTIVE_SESSIONS_KEY)
    return {"active_sessions": list(active), "timestamp": time.time()}


@app.post("/api/sessions/{session_id}/stop")
async def stop_session(session_id: str):
    _validate_session_id(session_id)
    await redis_client.sadd("stopped_sessions", session_id)
    await redis_client.srem(ACTIVE_SESSIONS_KEY, session_id)
    await manager.broadcast_global(json.dumps({
        "type": "session_ended", "session_id": session_id
    }))
    logger.info(f"Session stopped via REST: {session_id}")
    return {"status": "stopped", "session_id": session_id}